"""Operation creation with durable idempotency and resolution-gated retry.

Every refund Operation is persisted before any approval. The server derives the
business idempotency key (``refund:{order_id}``) and a stable arguments hash
from Pydantic-normalized parameters; the current occupancy
(``operation_idempotency_occupancy``) holds at most one executable Operation per
business key. Concurrent duplicate creates are serialized on a PostgreSQL
transaction advisory lock and then converge on the existing occupancy row, so
they return the same current Operation.

A retry of a MANUAL_REVIEW Operation is only possible when an ADMIN has written a
``manual_review_resolutions`` row with outcome ``RETRY_NEW_OPERATION``. The
authorization is consumed one shot: the retry transaction atomically binds the
resolution's ``replacement_operation_id`` to the single replacement Operation it
creates, so subsequent and concurrent retries return that same replacement (even
when the re-run Policy denies it — the MANUAL_REVIEW key stays held and no extra
rows are created). The binding is immutable at the database level (migration
0013): ``guard_resolution_binding`` refuses any clear or repoint once the value
is set, and the ``ON DELETE RESTRICT`` foreign key keeps a bound replacement
Operation undeletable, so a consumed authorization can never be revived. The
occupancy re-point, the new Operation (which re-runs Policy and Approval), the
status events and the outbox rows all commit in the caller's single transaction;
the database triggers prove the gate, and the pre-checks here only exist to
surface clean domain errors.
"""

import uuid
from collections.abc import Awaitable, Callable
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
from opspilot.jobs.service import enqueue_operation_job
from opspilot.runs.journal import append_event
from opspilot.tools.schemas import RefundOrderArgs
from opspilot.tools.types import ToolDefinition, ToolResult

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


class _ResolutionAlreadyClaimed(Exception):
    """Internal: a concurrent retry consumed the resolution first."""


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

    resolution: ManualReviewResolution | None = None
    if retry_of_operation_id is not None:
        resolution = await _validate_retry(session, retry_of_operation_id, role)
        if resolution.replacement_operation_id is not None:
            # the authorization was consumed: the replacement is the definitive
            # result, whatever its policy outcome (including DENIED)
            replacement = await session.get(Operation, resolution.replacement_operation_id)
            if replacement is not None:
                return replacement
            raise RetryNotAuthorizedError(
                f"resolution {resolution.id} points to a missing replacement operation"
            )
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

            if resolution is not None:
                # Atomically consume the retry authorization. The conditional
                # UPDATE (guard: still NULL) is the one-shot binding — a second
                # retry can never create a second replacement, even concurrently.
                claimed = await session.execute(
                    update(ManualReviewResolution)
                    .where(
                        ManualReviewResolution.id == resolution.id,
                        ManualReviewResolution.replacement_operation_id.is_(None),
                    )
                    .values(replacement_operation_id=operation.id)
                )
                if (claimed.rowcount or 0) == 0:  # type: ignore[attr-defined]
                    raise _ResolutionAlreadyClaimed()

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
            elif status is OperationStatus.READY:
                enqueue_operation_job(session, operation.id, operation.version, "EXECUTE")
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
    except _ResolutionAlreadyClaimed:
        # A concurrent retry consumed the resolution first: the savepoint rolled
        # back our attempt; return the winner's replacement.
        assert resolution is not None
        latest = await session.scalar(
            select(ManualReviewResolution).where(ManualReviewResolution.id == resolution.id)
        )
        if latest is not None and latest.replacement_operation_id is not None:
            replacement = await session.get(Operation, latest.replacement_operation_id)
            if replacement is not None:
                return replacement
        raise RetryNotAuthorizedError(
            "resolution was consumed but its replacement is unreadable"
        ) from None
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
) -> ManualReviewResolution:
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
        select(ManualReviewResolution)
        .where(
            ManualReviewResolution.operation_id == retry_of_operation_id,
            ManualReviewResolution.outcome == ResolutionOutcome.RETRY_NEW_OPERATION,
        )
        .order_by(ManualReviewResolution.created_at.desc())
        .limit(1)
    )
    if resolution is None:
        raise RetryNotAuthorizedError(
            f"operation {retry_of_operation_id} has no RETRY_NEW_OPERATION resolution"
        )
    return resolution


async def build_refund_operation_handler(
    session_factory: Callable[[], AsyncSession],
    run_id: uuid.UUID,
    *,
    transaction_guard: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> Callable[[ToolDefinition, RefundOrderArgs], Awaitable[ToolResult]]:
    """Wire a refund_order SIDE_EFFECT decision into the durable Operation flow.

    The runner hands every refund_order decision to this handler instead of
    invoking the payment adapter: it runs the deterministic policy, persists the
    Operation together with its occupancy / approval binding / events / outbox in
    a single transaction and returns a journal-safe ``ToolResult``. Task 9 never
    calls the Payment Provider.
    """

    async def _handle(definition: ToolDefinition, arguments: RefundOrderArgs) -> ToolResult:
        if definition.name != REFUND_TOOL:
            return ToolResult(
                ok=False, error=f"handler received unexpected tool {definition.name!r}"
            )
        try:
            async with session_factory() as session:
                if transaction_guard is not None:
                    await transaction_guard(session)
                operation = await create_refund_operation(
                    session, run_id, arguments.order_number, arguments.amount
                )
                await session.commit()
        except OperationError as error:
            return ToolResult(ok=False, error=str(error))
        return ToolResult(
            ok=True,
            data={
                "operation_id": str(operation.id),
                "status": operation.status.value,
                "idempotency_key": operation.idempotency_key,
            },
        )

    return _handle
