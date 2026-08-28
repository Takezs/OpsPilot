from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select

from opspilot.db import async_session_factory
from opspilot.jobs.models import OperationJobOutbox, RunJobOutbox


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
            "process_operation_job", str(operation_id), expected_version, kind
        )


class RunQueue(Protocol):
    async def enqueue_run_message(self, message_id: object) -> None: ...


class ArqRunQueue:
    def __init__(self, redis: object) -> None:
        self.redis = redis

    async def enqueue_run_message(self, message_id: object) -> None:
        await self.redis.enqueue_job("process_run_message", str(message_id))  # type: ignore[attr-defined]


async def publish_pending_run_jobs(queue: RunQueue, batch_size: int = 100) -> int:
    delivered = 0
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(RunJobOutbox)
                .where(
                    RunJobOutbox.delivered_at.is_(None),
                    RunJobOutbox.available_at <= datetime.now(UTC),
                )
                .order_by(RunJobOutbox.available_at, RunJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            row.attempts += 1
            try:
                await queue.enqueue_run_message(row.message_id)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: enqueue failed"[:500]
                await session.commit()
                continue
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered


async def publish_pending_operation_jobs(queue: OperationQueue, batch_size: int = 100) -> int:
    delivered = 0
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(OperationJobOutbox)
                .where(
                    OperationJobOutbox.delivered_at.is_(None),
                    OperationJobOutbox.available_at <= datetime.now(UTC),
                )
                .order_by(OperationJobOutbox.available_at, OperationJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            row.attempts += 1
            try:
                await queue.enqueue_operation(row.operation_id, row.expected_version, row.kind)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: enqueue failed"[:500]
                await session.commit()
                continue
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered
