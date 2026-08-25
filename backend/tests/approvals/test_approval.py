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
"""

import asyncio
import uuid
from collections.abc import Sequence

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from opspilot.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
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
    create_refund_operation,
)
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app
from opspilot.runs.models import Run, RunStatus


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
            assert "RETRY_NEW_OPERATION" in str(excinfo.value)
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
