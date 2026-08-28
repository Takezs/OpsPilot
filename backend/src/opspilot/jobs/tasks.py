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
from opspilot.execution.executor import execute_operation
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.reconciliation import ReconciliationLookup, reconcile_operation
from opspilot.jobs.models import RunJobOutbox, RunJobStatus
from opspilot.jobs.run_executor import run_processor_under_lease
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


RunMessageProcessor = Callable[[RunMessage], Awaitable[RunMessageResult]]


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
        invoke=lambda: processor(message),
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
        expected_status = (
            OperationStatus.READY if kind == "EXECUTE" else OperationStatus.OUTCOME_UNKNOWN
        )
        if operation.status is not expected_status:
            return

    settings = Settings()
    if kind == "EXECUTE":
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
