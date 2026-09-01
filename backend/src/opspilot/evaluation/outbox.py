"""Recoverable PostgreSQL-to-ARQ evaluation job publication."""

import random
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db import async_session_factory
from opspilot.evaluation.models import (
    EvaluationExecutionStatus,
    EvaluationJobOutbox,
    EvaluationTestExecution,
)
from opspilot.jobs.queues import WORKER_QUEUE


class EvaluationQueue(Protocol):
    async def enqueue_evaluation(self, execution_id: object) -> None: ...


class ArqEvaluationQueue:
    def __init__(self, redis: object) -> None:
        self.redis = redis

    async def enqueue_evaluation(self, execution_id: object) -> None:
        await self.redis.enqueue_job(  # type: ignore[attr-defined]
            "process_evaluation_execution", str(execution_id), _queue_name=WORKER_QUEUE
        )


async def _retry_at(session: AsyncSession, attempts: int) -> datetime:
    base = min(60.0, float(2 ** min(max(attempts - 1, 0), 5)))
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    return now + timedelta(seconds=base + random.uniform(0.0, min(1.0, base * 0.25)))


async def publish_pending_evaluation_jobs(queue: EvaluationQueue, batch_size: int = 100) -> int:
    delivered = 0
    attempted: set[object] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(EvaluationJobOutbox)
                .where(
                    EvaluationJobOutbox.delivered_at.is_(None),
                    EvaluationJobOutbox.available_at <= func.clock_timestamp(),
                    EvaluationJobOutbox.id.not_in(attempted),
                )
                .order_by(EvaluationJobOutbox.available_at, EvaluationJobOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            attempted.add(row.id)
            row.attempts += 1
            try:
                await queue.enqueue_evaluation(row.execution_id)
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


async def recover_stale_evaluation_delivery(
    *, grace_seconds: int = 120, batch_size: int = 100
) -> int:
    """Re-arm delivered jobs which never acquired a durable execution lease."""
    recovered = 0
    attempted: set[object] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(EvaluationJobOutbox)
                .join(
                    EvaluationTestExecution,
                    EvaluationTestExecution.id == EvaluationJobOutbox.execution_id,
                )
                .where(
                    EvaluationJobOutbox.delivered_at.is_not(None),
                    EvaluationJobOutbox.delivered_at
                    < func.clock_timestamp() - timedelta(seconds=grace_seconds),
                    EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
                    EvaluationTestExecution.claim_token.is_(None),
                    EvaluationJobOutbox.id.not_in(attempted),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            attempted.add(row.id)
            row.delivered_at = None
            row.available_at = await _retry_at(session, max(row.attempts, 1))
            row.last_error = "delivery acknowledgement expired before claim"
            await session.commit()
            recovered += 1
    return recovered


async def recover_expired_evaluation_claims(*, batch_size: int = 100) -> int:
    """Re-arm expired claims without creating another frozen-test execution."""
    recovered = 0
    attempted: set[object] = set()
    for _ in range(batch_size):
        async with async_session_factory() as session:
            execution = await session.scalar(
                select(EvaluationTestExecution)
                .where(
                    EvaluationTestExecution.status == EvaluationExecutionStatus.RUNNING,
                    EvaluationTestExecution.claim_token.is_not(None),
                    EvaluationTestExecution.lease_expires_at < func.clock_timestamp(),
                    EvaluationTestExecution.id.not_in(attempted),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if execution is None:
                break
            attempted.add(execution.id)
            row = await session.scalar(
                select(EvaluationJobOutbox)
                .where(EvaluationJobOutbox.execution_id == execution.id)
                .with_for_update()
            )
            if row is None:
                row = EvaluationJobOutbox(execution_id=execution.id)
                session.add(row)
            execution.claim_token = None
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.version += 1
            row.delivered_at = None
            row.available_at = await _retry_at(session, max(row.attempts, 1))
            row.last_error = "worker lease expired; same execution scheduled for recovery"
            await session.commit()
            recovered += 1
    return recovered
