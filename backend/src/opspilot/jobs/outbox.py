import random
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db import async_session_factory
from opspilot.jobs.models import OperationJobOutbox, RunJobOutbox
from opspilot.jobs.queues import WORKER_QUEUE


class OperationQueue(Protocol):
    async def enqueue_operation(
        self, operation_id: object, expected_version: int, kind: str
    ) -> None: ...


class ArqOperationQueue:
    def __init__(self, redis: object) -> None:
        self.redis = redis

    async def enqueue_operation(
        self, operation_id: object, expected_version: int, kind: str
    ) -> None:
        await self.redis.enqueue_job(  # type: ignore[attr-defined]
            "process_operation_job",
            str(operation_id),
            expected_version,
            kind,
            _queue_name=WORKER_QUEUE,
        )


class RunQueue(Protocol):
    async def enqueue_run_message(self, message_id: object) -> None: ...


class ArqRunQueue:
    def __init__(self, redis: object) -> None:
        self.redis = redis

    async def enqueue_run_message(self, message_id: object) -> None:
        await self.redis.enqueue_job(  # type: ignore[attr-defined]
            "process_run_message", str(message_id), _queue_name=WORKER_QUEUE
        )


async def publish_pending_run_jobs(queue: RunQueue, batch_size: int = 100) -> int:
    delivered = 0
    attempted_ids: set[object] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(RunJobOutbox)
                .where(
                    RunJobOutbox.delivered_at.is_(None),
                    RunJobOutbox.available_at <= func.clock_timestamp(),
                    RunJobOutbox.id.not_in(attempted_ids),
                )
                .order_by(RunJobOutbox.available_at, RunJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            attempted_ids.add(row.id)
            row.attempts += 1
            try:
                await queue.enqueue_run_message(row.message_id)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: enqueue failed"[:500]
                row.available_at = await _retry_at(session, row.attempts)
                await session.commit()
                continue
            row.delivered_at = await session.scalar(select(func.clock_timestamp()))
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered


async def publish_pending_operation_jobs(queue: OperationQueue, batch_size: int = 100) -> int:
    delivered = 0
    attempted_ids: set[object] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(OperationJobOutbox)
                .where(
                    OperationJobOutbox.delivered_at.is_(None),
                    OperationJobOutbox.available_at <= func.clock_timestamp(),
                    OperationJobOutbox.id.not_in(attempted_ids),
                )
                .order_by(OperationJobOutbox.available_at, OperationJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            attempted_ids.add(row.id)
            row.attempts += 1
            try:
                await queue.enqueue_operation(row.operation_id, row.expected_version, row.kind)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: enqueue failed"[:500]
                row.available_at = await _retry_at(session, row.attempts)
                await session.commit()
                continue
            row.delivered_at = await session.scalar(select(func.clock_timestamp()))
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered


async def _retry_at(session: AsyncSession, attempts: int) -> datetime:
    """Return a bounded exponential retry instant based on PostgreSQL time."""
    base_seconds = min(60.0, float(2 ** min(max(attempts - 1, 0), 5)))
    jitter_seconds = random.uniform(0.0, min(1.0, base_seconds * 0.25))
    result = await session.execute(select(func.clock_timestamp()))
    now: datetime = result.scalar_one()
    return now + timedelta(seconds=base_seconds + jitter_seconds)
