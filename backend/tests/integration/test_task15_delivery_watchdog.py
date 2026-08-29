"""Watchdog contracts for broker-acknowledged jobs that were never claimed."""

import asyncio
import uuid
from collections.abc import Sequence

import asyncpg
import pytest

from opspilot.config import Settings


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))


async def _cleanup(run_ids: Sequence[uuid.UUID]) -> None:
    connection = await _connect()
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids))
    finally:
        await connection.close()


async def _create_run_job(*, with_reply: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, message_id = uuid.uuid4(), uuid.uuid4()
    connection = await _connect()
    try:
        await connection.execute(
            "INSERT INTO agent_runs (id, status, next_seq, created_at) "
            "VALUES ($1, 'RUNNING', 0, clock_timestamp())",
            run_id,
        )
        await connection.execute(
            "INSERT INTO run_messages (id, run_id, role, content, created_at) "
            "VALUES ($1, $2, 'USER', 'refund ORD-002', clock_timestamp())",
            message_id,
            run_id,
        )
        if with_reply:
            await connection.execute(
                "INSERT INTO run_messages "
                "(id, run_id, role, content, in_reply_to_message_id, created_at) "
                "VALUES ($1, $2, 'ASSISTANT', 'done', $3, clock_timestamp())",
                uuid.uuid4(),
                run_id,
                message_id,
            )
        await connection.execute(
            "INSERT INTO run_job_outbox "
            "(id, message_id, available_at, attempts, delivered_at, status, created_at) "
            "VALUES ($1, $2, clock_timestamp(), 1, "
            "clock_timestamp() - interval '5 minutes', 'PENDING', clock_timestamp())",
            uuid.uuid4(),
            message_id,
        )
    finally:
        await connection.close()
    return run_id, message_id


async def _create_operation_job(
    kind: str,
    status: str,
    *,
    expected_version: int = 3,
    operation_version: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, operation_id = uuid.uuid4(), uuid.uuid4()
    connection = await _connect()
    try:
        await connection.execute(
            "INSERT INTO agent_runs (id, status, next_seq, created_at) "
            "VALUES ($1, 'RUNNING', 0, clock_timestamp())",
            run_id,
        )
        await connection.execute(
            "INSERT INTO tool_operations "
            "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
            "status, version, created_at, updated_at) VALUES "
            "($1, $2, 'refund_order', '{}'::jsonb, $3, $4, $5, $6, "
            "clock_timestamp(), clock_timestamp())",
            operation_id,
            run_id,
            "a" * 64,
            f"refund:{operation_id}",
            status,
            operation_version,
        )
        await connection.execute(
            "INSERT INTO operation_job_outbox "
            "(id, operation_id, expected_version, kind, available_at, attempts, delivered_at, "
            "created_at) VALUES ($1, $2, $3, $4, clock_timestamp(), 1, "
            "clock_timestamp() - interval '5 minutes', clock_timestamp())",
            uuid.uuid4(),
            operation_id,
            expected_version,
            kind,
        )
    finally:
        await connection.close()
    return run_id, operation_id


@pytest.mark.integration
async def test_stale_delivered_run_job_is_requeued_or_completed_from_reply() -> None:
    from opspilot.jobs.recovery import recover_stale_delivered_jobs

    pending_run, pending_message = await _create_run_job()
    replied_run, replied_message = await _create_run_job(with_reply=True)
    try:
        assert await recover_stale_delivered_jobs(grace_seconds=30) == 2
        connection = await _connect()
        try:
            pending = await connection.fetchrow(
                "SELECT status, delivered_at, available_at > clock_timestamp() AS deferred "
                "FROM run_job_outbox WHERE message_id = $1",
                pending_message,
            )
            replied = await connection.fetchrow(
                "SELECT status, delivered_at, completed_at "
                "FROM run_job_outbox WHERE message_id = $1",
                replied_message,
            )
        finally:
            await connection.close()
        assert pending is not None
        assert (pending["status"], pending["delivered_at"], pending["deferred"]) == (
            "PENDING",
            None,
            True,
        )
        assert replied is not None
        assert replied["status"] == "COMPLETED"
        assert replied["delivered_at"] is not None
        assert replied["completed_at"] is not None
    finally:
        await _cleanup([pending_run, replied_run])


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "status"), [("EXECUTE", "READY"), ("RECONCILE", "OUTCOME_UNKNOWN")]
)
async def test_stale_delivered_operation_job_is_requeued_only_while_fact_matches(
    kind: str, status: str
) -> None:
    from opspilot.jobs.recovery import recover_stale_delivered_jobs

    matching_run, matching_operation = await _create_operation_job(kind, status)
    stale_run, stale_operation = await _create_operation_job(
        kind, status, expected_version=2, operation_version=3
    )
    try:
        assert await recover_stale_delivered_jobs(grace_seconds=30) == 2
        connection = await _connect()
        try:
            matching = await connection.fetchrow(
                "SELECT delivered_at, available_at > clock_timestamp() AS deferred, last_error "
                "FROM operation_job_outbox WHERE operation_id = $1",
                matching_operation,
            )
            stale = await connection.fetchrow(
                "SELECT delivered_at, last_error FROM operation_job_outbox WHERE operation_id = $1",
                stale_operation,
            )
        finally:
            await connection.close()
        assert matching is not None
        assert matching["delivered_at"] is None
        assert matching["deferred"] is True
        assert matching["last_error"] == "delivery acknowledgement expired before claim"
        assert stale is not None
        assert stale["delivered_at"] is not None
        assert stale["last_error"] == "stale operation job intent"
        assert await recover_stale_delivered_jobs(grace_seconds=30) == 0
    finally:
        await _cleanup([matching_run, stale_run])


@pytest.mark.integration
async def test_delivery_watchdog_is_concurrent_safe_and_does_not_hot_loop() -> None:
    from opspilot.jobs.recovery import recover_stale_delivered_jobs

    run_id, message_id = await _create_run_job()
    try:
        results = await asyncio.gather(
            recover_stale_delivered_jobs(batch_size=50, grace_seconds=30),
            recover_stale_delivered_jobs(batch_size=50, grace_seconds=30),
        )
        assert sum(results) == 1
        assert await recover_stale_delivered_jobs(batch_size=50, grace_seconds=30) == 0
        connection = await _connect()
        try:
            row = await connection.fetchrow(
                "SELECT delivered_at, attempts FROM run_job_outbox WHERE message_id = $1",
                message_id,
            )
        finally:
            await connection.close()
        assert row is not None
        assert row["delivered_at"] is None
        assert row["attempts"] == 1
    finally:
        await _cleanup([run_id])
