import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, update

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.executor import ExecutionHook, execute_operation
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.reconciliation import ReconciliationLookup, reconcile_operation
from opspilot.jobs.models import RunJobOutbox, RunJobStatus
from opspilot.jobs.run_executor import RunJobFence, run_processor_under_lease
from opspilot.runs.journal import append_event
from opspilot.runs.models import RunMessage
from opspilot.runs.status import recompute_run_status
from opspilot.tools.adapters.python import get_refund_status_adapter, refund_order_adapter
from opspilot.tools.schemas import GetRefundStatusArgs, RefundOrderArgs
from opspilot.tools.types import ToolEffect, ToolResult


@dataclass(frozen=True)
class RunMessageResult:
    content: str
    citation_snapshots: list[dict[str, object]]
    tool_calls: list[dict[str, object]] | None = None
    response_kind: str = "ANSWERED"


RunMessageProcessor = Callable[[RunMessage, RunJobFence], Awaitable[RunMessageResult]]


async def process_run_message(ctx: dict[str, Any], message_id: str) -> None:
    """Consume one durable user message and persist exactly one assistant reply."""
    parsed_id = uuid.UUID(message_id)
    processor = ctx.get("run_message_processor")
    if not callable(processor):
        raise RuntimeError("run message processor is not configured")
    owner = f"arq:{ctx.get('job_id', 'run-message')}"
    token = secrets.token_urlsafe(32)
    lease_seconds = int(ctx.get("run_job_lease_seconds", 120))
    async with async_session_factory() as session:
        claimed = await session.execute(
            update(RunJobOutbox)
            .where(
                RunJobOutbox.message_id == parsed_id,
                (
                    (RunJobOutbox.status == RunJobStatus.PENDING)
                    | (
                        (RunJobOutbox.status == RunJobStatus.RUNNING)
                        & (RunJobOutbox.lease_expires_at < func.clock_timestamp())
                    )
                ),
            )
            .values(
                status=RunJobStatus.RUNNING,
                claim_token=token,
                lease_owner=owner,
                lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
            )
        )
        if (claimed.rowcount or 0) != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return
        await session.commit()
    async with async_session_factory() as session:
        message = await session.get(RunMessage, parsed_id)
        if message is None or message.role != "USER":
            return
        existing = await session.scalar(
            select(RunMessage).where(RunMessage.in_reply_to_message_id == parsed_id)
        )
        if existing is not None:
            await session.execute(
                update(RunJobOutbox)
                .where(
                    RunJobOutbox.message_id == parsed_id,
                    RunJobOutbox.claim_token == token,
                )
                .values(
                    status=RunJobStatus.COMPLETED,
                    completed_at=func.clock_timestamp(),
                    claim_token=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await session.commit()
            return
    result = await run_processor_under_lease(
        async_session_factory,
        parsed_id,
        owner=owner,
        token=token,
        lease_seconds=lease_seconds,
        invoke=lambda: processor(message, RunJobFence(parsed_id, owner, token)),
    )
    async with async_session_factory() as session:
        job = await session.scalar(
            select(RunJobOutbox)
            .where(
                RunJobOutbox.message_id == parsed_id,
                RunJobOutbox.status == RunJobStatus.RUNNING,
                RunJobOutbox.claim_token == token,
                RunJobOutbox.lease_owner == owner,
                RunJobOutbox.lease_expires_at >= func.clock_timestamp(),
            )
            .with_for_update()
        )
        if job is None:
            return
        message = await session.get(RunMessage, parsed_id)
        if message is None:
            return
        existing = await session.scalar(
            select(RunMessage).where(RunMessage.in_reply_to_message_id == parsed_id)
        )
        if existing is not None:
            job.status = RunJobStatus.COMPLETED
            job.completed_at = await session.scalar(select(func.clock_timestamp()))
            job.claim_token = None
            job.lease_owner = None
            job.lease_expires_at = None
            await session.commit()
            return
        reply = RunMessage(
            run_id=message.run_id,
            role="ASSISTANT",
            content=result.content,
            citation_snapshots=result.citation_snapshots,
            in_reply_to_message_id=parsed_id,
        )
        session.add(reply)
        await session.flush()
        await recompute_run_status(session, message.run_id)
        await append_event(
            session,
            message.run_id,
            "assistant_message_created",
            {
                "message_id": str(reply.id),
                "content": result.content,
                "citations": result.citation_snapshots,
                "tool_calls": result.tool_calls or [],
                "response_kind": result.response_kind,
            },
        )
        job.status = RunJobStatus.COMPLETED
        job.completed_at = await session.scalar(select(func.clock_timestamp()))
        job.claim_token = None
        job.lease_owner = None
        job.lease_expires_at = None
        await session.commit()


async def process_operation_job(
    ctx: dict[str, Any], operation_id: str, expected_version: int, kind: str
) -> None:
    """Idempotently dispatch one version-bound operation intent."""
    parsed_id = uuid.UUID(operation_id)
    async with async_session_factory() as session:
        operation = await session.get(Operation, parsed_id)
        if operation is None or operation.version != expected_version:
            return
        expected_statuses = (
            {OperationStatus.READY, OperationStatus.RETRYING}
            if kind == "EXECUTE"
            else {OperationStatus.OUTCOME_UNKNOWN}
        )
        if operation.status not in expected_statuses:
            return

    settings = Settings()
    if kind == "EXECUTE":
        before_hook: ExecutionHook | None = None
        after_invoke_hook: ExecutionHook | None = None
        after_commit_hook: ExecutionHook | None = None
        if settings.evaluation_fault_matrix:
            from opspilot.evaluation.faults import crash_if_planned
            from opspilot.evaluation.models import EvaluationFaultPoint

            worker_id = f"arq:{ctx.get('job_id', 'operation')}"

            async def before_invoke(current: Operation) -> None:
                await crash_if_planned(
                    current.id,
                    EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT,
                    worker_id=worker_id,
                )

            async def after_invoke(current: Operation) -> None:
                await crash_if_planned(
                    current.id,
                    EvaluationFaultPoint.AFTER_EXTERNAL_EFFECT_BEFORE_LOCAL_COMMIT,
                    worker_id=worker_id,
                )

            async def after_commit(current: Operation) -> None:
                await crash_if_planned(
                    current.id,
                    EvaluationFaultPoint.AFTER_LOCAL_COMMIT_BEFORE_JOB_ACK,
                    worker_id=worker_id,
                )

            before_hook = before_invoke
            after_invoke_hook = after_invoke
            after_commit_hook = after_commit
        async with httpx.AsyncClient(
            base_url=settings.payment_service_url,
            timeout=settings.payment_timeout_seconds,
        ) as client:
            refund = refund_order_adapter(client)

            async def invoke(current: Operation) -> ToolResult:
                return await refund(RefundOrderArgs.model_validate(current.normalized_arguments))

            await execute_operation(
                async_session_factory,
                parsed_id,
                owner=f"arq:{ctx.get('job_id', 'operation')}",
                lease_seconds=settings.operation_lease_seconds,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=invoke,
                before_invoke=before_hook,
                after_invoke=after_invoke_hook,
                after_commit=after_commit_hook,
            )
        return
    if kind == "RECONCILE":
        async with httpx.AsyncClient(
            base_url=settings.payment_service_url,
            timeout=settings.payment_timeout_seconds,
        ) as client:
            get_status = get_refund_status_adapter(client)

            async def query(lookup: ReconciliationLookup) -> ToolResult:
                return await get_status(GetRefundStatusArgs(order_number=lookup.order_number))

            await reconcile_operation(
                async_session_factory,
                parsed_id,
                owner=f"arq:{ctx.get('job_id', 'reconcile')}",
                lease_seconds=settings.operation_lease_seconds,
                query=query,
            )
