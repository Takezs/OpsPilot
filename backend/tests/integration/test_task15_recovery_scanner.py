"""Task 15 production lease-recovery scheduling contracts.

The scanner is deliberately tested against PostgreSQL facts.  It may schedule
reconciliation, but must never invoke a Provider itself.
"""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.auth.models import User  # noqa: F401 -- registers users FK metadata
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.models import Operation, OperationStatus
from opspilot.runs.models import Run, RunEvent, RunStatus


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))


async def _create_expired_operation(status: OperationStatus) -> tuple[uuid.UUID, uuid.UUID]:
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
                arguments_hash=uuid.uuid4().hex * 2,
                idempotency_key=f"refund:ORD-002:{operation_id}",
                status=status,
                version=7,
                policy_decision="REQUIRE_APPROVAL",
                claim_token=str(uuid.uuid4()),
                lease_owner="dead-worker",
                lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await session.commit()
    return run_id, operation_id


async def _cleanup(run_ids: Sequence[uuid.UUID]) -> None:
    connection = await _connect()
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids))
    finally:
        await connection.close()


async def _scan_once(*, batch_size: int = 25) -> int:
    from opspilot.jobs.recovery import recover_expired_operations

    return await recover_expired_operations(batch_size=batch_size)


@pytest.mark.parametrize(
    ("source_status", "event_type"),
    [
        (OperationStatus.EXECUTING, "operation_recovered"),
        (OperationStatus.RECONCILING, "operation_reconciliation_lease_expired"),
    ],
)
async def test_recovery_scanner_schedules_version_bound_reconciliation_without_provider(
    source_status: OperationStatus,
    event_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, operation_id = await _create_expired_operation(source_status)

    async def provider_must_not_run(*args: object, **kwargs: object) -> None:
        pytest.fail("lease recovery must not invoke the side-effect or reconciliation Provider")

    monkeypatch.setattr("opspilot.jobs.tasks.execute_operation", provider_must_not_run)
    monkeypatch.setattr("opspilot.jobs.tasks.reconcile_operation", provider_must_not_run)
    try:
        assert await _scan_once() == 1
        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None
            assert operation.status is OperationStatus.OUTCOME_UNKNOWN
            assert operation.version == 8
            events = list(
                await session.scalars(
                    __import__("sqlalchemy").select(RunEvent).where(RunEvent.run_id == run_id)
                )
            )
            assert [event.event_type for event in events] == [event_type]

        connection = await _connect()
        try:
            intent = await connection.fetchrow(
                "SELECT expected_version, kind, delivered_at "
                "FROM operation_job_outbox WHERE operation_id = $1",
                operation_id,
            )
        finally:
            await connection.close()
        assert intent is not None
        assert (intent["expected_version"], intent["kind"], intent["delivered_at"]) == (
            8,
            "RECONCILE",
            None,
        )
    finally:
        await _cleanup([run_id])


async def test_two_recovery_scanners_only_one_wins() -> None:
    run_id, operation_id = await _create_expired_operation(OperationStatus.EXECUTING)
    try:
        recovered = await asyncio.gather(_scan_once(batch_size=1), _scan_once(batch_size=1))
        assert sum(recovered) == 1
        connection = await _connect()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM operation_job_outbox WHERE operation_id = $1",
                    operation_id,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM run_events "
                    "WHERE run_id = $1 AND event_type = 'operation_recovered'",
                    run_id,
                )
                == 1
            )
        finally:
            await connection.close()
    finally:
        await _cleanup([run_id])


async def test_recovery_scanner_rolls_back_operation_event_and_intent_on_journal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, operation_id = await _create_expired_operation(OperationStatus.EXECUTING)

    async def fail_event(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected append failure")

    monkeypatch.setattr("opspilot.execution.claim.append_event", fail_event)
    try:
        with pytest.raises(RuntimeError, match="injected append failure"):
            await _scan_once()
        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None
            assert operation.status is OperationStatus.EXECUTING
            assert operation.version == 7
            assert operation.lease_owner == "dead-worker"
        connection = await _connect()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM run_events WHERE run_id = $1", run_id
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM operation_job_outbox WHERE operation_id = $1",
                    operation_id,
                )
                == 0
            )
        finally:
            await connection.close()
    finally:
        await _cleanup([run_id])


async def test_recovery_scanner_rolls_back_when_job_intent_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, operation_id = await _create_expired_operation(OperationStatus.EXECUTING)

    def fail_intent(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected job intent failure")

    monkeypatch.setattr("opspilot.execution.claim.enqueue_operation_job", fail_intent)
    try:
        with pytest.raises(RuntimeError, match="injected job intent failure"):
            await _scan_once()
        async with async_session_factory() as session:
            operation = await session.get(Operation, operation_id)
            assert operation is not None
            assert operation.status is OperationStatus.EXECUTING
            assert operation.version == 7
        connection = await _connect()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM run_events WHERE run_id = $1", run_id
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM operation_job_outbox WHERE operation_id = $1",
                    operation_id,
                )
                == 0
            )
        finally:
            await connection.close()
    finally:
        await _cleanup([run_id])


async def test_recovery_scanner_does_not_hot_loop_one_conflicting_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot.execution.claim import LeaseConflictError

    run_id, operation_id = await _create_expired_operation(OperationStatus.EXECUTING)
    calls: list[uuid.UUID] = []

    async def conflict(*args: object, **kwargs: object) -> None:
        calls.append(operation_id)
        raise LeaseConflictError("injected race")

    monkeypatch.setattr("opspilot.jobs.recovery.recover_expired", conflict)
    try:
        assert await _scan_once(batch_size=20) == 0
        assert calls == [operation_id]
    finally:
        await _cleanup([run_id])
