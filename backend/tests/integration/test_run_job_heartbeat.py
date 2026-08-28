"""PostgreSQL contracts for the durable run-message claim heartbeat."""

import asyncio
import uuid

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.jobs.models import RunJobOutbox
from opspilot.jobs.recovery import recover_expired_run_jobs
from opspilot.jobs.tasks import RunMessageResult, process_run_message
from opspilot.runs.models import Run, RunMessage, RunStatus


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))


async def _create_job() -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.RUNNING))
        session.add(RunMessage(id=message_id, run_id=run_id, role="USER", content="hello"))
        await session.flush()
        session.add(RunJobOutbox(message_id=message_id))
        await session.commit()
    return run_id, message_id


async def _cleanup(run_id: uuid.UUID) -> None:
    connection = await _connect()
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
    finally:
        await connection.close()


async def test_heartbeat_keeps_long_processor_claim_from_recovery_and_duplicate_worker() -> None:
    run_id, message_id = await _create_job()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def processor(message: RunMessage) -> RunMessageResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return RunMessageResult(content="done", citation_snapshots=[])

    first = asyncio.create_task(
        process_run_message(
            {"run_message_processor": processor, "run_job_lease_seconds": 3}, str(message_id)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(4.2)
        assert await recover_expired_run_jobs(batch_size=1) == 0
        await process_run_message(
            {"run_message_processor": processor, "run_job_lease_seconds": 3}, str(message_id)
        )
        assert calls == 1
        release.set()
        await asyncio.wait_for(first, timeout=5)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await _cleanup(run_id)


async def test_heartbeat_loss_fences_late_processor_result() -> None:
    from opspilot.jobs.run_executor import RunJobLeaseConflictError

    run_id, message_id = await _create_job()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def processor(message: RunMessage) -> RunMessageResult:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            release.set()
        return RunMessageResult(content="must-not-write", citation_snapshots=[])

    task = asyncio.create_task(
        process_run_message(
            {"run_message_processor": processor, "run_job_lease_seconds": 3}, str(message_id)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        connection = await _connect()
        try:
            await connection.execute(
                "UPDATE run_job_outbox SET claim_token = $2 WHERE message_id = $1",
                message_id,
                "stolen-token",
            )
        finally:
            await connection.close()
        with pytest.raises(RunJobLeaseConflictError):
            await asyncio.wait_for(task, timeout=5)
        connection = await _connect()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM run_messages WHERE in_reply_to_message_id = $1",
                    message_id,
                )
                == 0
            )
        finally:
            await connection.close()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await _cleanup(run_id)


async def test_cancelled_worker_collects_processor_and_heartbeat_tasks() -> None:
    from opspilot.jobs.run_executor import detached_run_processor_task_count

    run_id, message_id = await _create_job()
    entered = asyncio.Event()

    async def processor(message: RunMessage) -> RunMessageResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        process_run_message(
            {"run_message_processor": processor, "run_job_lease_seconds": 3}, str(message_id)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert detached_run_processor_task_count() == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _cleanup(run_id)


async def test_lease_loss_supervises_processor_that_ignores_cancellation() -> None:
    from opspilot.jobs.run_executor import (
        RunJobLeaseConflictError,
        detached_run_processor_task_count,
        drain_detached_run_processor_tasks,
    )

    run_id, message_id = await _create_job()
    entered = asyncio.Event()
    teardown = asyncio.Event()

    async def processor(message: RunMessage) -> RunMessageResult:
        entered.set()
        while not teardown.is_set():
            try:
                await teardown.wait()
            except asyncio.CancelledError:
                continue
        return RunMessageResult(content="must-not-write", citation_snapshots=[])

    task = asyncio.create_task(
        process_run_message(
            {"run_message_processor": processor, "run_job_lease_seconds": 3}, str(message_id)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        connection = await _connect()
        try:
            await connection.execute(
                "UPDATE run_job_outbox SET claim_token = 'stolen-token' WHERE message_id = $1",
                message_id,
            )
        finally:
            await connection.close()
        with pytest.raises(RunJobLeaseConflictError):
            await asyncio.wait_for(task, timeout=6)
        assert detached_run_processor_task_count() == 1
    finally:
        teardown.set()
        await asyncio.wait_for(drain_detached_run_processor_tasks(), timeout=5)
        assert detached_run_processor_task_count() == 0
        await asyncio.gather(task, return_exceptions=True)
        await _cleanup(run_id)
