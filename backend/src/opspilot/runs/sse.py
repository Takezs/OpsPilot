"""Gap-free SSE streaming backed by PostgreSQL facts and Redis hints."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import redis.asyncio as redis_async
from sqlalchemy import select

from opspilot.db import async_session_factory
from opspilot.runs.models import Run, RunEvent

BUFFER_LIMIT = 128
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 15.0


def encode_event(event: RunEvent) -> str:
    data = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {data}\n\n"


class RedisSSEStream:
    def __init__(self, run_id: uuid.UUID, redis: redis_async.Redis) -> None:
        self.run_id = run_id
        self.redis = redis
        self.channel = f"run_events:{run_id}"
        self.pubsub: Any = None
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=BUFFER_LIMIT)
        self.closed = False

    async def open(self) -> None:
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(self.channel)

    async def _reconnect(self) -> None:
        with suppress(Exception):
            await self.pubsub.aclose()
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(self.channel)

    async def _reader(self) -> None:
        while not self.closed:
            try:
                message = await self.pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=POLL_SECONDS
                )
                if not message:
                    await asyncio.sleep(0)
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="strict")
                value = json.loads(raw)
                if value.get("run_id") != str(self.run_id):
                    continue
                seq = value.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                    continue
                with suppress(asyncio.QueueFull):
                    self.queue.put_nowait(seq)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.closed:
                    return
                await asyncio.sleep(POLL_SECONDS)
                with suppress(Exception):
                    await self._reconnect()

    async def events(self, after_seq: int) -> AsyncIterator[str]:
        next_expected = after_seq + 1
        reader = asyncio.create_task(self._reader())
        last_activity = asyncio.get_running_loop().time()
        try:
            while True:
                async with async_session_factory() as session:
                    facts = list(
                        await session.scalars(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == self.run_id,
                                RunEvent.seq >= next_expected,
                            )
                            .order_by(RunEvent.seq)
                            .limit(BUFFER_LIMIT)
                        )
                    )
                for fact in facts:
                    if fact.seq != next_expected:
                        break
                    yield encode_event(fact)
                    next_expected += 1
                    last_activity = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(self.queue.get(), timeout=POLL_SECONDS)
                except TimeoutError:
                    pass
                now = asyncio.get_running_loop().time()
                if now - last_activity >= HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_activity = now
        finally:
            self.closed = True
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
            if self.pubsub is not None:
                with suppress(Exception):
                    await self.pubsub.unsubscribe(self.channel)
                with suppress(Exception):
                    await self.pubsub.aclose()
            with suppress(Exception):
                await self.redis.aclose()


async def validate_last_event_id(run: Run, value: str | None) -> int:
    if value is None or value == "":
        return 0
    if not value.isascii() or not value.isdecimal():
        raise ValueError("Last-Event-ID must be a non-negative decimal integer")
    parsed = int(value)
    if parsed > run.next_seq:
        raise ValueError("Last-Event-ID exceeds the run watermark")
    return parsed
