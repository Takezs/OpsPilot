"""Bounded production recovery for expired operation and run-job leases."""

from sqlalchemy import func, or_, select, update

from opspilot.db import async_session_factory
from opspilot.execution.claim import LeaseConflictError, recover_expired
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.reconciliation import recover_expired_reconciliation
from opspilot.jobs.models import RunJobOutbox, RunJobStatus
from opspilot.tools.types import ToolEffect


async def recover_expired_operations(batch_size: int = 50) -> int:
    """Recover expired leases without ever invoking an external Provider."""
    recovered = 0
    for _ in range(batch_size):
        async with async_session_factory() as session:
            operation = await session.scalar(
                select(Operation)
                .where(
                    Operation.status.in_((OperationStatus.EXECUTING, OperationStatus.RECONCILING)),
                    Operation.lease_expires_at.is_not(None),
                    Operation.lease_expires_at < func.clock_timestamp(),
                )
                .order_by(Operation.lease_expires_at, Operation.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is None:
                break
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
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(RunJobOutbox)
                .where(
                    RunJobOutbox.status == RunJobStatus.RUNNING,
                    RunJobOutbox.lease_expires_at.is_not(None),
                    RunJobOutbox.lease_expires_at < func.clock_timestamp(),
                )
                .order_by(RunJobOutbox.lease_expires_at, RunJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
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
