"""Task 12 authorization and SSE protocol tests."""

import asyncio
import json
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
from opspilot.runs.models import Run, RunEvent, RunStatus
from opspilot.runs.service import create_agent_run, require_run_access
from opspilot.runs.sse import RedisSSEStream, encode_event, validate_last_event_id


class _FailingPubSub:
    def __init__(self) -> None:
        self.aclose_calls = 0
        self.unsubscribe_calls = 0

    async def subscribe(self, channel: str) -> None:
        raise RuntimeError("redis subscribe failed")

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribe_calls += 1

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _FailingRedis:
    def __init__(self) -> None:
        self.pubsub_instance = _FailingPubSub()
        self.aclose_calls = 0

    def pubsub(self) -> _FailingPubSub:
        return self.pubsub_instance

    async def aclose(self) -> None:
        self.aclose_calls += 1


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
    client_created = False

    async def _open(self) -> None:
        nonlocal subscribed
        subscribed = True

    async def _principal_override() -> Principal:
        return _principal(other.id, Role.USER)

    def _from_url(url: str) -> _FailingRedis:
        nonlocal client_created
        client_created = True
        return _FailingRedis()

    monkeypatch.setattr("opspilot.runs.router.RedisSSEStream.open", _open)
    monkeypatch.setattr("opspilot.runs.router.redis_async.from_url", _from_url)
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
        assert client_created is False
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner, other])


async def test_endpoint_open_failure_returns_503_and_closes_resources_once(monkeypatch) -> None:
    owner = await _user(Role.USER)
    client = _FailingRedis()

    async def _principal_override() -> Principal:
        return _principal(owner.id, Role.USER)

    monkeypatch.setattr("opspilot.runs.router.redis_async.from_url", lambda url: client)
    app.dependency_overrides[get_current_principal] = _principal_override
    try:
        async with async_session_factory() as session:
            run = await create_agent_run(session, _principal(owner.id, Role.USER))
            await session.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get(f"/api/v1/runs/{run.id}/events")
        assert response.status_code == 503
        assert response.json()["detail"] == "run event stream unavailable"
        assert client.pubsub_instance.aclose_calls == 1
        assert client.aclose_calls == 1
    finally:
        app.dependency_overrides.clear()
        await _cleanup([owner])


async def test_last_event_id_is_fail_closed() -> None:
    run = Run(id=uuid.uuid4(), status=RunStatus.QUEUED, next_seq=3)
    assert await validate_last_event_id(run, None) == 0
    assert await validate_last_event_id(run, "2") == 2
    for invalid in ("-1", "abc", "run:1", "4"):
        with pytest.raises(ValueError):
            await validate_last_event_id(run, invalid)


async def test_open_failure_closes_partial_pubsub_and_redis_once_without_reader() -> None:
    client = _FailingRedis()
    stream = RedisSSEStream(uuid.uuid4(), client)  # type: ignore[arg-type]
    before = set(asyncio.all_tasks())
    with pytest.raises(RuntimeError, match="redis subscribe failed"):
        await stream.open()
    assert client.pubsub_instance.aclose_calls == 1
    assert client.aclose_calls == 1
    assert set(asyncio.all_tasks()) == before
    await stream.close()
    assert client.pubsub_instance.aclose_calls == 1
    assert client.aclose_calls == 1


@pytest.mark.parametrize(
    "invalid",
    ["", "bad\revent", "bad\nevent", "bad\r\nevent", "x" * 65, "bad\u0085event"],
)
def test_encode_event_rejects_frame_injection(invalid: str) -> None:
    event = RunEvent(run_id=uuid.uuid4(), seq=1, event_type=invalid, payload={"line": "a\nb"})
    with pytest.raises(ValueError):
        encode_event(event)


class _HintPubSub:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    async def get_message(self, **kwargs):
        if self.values:
            return {"data": json.dumps(self.values.pop(0))}
        await asyncio.sleep(0.01)
        return None

    async def aclose(self) -> None:
        return None


async def test_non_object_redis_hints_are_dropped_without_reconnect() -> None:
    run_id = uuid.uuid4()
    invalid: list[object] = [
        {},
        [],
        "text",
        7,
        None,
        {"run_id": "wrong", "seq": 1},
        {"run_id": str(run_id), "seq": True},
        {"run_id": str(run_id), "seq": -1},
        {"run_id": str(run_id), "seq": "1"},
        {"run_id": str(run_id), "seq": 1, "padding": "x" * 5000},
        {"run_id": str(run_id), "seq": 1},
    ]
    stream = RedisSSEStream(run_id, _FailingRedis())  # type: ignore[arg-type]
    stream.pubsub = _HintPubSub(invalid)
    reconnects = 0

    async def _reconnect() -> None:
        nonlocal reconnects
        reconnects += 1

    stream._reconnect = _reconnect  # type: ignore[method-assign]
    reader = asyncio.create_task(stream._reader())
    try:
        await asyncio.wait_for(stream.queue.join(), timeout=0.1)
        for _ in range(50):
            if stream.queue.qsize() == 1:
                break
            await asyncio.sleep(0.01)
        assert stream.queue.get_nowait() == 1
        stream.queue.task_done()
        assert reconnects == 0
    finally:
        stream.closed = True
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
