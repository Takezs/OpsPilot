"""Operation creation with durable idempotency and resolution-gated retry.

Every refund Operation is persisted before any approval. The server derives the
business idempotency key (``refund:{order_id}``) and a stable arguments hash
from Pydantic-normalized parameters; the current occupancy
(``operation_idempotency_occupancy``) holds at most one executable Operation per
business key. Concurrent duplicate creates are serialized on a PostgreSQL
transaction advisory lock and then converge on the existing occupancy row, so
they return the same current Operation.

A retry of a MANUAL_REVIEW Operation is only possible when an ADMIN has written a
``manual_review_resolutions`` row with outcome ``RETRY_NEW_OPERATION``; the
occupancy re-point and the new Operation (which re-runs Policy and Approval)
happen in the caller's single transaction. The database triggers prove the gate;
the pre-checks here only exist to surface clean domain errors. A retry whose
re-run Policy denies the amount creates a terminal audit Operation without
re-pointing the occupancy (the MANUAL_REVIEW key stays held).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
    ManualReviewResolution,
    ResolutionOutcome,
)
from opspilot.auth.models import Role
from opspilot.config import Settings
from opspilot.execution.idempotency import (
    derive_refund_idempotency_key,
    normalize_refund_arguments,
    stable_arguments_hash,
)
from opspilot.execution.models import Operation, OperationIdempotencyOccupancy, OperationStatus
from opspilot.execution.policy import PolicyDecision, decide_refund_policy
from opspilot.runs.journal import append_event

REFUND_TOOL = "refund_order"

_TERMINAL_STATUSES = {
    OperationStatus.DENIED,
    OperationStatus.REJECTED,
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
}


class OperationError(Exception):
    """Base error for operation persistence."""


class OperationNotFoundError(OperationError, LookupError):
    """Raised when a referenced Operation does not exist."""


class RetryNotAuthorizedError(OperationError):
    """Raised when a MANUAL_REVIEW retry lacks an allow-retry resolution."""


class ManualReviewNotTerminalError(OperationError):
    """Raised when a retry references an Operation that is not MANUAL_REVIEW."""


class ResolutionForbiddenError(OperationError):
    """Raised when a non-ADMIN attempts a resolution-gated action."""


def operation_lock_key(tool_name: str, idempotency_key: str) -> str:
    """Return the advisory-lock key serializing creates for one business key."""
    return f"{tool_name}:{idempotency_key}"


def _status_for(policy: PolicyDecision) -> OperationStatus:
    if policy is PolicyDecision.ALLOW:
        return OperationStatus.READY
    if policy is PolicyDecision.REQUIRE_APPROVAL:
        return OperationStatus.WAITING_APPROVAL
    return OperationStatus.DENIED


async def _current_occupant(session: AsyncSession, key: str) -> Operation | None:
    occupancy = await session.scalar(
        select(OperationIdempotencyOccupancy).where(
            OperationIdempotencyOccupancy.tool_name == REFUND_TOOL,
            OperationIdempotencyOccupancy.idempotency_key == key,
        )
    )
    if occupancy is None:
        return None
    return await session.get(Operation, occupancy.operation_id)


async def create_refund_operation(
    session: AsyncSession,
    run_id: uuid.UUID,
    order_number: str,
    amount: float,
    *,
    retry_of_operation_id: uuid.UUID | None = None,
    role: Role | None = None,
    approval_ttl_seconds: int | None = None,
) -> Operation:
    """Create (or return the current) refund Operation for the business key.

    Normalizes the refund parameters through the Pydantic schema, derives the
    stable server-side idempotency key and arguments hash, runs the deterministic
    policy, and persists the Operation together with its run events, outbox rows
    and (for REQUIRE_APPROVAL) a binding ApprovalRequest in the caller's
    transaction. Does not commit; the caller owns the transaction.
    """
    normalized = normalize_refund_arguments(order_number=order_number, amount=amount)
    key = derive_refund_idempotency_key(order_number)
    arguments_hash = stable_arguments_hash(normalized)
    policy = decide_refund_policy(amount)
    status = _status_for(policy)

    # Serialize creates/retries for the same business key; the transaction-level
    # advisory lock is released when the caller commits or rolls back.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": operation_lock_key(REFUND_TOOL, key)},
    )

    if retry_of_operation_id is not None:
        await _validate_retry(session, retry_of_operation_id, role)

        current = await _current_occupant(session, key)
        if current is not None and current.id != retry_of_operation_id:
            # a previous retry already re-pointed the key
            return current
    else:
        current = await _current_occupant(session, key)
        if current is not None:
            return current

    ttl = (
        approval_ttl_seconds
        if approval_ttl_seconds is not None
        else Settings().approval_ttl_seconds
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)

    try:
        async with session.begin_nested():
            operation = Operation(
                run_id=run_id,
                tool_name=REFUND_TOOL,
                normalized_arguments=normalized,
                arguments_hash=arguments_hash,
                idempotency_key=key,
                status=status,
                version=1,
                policy_decision=policy.value,
                retry_of_operation_id=retry_of_operation_id,
            )
            session.add(operation)
            await session.flush()

            if retry_of_operation_id is None:
                if status not in _TERMINAL_STATUSES:
                    session.add(
                        OperationIdempotencyOccupancy(
                            tool_name=REFUND_TOOL, idempotency_key=key, operation_id=operation.id
                        )
                    )
                    await session.flush()
            elif status not in _TERMINAL_STATUSES:
                result = await session.execute(
                    update(OperationIdempotencyOccupancy)
                    .where(
                        OperationIdempotencyOccupancy.tool_name == REFUND_TOOL,
                        OperationIdempotencyOccupancy.idempotency_key == key,
                        OperationIdempotencyOccupancy.operation_id == retry_of_operation_id,
                    )
                    .values(operation_id=operation.id)
                )
                if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
                    # lost the re-point race without the advisory lock
                    raise _RepointLost()

            await append_event(
                session,
                run_id,
                "operation_created",
                {
                    "operation_id": str(operation.id),
                    "tool_name": REFUND_TOOL,
                    "idempotency_key": key,
                    "arguments_hash": arguments_hash,
                    "status": status.value,
                },
            )
            await append_event(
                session,
                run_id,
                "policy_decided",
                {
                    "operation_id": str(operation.id),
                    "policy_decision": policy.value,
                    "amount": amount,
                },
            )
            if retry_of_operation_id is not None:
                await append_event(
                    session,
                    run_id,
                    "operation_retried",
                    {
                        "operation_id": str(operation.id),
                        "retry_of_operation_id": str(retry_of_operation_id),
                    },
                )

            if status is OperationStatus.WAITING_APPROVAL:
                approval = ApprovalRequest(
                    operation_id=operation.id,
                    arguments_hash=arguments_hash,
                    operation_version=operation.version,
                    status=ApprovalStatus.PENDING,
                    expires_at=expires_at,
                )
                session.add(approval)
                await session.flush()
                await append_event(
                    session,
                    run_id,
                    "approval_requested",
                    {
                        "operation_id": str(operation.id),
                        "approval_request_id": str(approval.id),
                        "expires_at": expires_at.isoformat(),
                    },
                )
    except IntegrityError as error:
        # Concurrent occupancy winner (fresh create) or a trigger refusal.
        if retry_of_operation_id is not None:
            raise RetryNotAuthorizedError(
                f"retry of {retry_of_operation_id} is not authorized by a resolution"
            ) from error
        current = await _current_occupant(session, key)
        if current is not None:
            return current
        raise
    except _RepointLost:
        current = await _current_occupant(session, key)
        if current is not None:
            return current
        raise

    return operation


class _RepointLost(Exception):
    """Internal: the occupancy was re-pointed by a concurrent retry."""


async def _validate_retry(
    session: AsyncSession, retry_of_operation_id: uuid.UUID, role: Role | None
) -> None:
    if role is not Role.ADMIN:
        raise ResolutionForbiddenError("only ADMIN may retry a manual-review operation")
    original = await session.get(Operation, retry_of_operation_id)
    if original is None:
        raise OperationNotFoundError(f"operation {retry_of_operation_id} does not exist")
    if original.status is not OperationStatus.MANUAL_REVIEW:
        raise ManualReviewNotTerminalError(
            f"operation {retry_of_operation_id} is not in MANUAL_REVIEW"
        )
    resolution = await session.scalar(
        select(ManualReviewResolution).where(
            ManualReviewResolution.operation_id == retry_of_operation_id,
            ManualReviewResolution.outcome == ResolutionOutcome.RETRY_NEW_OPERATION,
        )
    )
    if resolution is None:
        raise RetryNotAuthorizedError(
            f"operation {retry_of_operation_id} has no RETRY_NEW_OPERATION resolution"
        )
