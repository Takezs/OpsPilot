"""Task 15 workspace/read API contract tests against real PostgreSQL.

These tests intentionally exercise only public HTTP boundaries.  PostgreSQL is
queried after writes to prove that the UI-facing acknowledgement is backed by
durable facts rather than process-local state.
"""

import uuid
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
from sqlalchemy import select, text

from opspilot.approvals.models import ApprovalRequest, ApprovalStatus
from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role, User
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.models import OperationAttempt
from opspilot.execution.service import create_refund_operation
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app
from opspilot.runs.journal import append_event
from opspilot.runs.service import create_agent_run


def _principal(user: User) -> Principal:
    return Principal(
        user_id=str(user.id),
        role=user.role,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.INTERNAL,
    )


async def _user(role: Role) -> User:
    user = User(
        id=uuid.uuid4(),
        username=f"task15-{uuid.uuid4().hex}",
        password_hash="unused",
        role=role,
        allowed_departments=[],
        max_access_level=1,
    )
    async with async_session_factory() as session:
        session.add(user)
        await session.commit()
    return user


async def _cleanup(users: list[User]) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        user_ids = [user.id for user in users]
        await connection.execute(
            "DELETE FROM agent_runs WHERE owner_user_id = ANY($1::uuid[])", user_ids
        )
        await connection.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", user_ids)
    finally:
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_run_binds_authenticated_owner_and_persists_created_event() -> None:
    owner = await _user(Role.USER)
    app.dependency_overrides[get_current_principal] = lambda: _principal(owner)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/runs", json={})

        assert response.status_code == 202
        run_id = uuid.UUID(response.json()["run_id"])
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT owner_user_id, next_seq FROM agent_runs WHERE id = :run_id"),
                    {"run_id": run_id},
                )
            ).one()
            assert row.owner_user_id == owner.id
            assert row.next_seq == 1
            event = (
                await session.execute(
                    text(
                        "SELECT seq, event_type FROM run_events WHERE run_id = :run_id ORDER BY seq"
                    ),
                    {"run_id": run_id},
                )
            ).one()
            assert event == (1, "run_created")
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_message_atomically_persists_message_event_and_run_job_intent() -> None:
    owner = await _user(Role.USER)
    async with async_session_factory() as session:
        run = await create_agent_run(session, _principal(owner))
        await session.commit()
    app.dependency_overrides[get_current_principal] = lambda: _principal(owner)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/runs/{run.id}/messages",
                json={"content": "Please check refund eligibility for ORD-002"},
            )

        assert response.status_code == 202
        message_id = uuid.UUID(response.json()["message_id"])
        async with async_session_factory() as session:
            message = (
                await session.execute(
                    text("SELECT run_id, role, content FROM run_messages WHERE id = :message_id"),
                    {"message_id": message_id},
                )
            ).one()
            assert message == (
                run.id,
                "USER",
                "Please check refund eligibility for ORD-002",
            )
            event = (
                await session.execute(
                    text(
                        "SELECT event_type FROM run_events "
                        "WHERE run_id = :run_id ORDER BY seq DESC LIMIT 1"
                    ),
                    {"run_id": run.id},
                )
            ).scalar_one()
            assert event == "user_message_created"
            intent = (
                await session.execute(
                    text(
                        "SELECT message_id, delivered_at FROM run_job_outbox "
                        "WHERE message_id = :message_id"
                    ),
                    {"message_id": message_id},
                )
            ).one()
            assert intent.message_id == message_id
            assert intent.delivered_at is None
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_history_is_owner_scoped_and_returns_strict_seq_window() -> None:
    owner, other = await _user(Role.USER), await _user(Role.USER)
    async with async_session_factory() as session:
        run = await create_agent_run(session, _principal(owner))
        for index in range(1, 4):
            await append_event(session, run.id, "history_test", {"index": index})
        await session.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_current_principal] = lambda: _principal(owner)
            response = await client.get(
                f"/api/v1/runs/{run.id}/history", params={"after_seq": 1, "limit": 20}
            )
            assert response.status_code == 200
            assert [row["seq"] for row in response.json()] == [2, 3]

            app.dependency_overrides[get_current_principal] = lambda: _principal(other)
            assert (await client.get(f"/api/v1/runs/{run.id}/history")).status_code == 404
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner, other])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approval_list_is_reviewer_only_and_exposes_immutable_binding() -> None:
    owner, reviewer = await _user(Role.USER), await _user(Role.REVIEWER)
    async with async_session_factory() as session:
        run = await create_agent_run(session, _principal(owner))
        operation = await create_refund_operation(session, run.id, "ORD-002", 350)
        approval = await session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.operation_id == operation.id)
        )
        assert approval is not None
        await session.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_current_principal] = lambda: _principal(owner)
            assert (await client.get("/api/v1/approval-requests")).status_code == 403

            app.dependency_overrides[get_current_principal] = lambda: _principal(reviewer)
            response = await client.get(
                "/api/v1/approval-requests", params={"status": ApprovalStatus.PENDING.value}
            )
            assert response.status_code == 200
            row = next(item for item in response.json() if item["id"] == str(approval.id))
            assert row["operation_id"] == str(operation.id)
            assert row["arguments_hash"] == operation.arguments_hash
            assert row["operation_version"] == operation.version
            assert row["operation_status"] == "WAITING_APPROVAL"
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner, reviewer])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_detail_contains_owned_operation_and_attempt_timeline() -> None:
    owner, other = await _user(Role.USER), await _user(Role.USER)
    async with async_session_factory() as session:
        run = await create_agent_run(session, _principal(owner))
        operation = await create_refund_operation(session, run.id, "ORD-DETAIL", 50)
        attempt = OperationAttempt(
            operation_id=operation.id,
            attempt_number=1,
            kind="EXECUTION",
            status="SUCCEEDED",
            request_payload={"order_number": "ORD-DETAIL"},
            response_payload={"provider_reference": "demo-ref"},
            completed_at=datetime.now(UTC),
        )
        session.add(attempt)
        await session.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            app.dependency_overrides[get_current_principal] = lambda: _principal(owner)
            response = await client.get(f"/api/v1/runs/{run.id}")
            assert response.status_code == 200
            body = response.json()
            assert body["id"] == str(run.id)
            operation_row = next(
                row for row in body["operations"] if row["id"] == str(operation.id)
            )
            assert operation_row["status"] == "READY"
            assert operation_row["attempts"] == [
                {
                    "id": str(attempt.id),
                    "attempt_number": 1,
                    "kind": "EXECUTION",
                    "status": "SUCCEEDED",
                    "error": None,
                    "completed_at": attempt.completed_at.isoformat(),
                }
            ]

            app.dependency_overrides[get_current_principal] = lambda: _principal(other)
            assert (await client.get(f"/api/v1/runs/{run.id}")).status_code == 404
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner, other])
