"""Transactional Outbox publisher tests against real PostgreSQL and Redis."""

import asyncio
import json
import uuid
from collections.abc import Sequence

import asyncpg
import redis.asyncio
from sqlalchemy import select

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.runs.journal import append_event
from opspilot.runs.models import EventOutbox, Run, RunStatus
from opspilot.runs.outbox import RedisRunEventNotifier, publish_pending_events


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def notify_run_event(self, run_id: str, seq: int) -> None:
        self.calls.append((run_id, seq))


class _FlakyNotifier(_RecordingNotifier):
    """Fails on the first notify, succeeds afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def notify_run_event(self, run_id: str, seq: int) -> None:
        self.calls.append((run_id, seq))
        if not self._failed:
            self._failed = True
            raise RuntimeError("redis unavailable")


async def cleanup_runs(run_ids: Sequence[uuid.UUID]) -> None:
    """Delete runs with a fresh connection; FK CASCADE removes events + outbox."""
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if run_ids:
            await connection.execute(
                "DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids)
            )
    finally:
        await connection.close()


async def seed_run_with_events(count: int) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        run = Run(id=run_id, status=RunStatus.QUEUED)
        session.add(run)
        await session.flush()
        for i in range(count):
            await append_event(session, run_id, "retrieved", {"n": i})
        await session.commit()
    return run_id


async def test_publisher_delivers_identity_and_marks_delivered() -> None:
    run_id = await seed_run_with_events(3)
    notifier = _RecordingNotifier()
    try:
        published = await publish_pending_events(notifier)

        assert published == 3
        assert sorted(notifier.calls) == [(str(run_id), 1), (str(run_id), 2), (str(run_id), 3)]

        idle = _RecordingNotifier()
        assert await publish_pending_events(idle) == 0
        assert idle.calls == []
    finally:
        await cleanup_runs([run_id])


async def test_publisher_retries_after_failed_notify() -> None:
    run_id = await seed_run_with_events(1)
    flaky = _FlakyNotifier()
    try:
        assert await publish_pending_events(flaky) == 0

        async with async_session_factory() as session:
            row = await session.scalar(select(EventOutbox).where(EventOutbox.run_id == run_id))
            assert row is not None
            assert row.delivered_at is None
            assert row.last_error is not None
            assert row.attempts == 1

        assert await publish_pending_events(flaky) == 1
        assert len(flaky.calls) == 2

        async with async_session_factory() as session:
            row = await session.scalar(select(EventOutbox).where(EventOutbox.run_id == run_id))
            assert row is not None
            assert row.delivered_at is not None
    finally:
        await cleanup_runs([run_id])


async def _wait_for_message(pubsub, timeout_seconds: float = 3.0) -> dict | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message is not None:
            return message
    return None


async def test_publisher_notifies_real_redis_channel() -> None:
    run_id = await seed_run_with_events(1)
    client = redis.asyncio.from_url(Settings().redis_url)
    channel = f"run_events:{run_id}"
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        notifier = RedisRunEventNotifier(client)
        assert await publish_pending_events(notifier) == 1

        message = await _wait_for_message(pubsub)
        assert message is not None
        payload = json.loads(message["data"].decode())
        assert payload == {"run_id": str(run_id), "seq": 1}
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()
        await cleanup_runs([run_id])
