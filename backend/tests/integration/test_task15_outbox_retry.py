"""Task 15 job publisher retry scheduling against real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.config import Settings


class _FailingThenSuccessfulOperationQueue:
    def __init__(self, failing_operation_id: uuid.UUID) -> None:
        self.failing_operation_id = failing_operation_id
        self.fail = True
        self.calls: list[uuid.UUID] = []

    async def enqueue_operation(
        self, operation_id: uuid.UUID, expected_version: int, kind: str
    ) -> None:
        self.calls.append(operation_id)
        if self.fail and operation_id == self.failing_operation_id:
            raise ConnectionError("broker unavailable")


class _FailingRunQueue:
    def __init__(self) -> None:
        self.calls = 0

    async def enqueue_run_message(self, message_id: uuid.UUID) -> None:
        self.calls += 1
        raise ConnectionError("broker unavailable")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_enqueue_is_deferred_once_per_publish_pass_and_can_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker outage must not retry one poison row in a tight batch loop."""
    from opspilot.jobs import outbox

    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    run_ids = [uuid.uuid4(), uuid.uuid4()]
    operation_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        for run_id, operation_id in zip(run_ids, operation_ids, strict=True):
            await connection.execute(
                "INSERT INTO agent_runs (id, status, next_seq, created_at) "
                "VALUES ($1, 'RUNNING', 0, clock_timestamp())",
                run_id,
            )
            await connection.execute(
                "INSERT INTO tool_operations "
                "(id, run_id, tool_name, normalized_arguments, arguments_hash, "
                "idempotency_key, status, version, created_at, updated_at) "
                "VALUES ($1, $2, 'refund_order', '{}'::jsonb, $3, $4, "
                "'READY', 1, clock_timestamp(), clock_timestamp())",
                operation_id,
                run_id,
                "a" * 64,
                f"refund:{operation_id}",
            )
            await connection.execute(
                "INSERT INTO operation_job_outbox "
                "(id, operation_id, expected_version, kind, available_at, attempts, created_at) "
                "VALUES ($1, $2, 1, 'EXECUTE', clock_timestamp(), 0, clock_timestamp())",
                uuid.uuid4(),
                operation_id,
            )
        pg_before = await connection.fetchval("SELECT clock_timestamp()")

        class SkewedDateTime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2099, 1, 1, tzinfo=UTC)

        monkeypatch.setattr(outbox, "datetime", SkewedDateTime, raising=False)
        queue = _FailingThenSuccessfulOperationQueue(operation_ids[0])
        delivered = await outbox.publish_pending_operation_jobs(queue, batch_size=100)

        assert delivered == 1
        assert queue.calls.count(operation_ids[0]) == 1
        assert queue.calls.count(operation_ids[1]) == 1
        failed = await connection.fetchrow(
            "SELECT attempts, available_at, delivered_at, last_error "
            "FROM operation_job_outbox WHERE operation_id = $1",
            operation_ids[0],
        )
        pg_after = await connection.fetchval("SELECT clock_timestamp()")
        assert failed is not None
        assert failed["attempts"] == 1
        assert failed["delivered_at"] is None
        assert failed["last_error"] == "ConnectionError: enqueue failed"
        assert pg_before < failed["available_at"] <= pg_after + timedelta(minutes=2)

        queue.fail = False
        await connection.execute(
            "UPDATE operation_job_outbox "
            "SET available_at = clock_timestamp() - interval '1 second' "
            "WHERE operation_id = $1",
            operation_ids[0],
        )
        assert await outbox.publish_pending_operation_jobs(queue, batch_size=100) == 1
        recovered = await connection.fetchrow(
            "SELECT attempts, delivered_at FROM operation_job_outbox WHERE operation_id = $1",
            operation_ids[0],
        )
        assert recovered is not None
        assert recovered["attempts"] == 2
        assert recovered["delivered_at"] is not None
        assert recovered["delivered_at"] < datetime(2099, 1, 1, tzinfo=UTC)
    finally:
        await connection.execute("DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", run_ids)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_run_enqueue_is_also_deferred_once_per_publish_pass() -> None:
    from opspilot.jobs.outbox import publish_pending_run_jobs

    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    run_id, message_id = uuid.uuid4(), uuid.uuid4()
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
        await connection.execute(
            "INSERT INTO run_job_outbox "
            "(id, message_id, available_at, attempts, created_at) "
            "VALUES ($1, $2, clock_timestamp(), 0, clock_timestamp())",
            uuid.uuid4(),
            message_id,
        )
        queue = _FailingRunQueue()
        assert await publish_pending_run_jobs(queue, batch_size=100) == 0
        assert queue.calls == 1
        row = await connection.fetchrow(
            "SELECT attempts, available_at > clock_timestamp() AS deferred "
            "FROM run_job_outbox WHERE message_id = $1",
            message_id,
        )
        assert row is not None
        assert row["attempts"] == 1
        assert row["deferred"] is True
    finally:
        await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
        await connection.close()
