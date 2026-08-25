"""Claim/lease and fenced state writes for durable Operation execution (Task 10).

ARQ only delivers work; it provides no business lease. A worker claims a READY
or RETRYING Operation with one conditional PostgreSQL UPDATE that bumps
``version``, installs a fresh one-shot ``claim_token`` and sets ``lease_owner``
and ``lease_expires_at``: among concurrent workers at most one matches, so at
most one wins.

Every renewal and every result write is fenced by ``id + version +
claim_token + owner`` and by the live lease (``lease_expires_at > now``), so a
stale worker — old version/token, wrong owner, or an expired lease — matches
zero rows and cannot mutate business state, and a late external response cannot
overwrite a new holder's result. Each state write appends its ``run_event`` and
``event_outbox`` row in the caller's transaction, so the business fact, the
journal and the outbox commit or roll back together (Redis publish is not part
of this transaction; the task-7 publisher handles it separately).

Lease expiry recovery follows the state machine: a READ_ONLY operation moves
atomically to RETRYING (claimable again), while a SIDE_EFFECT operation may
only move atomically to OUTCOME_UNKNOWN. OUTCOME_UNKNOWN is not claimable, so
an expired side-effect lease can never re-invoke the provider; only task 11's
reconciliation may decide its verdict.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.service import OperationError, OperationNotFoundError
from opspilot.execution.state_machine import CLAIMABLE_STATUSES, assert_transition
from opspilot.runs.journal import append_event
from opspilot.tools.types import ToolEffect


class OperationNotClaimableError(OperationError):
    """Raised when an Operation is not in a claimable status."""


class LeaseConflictError(OperationError):
    """Raised when a fenced write loses the lease (stale token/version/owner)."""


class LeaseStateError(OperationError):
    """Raised when an Operation is not in a state that holds a live lease."""


def _ensure_aware_utc(value: datetime) -> datetime:
    """Normalize a timestamp to an explicit UTC-aware value.

    Lease decisions must agree on one timezone; a naive injected clock is
    interpreted as UTC so it can never be mis-bound against a timestamptz column.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _resolve_now(session: AsyncSession, now: datetime | None) -> datetime:
    """Resolve the authoritative "now" for a lease decision.

    The default is PostgreSQL ``clock_timestamp()`` so every worker — whatever
    its host clock — judges expiry against the same database clock. ``now`` is a
    deterministic test seam: when injected it stands in for the DB clock.
    """
    if now is not None:
        return _ensure_aware_utc(now)
    resolved: datetime | None = await session.scalar(select(func.clock_timestamp()))
    if resolved is None:  # pragma: no cover - clock_timestamp() never yields NULL
        return datetime.now(UTC)
    return resolved


async def _load_or_raise(session: AsyncSession, operation_id: uuid.UUID) -> Operation:
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFoundError(f"operation {operation_id} does not exist")
    return operation


async def claim_operation(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> Operation:
    """Claim a READY/RETRYING Operation and hand it a fresh lease.

    The conditional UPDATE only matches claimable statuses; a concurrent claim
    that loses the race sees EXECUTING and matches zero rows. The caller owns
    the transaction and commits together with the journal and outbox rows.
    """
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    if operation.status not in CLAIMABLE_STATUSES:
        raise OperationNotClaimableError(
            f"operation {operation_id} in {operation.status.value} is not claimable"
        )
    assert_transition(operation.status, OperationStatus.EXECUTING)
    token = secrets.token_urlsafe(32)
    expires_at = timestamp + timedelta(seconds=lease_seconds)
    claimed_id = await session.scalar(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status.in_(CLAIMABLE_STATUSES),
        )
        .values(
            status=OperationStatus.EXECUTING,
            version=Operation.version + 1,
            claim_token=token,
            lease_owner=owner,
            lease_expires_at=expires_at,
        )
        .returning(Operation.id)
    )
    if claimed_id is None:
        # lost the claim race: someone already moved it out of READY/RETRYING
        raise OperationNotClaimableError(
            f"operation {operation_id} was already claimed by a concurrent worker"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_claimed",
        {
            "operation_id": str(operation_id),
            "owner": owner,
            "version": operation.version + 1,
            "lease_expires_at": expires_at.isoformat(),
        },
    )
    claimed = await _load_or_raise(session, operation_id)
    await session.refresh(claimed)
    return claimed


async def renew_lease(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> Operation:
    """Extend the lease while the worker still holds it.

    Fenced by id + expected version + claim token + owner + a live lease; a
    stale worker (or one whose lease already expired) matches zero rows and
    raises ``LeaseConflictError`` — the loss of the DB write right.
    """
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    expires_at = timestamp + timedelta(seconds=lease_seconds)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.EXECUTING,
            Operation.version == expected_version,
            Operation.claim_token == token,
            Operation.lease_owner == owner,
            Operation.lease_expires_at > timestamp,
        )
        .values(lease_expires_at=expires_at)
    )
    if (updated.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise LeaseConflictError(
            f"lease renewal denied for operation {operation_id}: fencing mismatch"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_lease_renewed",
        {
            "operation_id": str(operation_id),
            "owner": owner,
            "version": expected_version,
            "lease_expires_at": expires_at.isoformat(),
        },
    )
    renewed = await _load_or_raise(session, operation_id)
    await session.refresh(renewed)
    return renewed


async def mark_succeeded(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    result: dict[str, object] | None = None,
    provider_reference_id: str | None = None,
    now: datetime | None = None,
) -> Operation:
    """Commit a successful result while the lease is still held and fenced."""
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.EXECUTING,
            Operation.version == expected_version,
            Operation.claim_token == token,
            Operation.lease_owner == owner,
            Operation.lease_expires_at > timestamp,
        )
        .values(
            status=OperationStatus.SUCCEEDED,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
            provider_reference_id=provider_reference_id,
            result_payload=result,
        )
    )
    if (updated.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise LeaseConflictError(
            f"success write denied for operation {operation_id}: fencing mismatch"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_succeeded",
        {
            "operation_id": str(operation_id),
            "version": expected_version + 1,
            "result": result,
            "provider_reference_id": provider_reference_id,
        },
    )
    succeeded = await _load_or_raise(session, operation_id)
    await session.refresh(succeeded)
    return succeeded


async def mark_failed(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    error: str | None = None,
    now: datetime | None = None,
) -> Operation:
    """Commit a definitive failure while the lease is still held and fenced."""
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.EXECUTING,
            Operation.version == expected_version,
            Operation.claim_token == token,
            Operation.lease_owner == owner,
            Operation.lease_expires_at > timestamp,
        )
        .values(
            status=OperationStatus.FAILED,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if (updated.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise LeaseConflictError(
            f"failure write denied for operation {operation_id}: fencing mismatch"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_failed",
        {
            "operation_id": str(operation_id),
            "version": expected_version + 1,
            "error": error,
        },
    )
    failed = await _load_or_raise(session, operation_id)
    await session.refresh(failed)
    return failed


async def mark_unknown(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    error: str | None = None,
    now: datetime | None = None,
) -> Operation:
    """Record an uncertain side-effect outcome (Task 10).

    Used when a SIDE_EFFECT failure cannot prove the provider was never reached:
    the external effect may or may not have happened. The Operation moves to
    OUTCOME_UNKNOWN, which is not claimable, so nothing here re-invokes the
    provider; task 11's reconciliation decides the verdict. Fenced exactly like
    ``mark_failed``, including the live-lease condition.
    """
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.EXECUTING,
            Operation.version == expected_version,
            Operation.claim_token == token,
            Operation.lease_owner == owner,
            Operation.lease_expires_at > timestamp,
        )
        .values(
            status=OperationStatus.OUTCOME_UNKNOWN,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if (updated.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise LeaseConflictError(
            f"unknown-outcome write denied for operation {operation_id}: fencing mismatch"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_outcome_unknown",
        {
            "operation_id": str(operation_id),
            "version": expected_version + 1,
            "error": error,
        },
    )
    unknown = await _load_or_raise(session, operation_id)
    await session.refresh(unknown)
    return unknown


async def recover_expired(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    effect: ToolEffect,
    now: datetime | None = None,
) -> Operation:
    """Atomically recover an EXECUTING Operation whose lease has expired.

    A READ_ONLY Operation moves to RETRYING (claimable again — safe to re-run).
    A SIDE_EFFECT Operation moves to OUTCOME_UNKNOWN, which is not claimable, so
    the provider is never re-invoked here; only task 11's reconciliation can
    decide the verdict. The write is fenced by status + expired lease, the
    claim_token is dropped and the version is bumped so any stale write by the
    previous holder matches zero rows.
    """
    timestamp = await _resolve_now(session, now)
    operation = await _load_or_raise(session, operation_id)
    target = (
        OperationStatus.RETRYING
        if effect is ToolEffect.READ_ONLY
        else OperationStatus.OUTCOME_UNKNOWN
    )
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.EXECUTING,
            Operation.lease_expires_at.is_not(None),
            Operation.lease_expires_at < timestamp,
        )
        .values(
            status=target,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if (updated.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise LeaseConflictError(
            f"expired-lease recovery denied for operation {operation_id}: "
            "not an EXECUTING operation with an expired lease"
        )
    await append_event(
        session,
        operation.run_id,
        "operation_recovered",
        {
            "operation_id": str(operation_id),
            "from_status": OperationStatus.EXECUTING.value,
            "to_status": target.value,
        },
    )
    recovered = await _load_or_raise(session, operation_id)
    await session.refresh(recovered)
    return recovered
