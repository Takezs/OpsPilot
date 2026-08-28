"""Approval decisions and manual-review resolutions.

``decide_approval`` locks the pending ApprovalRequest row with ``FOR UPDATE`` so
concurrent reviewers serialize: the first transitions the operation, every later
call returns the already-processed fact without duplicate events or state
changes. The binding is immutable — a tampered ``arguments_hash`` or
``operation_version`` rejects with a conflict, an expired approval cannot be
decided, and only REVIEWER/ADMIN may decide. A rejection transitions the
operation to the definitive REJECTED status and releases the idempotency
occupancy (after the status change, so the release trigger permits it).

``record_manual_review_resolution`` is an ADMIN-only audit write: it never
mutates the terminal MANUAL_REVIEW Operation. Only an outcome of
``RETRY_NEW_OPERATION`` authorizes a retry (enforced by database triggers).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
    ManualReviewResolution,
    ResolutionOutcome,
)
from opspilot.auth.models import Role
from opspilot.execution.models import Operation, OperationIdempotencyOccupancy, OperationStatus
from opspilot.execution.service import (
    ManualReviewNotTerminalError,
    OperationNotFoundError,
)
from opspilot.jobs.service import enqueue_operation_job
from opspilot.runs.journal import append_event


class ApprovalError(Exception):
    """Base error for the approval domain."""


class ApprovalNotFoundError(ApprovalError, LookupError):
    """Raised when an approval request does not exist."""


class ApprovalExpiredError(ApprovalError):
    """Raised when a pending approval is decided after its expiry."""


class ApprovalVersionConflictError(ApprovalError):
    """Raised when the approval binding no longer matches the operation."""


class ApprovalForbiddenError(ApprovalError):
    """Raised when the acting principal lacks the required role."""


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ApprovalDecisionResult:
    already_processed: bool
    transitioned: bool
    approval: ApprovalRequest
    operation: Operation


async def decide_approval(
    session: AsyncSession,
    approval_request_id: uuid.UUID,
    decision: ApprovalDecision,
    decided_by: str,
    role: Role,
    comment: str | None = None,
) -> ApprovalDecisionResult:
    """Decide a pending approval request; callers own the transaction.

    Only the first concurrent decision transitions the operation. Every later
    decision sees the already-processed fact and performs no writes.
    """
    if role not in {Role.REVIEWER, Role.ADMIN}:
        raise ApprovalForbiddenError("reviewer or admin role required")
    approval = await session.scalar(
        select(ApprovalRequest).where(ApprovalRequest.id == approval_request_id).with_for_update()
    )
    if approval is None:
        raise ApprovalNotFoundError(f"approval request {approval_request_id} does not exist")
    operation = await session.get(Operation, approval.operation_id)
    if operation is None:
        raise OperationNotFoundError(f"operation {approval.operation_id} does not exist")

    if approval.status is not ApprovalStatus.PENDING:
        return ApprovalDecisionResult(
            already_processed=True, transitioned=False, approval=approval, operation=operation
        )
    if datetime.now(UTC) >= approval.expires_at:
        raise ApprovalExpiredError(f"approval request {approval_request_id} has expired")
    if (
        approval.arguments_hash != operation.arguments_hash
        or approval.operation_version != operation.version
    ):
        raise ApprovalVersionConflictError(
            "approval binding no longer matches the operation arguments or version"
        )

    approved = decision is ApprovalDecision.APPROVE
    operation.status = OperationStatus.READY if approved else OperationStatus.REJECTED
    operation.version = operation.version + 1
    approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    approval.decided_at = datetime.now(UTC)
    approval.reviewed_by = decided_by
    approval.comment = comment

    if not approved:
        # Release the key only after the status is terminal so the release
        # trigger sees REJECTED, not WAITING_APPROVAL.
        await session.flush()
        await session.execute(
            delete(OperationIdempotencyOccupancy).where(
                OperationIdempotencyOccupancy.operation_id == operation.id
            )
        )

    await append_event(
        session,
        operation.run_id,
        "approval_decided",
        {
            "operation_id": str(operation.id),
            "approval_request_id": str(approval.id),
            "decision": decision.value,
            "reviewed_by": decided_by,
        },
    )
    if approved:
        enqueue_operation_job(session, operation.id, operation.version, "EXECUTE")
    return ApprovalDecisionResult(
        already_processed=False, transitioned=True, approval=approval, operation=operation
    )


async def record_manual_review_resolution(
    session: AsyncSession,
    operation_id: uuid.UUID,
    outcome: ResolutionOutcome,
    resolved_by: str,
    role: Role,
    note: str | None = None,
) -> ManualReviewResolution:
    """Append an ADMIN-only audit resolution; the operation stays unchanged."""
    if role is not Role.ADMIN:
        raise ApprovalForbiddenError("admin role required")
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise OperationNotFoundError(f"operation {operation_id} does not exist")
    if operation.status is not OperationStatus.MANUAL_REVIEW:
        raise ManualReviewNotTerminalError(f"operation {operation_id} is not in MANUAL_REVIEW")
    resolution = ManualReviewResolution(
        operation_id=operation_id, outcome=outcome, resolved_by=resolved_by, note=note
    )
    session.add(resolution)
    await session.flush()
    await append_event(
        session,
        operation.run_id,
        "manual_review_resolution",
        {
            "operation_id": str(operation_id),
            "outcome": outcome.value,
            "resolved_by": resolved_by,
        },
    )
    return resolution
