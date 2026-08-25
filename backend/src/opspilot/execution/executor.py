"""Lease-guarded execution driver: claim, invoke and fence the result write.

``execute_operation`` composes the Task 10 primitives into a worker loop for one
Operation. The claim is committed first so the lease is durable: once a worker
is EXECUTING, a lost lease (failed renewal) can never put the Operation back
into a claimable state — the recovery path moves it to OUTCOME_UNKNOWN (side
effect) or RETRYING (read only) instead. The result write is a second
transaction fenced by ``version + claim_token + owner + live lease``; a renewal
or result write that matches zero rows raises ``LeaseConflictError`` and nothing
is committed.

All lease decisions resolve to PostgreSQL ``clock_timestamp()`` (the injected
``now`` clock is a deterministic test seam only), so every worker judges expiry
against the same database clock regardless of its host clock.

Renewal is inline, not a background task: there is no asyncio task to leak, and
a cancelled or crashed worker simply abandons an EXECUTING Operation whose lease
expires into the recovery path. A renewal or result write that loses the lease
raises ``LeaseConflictError`` — it is never silently swallowed.

Each state write still commits its business state, ``run_event`` and
``event_outbox`` row together in one PostgreSQL transaction (the Redis publish
stays out of it, handled by the task-7 outbox publisher).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.claim import (
    LeaseConflictError,
    claim_operation,
    mark_failed,
    mark_succeeded,
    renew_lease,
)
from opspilot.execution.models import Operation
from opspilot.tools.types import ToolEffect, ToolResult

Invoker = Callable[[Operation], Awaitable[ToolResult]]
Clock = Callable[[], datetime]
SessionFactory = Callable[[], AsyncSession]


async def _clock_value(session: AsyncSession, clock: Clock | None) -> datetime:
    """Resolve the authoritative "now": the injected clock or the DB clock."""
    if clock is not None:
        return clock()
    resolved: datetime | None = await session.scalar(select(func.clock_timestamp()))
    if resolved is None:  # pragma: no cover - clock_timestamp() never yields NULL
        return datetime.now(UTC)
    return resolved


async def execute_operation(
    session_factory: SessionFactory,
    operation_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int,
    effect: ToolEffect,
    invoke: Invoker,
    now: Clock | None = None,
) -> Operation:
    """Claim, run under a fenced lease, and write the terminal result.

    ``invoke`` performs the tool call and returns a journal-safe ``ToolResult``.
    The claim transaction is committed before the invocation so an interrupted
    or stale worker leaves the Operation in EXECUTING (never claimable again).
    After the invocation the lease is renewed once when it crossed the ~1/3
    renewal mark, then the terminal result is written fenced; a
    ``LeaseConflictError`` means the DB write right was lost and the result is
    never committed. The clock is resolved once per session and shared between
    the renewal decision and the writes that follow it.
    """
    async with session_factory() as session:
        timestamp = await _clock_value(session, now)
        operation = await claim_operation(
            session, operation_id, owner=owner, lease_seconds=lease_seconds, now=timestamp
        )
        await session.commit()

    # the claim just installed these, so the worker holds a concrete lease
    token = operation.claim_token
    expires_at = operation.lease_expires_at
    assert token is not None and expires_at is not None

    outcome = await invoke(operation)

    async with session_factory() as session:
        timestamp = await _clock_value(session, now)
        renew_at = expires_at - timedelta(seconds=max(1, lease_seconds // 3))
        try:
            if timestamp >= renew_at:
                operation = await renew_lease(
                    session,
                    operation_id,
                    owner=owner,
                    token=token,
                    expected_version=operation.version,
                    lease_seconds=lease_seconds,
                    now=timestamp,
                )
            if outcome.ok:
                operation = await mark_succeeded(
                    session,
                    operation_id,
                    owner=owner,
                    token=token,
                    expected_version=operation.version,
                    result=outcome.data,
                    now=timestamp,
                )
            else:
                operation = await mark_failed(
                    session,
                    operation_id,
                    owner=owner,
                    token=token,
                    expected_version=operation.version,
                    error=outcome.error,
                    now=timestamp,
                )
            await session.commit()
        except LeaseConflictError:
            await session.rollback()
            raise
    return operation
