"""Real PostgreSQL and Redis recovery tests for Task 12 SSE."""

import asyncio
import json
import uuid

import asyncpg
import redis.asyncio as redis_async

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.runs.journal import append_event
from opspilot.runs.models import Run, RunStatus
from opspilot.runs.sse import RedisSSEStream


async def _run_with_events(count: int) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.flush()
        for marker in range(1, count + 1):
            await append_event(session, run_id, "step", {"marker": marker})
        await session.commit()
    return run_id


async def _cleanup(run_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
    finally:
        await connection.close()


def _seq(frame: str) -> int:
    return int(frame.splitlines()[0].removeprefix("id: "))


async def test_history_resume_and_redis_payload_is_only_an_untrusted_hint() -> None:
    run_id = await _run_with_events(4)
    client = redis_async.from_url(Settings().redis_url)
    stream = RedisSSEStream(run_id, client)
    await stream.open()
    generator = stream.events(2)
    try:
        frames = [await anext(generator), await anext(generator)]
        assert [_seq(frame) for frame in frames] == [3, 4]
        assert all("event: step" in frame and 'data: {"marker":' in frame for frame in frames)
    finally:
        await generator.aclose()
        await _cleanup(run_id)


async def test_out_of_order_duplicate_and_missing_notifications_are_recovered_from_pg() -> None:
    run_id = await _run_with_events(0)
    publisher = redis_async.from_url(Settings().redis_url)
    stream = RedisSSEStream(run_id, redis_async.from_url(Settings().redis_url))
    await stream.open()
    generator = stream.events(0)
    try:
        async with async_session_factory() as session:
            for marker in range(1, 4):
                await append_event(session, run_id, "step", {"marker": marker})
            await session.commit()
        channel = f"run_events:{run_id}"
        for seq in (3, 1, 3):
            await publisher.publish(channel, json.dumps({"run_id": str(run_id), "seq": seq}))
        frames = [await anext(generator) for _ in range(3)]
        assert [_seq(frame) for frame in frames] == [1, 2, 3]
    finally:
        await generator.aclose()
        await publisher.aclose()
        await _cleanup(run_id)


async def test_fake_notification_before_commit_never_outputs_an_event() -> None:
    run_id = await _run_with_events(0)
    publisher = redis_async.from_url(Settings().redis_url)
    stream = RedisSSEStream(run_id, redis_async.from_url(Settings().redis_url))
    await stream.open()
    generator = stream.events(0)
    try:
        await publisher.publish(
            f"run_events:{run_id}", json.dumps({"run_id": str(run_id), "seq": 1})
        )
        try:
            await asyncio.wait_for(anext(generator), timeout=0.7)
            raise AssertionError("Redis hint without a committed PG fact was emitted")
        except TimeoutError:
            pass
    finally:
        await generator.aclose()
        await publisher.aclose()
        await _cleanup(run_id)


async def test_pg_poll_recovers_after_pubsub_disconnect_and_cleanup_has_no_reader_task() -> None:
    run_id = await _run_with_events(0)
    stream = RedisSSEStream(run_id, redis_async.from_url(Settings().redis_url))
    await stream.open()
    generator = stream.events(0)
    pending = asyncio.create_task(anext(generator), name="sse-consumer-test")
    try:
        await asyncio.sleep(0.1)
        await stream.pubsub.aclose()
        async with async_session_factory() as session:
            await append_event(session, run_id, "step", {"marker": 1})
            await session.commit()
        assert _seq(await asyncio.wait_for(pending, timeout=2)) == 1
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await generator.aclose()
        await asyncio.sleep(0)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__.endswith("._reader")
        ]
        await _cleanup(run_id)


async def test_slow_client_notification_buffer_is_bounded() -> None:
    run_id = await _run_with_events(1)
    publisher = redis_async.from_url(Settings().redis_url)
    stream = RedisSSEStream(run_id, redis_async.from_url(Settings().redis_url))
    await stream.open()
    generator = stream.events(0)
    try:
        assert _seq(await anext(generator)) == 1
        channel = f"run_events:{run_id}"
        for seq in range(1, 301):
            await publisher.publish(channel, json.dumps({"run_id": str(run_id), "seq": seq}))
        await asyncio.sleep(0.3)
        assert stream.queue.qsize() <= 128
        assert stream.queue.maxsize == 128
    finally:
        await generator.aclose()
        await publisher.aclose()
        await _cleanup(run_id)


async def test_heartbeat_has_no_event_id_and_does_not_advance_business_seq(monkeypatch) -> None:
    from opspilot.runs import sse as sse_module

    run_id = await _run_with_events(0)
    monkeypatch.setattr(sse_module, "HEARTBEAT_SECONDS", 0.05)
    stream = RedisSSEStream(run_id, redis_async.from_url(Settings().redis_url))
    await stream.open()
    generator = stream.events(0)
    try:
        heartbeat = await asyncio.wait_for(anext(generator), timeout=1)
        assert heartbeat == ": heartbeat\n\n"
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            assert (
                await connection.fetchval("SELECT next_seq FROM agent_runs WHERE id = $1", run_id)
                == 0
            )
        finally:
            await connection.close()
    finally:
        await generator.aclose()
        await _cleanup(run_id)
