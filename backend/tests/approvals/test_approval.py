"""Operation persistence, immutable approval binding and manual-review retry.

Integration tests against real PostgreSQL. Task 9 acceptance scenarios:

- (a) concurrent duplicate creates return the same current Operation;
- (b) without an allow-retry resolution a MANUAL_REVIEW Operation cannot create
      a new Operation;
- (c) after an allow-retry resolution a new Operation is creatable with the same
      business idempotency key and a different id;
- (d) retry lineage is auditable via ``retry_of_operation_id``;
- (e) two concurrent admin retries create at most one new Operation;
- (f) the original MANUAL_REVIEW Operation stays unchanged;
- (g) occupancy switch / Operation / run_event / outbox failure rolls everything
      back atomically.

Plus approval binding: arguments-hash or version tampering -> 409, expiry cannot
approve, only REVIEWER/ADMIN may decide, concurrent reviewers yield a single
transition with no duplicate events. The resolution gate is additionally proven
at the database level by the occupancy triggers.

Task 9 review-fix guarantees: one-shot resolution consumption durably bound to
``replacement_operation_id`` (subsequent and concurrent retries return the same
replacement — including a DENIED one — and never create extra rows); a hardened
repoint trigger that validates the new occupant's tool, business key, lineage and
resolution binding at the database level; and atomic rollback of the claim /
replacement / occupancy switch / events / outbox when any step fails.
"""

import asyncio
import json
import uuid
from collections.abc import Sequence

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from opspilot.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
    ManualReviewResolution,
    ResolutionOutcome,
)
from opspilot.approvals.service import (
    ApprovalDecision,
    ApprovalDecisionResult,
    ApprovalExpiredError,
    ApprovalForbiddenError,
    ApprovalVersionConflictError,
    decide_approval,
    record_manual_review_resolution,
)
from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.service import (
    RetryNotAuthorizedError,
    build_refund_operation_handler,
    create_refund_operation,
)
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app
from opspilot.runs.models import Run, RunStatus
from opspilot.tools.schemas import RefundOrderArgs
from opspilot.tools.types import ToolDefinition, ToolEffect, ToolResult


async def _create_run() -> uuid.UUID:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    return run_id


async def _cleanup_runs(run_ids: Sequence[uuid.UUID]) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if run_ids:
            await connection.execute(
                "DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids)
            )
    finally:
        await connection.close()


async def _count(query: str, *args: object) -> int:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        value = await connection.fetchval(query, *args)
        return int(value or 0)
    finally:
        await connection.close()


async def _set_status(operation_id: uuid.UUID, status: str) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "UPDATE tool_operations SET status = $1 WHERE id = $2", status, operation_id
        )
    finally:
        await connection.close()


async def _approval_id(operation_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as session:
        approval = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.operation_id == operation_id)
        )
        assert approval is not None
        return approval.id


async def _create_waiting_approval(run_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as session:
        operation = await create_refund_operation(session, run_id, "A100", 250.0)
        await session.commit()
        return operation.id


async def _insert_operation_raw(
    run_id: uuid.UUID,
    *,
    tool_name: str = "refund_order",
    order_number: str = "A100",
    amount: float = 250.0,
    status: str = "READY",
    retry_of: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a tool_operations row with arbitrary attributes (trigger probing)."""
    key = f"refund:{order_number}" if tool_name == "refund_order" else f"{tool_name}:{order_number}"
    operation_id = uuid.uuid4()
    arguments = json.dumps({"order_number": order_number, "amount": amount})
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "INSERT INTO tool_operations "
            "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
            " status, version, policy_decision, retry_of_operation_id, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::operation_status, 1, 'ALLOW', $8, "
            "now(), now())",
            operation_id,
            run_id,
            tool_name,
            arguments,
            "0" * 64,
            key,
            status,
            retry_of,
        )
        return operation_id
    finally:
        await connection.close()


async def _resolution_for(operation_id: uuid.UUID) -> ManualReviewResolution:
    async with async_session_factory() as session:
        resolution = await session.scalar(
            select(ManualReviewResolution).where(
                ManualReviewResolution.operation_id == operation_id,
                ManualReviewResolution.outcome == ResolutionOutcome.RETRY_NEW_OPERATION,
            )
        )
        assert resolution is not None
        return resolution


async def _manual_review_original(run_id: uuid.UUID, order_number: str = "A100") -> uuid.UUID:
    async with async_session_factory() as session:
        original = await create_refund_operation(session, run_id, order_number, 250.0)
        await session.commit()
        original_id = original.id
    await _set_status(original_id, "MANUAL_REVIEW")
    async with async_session_factory() as session:
        await record_manual_review_resolution(
            session,
            original_id,
            ResolutionOutcome.RETRY_NEW_OPERATION,
            resolved_by="admin-1",
            role=Role.ADMIN,
            note="retry",
        )
        await session.commit()
    return original_id


# --- Operation persistence --------------------------------------------------


async def test_create_low_amount_persists_ready_operation() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            operation = await create_refund_operation(session, run_id, "A101", 50.0)
            await session.commit()
            assert operation.tool_name == "refund_order"
            assert operation.status == OperationStatus.READY
            assert operation.idempotency_key == "refund:A101"
            assert operation.policy_decision == "ALLOW"
            assert len(operation.arguments_hash) == 64
            assert operation.version == 1
            assert operation.retry_of_operation_id is None
            assert operation.normalized_arguments == {"order_number": "A101", "amount": 50.0}

        assert await _count("SELECT count(*) FROM tool_operations WHERE id = $1", operation.id) == 1
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                operation.id,
            )
            == 1
        )
        # state + run_event + outbox landed in the same transaction
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 2
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 2
        assert (
            await _count(
                "SELECT count(*) FROM approval_requests WHERE operation_id = $1", operation.id
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_create_medium_amount_creates_pending_approval_binding() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            operation = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()
            operation_id = operation.id

        async with async_session_factory() as session:
            approval = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.operation_id == operation_id)
            )
            assert approval is not None
            assert approval.status == ApprovalStatus.PENDING
            assert approval.arguments_hash == operation.arguments_hash
            assert approval.operation_version == 1
            assert approval.expires_at is not None

        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                operation_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_create_high_amount_denies_and_releases_key() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            denied = await create_refund_operation(session, run_id, "A103", 1200.0)
            await session.commit()
        assert denied.status == OperationStatus.DENIED
        assert denied.policy_decision == "DENY"

        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                denied.id,
            )
            == 0
        )
        # a definitive terminal operation released the key: a later create is a new operation
        async with async_session_factory() as session:
            again = await create_refund_operation(session, run_id, "A103", 1200.0)
            await session.commit()
        assert again.id != denied.id
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A103'"
            )
            == 2
        )
    finally:
        await _cleanup_runs([run_id])


async def test_sequential_duplicate_create_returns_existing_operation() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            first = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()
        async with async_session_factory() as session:
            second = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()

        assert second.id == first.id
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 1
        )
        # the duplicate call appended no new events
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 3
    finally:
        await _cleanup_runs([run_id])


async def test_concurrent_duplicate_create_returns_same_operation() -> None:
    run_id = await _create_run()
    try:

        async def _create() -> uuid.UUID:
            async with async_session_factory() as session:
                operation = await create_refund_operation(session, run_id, "A100", 250.0)
                await session.commit()
                return operation.id

        ids = await asyncio.gather(_create(), _create(), _create())

        assert len(set(ids)) == 1
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 1
        )
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy "
                "WHERE operation_id = ANY(ARRAY[$1]::uuid[])",
                ids[0],
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


# --- Immutable approval binding ---------------------------------------------


async def test_approve_transitions_operation_to_ready() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        async with async_session_factory() as session:
            result = await decide_approval(
                session,
                approval_id,
                ApprovalDecision.APPROVE,
                decided_by="reviewer-1",
                role=Role.REVIEWER,
                comment="ok",
            )
            await session.commit()
            assert result.transitioned is True
            assert result.already_processed is False
            assert result.operation.status == OperationStatus.READY
            assert result.operation.version == 2
            assert result.approval.status == ApprovalStatus.APPROVED
            assert result.approval.reviewed_by == "reviewer-1"

        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "approval_decided",
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_reject_transitions_operation_to_rejected_and_releases_key() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        async with async_session_factory() as session:
            result = await decide_approval(
                session,
                approval_id,
                ApprovalDecision.REJECT,
                decided_by="reviewer-2",
                role=Role.REVIEWER,
                comment="no",
            )
            await session.commit()
            assert result.operation.status == OperationStatus.REJECTED
            assert result.approval.status == ApprovalStatus.REJECTED

        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                operation_id,
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_approval_requires_reviewer_or_admin_role() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        async with async_session_factory() as session:
            with pytest.raises(ApprovalForbiddenError):
                await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by="user-1",
                    role=Role.USER,
                    comment="bypass",
                )
            await session.rollback()
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "approval_decided",
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_approval_rejects_tampered_arguments_hash() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute(
                "UPDATE tool_operations SET arguments_hash = repeat('0', 64) WHERE id = $1",
                operation_id,
            )
        finally:
            await connection.close()

        async with async_session_factory() as session:
            with pytest.raises(ApprovalVersionConflictError):
                await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by="reviewer-1",
                    role=Role.REVIEWER,
                )
            await session.rollback()

        # no transition, no decision event
        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None and operation.status == OperationStatus.WAITING_APPROVAL
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "approval_decided",
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_approval_rejects_tampered_operation_version() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute(
                "UPDATE tool_operations SET version = version + 1 WHERE id = $1", operation_id
            )
        finally:
            await connection.close()

        async with async_session_factory() as session:
            with pytest.raises(ApprovalVersionConflictError):
                await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by="reviewer-1",
                    role=Role.REVIEWER,
                )
            await session.rollback()
    finally:
        await _cleanup_runs([run_id])


async def test_expired_approval_cannot_approve() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            operation = await create_refund_operation(
                session, run_id, "A100", 250.0, approval_ttl_seconds=-1
            )
            await session.commit()
            operation_id = operation.id
        approval_id = await _approval_id(operation_id)

        async with async_session_factory() as session:
            with pytest.raises(ApprovalExpiredError):
                await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by="reviewer-1",
                    role=Role.REVIEWER,
                )
            await session.rollback()

        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "approval_decided",
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_concurrent_reviewers_single_transition() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        async def _decide(decided_by: str) -> ApprovalDecisionResult:
            async with async_session_factory() as session:
                result = await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by=decided_by,
                    role=Role.REVIEWER,
                    comment="ok",
                )
                await session.commit()
                return result

        results = await asyncio.gather(_decide("reviewer-1"), _decide("reviewer-2"))

        assert sum(1 for r in results if r.transitioned) == 1
        assert sum(1 for r in results if r.already_processed) == 1

        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            approval = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
            assert operation is not None and operation.status == OperationStatus.READY
            assert operation.version == 2
            assert approval is not None and approval.status == ApprovalStatus.APPROVED

        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "approval_decided",
            )
            == 1
        )
        # every journaled event has exactly one outbox row
        events = await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id)
        outbox = await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id)
        assert outbox == events
    finally:
        await _cleanup_runs([run_id])


# --- MANUAL_REVIEW terminal + resolution-gated retry ------------------------


async def test_manual_review_without_resolution_cannot_create_new_operation() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            again = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()

        # idempotent: the existing MANUAL_REVIEW operation is returned, no new row
        assert again.id == operation_id
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_resolution_recorded_but_operation_unchanged() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            resolution = await record_manual_review_resolution(
                session,
                operation_id,
                ResolutionOutcome.RESOLVED,
                resolved_by="admin-1",
                role=Role.ADMIN,
                note="no action",
            )
            await session.commit()
            assert resolution.operation_id == operation_id
            assert resolution.outcome == ResolutionOutcome.RESOLVED

        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None and operation.status == OperationStatus.MANUAL_REVIEW

        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 AND event_type = $2",
                run_id,
                "manual_review_resolution",
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_resolution_requires_admin_role() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            with pytest.raises(ApprovalForbiddenError):
                await record_manual_review_resolution(
                    session,
                    operation_id,
                    ResolutionOutcome.RESOLVED,
                    resolved_by="reviewer-1",
                    role=Role.REVIEWER,
                )
            await session.rollback()
        assert (
            await _count(
                "SELECT count(*) FROM manual_review_resolutions WHERE operation_id = $1",
                operation_id,
            )
            == 0
        )
    finally:
        await _cleanup_runs([run_id])


async def test_retry_without_resolution_raises_retry_not_authorized() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            with pytest.raises(RetryNotAuthorizedError):
                await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    250.0,
                    retry_of_operation_id=operation_id,
                    role=Role.ADMIN,
                )
            await session.rollback()

        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_retry_with_resolution_creates_new_operation_lineage() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            original = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()
            original_id = original.id
            original_hash = original.arguments_hash
        await _set_status(original_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            await record_manual_review_resolution(
                session,
                original_id,
                ResolutionOutcome.RETRY_NEW_OPERATION,
                resolved_by="admin-1",
                role=Role.ADMIN,
                note="retry",
            )
            await session.commit()

        async with async_session_factory() as session:
            retried = await create_refund_operation(
                session,
                run_id,
                "A100",
                250.0,
                retry_of_operation_id=original_id,
                role=Role.ADMIN,
            )
            await session.commit()

        assert retried.id != original_id
        assert retried.retry_of_operation_id == original_id
        assert retried.idempotency_key == "refund:A100"
        assert retried.arguments_hash == original_hash
        assert retried.status == OperationStatus.WAITING_APPROVAL
        # policy + approval re-run: a fresh approval request is bound to the retry
        assert (
            await _count(
                "SELECT count(*) FROM approval_requests WHERE operation_id = $1", retried.id
            )
            == 1
        )

        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 2
        )
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                retried.id,
            )
            == 1
        )
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                original_id,
            )
            == 0
        )

        # original MANUAL_REVIEW operation stays terminal and unchanged
        async with async_session_factory() as session:
            operation = await session.get(Operation, original_id)
            assert operation is not None and operation.status == OperationStatus.MANUAL_REVIEW
            assert operation.version == 1
    finally:
        await _cleanup_runs([run_id])


async def test_concurrent_admin_retries_create_at_most_one_new_operation() -> None:
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            original = await create_refund_operation(session, run_id, "A100", 250.0)
            await session.commit()
            original_id = original.id
        await _set_status(original_id, "MANUAL_REVIEW")

        async with async_session_factory() as session:
            await record_manual_review_resolution(
                session,
                original_id,
                ResolutionOutcome.RETRY_NEW_OPERATION,
                resolved_by="admin-1",
                role=Role.ADMIN,
                note="retry",
            )
            await session.commit()

        async def _retry() -> uuid.UUID:
            async with async_session_factory() as session:
                operation = await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    250.0,
                    retry_of_operation_id=original_id,
                    role=Role.ADMIN,
                )
                await session.commit()
                return operation.id

        new_ids = await asyncio.gather(_retry(), _retry())

        assert new_ids[0] == new_ids[1]
        assert new_ids[0] != original_id
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 2
        )
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                new_ids[0],
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


# --- Atomicity --------------------------------------------------------------


async def test_create_rolls_back_operation_occupancy_and_events_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _create_run()

    async def _boom(
        session: object, run_id: uuid.UUID, event_type: str, payload: dict[str, object]
    ) -> None:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr("opspilot.execution.service.append_event", _boom)
    try:
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="outbox write failed"):
                await create_refund_operation(session, run_id, "A100", 250.0)
            await session.rollback()

        assert await _count("SELECT count(*) FROM tool_operations WHERE run_id = $1", run_id) == 0
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy o "
                "WHERE o.operation_id IN (SELECT id FROM tool_operations WHERE run_id = $1)",
                run_id,
            )
            == 0
        )
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 0
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 0

        async with async_session_factory() as session:
            run = await session.get(Run, run_id)
            assert run is not None and run.next_seq == 0
    finally:
        await _cleanup_runs([run_id])


# --- Database-level proof of the resolution gate ----------------------------


async def test_occupancy_repoint_blocked_without_resolution_by_trigger() -> None:
    # With the hardened trigger an unrelated operation (different business key)
    # is rejected at the occupancy checks before the resolution binding check; the
    # missing-resolution-binding case is covered by
    # test_repoint_denies_arbitrary_operation_not_bound_to_resolution.
    run_id = await _create_run()
    try:
        async with async_session_factory() as session:
            original = await create_refund_operation(session, run_id, "A100", 250.0)
            other = await create_refund_operation(session, run_id, "A101", 50.0)
            await session.commit()
            original_id, other_id = original.id, other.id
        await _set_status(original_id, "MANUAL_REVIEW")

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            with pytest.raises(Exception) as excinfo:
                await connection.execute(
                    "UPDATE operation_idempotency_occupancy SET operation_id = $1 "
                    "WHERE operation_id = $2 AND tool_name = 'refund_order' "
                    "AND idempotency_key = 'refund:A100'",
                    other_id,
                    original_id,
                )
            assert "repoint denied" in str(excinfo.value)
        finally:
            await connection.close()

        # occupancy unchanged
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                original_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_occupancy_delete_blocked_while_active_by_trigger() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            with pytest.raises(Exception) as excinfo:
                await connection.execute(
                    "DELETE FROM operation_idempotency_occupancy WHERE operation_id = $1",
                    operation_id,
                )
            assert "release denied" in str(excinfo.value)
        finally:
            await connection.close()

        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                operation_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


# --- HTTP boundary ----------------------------------------------------------


def _principal(role: Role) -> Principal:
    return Principal(
        user_id=str(uuid.uuid4()),
        role=role,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.CONFIDENTIAL,
    )


async def test_approval_decision_endpoint_forbids_user_role() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.USER)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/approval-requests/{approval_id}/decisions",
                    json={"decision": "APPROVE", "comment": "hack"},
                )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
    finally:
        await _cleanup_runs([run_id])


async def test_approval_decision_endpoint_approves_as_reviewer() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.REVIEWER)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/approval-requests/{approval_id}/decisions",
                    json={"decision": "APPROVE", "comment": "ok"},
                )
            assert response.status_code == 200
            body = response.json()
            assert body["transitioned"] is True
            assert body["operation_status"] == "READY"
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
    finally:
        await _cleanup_runs([run_id])


async def test_approval_decision_endpoint_conflicts_on_tamper() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        approval_id = await _approval_id(operation_id)

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute(
                "UPDATE tool_operations SET version = version + 1 WHERE id = $1", operation_id
            )
        finally:
            await connection.close()

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.REVIEWER)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/approval-requests/{approval_id}/decisions",
                    json={"decision": "APPROVE"},
                )
            assert response.status_code == 409
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
    finally:
        await _cleanup_runs([run_id])


async def test_manual_review_resolution_endpoint_requires_admin() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.REVIEWER)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/operations/{operation_id}/manual-review-resolutions",
                    json={"outcome": "RESOLVED", "note": "no action"},
                )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_principal, None)

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.ADMIN)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/operations/{operation_id}/manual-review-resolutions",
                    json={"outcome": "RESOLVED", "note": "no action"},
                )
            assert response.status_code == 200
            assert response.json()["operation_id"] == str(operation_id)
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
    finally:
        await _cleanup_runs([run_id])


async def test_retry_endpoint_requires_admin_and_resolution() -> None:
    run_id = await _create_run()
    try:
        operation_id = await _create_waiting_approval(run_id)
        await _set_status(operation_id, "MANUAL_REVIEW")

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.USER)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/operations/{operation_id}/retry", json={"amount": 250.0}
                )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_principal, None)

        app.dependency_overrides[get_current_principal] = lambda: _principal(Role.ADMIN)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/operations/{operation_id}/retry", json={"amount": 250.0}
                )
            # no allow-retry resolution has been written -> the gate refuses
            assert response.status_code == 409
        finally:
            app.dependency_overrides.pop(get_current_principal, None)
    finally:
        await _cleanup_runs([run_id])


# --- Task 9 fix: one-shot resolution consumption ----------------------------


async def test_retry_resolution_is_consumed_after_single_replacement() -> None:
    """A RETRY_NEW_OPERATION resolution yields at most one replacement."""
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        resolution = await _resolution_for(original_id)

        async def _retry() -> uuid.UUID:
            async with async_session_factory() as session:
                operation = await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    250.0,
                    retry_of_operation_id=original_id,
                    role=Role.ADMIN,
                )
                await session.commit()
                return operation.id

        first = await _retry()
        second = await _retry()

        assert first != original_id
        assert second == first
        # exactly one replacement row exists for the business key
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 2
        )
        # the resolution is durably bound to the single replacement
        async with async_session_factory() as session:
            latest = await session.get(ManualReviewResolution, resolution.id)
            assert latest is not None
            assert latest.replacement_operation_id == first
    finally:
        await _cleanup_runs([run_id])


async def test_retry_denied_replacement_is_definitive_result() -> None:
    """A DENIED replacement is still the resolution's single definitive result."""
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)

        async def _retry(amount: float) -> uuid.UUID:
            async with async_session_factory() as session:
                operation = await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    amount,
                    retry_of_operation_id=original_id,
                    role=Role.ADMIN,
                )
                await session.commit()
                return operation.id

        denied = await _retry(1200.0)
        async with async_session_factory() as session:
            operation = await session.get(Operation, denied)
            assert operation is not None and operation.status == OperationStatus.DENIED

        again = await _retry(1200.0)
        assert again == denied
        # no extra DENIED rows are created on subsequent requests
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 2
        )
        # the MANUAL_REVIEW key was not re-pointed by a DENIED replacement
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                original_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


@pytest.mark.parametrize(
    ("amount", "expected_status"),
    [
        (50.0, "READY"),
        (250.0, "WAITING_APPROVAL"),
        (1200.0, "DENIED"),
    ],
)
async def test_concurrent_admin_retries_single_replacement_per_policy(
    amount: float, expected_status: str
) -> None:
    """Concurrent ADMIN retries produce one replacement for every policy outcome."""
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)

        async def _retry() -> uuid.UUID:
            async with async_session_factory() as session:
                operation = await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    amount,
                    retry_of_operation_id=original_id,
                    role=Role.ADMIN,
                )
                await session.commit()
                return operation.id

        ids = await asyncio.gather(_retry(), _retry(), _retry())

        assert len(set(ids)) == 1
        assert ids[0] != original_id
        # original + exactly one replacement for the business key
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 2
        )
        async with async_session_factory() as session:
            operation = await session.get(Operation, ids[0])
            assert operation is not None and operation.status == expected_status
    finally:
        await _cleanup_runs([run_id])


async def test_retry_rolls_back_resolution_claim_and_replacement_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure anywhere in the retry transaction rolls back the whole unit."""
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        resolution = await _resolution_for(original_id)
        events_before = await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id)

        async def _boom(
            session: object, run_id: uuid.UUID, event_type: str, payload: dict[str, object]
        ) -> None:
            raise RuntimeError("outbox write failed")

        monkeypatch.setattr("opspilot.execution.service.append_event", _boom)
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="outbox write failed"):
                await create_refund_operation(
                    session,
                    run_id,
                    "A100",
                    250.0,
                    retry_of_operation_id=original_id,
                    role=Role.ADMIN,
                )
            await session.rollback()

        # nothing persisted: the claim, the replacement, events and outbox all rolled back
        assert (
            await _count(
                "SELECT count(*) FROM tool_operations WHERE idempotency_key = 'refund:A100'"
            )
            == 1
        )
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                original_id,
            )
            == 1
        )
        assert (
            await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id)
            == events_before
        )
        assert (
            await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id)
            == events_before
        )
        async with async_session_factory() as session:
            latest = await session.get(ManualReviewResolution, resolution.id)
            assert latest is not None and latest.replacement_operation_id is None

        # the resolution is still claimable: a clean retry succeeds
        monkeypatch.undo()
        async with async_session_factory() as session:
            operation = await create_refund_operation(
                session,
                run_id,
                "A100",
                250.0,
                retry_of_operation_id=original_id,
                role=Role.ADMIN,
            )
            await session.commit()
            retried_id = operation.id
        assert retried_id != original_id
    finally:
        await _cleanup_runs([run_id])


# --- Task 9 fix: hardened occupancy repoint validates the new occupant ------


async def _bind_replacement(operation_id: uuid.UUID, occupant_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "UPDATE manual_review_resolutions SET replacement_operation_id = $1 "
            "WHERE operation_id = $2 AND outcome = 'RETRY_NEW_OPERATION'",
            occupant_id,
            operation_id,
        )
    finally:
        await connection.close()


async def _expect_repoint_denied(
    original_id: uuid.UUID, occupant_id: uuid.UUID, keyword: str
) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        with pytest.raises(Exception) as excinfo:
            await connection.execute(
                "UPDATE operation_idempotency_occupancy SET operation_id = $1 "
                "WHERE operation_id = $2 AND tool_name = 'refund_order' "
                "AND idempotency_key = 'refund:A100'",
                occupant_id,
                original_id,
            )
        assert keyword in str(excinfo.value)
    finally:
        await connection.close()


async def test_repoint_denies_new_occupant_with_wrong_tool() -> None:
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        occupant_id = await _insert_operation_raw(
            run_id, tool_name="pay_refund", order_number="A100", retry_of=original_id
        )
        await _bind_replacement(original_id, occupant_id)
        await _expect_repoint_denied(original_id, occupant_id, "tool does not match")
    finally:
        await _cleanup_runs([run_id])


async def test_repoint_denies_new_occupant_with_wrong_business_key() -> None:
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        occupant_id = await _insert_operation_raw(run_id, order_number="B200", retry_of=original_id)
        await _bind_replacement(original_id, occupant_id)
        await _expect_repoint_denied(original_id, occupant_id, "idempotency key does not match")
    finally:
        await _cleanup_runs([run_id])


async def test_repoint_denies_new_occupant_with_wrong_lineage() -> None:
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        other_id = await _manual_review_original(run_id, order_number="B200")
        occupant_id = await _insert_operation_raw(run_id, order_number="A100", retry_of=other_id)
        await _bind_replacement(original_id, occupant_id)
        await _expect_repoint_denied(original_id, occupant_id, "must be a retry")
    finally:
        await _cleanup_runs([run_id])


async def test_repoint_denies_arbitrary_operation_not_bound_to_resolution() -> None:
    run_id = await _create_run()
    try:
        original_id = await _manual_review_original(run_id)
        # the operation is a legitimate retry of the original (tool/key/lineage all
        # match) but no resolution is bound to it -> the binding check is decisive
        occupant_id = await _insert_operation_raw(run_id, order_number="A100", retry_of=original_id)
        await _expect_repoint_denied(original_id, occupant_id, "resolution")
    finally:
        await _cleanup_runs([run_id])


# --- Task 9 fix: Agent refund decisions enter the durable Operation flow ------


async def test_refund_handler_routes_to_durable_operation_flow() -> None:
    """The production handler creates a durable Operation, never the adapter."""
    run_id = await _create_run()
    try:
        handler = await build_refund_operation_handler(async_session_factory, run_id)

        async def _never_invoked(*args: object, **kwargs: object) -> ToolResult:
            raise AssertionError("payment adapter must never be invoked")

        definition = ToolDefinition(
            name="refund_order",
            description="refund an order",
            input_schema=RefundOrderArgs,
            effect=ToolEffect.SIDE_EFFECT,
            idempotency_capable=True,
            supports_reconciliation=False,
            invoke=_never_invoked,
        )

        result = await handler(definition, RefundOrderArgs(order_number="A100", amount=250.0))

        assert result.ok is True
        assert result.data is not None
        assert result.data["status"] == "WAITING_APPROVAL"
        assert result.data["idempotency_key"] == "refund:A100"
        # the durable Operation + its occupancy + approval landed in PostgreSQL
        assert await _count("SELECT count(*) FROM tool_operations WHERE run_id = $1", run_id) == 1
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy o "
                "JOIN tool_operations t ON t.id = o.operation_id WHERE t.run_id = $1",
                run_id,
            )
            == 1
        )
        assert (
            await _count(
                "SELECT count(*) FROM approval_requests a "
                "JOIN tool_operations t ON t.id = a.operation_id WHERE t.run_id = $1",
                run_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])
