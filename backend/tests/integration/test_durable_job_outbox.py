"""Task 15 durable run/operation job intent integration contract.

These tests intentionally use PostgreSQL as the fact source.  Redis/ARQ delivery is
at-least-once: an enqueue can succeed before ``delivered_at`` commits, therefore the
database identity ``(operation_id, expected_version, kind)`` and worker fencing—not
the broker—provide idempotency.
"""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.approvals.models import ApprovalRequest, ApprovalStatus
from opspilot.approvals.service import ApprovalDecision, decide_approval
from opspilot.auth.models import Role
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.claim import claim_operation, mark_unknown
from opspilot.execution.models import Operation, OperationStatus
from opspilot.jobs.models import RunJobOutbox
from opspilot.runs.models import Run, RunMessage, RunStatus

_NOW = datetime(2026, 8, 28, tzinfo=UTC)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))


async def _create_operation(
    *, status: OperationStatus, version: int = 1
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.RUNNING))
        await session.flush()
        session.add(
            Operation(
                id=operation_id,
                run_id=run_id,
                tool_name="refund_order",
                normalized_arguments={"order_id": "ORD-002", "amount": 350},
                arguments_hash="a" * 64,
                idempotency_key="refund:ORD-002",
                status=status,
                version=version,
                policy_decision="REQUIRE_APPROVAL",
            )
        )
        await session.commit()
    return run_id, operation_id


async def _cleanup_runs(run_ids: Sequence[uuid.UUID]) -> None:
    connection = await _connect()
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids))
    finally:
        await connection.close()


async def _operation_intents(operation_id: uuid.UUID) -> list[asyncpg.Record]:
    connection = await _connect()
    try:
        return list(
            await connection.fetch(
                "SELECT operation_id, expected_version, kind, delivered_at, attempts "
                "FROM operation_job_outbox WHERE operation_id = $1 "
                "ORDER BY expected_version, kind",
                operation_id,
            )
        )
    finally:
        await connection.close()


async def test_operation_intent_identity_allows_new_version_but_rejects_duplicate() -> None:
    """A replay of one transition is one intent; a later version is a new intent."""
    run_id, operation_id = await _create_operation(status=OperationStatus.READY)
    connection = await _connect()
    try:
        await connection.execute(
            "INSERT INTO operation_job_outbox "
            "(id, operation_id, expected_version, kind, available_at, attempts, created_at) "
            "VALUES ($1, $2, 2, 'EXECUTE', clock_timestamp(), 0, clock_timestamp())",
            uuid.uuid4(),
            operation_id,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO operation_job_outbox "
                "(id, operation_id, expected_version, kind, available_at, attempts, created_at) "
                "VALUES ($1, $2, 2, 'EXECUTE', clock_timestamp(), 0, clock_timestamp())",
                uuid.uuid4(),
                operation_id,
            )
        await connection.execute(
            "INSERT INTO operation_job_outbox "
            "(id, operation_id, expected_version, kind, available_at, attempts, created_at) "
            "VALUES ($1, $2, 3, 'EXECUTE', clock_timestamp(), 0, clock_timestamp())",
            uuid.uuid4(),
            operation_id,
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_job_outbox WHERE operation_id = $1",
                operation_id,
            )
            == 2
        )
    finally:
        await connection.close()
        await _cleanup_runs([run_id])


async def test_approval_ready_transition_creates_execute_intent_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, operation_id = await _create_operation(status=OperationStatus.WAITING_APPROVAL)
    approval_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            ApprovalRequest(
                id=approval_id,
                operation_id=operation_id,
                arguments_hash="a" * 64,
                operation_version=1,
                status=ApprovalStatus.PENDING,
                expires_at=_NOW + timedelta(days=365),
            )
        )
        await session.commit()

    try:
        async with async_session_factory() as session:
            async with session.begin():
                result = await decide_approval(
                    session,
                    approval_id,
                    ApprovalDecision.APPROVE,
                    decided_by="reviewer-1",
                    role=Role.REVIEWER,
                )
        assert result.operation.status is OperationStatus.READY
        intents = await _operation_intents(operation_id)
        assert [(row["expected_version"], row["kind"]) for row in intents] == [(2, "EXECUTE")]

        # Prove intent participates in the caller transaction.  This second operation
        # reaches the intent hook, then journal failure must roll everything back.
        other_run, other_operation = await _create_operation(
            status=OperationStatus.WAITING_APPROVAL
        )
        other_approval = uuid.uuid4()
        async with async_session_factory() as session:
            session.add(
                ApprovalRequest(
                    id=other_approval,
                    operation_id=other_operation,
                    arguments_hash="a" * 64,
                    operation_version=1,
                    status=ApprovalStatus.PENDING,
                    expires_at=_NOW + timedelta(days=365),
                )
            )
            await session.commit()

        async def fail_journal(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected journal failure")

        monkeypatch.setattr("opspilot.approvals.service.append_event", fail_journal)
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="injected journal failure"):
                async with session.begin():
                    await decide_approval(
                        session,
                        other_approval,
                        ApprovalDecision.APPROVE,
                        decided_by="reviewer-1",
                        role=Role.REVIEWER,
                    )
        async with async_session_factory() as session:
            operation = await session.get(Operation, other_operation)
            approval = await session.get(ApprovalRequest, other_approval)
            assert operation is not None and operation.status is OperationStatus.WAITING_APPROVAL
            assert approval is not None and approval.status is ApprovalStatus.PENDING
        assert await _operation_intents(other_operation) == []
    finally:
        cleanup_ids = [run_id]
        if "other_run" in locals():
            cleanup_ids.append(other_run)
        await _cleanup_runs(cleanup_ids)


async def test_outcome_unknown_transition_creates_reconcile_intent_with_new_version() -> None:
    run_id, operation_id = await _create_operation(status=OperationStatus.READY)
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session,
                operation_id,
                owner="worker-1",
                lease_seconds=60,
                now=_NOW,
            )
            await session.commit()
        assert operation.claim_token is not None

        async with async_session_factory() as session:
            async with session.begin():
                unknown = await mark_unknown(
                    session,
                    operation_id,
                    owner="worker-1",
                    token=operation.claim_token,
                    expected_version=operation.version,
                    error="response read timeout",
                    now=_NOW + timedelta(seconds=1),
                )
        assert unknown.status is OperationStatus.OUTCOME_UNKNOWN
        intents = await _operation_intents(operation_id)
        assert [(row["expected_version"], row["kind"]) for row in intents] == [
            (unknown.version, "RECONCILE")
        ]
    finally:
        await _cleanup_runs([run_id])


class _BlockingQueue:
    def __init__(self) -> None:
        self.items: list[tuple[uuid.UUID, int, str]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def enqueue_operation(
        self, operation_id: uuid.UUID, expected_version: int, kind: str
    ) -> None:
        self.items.append((operation_id, expected_version, kind))
        self.entered.set()
        await self.release.wait()


async def test_concurrent_publishers_skip_locked_and_deliver_each_intent_once() -> None:
    """Two publishers must not enqueue the same locked row in one delivery pass."""
    from opspilot.jobs.outbox import publish_pending_operation_jobs

    run_a, operation_a = await _create_operation(status=OperationStatus.READY)
    run_b, operation_b = await _create_operation(status=OperationStatus.READY)
    connection = await _connect()
    try:
        for operation_id in (operation_a, operation_b):
            await connection.execute(
                "INSERT INTO operation_job_outbox "
                "(id, operation_id, expected_version, kind, available_at, attempts, created_at) "
                "VALUES ($1, $2, 1, 'EXECUTE', clock_timestamp(), 0, clock_timestamp())",
                uuid.uuid4(),
                operation_id,
            )
        queue = _BlockingQueue()
        first = asyncio.create_task(publish_pending_operation_jobs(queue, batch_size=1))
        await asyncio.wait_for(queue.entered.wait(), timeout=2)
        second = asyncio.create_task(publish_pending_operation_jobs(queue, batch_size=1))
        await asyncio.sleep(0.1)
        queue.release.set()
        assert sorted(await asyncio.gather(first, second)) == [1, 1]
        assert sorted(queue.items, key=lambda item: str(item[0])) == sorted(
            [(operation_a, 1, "EXECUTE"), (operation_b, 1, "EXECUTE")],
            key=lambda item: str(item[0]),
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_job_outbox "
                "WHERE operation_id = ANY($1::uuid[]) AND delivered_at IS NOT NULL",
                [operation_a, operation_b],
            )
            == 2
        )
    finally:
        await connection.close()
        await _cleanup_runs([run_a, run_b])


async def test_run_job_intent_has_stable_unique_message_identity() -> None:
    """The durable Run job is keyed by message, so publisher replay is harmless."""
    run_id = uuid.uuid4()
    connection = await _connect()
    message_id = uuid.uuid4()
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
        with pytest.raises(asyncpg.UniqueViolationError):
            await connection.execute(
                "INSERT INTO run_job_outbox "
                "(id, message_id, available_at, attempts, created_at) "
                "VALUES ($1, $2, clock_timestamp(), 0, clock_timestamp())",
                uuid.uuid4(),
                message_id,
            )
    finally:
        await connection.close()
        await _cleanup_runs([run_id])


async def test_duplicate_run_jobs_claim_before_calling_processor() -> None:
    """At-least-once ARQ delivery must not duplicate Provider/tool orchestration."""
    from opspilot.jobs.tasks import RunMessageResult, process_run_message

    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.RUNNING))
        session.add(
            RunMessage(
                id=message_id,
                run_id=run_id,
                role="USER",
                content="refund ORD-002",
            )
        )
        await session.flush()
        session.add(RunJobOutbox(message_id=message_id))
        await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def processor(message: RunMessage, fence: object) -> RunMessageResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return RunMessageResult(content="approval requested", citation_snapshots=[])

    try:
        first = asyncio.create_task(
            process_run_message({"run_message_processor": processor}, str(message_id))
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(
            process_run_message({"run_message_processor": processor}, str(message_id))
        )
        await asyncio.sleep(0.15)
        release.set()
        await asyncio.gather(first, second)
        assert calls == 1
        async with async_session_factory() as session:
            replies = list(
                await session.scalars(
                    __import__("sqlalchemy")
                    .select(RunMessage)
                    .where(RunMessage.in_reply_to_message_id == message_id)
                )
            )
        assert len(replies) == 1
    finally:
        release.set()
        await _cleanup_runs([run_id])


async def test_run_stays_running_while_created_operation_is_not_terminal() -> None:
    from opspilot.jobs.tasks import RunMessageResult, process_run_message

    run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.RUNNING))
        await session.flush()
        session.add(
            RunMessage(
                id=message_id,
                run_id=run_id,
                role="USER",
                content="refund ORD-002",
            )
        )
        session.add(
            Operation(
                id=operation_id,
                run_id=run_id,
                tool_name="refund_order",
                normalized_arguments={"order_id": "ORD-002", "amount": 350},
                arguments_hash=uuid.uuid4().hex * 2,
                idempotency_key=f"refund:ORD-002:{operation_id}",
                status=OperationStatus.WAITING_APPROVAL,
                version=1,
                policy_decision="REQUIRE_APPROVAL",
            )
        )
        await session.commit()

    async def processor(message: RunMessage, fence: object) -> RunMessageResult:
        return RunMessageResult(content="approval requested", citation_snapshots=[])

    try:
        await process_run_message({"run_message_processor": processor}, str(message_id))
        async with async_session_factory() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            assert run.status is RunStatus.RUNNING
    finally:
        await _cleanup_runs([run_id])
