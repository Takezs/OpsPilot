"""Transactional Outbox publisher that notifies event identity over Redis."""

import json
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select

from opspilot.db import async_session_factory
from opspilot.runs.models import EventOutbox


class RunEventNotifier(Protocol):
    async def notify_run_event(self, run_id: str, seq: int) -> None: ...


class RedisPublisher(Protocol):
    async def publish(self, channel: str, message: str) -> int: ...


class RedisRunEventNotifier:
    """Publishes only the event identity ``(run_id, seq)`` to a per-run channel.

    The event payload stays in PostgreSQL ``run_events``; consumers fetch it by
    identity, so Redis never carries mutable business data.
    """

    def __init__(self, redis: RedisPublisher, channel_prefix: str = "run_events:") -> None:
        self._redis = redis
        self.channel_prefix = channel_prefix

    async def notify_run_event(self, run_id: str, seq: int) -> None:
        payload = json.dumps({"run_id": run_id, "seq": seq})
        await self._redis.publish(f"{self.channel_prefix}{run_id}", payload)


async def publish_pending_events(notifier: RunEventNotifier, batch_size: int = 100) -> int:
    """Publish undelivered outbox rows, marking them delivered on success.

    Rows are claimed with ``FOR UPDATE SKIP LOCKED`` so concurrent publishers
    never process the same row twice. Only ``{run_id, seq}`` identity is
    published; the event payload lives in ``run_events``. A failed notify keeps
    the row undelivered (with a recorded error) so it is retried on the next
    pass — repeat delivery is safe because consumers deduplicate by
    ``(run_id, seq)``.
    """
    delivered = 0
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(EventOutbox)
                .where(EventOutbox.delivered_at.is_(None))
                .order_by(EventOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            row.attempts += 1
            try:
                await notifier.notify_run_event(str(row.run_id), row.seq)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: publish failed"[:500]
                await session.commit()
                break
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered
