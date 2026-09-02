"""Idempotent ARQ evaluation execution over public HTTP API case processors."""

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationExecutionAttempt,
    EvaluationExecutionStatus,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationTestExecution,
)
from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    EvaluationCase,
    EvaluationConfiguration,
    frozen_dataset_identity,
    load_jsonl,
)


@dataclass(frozen=True)
class EvaluationCaseResult:
    actual_output: dict[str, object]
    deterministic_scores: dict[str, object]


EvaluationCaseProcessor = Callable[[EvaluationCase, int], Coroutine[Any, Any, EvaluationCaseResult]]
_DETACHED_EVALUATION_TASKS: set[asyncio.Task[Any]] = set()
_CANCEL_GRACE_SECONDS = 1.0


class EvaluationLeaseLost(RuntimeError):
    pass


async def _lock_fenced_execution(
    session: AsyncSession,
    execution_id: uuid.UUID,
    owner: str,
    token: str,
    version: int,
) -> EvaluationTestExecution:
    current = await session.scalar(
        select(EvaluationTestExecution)
        .where(
            EvaluationTestExecution.id == execution_id,
            EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
            EvaluationTestExecution.claim_token == token,
            EvaluationTestExecution.lease_owner == owner,
            EvaluationTestExecution.version == version,
            EvaluationTestExecution.lease_expires_at >= func.clock_timestamp(),
        )
        .with_for_update()
    )
    if current is None:
        raise EvaluationLeaseLost("evaluation execution lease lost")
    return current


def _consume(task: asyncio.Task[Any]) -> None:
    if task.done() and not task.cancelled():
        try:
            task.result()
        except BaseException:
            pass


def _forget(task: asyncio.Task[Any]) -> None:
    _consume(task)
    _DETACHED_EVALUATION_TASKS.discard(task)


async def _stop_task(task: asyncio.Task[Any], *, detach: bool = False) -> None:
    if task.done():
        _consume(task)
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS)
    except TimeoutError:
        if detach:
            _DETACHED_EVALUATION_TASKS.add(task)
            task.add_done_callback(_forget)
    except asyncio.CancelledError:
        if task.done():
            _consume(task)
            return
        if detach:
            _DETACHED_EVALUATION_TASKS.add(task)
            task.add_done_callback(_forget)
        raise
    except BaseException:
        _consume(task)


def detached_evaluation_task_count() -> int:
    return len(_DETACHED_EVALUATION_TASKS)


async def drain_detached_evaluation_tasks() -> None:
    tasks = tuple(_DETACHED_EVALUATION_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _renew(execution_id: uuid.UUID, owner: str, token: str, lease_seconds: int) -> None:
    async with async_session_factory() as session:
        changed = await session.execute(
            update(EvaluationTestExecution)
            .where(
                EvaluationTestExecution.id == execution_id,
                EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
                EvaluationTestExecution.claim_token == token,
                EvaluationTestExecution.lease_owner == owner,
                EvaluationTestExecution.lease_expires_at >= func.clock_timestamp(),
            )
            .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds))
        )
        if (changed.rowcount or 0) != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise EvaluationLeaseLost("evaluation execution lease lost")
        await session.commit()


async def _with_heartbeat[T](
    invoke: Callable[[], Coroutine[Any, Any, T]],
    execution_id: uuid.UUID,
    owner: str,
    token: str,
    lease: int,
) -> T:
    task: asyncio.Task[T] = asyncio.create_task(invoke())

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(0.05, lease / 3))
            await _renew(execution_id, owner, token, lease)

    beat = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait({task, beat}, return_when=asyncio.FIRST_COMPLETED)
        if beat in done:
            task.cancel()
            error = beat.exception()
            if error is not None:
                raise error
            raise EvaluationLeaseLost("evaluation heartbeat stopped")
        return task.result()
    finally:
        await _stop_task(beat)
        await _stop_task(task, detach=True)


async def process_evaluation_execution(ctx: dict[str, Any], execution_id: str) -> None:
    parsed_id = uuid.UUID(execution_id)
    processor_value = ctx.get("evaluation_case_processor")
    if not callable(processor_value):
        raise RuntimeError("evaluation case processor is not configured")
    processor = cast(EvaluationCaseProcessor, processor_value)
    settings = Settings()
    owner = f"arq:{ctx.get('job_id', 'evaluation')}"
    token = uuid.uuid4().hex
    async with async_session_factory() as session:
        claimed = await session.execute(
            update(EvaluationTestExecution)
            .where(
                EvaluationTestExecution.id == parsed_id,
                EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
                (
                    EvaluationTestExecution.claim_token.is_(None)
                    | (EvaluationTestExecution.lease_expires_at < func.clock_timestamp())
                ),
            )
            .values(
                claim_token=token,
                lease_owner=owner,
                lease_expires_at=func.clock_timestamp()
                + timedelta(seconds=settings.evaluation_lease_seconds),
                version=EvaluationTestExecution.version + 1,
            )
            .returning(EvaluationTestExecution.version)
        )
        claimed_version = claimed.scalar_one_or_none()
        if claimed_version is None:
            await session.rollback()
            return
        await session.commit()

    dataset_root = Path(settings.evaluation_dataset_root)
    async with async_session_factory() as session:
        receipt = await session.get(EvaluationTestExecution, parsed_id)
        if receipt is None:
            return
        expected_identity = receipt.dataset_identity
    if frozen_dataset_identity(dataset_root) != expected_identity:
        raise ValueError("frozen evaluation dataset identity mismatch")
    cases: tuple[EvaluationCase, ...] = (
        *load_jsonl(dataset_root / "test.jsonl", EvaluationCase),
        *load_jsonl(dataset_root / "agent_tasks.jsonl", AgentEvaluationCase),
    )
    async with async_session_factory() as session:
        execution = await session.get(EvaluationTestExecution, parsed_id)
        if execution is None:
            return
        run_id = execution.evaluation_run_id
        repetitions = EvaluationConfiguration.model_validate(execution.configuration).repetitions
    try:
        for case in cases:
            case_repetitions = repetitions if isinstance(case, AgentEvaluationCase) else 1
            for repetition in range(1, case_repetitions + 1):
                async with async_session_factory() as session:
                    existing = await session.scalar(
                        select(EvaluationCaseRecord.id).where(
                            EvaluationCaseRecord.evaluation_run_id == run_id,
                            EvaluationCaseRecord.dataset_case_id == case.case_id,
                            EvaluationCaseRecord.repetition == repetition,
                        )
                    )
                if existing is not None:
                    continue
                started = time.perf_counter()

                async def invoke_case(
                    selected_case: EvaluationCase = case,
                    selected_repetition: int = repetition,
                ) -> EvaluationCaseResult:
                    return await processor(selected_case, selected_repetition)

                result = await _with_heartbeat(
                    invoke_case,
                    parsed_id,
                    owner,
                    token,
                    settings.evaluation_lease_seconds,
                )
                latency_ms = max(0, round((time.perf_counter() - started) * 1000))
                async with async_session_factory() as session:
                    current = await _lock_fenced_execution(
                        session, parsed_id, owner, token, claimed_version
                    )
                    duplicate = await session.scalar(
                        select(EvaluationCaseRecord.id).where(
                            EvaluationCaseRecord.evaluation_run_id == run_id,
                            EvaluationCaseRecord.dataset_case_id == case.case_id,
                            EvaluationCaseRecord.repetition == repetition,
                        )
                    )
                    if duplicate is None:
                        session.add(
                            EvaluationCaseRecord(
                                evaluation_run_id=run_id,
                                dataset_case_id=case.case_id,
                                repetition=repetition,
                                actual_output=result.actual_output,
                                deterministic_scores=result.deterministic_scores,
                                latency_ms=latency_ms,
                            )
                        )
                    current.lease_expires_at = func.clock_timestamp() + timedelta(
                        seconds=settings.evaluation_lease_seconds
                    )
                    await session.commit()
        async with async_session_factory() as session:
            current = await _lock_fenced_execution(
                session, parsed_id, owner, token, claimed_version
            )
            now = await session.scalar(select(func.clock_timestamp()))
            current.status = EvaluationExecutionStatus.COMPLETED
            current.completed_at = now
            current.claim_token = None
            current.lease_owner = None
            current.lease_expires_at = None
            run = await session.get(EvaluationRun, run_id)
            if run is not None:
                run.status = EvaluationRunStatus.COMPLETED
                run.completed_at = now
            attempt = await session.scalar(
                select(EvaluationExecutionAttempt)
                .where(
                    EvaluationExecutionAttempt.execution_id == parsed_id,
                    EvaluationExecutionAttempt.status == "RUNNING",
                )
                .order_by(EvaluationExecutionAttempt.attempt_number.desc())
                .with_for_update()
            )
            if attempt is not None:
                attempt.status = "COMPLETED"
                attempt.completed_at = now
            await session.commit()
    except BaseException as error:
        async with async_session_factory() as session:
            try:
                current = await _lock_fenced_execution(
                    session, parsed_id, owner, token, claimed_version
                )
            except EvaluationLeaseLost:
                await session.rollback()
            else:
                now = await session.scalar(select(func.clock_timestamp()))
                current.status = EvaluationExecutionStatus.FAILED
                current.completed_at = now
                current.failure_summary = f"{type(error).__name__}: evaluation failed"[:500]
                current.claim_token = None
                current.lease_owner = None
                current.lease_expires_at = None
                run = await session.get(EvaluationRun, run_id)
                if run is not None:
                    run.status = EvaluationRunStatus.FAILED
                    run.completed_at = now
                    run.error = current.failure_summary
                attempt = await session.scalar(
                    select(EvaluationExecutionAttempt)
                    .where(
                        EvaluationExecutionAttempt.execution_id == parsed_id,
                        EvaluationExecutionAttempt.status == "RUNNING",
                    )
                    .order_by(EvaluationExecutionAttempt.attempt_number.desc())
                    .with_for_update()
                )
                if attempt is not None:
                    attempt.status = "FAILED"
                    attempt.completed_at = now
                    attempt.error = current.failure_summary
                await session.commit()
        raise
