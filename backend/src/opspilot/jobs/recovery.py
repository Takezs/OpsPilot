"""Bounded production recovery for expired operation and run-job leases."""

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update

from opspilot.db import async_session_factory
from opspilot.execution.claim import LeaseConflictError, recover_expired
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.reconciliation import recover_expired_reconciliation
from opspilot.jobs.models import OperationJobOutbox, RunJobOutbox, RunJobStatus
from opspilot.runs.models import RunMessage
from opspilot.tools.types import ToolEffect


async def recover_expired_operations(batch_size: int = 50) -> int:
    """Recover expired leases without ever invoking an external Provider."""
    recovered = 0
    attempted_ids: set[uuid.UUID] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            query = select(Operation).where(
                Operation.status.in_((OperationStatus.EXECUTING, OperationStatus.RECONCILING)),
                Operation.lease_expires_at.is_not(None),
                Operation.lease_expires_at < func.clock_timestamp(),
            )
            if attempted_ids:
                query = query.where(Operation.id.not_in(attempted_ids))
            operation = await session.scalar(
                query.order_by(Operation.lease_expires_at, Operation.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is None:
                break
            attempted_ids.add(operation.id)
            try:
                if operation.status is OperationStatus.RECONCILING:
                    await recover_expired_reconciliation(session, operation.id)
                else:
                    # Task 15's durable operation runtime only executes refund_order.
                    # Unknown tools fail closed as side effects: recovery reconciles,
                    # and never retries a possibly-delivered Provider call.
                    await recover_expired(session, operation.id, effect=ToolEffect.SIDE_EFFECT)
                await session.commit()
            except LeaseConflictError:
                await session.rollback()
                continue
            recovered += 1
    return recovered


async def recover_expired_run_jobs(batch_size: int = 50) -> int:
    """Make expired message claims publishable again for idempotent redelivery."""
    recovered = 0
    attempted_ids: set[uuid.UUID] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            query = select(RunJobOutbox).where(
                RunJobOutbox.status == RunJobStatus.RUNNING,
                RunJobOutbox.lease_expires_at.is_not(None),
                RunJobOutbox.lease_expires_at < func.clock_timestamp(),
            )
            if attempted_ids:
                query = query.where(RunJobOutbox.id.not_in(attempted_ids))
            row = await session.scalar(
                query.order_by(RunJobOutbox.lease_expires_at, RunJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            attempted_ids.add(row.id)
            changed = await session.execute(
                update(RunJobOutbox)
                .where(
                    RunJobOutbox.id == row.id,
                    RunJobOutbox.status == RunJobStatus.RUNNING,
                    RunJobOutbox.claim_token == row.claim_token,
                    or_(
                        RunJobOutbox.lease_expires_at.is_(None),
                        RunJobOutbox.lease_expires_at < func.clock_timestamp(),
                    ),
                )
                .values(
                    status=RunJobStatus.PENDING,
                    claim_token=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    delivered_at=None,
                    available_at=func.clock_timestamp(),
                )
            )
            if (changed.rowcount or 0) == 1:  # type: ignore[attr-defined]
                await session.commit()
                recovered += 1
            else:
                await session.rollback()
    return recovered


async def recover_stale_delivered_jobs(batch_size: int = 50, *, grace_seconds: int = 60) -> int:
    """Requeue broker-acknowledged intents that were never durably claimed."""
    recovered = 0
    attempted_run_ids: set[uuid.UUID] = set()
    attempted_operation_ids: set[uuid.UUID] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            run_query = select(RunJobOutbox).where(
                RunJobOutbox.status == RunJobStatus.PENDING,
                RunJobOutbox.delivered_at.is_not(None),
                RunJobOutbox.delivered_at
                < func.clock_timestamp() - timedelta(seconds=grace_seconds),
            )
            if attempted_run_ids:
                run_query = run_query.where(RunJobOutbox.id.not_in(attempted_run_ids))
            row = await session.scalar(
                run_query.order_by(RunJobOutbox.delivered_at, RunJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is not None:
                attempted_run_ids.add(row.id)
                reply = await session.scalar(
                    select(RunMessage.id)
                    .where(RunMessage.in_reply_to_message_id == row.message_id)
                    .limit(1)
                )
                if reply is not None:
                    row.status = RunJobStatus.COMPLETED
                    row.completed_at = await session.scalar(select(func.clock_timestamp()))
                    row.claim_token = None
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.last_error = None
                else:
                    row.delivered_at = None
                    row.available_at = func.clock_timestamp() + timedelta(seconds=1)
                    row.last_error = "delivery acknowledgement expired before claim"
                await session.commit()
                recovered += 1
                continue

        async with async_session_factory() as session:
            operation_query = (
                select(OperationJobOutbox, Operation)
                .join(Operation, Operation.id == OperationJobOutbox.operation_id)
                .where(
                    OperationJobOutbox.delivered_at.is_not(None),
                    OperationJobOutbox.delivered_at
                    < func.clock_timestamp() - timedelta(seconds=grace_seconds),
                    OperationJobOutbox.last_error.is_distinct_from("stale operation job intent"),
                )
            )
            if attempted_operation_ids:
                operation_query = operation_query.where(
                    OperationJobOutbox.id.not_in(attempted_operation_ids)
                )
            pair = (
                await session.execute(
                    operation_query.order_by(OperationJobOutbox.delivered_at, OperationJobOutbox.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).first()
            if pair is None:
                break
            job, operation = pair
            attempted_operation_ids.add(job.id)
            expected_statuses = (
                {OperationStatus.READY, OperationStatus.RETRYING}
                if job.kind == "EXECUTE"
                else {OperationStatus.OUTCOME_UNKNOWN}
            )
            if operation.version == job.expected_version and operation.status in expected_statuses:
                job.delivered_at = None
                job.available_at = func.clock_timestamp() + timedelta(seconds=1)
                job.last_error = "delivery acknowledgement expired before claim"
            else:
                job.last_error = "stale operation job intent"
            await session.commit()
            recovered += 1
    return recovered
