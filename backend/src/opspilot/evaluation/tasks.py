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
    capture_frozen_dataset,
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


async def _renew(
    execution_id: uuid.UUID,
    owner: str,
    token: str,
    lease_seconds: int,
    version: int | None = None,
) -> None:
    async with async_session_factory() as session:
        predicates = [
            EvaluationTestExecution.id == execution_id,
            EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
            EvaluationTestExecution.claim_token == token,
            EvaluationTestExecution.lease_owner == owner,
            EvaluationTestExecution.lease_expires_at >= func.clock_timestamp(),
        ]
        if version is not None:
            predicates.append(EvaluationTestExecution.version == version)
        changed = await session.execute(
            update(EvaluationTestExecution)
            .where(*predicates)
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
    version: int | None = None,
) -> T:
    # The first PG-clock fence is synchronous: a processor can never start
    # during the lease/3 delay before the periodic heartbeat's first renewal.
    await _renew(execution_id, owner, token, lease, version)
    task: asyncio.Task[T] = asyncio.create_task(invoke())

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(max(0.05, lease / 3))
            await _renew(execution_id, owner, token, lease, version)

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
    bind_configuration = getattr(processor, "bind_configuration", None)
    if callable(bind_configuration):
        async with async_session_factory() as session:
            pending = await session.get(EvaluationTestExecution, parsed_id)
            if pending is None or pending.status != EvaluationExecutionStatus.RUNNING:
                return
            locked_configuration = EvaluationConfiguration.model_validate(pending.configuration)
            expected_configuration_sha = pending.configuration_sha
        # Authentication/deployment mismatch is rejected before acquiring a
        # lease and before any case/provider invocation.
        processor = await bind_configuration(locked_configuration, expected_configuration_sha)
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
            .returning(
                EvaluationTestExecution.version,
                EvaluationTestExecution.evaluation_run_id,
                EvaluationTestExecution.dataset_identity,
                EvaluationTestExecution.configuration,
            )
        )
        claimed_row = claimed.one_or_none()
        if claimed_row is None:
            await session.rollback()
            return
        claimed_version, run_id, expected_identity, configuration = claimed_row
        await session.commit()

    try:
        snapshot = capture_frozen_dataset(Path(settings.evaluation_dataset_root))
        if snapshot.identity != expected_identity:
            raise ValueError("frozen evaluation dataset identity mismatch")
        cases: tuple[EvaluationCase, ...] = (*snapshot.test_cases, *snapshot.agent_cases)
        locked_configuration = EvaluationConfiguration.model_validate(configuration)

        async def execute_case(case: EvaluationCase) -> None:
            case_repetitions = (
                locked_configuration.repetitions if isinstance(case, AgentEvaluationCase) else 1
            )
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
                    claimed_version,
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

        # Concurrency is case-level: repetitions of a single Agent case stay
        # sequential, while distinct cases share this bounded worker pool.
        pending_cases = iter(cases)

        async def consume_cases() -> None:
            for selected_case in pending_cases:
                await execute_case(selected_case)

        case_workers = [
            asyncio.create_task(consume_cases())
            for _ in range(min(locked_configuration.concurrency, len(cases)))
        ]
        try:
            await asyncio.gather(*case_workers)
        finally:
            await asyncio.gather(*(_stop_task(task, detach=True) for task in case_workers))

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
