"""Lease heartbeat and task supervision for durable run-message processing."""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.jobs.models import RunJobOutbox, RunJobStatus

SessionFactory = Callable[[], AsyncSession]
_CANCEL_GRACE_SECONDS = 1.0
_DETACHED_RUN_PROCESSOR_TASKS: set[asyncio.Task[Any]] = set()


class RunJobLeaseConflictError(RuntimeError):
    """The run-message worker no longer owns its durable claim."""


@dataclass(frozen=True)
class RunJobFence:
    """Transaction-scoped capability for durable writes by an Agent job."""

    message_id: uuid.UUID
    owner: str
    token: str

    async def lock(self, session: AsyncSession) -> None:
        owned = await session.scalar(
            select(RunJobOutbox.id)
            .where(
                RunJobOutbox.message_id == self.message_id,
                RunJobOutbox.status == RunJobStatus.RUNNING,
                RunJobOutbox.claim_token == self.token,
                RunJobOutbox.lease_owner == self.owner,
                RunJobOutbox.lease_expires_at >= func.clock_timestamp(),
            )
            .with_for_update()
        )
        if owned is None:
            raise RunJobLeaseConflictError(
                f"run job durable write denied for message {self.message_id}"
            )


def _consume(task: asyncio.Task[Any]) -> None:
    if task.cancelled() or not task.done():
        return
    try:
        task.result()
    except BaseException:
        pass


def _forget(task: asyncio.Task[Any]) -> None:
    _consume(task)
    _DETACHED_RUN_PROCESSOR_TASKS.discard(task)


def _detach(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume(task)
        return
    _DETACHED_RUN_PROCESSOR_TASKS.add(task)
    task.add_done_callback(_forget)


def detached_run_processor_task_count() -> int:
    return len(_DETACHED_RUN_PROCESSOR_TASKS)


async def drain_detached_run_processor_tasks() -> None:
    tasks = tuple(_DETACHED_RUN_PROCESSOR_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _bounded_wait(task: asyncio.Task[Any], *, detach_on_timeout: bool = False) -> None:
    if task.done():
        _consume(task)
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS)
    except TimeoutError:
        if detach_on_timeout:
            _detach(task)
    except asyncio.CancelledError:
        if task.done():
            _consume(task)
            return
        if detach_on_timeout and not task.done():
            _detach(task)
        raise
    else:
        _consume(task)


async def renew_run_job_lease(
    session_factory: SessionFactory,
    message_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    lease_seconds: int,
) -> None:
    """Renew from PostgreSQL time in an independent transaction without changing token."""
    async with session_factory() as session:
        changed = await session.execute(
            update(RunJobOutbox)
            .where(
                RunJobOutbox.message_id == message_id,
                RunJobOutbox.status == RunJobStatus.RUNNING,
                RunJobOutbox.claim_token == token,
                RunJobOutbox.lease_owner == owner,
                RunJobOutbox.lease_expires_at >= func.clock_timestamp(),
            )
            .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds))
        )
        if (changed.rowcount or 0) != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise RunJobLeaseConflictError(f"run job lease lost for message {message_id}")
        await session.commit()


async def _heartbeat(
    session_factory: SessionFactory,
    message_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    lease_seconds: int,
    stop: asyncio.Event,
) -> None:
    interval = max(0.05, lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        await renew_run_job_lease(
            session_factory,
            message_id,
            owner=owner,
            token=token,
            lease_seconds=lease_seconds,
        )


async def run_processor_under_lease[T](
    session_factory: SessionFactory,
    message_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    lease_seconds: int,
    invoke: Callable[[], Coroutine[Any, Any, T]],
) -> T:
    """Run one processor concurrently with its heartbeat and fence lease loss."""
    processor_task: asyncio.Task[T] = asyncio.create_task(invoke())
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat(
            session_factory,
            message_id,
            owner=owner,
            token=token,
            lease_seconds=lease_seconds,
            stop=stop,
        )
    )
    try:
        while True:
            done, _ = await asyncio.wait(
                {processor_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat_task in done:
                failure = heartbeat_task.exception()
                processor_task.cancel()
                if failure is not None:
                    raise failure
                raise RunJobLeaseConflictError(
                    f"run job heartbeat stopped unexpectedly for message {message_id}"
                )
            if processor_task in done:
                result = processor_task.result()
                break
    finally:
        stop.set()
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        await _bounded_wait(heartbeat_task)
        if not processor_task.done():
            processor_task.cancel()
            await _bounded_wait(processor_task, detach_on_timeout=True)
        else:
            _consume(processor_task)
    return result
