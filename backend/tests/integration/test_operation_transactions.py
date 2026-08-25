"""Atomicity of operation state, run_event and outbox rows (Task 10).

Every fenced state write (claim / renewal / terminal result) appends its
``run_event`` and ``event_outbox`` row in the same PostgreSQL transaction. When
the journal/outbox write is injected to fail, the whole transaction rolls back:
the operation keeps its prior status / version / token / lease and no ``seq`` is
consumed. Redis publish is not part of the business-fact transaction (the
outbox publisher runs separately and is covered by task 7).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution import claim as claim_module
from opspilot.execution.claim import (
    claim_operation,
    mark_succeeded,
    recover_expired,
    renew_lease,
)
from opspilot.execution.executor import execute_operation
from opspilot.execution.models import OperationStatus
from opspilot.execution.service import OperationNotFoundError
from opspilot.runs.models import Run, RunStatus
from opspilot.tools.types import ToolEffect, ToolResult

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _create_run() -> uuid.UUID:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    return run_id


async def _cleanup_runs(run_ids: Sequence[uuid.UUID]) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if run_ids:
            await connection.execute(
                "DELETE FROM manual_review_resolutions "
                "WHERE operation_id IN "
                "(SELECT id FROM tool_operations WHERE run_id = ANY($1::uuid[])) "
                "OR replacement_operation_id IN "
                "(SELECT id FROM tool_operations WHERE run_id = ANY($1::uuid[]))",
                list(run_ids),
            )
            await connection.execute(
                "DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids)
            )
    finally:
        await connection.close()


async def _insert_operation_raw(run_id: uuid.UUID, *, status: str = "READY") -> uuid.UUID:
    operation_id = uuid.uuid4()
    arguments = '{"order_number": "A100", "amount": 250.0}'
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "INSERT INTO tool_operations "
            "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
            " status, version, policy_decision, retry_of_operation_id, created_at, updated_at) "
            "VALUES ($1, $2, 'refund_order', $3::jsonb, $4, 'refund:A100', $5::operation_status, "
            "1, 'ALLOW', NULL, now(), now())",
            operation_id,
            run_id,
            arguments,
            "a" * 64,
            status,
        )
        return operation_id
    finally:
        await connection.close()


async def _fetch_operation(operation_id: uuid.UUID) -> dict[str, object]:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        row = await connection.fetchrow("SELECT * FROM tool_operations WHERE id = $1", operation_id)
        assert row is not None
        return dict(row)
    finally:
        await connection.close()


async def _count(query: str, *args: object) -> int:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        return int(await connection.fetchval(query, *args) or 0)
    finally:
        await connection.close()


async def _fetch_next_seq(run_id: uuid.UUID) -> int:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        return int(
            await connection.fetchval("SELECT next_seq FROM agent_runs WHERE id = $1", run_id)
        )
    finally:
        await connection.close()


def _fail_journal(session: object, run_id: uuid.UUID, event_type: str, payload: object) -> None:
    raise RuntimeError("injected journal/outbox failure")


_real_append_event = claim_module.append_event


async def _fail_terminal_journal(
    session: object, run_id: uuid.UUID, event_type: str, payload: object
) -> None:
    """Fail the result write's journal/outbox but keep the claim event real."""
    if event_type in ("operation_succeeded", "operation_failed"):
        raise RuntimeError("injected journal/outbox failure")
    await _real_append_event(session, run_id, event_type, payload)


async def test_claim_commits_state_event_and_outbox_atomically() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            await session.commit()

        assert operation.status is OperationStatus.EXECUTING
        assert operation.version == 2
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 1
        assert await _fetch_next_seq(run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_event_failure_rolls_back_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    monkeypatch.setattr(claim_module, "append_event", _fail_journal)
    try:
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="injected"):
                async with session.begin():
                    await claim_operation(
                        session, op_id, owner="worker-1", lease_seconds=60, now=_T0
                    )

        row = await _fetch_operation(op_id)
        assert row["status"] == "READY"
        assert row["version"] == 1
        assert row["claim_token"] is None
        assert row["lease_owner"] is None
        assert row["lease_expires_at"] is None
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 0
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 0
        assert await _fetch_next_seq(run_id) == 0
    finally:
        await _cleanup_runs([run_id])


async def test_event_failure_rolls_back_success_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()

        monkeypatch.setattr(claim_module, "append_event", _fail_journal)
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="injected"):
                async with session.begin():
                    await mark_succeeded(
                        session,
                        op_id,
                        owner="worker-1",
                        token=token,
                        expected_version=2,
                        result={"refund_id": "r1"},
                        now=_T0 + timedelta(seconds=30),
                    )

        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["claim_token"] == token
        assert row["result_payload"] is None
        # only the claim event survived
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 1
        assert await _fetch_next_seq(run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_event_failure_rolls_back_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()

        monkeypatch.setattr(claim_module, "append_event", _fail_journal)
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="injected"):
                async with session.begin():
                    await renew_lease(
                        session,
                        op_id,
                        owner="worker-1",
                        token=token,
                        expected_version=2,
                        lease_seconds=60,
                        now=_T0 + timedelta(seconds=20),
                    )

        row = await _fetch_operation(op_id)
        assert row["lease_expires_at"] == _T0 + timedelta(seconds=60)
        assert row["version"] == 2
        assert await _fetch_next_seq(run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_claim_on_missing_operation_is_refused() -> None:
    async with async_session_factory() as session:
        with pytest.raises(OperationNotFoundError):
            await claim_operation(
                session, uuid.uuid4(), owner="worker-1", lease_seconds=60, now=_T0
            )


async def test_event_failure_rolls_back_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()

        monkeypatch.setattr(claim_module, "append_event", _fail_journal)
        async with async_session_factory() as session:
            with pytest.raises(RuntimeError, match="injected"):
                async with session.begin():
                    await recover_expired(
                        session,
                        op_id,
                        effect=ToolEffect.SIDE_EFFECT,
                        now=_T0 + timedelta(seconds=120),
                    )

        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["claim_token"] == token
        assert row["lease_expires_at"] == _T0 + timedelta(seconds=60)
        # only the claim event survived; seq was not consumed by the recovery
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        assert await _count("SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id) == 1
        assert await _fetch_next_seq(run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_executor_result_write_failure_keeps_committed_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing result transaction must not undo the already-committed claim."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(ok=True, data={"refund_id": "r1"})

    # let the claim event commit; only the terminal result write fails
    monkeypatch.setattr(claim_module, "append_event", _fail_terminal_journal)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: _T0,
            )
        # the claim transaction committed before the result write: the operation
        # stays EXECUTING with the token, never back to the claimable READY
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["claim_token"] is not None
        assert row["lease_owner"] == "worker-1"
        assert row["result_payload"] is None
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
    finally:
        await _cleanup_runs([run_id])
