"""Task 12 authorization and SSE protocol tests."""

import uuid

import asyncpg
import httpx
import pytest

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role, User
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app
from opspilot.runs.models import Run, RunStatus
from opspilot.runs.service import create_agent_run, require_run_access
from opspilot.runs.sse import validate_last_event_id


def _principal(user_id: uuid.UUID, role: Role) -> Principal:
    return Principal(
        user_id=str(user_id),
        role=role,
        allowed_departments=frozenset(),
        max_access_level=AccessLevel.INTERNAL,
    )


async def _user(role: Role) -> User:
    user = User(
        id=uuid.uuid4(),
        username=f"sse-{uuid.uuid4().hex}",
        password_hash="unused",
        role=role,
        allowed_departments=[],
        max_access_level=1,
    )
    async with async_session_factory() as session:
        session.add(user)
        await session.commit()
    return user


async def _cleanup(users: list[User], legacy_run_ids: list[uuid.UUID] | None = None) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if legacy_run_ids:
            await connection.execute(
                "DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", legacy_run_ids
            )
        await connection.execute(
            "DELETE FROM agent_runs WHERE owner_user_id = ANY($1::uuid[])", [u.id for u in users]
        )
        await connection.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])", [u.id for u in users]
        )
    finally:
        await connection.close()


@pytest.mark.parametrize("role", [Role.USER, Role.REVIEWER])
async def test_user_and_reviewer_can_only_access_their_owned_run(role: Role) -> None:
    owner, other = await _user(role), await _user(role)
    legacy_id: uuid.UUID | None = None
    try:
        async with async_session_factory() as session:
            owned = await create_agent_run(session, _principal(owner.id, role))
            other_run = await create_agent_run(session, _principal(other.id, role))
            legacy = Run(id=uuid.uuid4(), status=RunStatus.QUEUED)
            legacy_id = legacy.id
            session.add(legacy)
            await session.commit()
        async with async_session_factory() as session:
            assert (
                await require_run_access(session, owned.id, _principal(owner.id, role))
            ).id == owned.id
            for hidden in (other_run.id, legacy.id, uuid.uuid4()):
                with pytest.raises(Exception) as caught:
                    await require_run_access(session, hidden, _principal(owner.id, role))
                assert getattr(caught.value, "status_code", None) == 404
    finally:
        await _cleanup([owner, other], [legacy_id] if legacy_id is not None else [])


async def test_admin_can_access_owned_other_and_legacy_runs() -> None:
    admin, other = await _user(Role.ADMIN), await _user(Role.USER)
    legacy_id: uuid.UUID | None = None
    try:
        async with async_session_factory() as session:
            own = await create_agent_run(session, _principal(admin.id, Role.ADMIN))
            theirs = await create_agent_run(session, _principal(other.id, Role.USER))
            legacy = Run(id=uuid.uuid4(), status=RunStatus.QUEUED)
            legacy_id = legacy.id
            session.add(legacy)
            await session.commit()
        async with async_session_factory() as session:
            for run_id in (own.id, theirs.id, legacy.id):
                assert (
                    await require_run_access(session, run_id, _principal(admin.id, Role.ADMIN))
                ).id == run_id
    finally:
        await _cleanup([admin, other], [legacy_id] if legacy_id is not None else [])


async def test_unauthenticated_sse_is_rejected_before_redis_subscription(monkeypatch) -> None:
    subscribed = False

    async def _open(self) -> None:
        nonlocal subscribed
        subscribed = True

    monkeypatch.setattr("opspilot.runs.router.RedisSSEStream.open", _open)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/runs/{uuid.uuid4()}/events")
    assert response.status_code == 401
    assert subscribed is False


async def test_hidden_run_is_rejected_before_redis_subscription(monkeypatch) -> None:
    owner, other = await _user(Role.USER), await _user(Role.USER)
    subscribed = False

    async def _open(self) -> None:
        nonlocal subscribed
        subscribed = True

    async def _principal_override() -> Principal:
        return _principal(other.id, Role.USER)

    monkeypatch.setattr("opspilot.runs.router.RedisSSEStream.open", _open)
    app.dependency_overrides[get_current_principal] = _principal_override
    try:
        async with async_session_factory() as session:
            run = await create_agent_run(session, _principal(owner.id, Role.USER))
            await session.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{run.id}/events")
        assert response.status_code == 404
        assert subscribed is False
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner, other])


async def test_last_event_id_is_fail_closed() -> None:
    run = Run(id=uuid.uuid4(), status=RunStatus.QUEUED, next_seq=3)
    assert await validate_last_event_id(run, None) == 0
    assert await validate_last_event_id(run, "2") == 2
    for invalid in ("-1", "abc", "run:1", "4"):
        with pytest.raises(ValueError):
            await validate_last_event_id(run, invalid)
