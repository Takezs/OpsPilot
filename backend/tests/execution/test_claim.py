"""Claim/lease, fencing and state-machine recovery tests (Task 10).

Real PostgreSQL. The claim is a conditional UPDATE: only READY/RETRYING
operations are claimable, the winning worker bumps ``version`` monotonically
and receives a fresh one-shot ``claim_token`` plus a lease. Every renewal and
every result write is fenced by ``id + version + claim_token + owner`` and the
live lease, so a stale worker (old token / version, wrong owner, expired lease)
can never mutate business state and a late external response cannot overwrite a
new holder's result.

Lease expiry recovery follows the state machine: a READ_ONLY operation moves to
RETRYING (re-claimable); a SIDE_EFFECT operation may only move atomically to
OUTCOME_UNKNOWN and is then not claimable, so the provider is never re-invoked
(Task 11 owns the actual reconciliation decision).
"""

import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution import claim as claim_module
from opspilot.execution import executor as executor_module
from opspilot.execution.claim import (
    LeaseConflictError,
    OperationNotClaimableError,
    claim_operation,
    mark_failed,
    mark_succeeded,
    recover_expired,
    renew_lease,
)
from opspilot.execution.executor import (
    detached_provider_task_count,
    drain_detached_provider_tasks,
    execute_operation,
)
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.state_machine import IllegalStateTransitionError, assert_transition
from opspilot.runs.models import Run, RunStatus
from opspilot.tools.types import ToolEffect, ToolResult

_T0 = datetime(2026, 1, 1, tzinfo=UTC)

# A skewed application wall-clock (far behind the DB clock) used to prove the
# lease machinery reads PostgreSQL time by default, not the app's local time.
_FAKE_APP_NOW = datetime(1999, 1, 1, tzinfo=UTC)


class _FakeAppClockDatetime(datetime):
    @classmethod
    def now(cls, tz=None) -> datetime:  # noqa: D102
        return _FAKE_APP_NOW


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


async def _insert_operation_raw(
    run_id: uuid.UUID,
    *,
    status: str = "READY",
    order_number: str = "A100",
    tool_name: str = "refund_order",
    retry_of: uuid.UUID | None = None,
) -> uuid.UUID:
    key = f"refund:{order_number}" if tool_name == "refund_order" else f"{tool_name}:{order_number}"
    operation_id = uuid.uuid4()
    arguments = json.dumps({"order_number": order_number, "amount": 250.0})
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "INSERT INTO tool_operations "
            "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
            " status, version, policy_decision, retry_of_operation_id, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::operation_status, 1, 'ALLOW', $8, "
            "now(), now())",
            operation_id,
            run_id,
            tool_name,
            arguments,
            "a" * 64,
            key,
            status,
            retry_of,
        )
        return operation_id
    finally:
        await connection.close()


async def _insert_occupancy(operation_id: uuid.UUID, key: str) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "INSERT INTO operation_idempotency_occupancy "
            "(id, tool_name, idempotency_key, operation_id) VALUES ($1, 'refund_order', $2, $3)",
            uuid.uuid4(),
            key,
            operation_id,
        )
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


async def _fetch_result_payload(operation_id: uuid.UUID) -> dict[str, object] | None:
    """Read the JSONB result through the ORM (asyncpg returns jsonb as text)."""
    async with async_session_factory() as session:
        operation = await session.get(Operation, operation_id)
        assert operation is not None
        return operation.result_payload


def test_state_machine_allows_and_denies_transitions() -> None:
    assert_transition(OperationStatus.READY, OperationStatus.EXECUTING)
    assert_transition(OperationStatus.RETRYING, OperationStatus.EXECUTING)
    assert_transition(OperationStatus.EXECUTING, OperationStatus.SUCCEEDED)
    assert_transition(OperationStatus.EXECUTING, OperationStatus.FAILED)
    assert_transition(OperationStatus.EXECUTING, OperationStatus.OUTCOME_UNKNOWN)
    assert_transition(OperationStatus.EXECUTING, OperationStatus.RECONCILING)
    assert_transition(OperationStatus.EXECUTING, OperationStatus.RETRYING)
    assert_transition(OperationStatus.OUTCOME_UNKNOWN, OperationStatus.RECONCILING)
    for terminal in (
        OperationStatus.MANUAL_REVIEW,
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.DENIED,
        OperationStatus.REJECTED,
    ):
        with pytest.raises(IllegalStateTransitionError):
            assert_transition(terminal, OperationStatus.READY)
        with pytest.raises(IllegalStateTransitionError):
            assert_transition(terminal, OperationStatus.EXECUTING)
    with pytest.raises(IllegalStateTransitionError):
        assert_transition(OperationStatus.CREATED, OperationStatus.EXECUTING)
    with pytest.raises(IllegalStateTransitionError):
        assert_transition(OperationStatus.READY, OperationStatus.SUCCEEDED)


async def test_ready_operation_is_claimable() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=120, now=_T0
            )
            assert operation.status is OperationStatus.EXECUTING
            assert operation.version == 2
            assert operation.claim_token is not None
            assert operation.lease_owner == "worker-1"
            assert operation.lease_expires_at == _T0 + timedelta(seconds=120)
            await session.commit()

        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_retrying_operation_is_claimable() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="RETRYING")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-2", lease_seconds=60, now=_T0
            )
            assert operation.status is OperationStatus.EXECUTING
            assert operation.version == 2
            await session.commit()
    finally:
        await _cleanup_runs([run_id])


async def test_terminal_and_non_executable_statuses_are_not_claimable() -> None:
    for status in (
        "CREATED",
        "WAITING_APPROVAL",
        "MANUAL_REVIEW",
        "SUCCEEDED",
        "FAILED",
        "DENIED",
        "REJECTED",
        "OUTCOME_UNKNOWN",
        "RECONCILING",
    ):
        run_id = await _create_run()
        op_id = await _insert_operation_raw(run_id, status=status)
        try:
            async with async_session_factory() as session:
                with pytest.raises(OperationNotClaimableError):
                    await claim_operation(
                        session, op_id, owner="worker-1", lease_seconds=60, now=_T0
                    )
            row = await _fetch_operation(op_id)
            assert row["status"] == status
            assert row["version"] == 1
        finally:
            await _cleanup_runs([run_id])


async def test_concurrent_claim_allows_exactly_one_winner() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:

        async def _claim() -> OperationStatus:
            async with async_session_factory() as session:
                try:
                    operation = await claim_operation(
                        session, op_id, owner="worker", lease_seconds=60, now=_T0
                    )
                    await session.commit()
                    return operation.status
                except Exception:
                    await session.rollback()
                    raise

        results = await asyncio.gather(*[_claim() for _ in range(2)], return_exceptions=True)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert winners == [OperationStatus.EXECUTING]
        assert all(isinstance(loser, OperationNotClaimableError) for loser in losers)
        # the loser must not have consumed any journal seq or emitted an event
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        assert await _count("SELECT next_seq FROM agent_runs WHERE id = $1", run_id) == 1
    finally:
        await _cleanup_runs([run_id])


async def test_claim_token_is_one_shot_and_non_reusable() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()

        # a second claim is impossible while EXECUTING
        async with async_session_factory() as session:
            with pytest.raises(OperationNotClaimableError):
                await claim_operation(session, op_id, owner="worker-2", lease_seconds=60, now=_T0)
            await session.rollback()

        # and the token is cleared on a terminal write, so it can never be replayed
        async with async_session_factory() as session:
            await mark_succeeded(
                session,
                op_id,
                owner="worker-1",
                token=token,
                expected_version=2,
                result={"refund_id": "r1"},
                now=_T0 + timedelta(seconds=30),
            )
            await session.commit()
        row = await _fetch_operation(op_id)
        assert row["status"] == "SUCCEEDED"
        assert row["claim_token"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_stale_version_write_affects_zero_rows() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            await session.commit()
        # the claim bumped version 1 -> 2; a write fenced on version 1 must be refused
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-1",
                    token=operation.claim_token,
                    expected_version=1,
                    result={"refund_id": "r1"},
                    now=_T0 + timedelta(seconds=30),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_stale_token_write_affects_zero_rows() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-1",
                    token="stale-token",
                    expected_version=2,
                    result={"refund_id": "r1"},
                    now=_T0 + timedelta(seconds=30),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_wrong_owner_write_affects_zero_rows() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            await session.commit()
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-2",
                    token=operation.claim_token,
                    expected_version=2,
                    result={"refund_id": "r1"},
                    now=_T0 + timedelta(seconds=30),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_lease_renewal_extends_expiry_without_bumping_version() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()
        async with async_session_factory() as session:
            renewed = await renew_lease(
                session,
                op_id,
                owner="worker-1",
                token=token,
                expected_version=2,
                lease_seconds=60,
                now=_T0 + timedelta(seconds=20),
            )
            assert renewed.lease_expires_at == _T0 + timedelta(seconds=80)
            assert renewed.version == 2
            assert renewed.claim_token == token
            await session.commit()
    finally:
        await _cleanup_runs([run_id])


async def test_renewal_with_stale_token_or_version_or_owner_fails() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        row_before = await _fetch_operation(op_id)
        for kwargs in (
            {"token": "stale-token", "expected_version": 2, "owner": "worker-1"},
            {"token": row_before["claim_token"], "expected_version": 1, "owner": "worker-1"},
            {"token": row_before["claim_token"], "expected_version": 2, "owner": "worker-2"},
        ):
            async with async_session_factory() as session:
                with pytest.raises(LeaseConflictError):
                    await renew_lease(
                        session,
                        op_id,
                        owner=kwargs["owner"],
                        token=kwargs["token"],
                        expected_version=kwargs["expected_version"],
                        lease_seconds=60,
                        now=_T0 + timedelta(seconds=20),
                    )
                await session.rollback()
            assert await _fetch_operation(op_id) == row_before
    finally:
        await _cleanup_runs([run_id])


async def test_expired_lease_cannot_renew_and_worker_loses_write_rights() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(
                session, op_id, owner="worker-1", lease_seconds=60, now=_T0
            )
            token = operation.claim_token
            await session.commit()

        # the lease is dead; renewal is refused and so is the terminal write
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await renew_lease(
                    session,
                    op_id,
                    owner="worker-1",
                    token=token,
                    expected_version=2,
                    lease_seconds=60,
                    now=_T0 + timedelta(seconds=120),
                )
            await session.rollback()
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-1",
                    token=token,
                    expected_version=2,
                    result={"refund_id": "r1"},
                    now=_T0 + timedelta(seconds=120),
                )
            await session.rollback()

        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_read_only_expired_lease_moves_to_retrying_and_is_reclaimable() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(
        run_id, status="READY", tool_name="get_order_status", order_number="B200"
    )
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        async with async_session_factory() as session:
            recovered = await recover_expired(
                session, op_id, effect=ToolEffect.READ_ONLY, now=_T0 + timedelta(seconds=120)
            )
            assert recovered.status is OperationStatus.RETRYING
            assert recovered.version == 3
            assert recovered.claim_token is None
            assert recovered.lease_owner is None
            await session.commit()
        async with async_session_factory() as session:
            reclaim = await claim_operation(
                session, op_id, owner="worker-2", lease_seconds=60, now=_T0 + timedelta(seconds=180)
            )
            assert reclaim.status is OperationStatus.EXECUTING
            assert reclaim.version == 4
            await session.commit()
    finally:
        await _cleanup_runs([run_id])


async def test_side_effect_expired_lease_moves_to_outcome_unknown_and_is_not_reclaimable() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        async with async_session_factory() as session:
            recovered = await recover_expired(
                session, op_id, effect=ToolEffect.SIDE_EFFECT, now=_T0 + timedelta(seconds=120)
            )
            assert recovered.status is OperationStatus.OUTCOME_UNKNOWN
            assert recovered.version == 3
            assert recovered.claim_token is None
            await session.commit()
        # OUTCOME_UNKNOWN is not claimable: the provider can never be re-invoked
        async with async_session_factory() as session:
            with pytest.raises(OperationNotClaimableError):
                await claim_operation(
                    session,
                    op_id,
                    owner="worker-2",
                    lease_seconds=60,
                    now=_T0 + timedelta(seconds=180),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "OUTCOME_UNKNOWN"
    finally:
        await _cleanup_runs([run_id])


async def test_late_response_from_old_worker_cannot_overwrite_new_holder() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            old = await claim_operation(session, op_id, owner="worker-A", lease_seconds=60, now=_T0)
            await session.commit()
        # the lease expires and worker B takes the side-effect operation to
        # OUTCOME_UNKNOWN (bumping version and dropping the old token)
        async with async_session_factory() as session:
            await recover_expired(
                session, op_id, effect=ToolEffect.SIDE_EFFECT, now=_T0 + timedelta(seconds=120)
            )
            await session.commit()
        # worker A's late external response cannot overwrite worker B's result
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-A",
                    token=old.claim_token,
                    expected_version=2,
                    result={"refund_id": "late-refund"},
                    now=_T0 + timedelta(seconds=120),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "OUTCOME_UNKNOWN"
        assert row["claim_token"] is None
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_executor_commits_success_while_holding_lease() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    invoked: list[str] = []

    async def _invoke(operation: object) -> ToolResult:
        invoked.append("refund_order")
        return ToolResult(ok=True, data={"refund_id": "r1"})

    try:
        operation = await execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=60,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            now=lambda: _T0,
        )
        assert operation.status is OperationStatus.SUCCEEDED

        assert invoked == ["refund_order"]
        row = await _fetch_operation(op_id)
        assert row["status"] == "SUCCEEDED"
        assert row["version"] == 3
        assert row["claim_token"] is None
        assert row["lease_owner"] is None
        assert await _fetch_result_payload(op_id) == {"refund_id": "r1"}
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 2
    finally:
        await _cleanup_runs([run_id])


@pytest.mark.parametrize("provider_not_called", [None, False])
async def test_executor_maps_ambiguous_side_effect_failure_to_outcome_unknown(
    provider_not_called: bool | None,
) -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(
            ok=False,
            error="provider declined",
            provider_not_called=provider_not_called,
        )

    try:
        operation = await execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=60,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            now=lambda: _T0,
        )
        # the failure cannot prove the provider never executed, so it must never
        # be recorded as a definitive FAILED
        assert operation.status is OperationStatus.OUTCOME_UNKNOWN
        row = await _fetch_operation(op_id)
        assert row["status"] == "OUTCOME_UNKNOWN"
        assert row["version"] == 3
        assert row["claim_token"] is None
        assert row["lease_owner"] is None
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 "
                "AND event_type = 'operation_outcome_unknown'",
                run_id,
            )
            == 1
        )
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executor_retries_when_side_effect_provably_not_executed() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(
            ok=False,
            error="rejected before dispatch",
            provider_not_called=True,
        )

    try:
        operation = await execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=60,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            now=lambda: _T0,
        )
        assert operation.status is OperationStatus.RETRYING
        row = await _fetch_operation(op_id)
        assert row["status"] == "RETRYING"
        assert row["version"] == 3
        assert row["claim_token"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_executor_commits_failure_for_read_only_ambiguous_failure() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY", tool_name="get_order")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(ok=False, error="upstream timeout")

    try:
        operation = await execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=60,
            effect=ToolEffect.READ_ONLY,
            invoke=_invoke,
            now=lambda: _T0,
        )
        # read-only work has no external effect, so a definitive FAILED is safe
        assert operation.status is OperationStatus.FAILED
        row = await _fetch_operation(op_id)
        assert row["status"] == "FAILED"
    finally:
        await _cleanup_runs([run_id])


class _AdvancingClock:
    def __init__(self, times: list[datetime]) -> None:
        self._times = list(times)

    def __call__(self) -> datetime:
        return self._times.pop(0)


async def test_executor_does_not_commit_result_after_losing_lease() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    invoked: list[str] = []

    async def _invoke(operation: object) -> ToolResult:
        invoked.append("refund_order")
        return ToolResult(ok=True, data={"refund_id": "r1"})

    try:
        # claim at T0 (lease expires T0+60); the invocation returns instantly so
        # the heartbeat is stopped before its first tick, then the result write's
        # second session resolves T0+120 and the fenced update finds the lease
        # dead -> LeaseConflictError. The DB fence, not a clock check, decides.
        clock = _AdvancingClock([_T0, _T0 + timedelta(seconds=120)])
        with pytest.raises(LeaseConflictError):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=clock,
            )

        assert invoked == ["refund_order"]
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["result_payload"] is None
        assert row["lease_owner"] == "worker-1"
        # no success event was ever written
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 "
                "AND event_type = 'operation_succeeded'",
                run_id,
            )
            == 0
        )
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executing_occupancy_cannot_be_released() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    await _insert_occupancy(op_id, "refund:OCCUPANCY")
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()

        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            with pytest.raises(Exception, match="occupancy release denied"):
                await connection.execute(
                    "DELETE FROM operation_idempotency_occupancy WHERE operation_id = $1", op_id
                )
        finally:
            await connection.close()
        assert (
            await _count(
                "SELECT count(*) FROM operation_idempotency_occupancy WHERE operation_id = $1",
                op_id,
            )
            == 1
        )
    finally:
        await _cleanup_runs([run_id])


async def test_mark_failed_from_terminal_status_is_refused() -> None:
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="SUCCEEDED")
    try:
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_failed(
                    session,
                    op_id,
                    owner="worker-1",
                    token="token",
                    expected_version=1,
                    error="boom",
                    now=_T0,
                )
            await session.rollback()
    finally:
        await _cleanup_runs([run_id])


# --- adversarial self-review additions (Task 10 self-review) ---


async def _force_lease_into_past(operation_id: uuid.UUID) -> None:
    """Push ``lease_expires_at`` one hour into the past relative to the DB clock."""
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "UPDATE tool_operations SET lease_expires_at = "
            "clock_timestamp() - interval '1 hour' WHERE id = $1",
            operation_id,
        )
    finally:
        await connection.close()


async def test_claim_defaults_to_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim's lease must be anchored to PostgreSQL time, not the app clock."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    # skew the app wall clock far behind the DB clock
    monkeypatch.setattr(claim_module, "datetime", _FakeAppClockDatetime)
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(session, op_id, owner="worker-1", lease_seconds=60)
            # the DB clock (today) governs the lease, not the skewed 1999 app clock
            assert operation.lease_expires_at is not None
            assert operation.lease_expires_at > _FAKE_APP_NOW + timedelta(days=3650)
            await session.commit()
    finally:
        await _cleanup_runs([run_id])


async def test_recover_expired_defaults_to_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must judge expiry against PostgreSQL time, not the app clock."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    monkeypatch.setattr(claim_module, "datetime", _FakeAppClockDatetime)
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        # the lease is expired relative to the real DB clock; the skewed app
        # clock (1999) would incorrectly think it is still live
        await _force_lease_into_past(op_id)
        async with async_session_factory() as session:
            recovered = await recover_expired(session, op_id, effect=ToolEffect.READ_ONLY)
            assert recovered.status is OperationStatus.RETRYING
            await session.commit()
    finally:
        await _cleanup_runs([run_id])


async def test_fenced_write_defaults_to_database_clock() -> None:
    """A terminal write with no injected clock fences against the live DB lease."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(session, op_id, owner="worker-1", lease_seconds=60)
            token = operation.claim_token
            await session.commit()
        async with async_session_factory() as session:
            await mark_succeeded(
                session,
                op_id,
                owner="worker-1",
                token=token,
                expected_version=2,
                result={"refund_id": "r1"},
            )
            await session.commit()
        row = await _fetch_operation(op_id)
        assert row["status"] == "SUCCEEDED"
        assert row["version"] == 3
    finally:
        await _cleanup_runs([run_id])


async def test_database_clock_refuses_write_after_lease_forced_past() -> None:
    """A lease expired on the DB clock refuses a fenced write even without `now`."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            operation = await claim_operation(session, op_id, owner="worker-1", lease_seconds=60)
            token = operation.claim_token
            await session.commit()
        await _force_lease_into_past(op_id)
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-1",
                    token=token,
                    expected_version=2,
                    result={"refund_id": "r1"},
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_clock_skew_is_fenced_safe_not_time_dependent() -> None:
    """Under clock skew the version+token fencing — not the time check — decides."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            old = await claim_operation(session, op_id, owner="worker-A", lease_seconds=60, now=_T0)
            token = old.claim_token
            await session.commit()
        # the DB clock sees the lease as long expired (claim clock was skewed
        # into the past) and recovers the side-effect operation
        async with async_session_factory() as session:
            recovered = await recover_expired(session, op_id, effect=ToolEffect.SIDE_EFFECT)
            assert recovered.status is OperationStatus.OUTCOME_UNKNOWN
            await session.commit()
        # worker A still holds its (skewed) view that the lease is live, so the
        # pure time-fencing would pass — but recovery bumped the version and
        # cleared the token, so the fenced write is refused
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await mark_succeeded(
                    session,
                    op_id,
                    owner="worker-A",
                    token=token,
                    expected_version=2,
                    result={"refund_id": "r1"},
                    now=_T0 + timedelta(seconds=30),
                )
            await session.rollback()
        row = await _fetch_operation(op_id)
        assert row["status"] == "OUTCOME_UNKNOWN"
        assert row["claim_token"] is None
        assert row["result_payload"] is None
    finally:
        await _cleanup_runs([run_id])


async def test_concurrent_recovery_allows_exactly_one_winner() -> None:
    """Two recoverers racing on the same expired lease: exactly one wins."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    try:
        async with async_session_factory() as session:
            await claim_operation(session, op_id, owner="worker-1", lease_seconds=60, now=_T0)
            await session.commit()
        await _force_lease_into_past(op_id)

        async def _recover() -> OperationStatus:
            async with async_session_factory() as session:
                try:
                    recovered = await recover_expired(session, op_id, effect=ToolEffect.SIDE_EFFECT)
                    await session.commit()
                    return recovered.status
                except Exception:
                    await session.rollback()
                    raise

        results = await asyncio.gather(*[_recover() for _ in range(2)], return_exceptions=True)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert winners == [OperationStatus.OUTCOME_UNKNOWN]
        assert all(isinstance(loser, LeaseConflictError) for loser in losers)
        row = await _fetch_operation(op_id)
        assert row["status"] == "OUTCOME_UNKNOWN"
        assert row["version"] == 3
        assert row["claim_token"] is None
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 2
        assert await _count("SELECT next_seq FROM agent_runs WHERE id = $1", run_id) == 2
    finally:
        await _cleanup_runs([run_id])


async def test_executor_invoke_raises_leaves_operation_executing() -> None:
    """A provider exception after a committed claim must leave the op EXECUTING."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        raise RuntimeError("provider blew up")

    try:
        with pytest.raises(RuntimeError, match="provider blew up"):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: _T0,
            )
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["claim_token"] is not None
        assert row["lease_owner"] == "worker-1"
        # the claim event exists, but no terminal event was ever written
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 "
                "AND event_type IN ('operation_succeeded', 'operation_failed')",
                run_id,
            )
            == 0
        )
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executor_invoke_cancellation_leaves_operation_executing() -> None:
    """Cancelling the worker mid-invoke must not roll the committed claim back."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult(ok=True, data={"refund_id": "r1"})

    try:
        task = asyncio.create_task(
            execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: _T0,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["claim_token"] is not None
        assert row["lease_owner"] == "worker-1"
        assert await _count("SELECT count(*) FROM run_events WHERE run_id = $1", run_id) == 1
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


def _assert_no_leftover_tasks() -> None:
    """The executor must never leak an asyncio Task once it returns."""
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current]
    assert not leftovers, f"leftover asyncio tasks after execute_operation: {leftovers!r}"


async def _run_and_release(task: asyncio.Task, release: asyncio.Event) -> None:
    """Let a blocked invoke finish, then collect the executor task quietly."""
    release.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=15)


async def test_executor_heartbeat_renews_lease_while_invoke_blocked() -> None:
    """While the provider call is blocked, the lease must be extended in the DB."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult(ok=True, data={"refund_id": "r1"})

    task = asyncio.create_task(
        execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=3,  # heartbeat interval = max(1, 3//3) = 1s
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            # now=None -> the lease machinery reads PostgreSQL clock_timestamp()
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        original = (await _fetch_operation(op_id))["lease_expires_at"]
        assert original is not None
        # give at least one heartbeat interval while invoke is still blocked
        await asyncio.sleep(2.2)
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["lease_expires_at"] > original, (
            "lease was not extended while the provider call was in flight"
        )
        renewals = await _count(
            "SELECT count(*) FROM run_events WHERE run_id = $1 "
            "AND event_type = 'operation_lease_renewed'",
            run_id,
        )
        assert renewals >= 1
    finally:
        await _run_and_release(task, release)
        await _cleanup_runs([run_id])
    _assert_no_leftover_tasks()


async def test_executor_heartbeat_renews_multiple_times_across_intervals() -> None:
    """Across several heartbeat intervals the lease must be renewed repeatedly."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult(ok=True, data={"refund_id": "r1"})

    task = asyncio.create_task(
        execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=3,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # ~3 heartbeat intervals -> at least 2 renewals
        await asyncio.sleep(3.5)
        renewals = await _count(
            "SELECT count(*) FROM run_events WHERE run_id = $1 "
            "AND event_type = 'operation_lease_renewed'",
            run_id,
        )
        assert renewals >= 2, f"expected >= 2 renewals, observed {renewals}"
    finally:
        await _run_and_release(task, release)
        await _cleanup_runs([run_id])
    _assert_no_leftover_tasks()


async def test_executor_heartbeat_renews_from_database_clock_not_app_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat must read PostgreSQL time, not the application wall clock."""
    monkeypatch.setattr(claim_module, "datetime", _FakeAppClockDatetime)
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult(ok=True, data={"refund_id": "r1"})

    task = asyncio.create_task(
        execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=3,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        original = (await _fetch_operation(op_id))["lease_expires_at"]
        await asyncio.sleep(2.2)
        row = await _fetch_operation(op_id)
        assert row["lease_expires_at"] > original, (
            "heartbeat did not renew from the database clock despite the app "
            "clock being skewed far into the past"
        )
    finally:
        await _run_and_release(task, release)
        await _cleanup_runs([run_id])


async def test_executor_heartbeat_keeps_lease_live_so_recovery_cannot_take_over() -> None:
    """A running heartbeat must stop a concurrent recover_expired from grabbing."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult(ok=True, data={"refund_id": "r1"})

    task = asyncio.create_task(
        execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=4,  # heartbeat interval = 1s; natural lease would lapse at 4s
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # wait well past the natural lease duration; the heartbeat renews it
        await asyncio.sleep(5.0)
        async with async_session_factory() as session:
            with pytest.raises(LeaseConflictError):
                await recover_expired(session, op_id, effect=ToolEffect.SIDE_EFFECT)
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["claim_token"] is not None
    finally:
        await _run_and_release(task, release)
        await _cleanup_runs([run_id])
    _assert_no_leftover_tasks()


async def test_executor_heartbeat_failure_revokes_worker_and_never_writes_result() -> None:
    """When a renewal loses the lease, the worker is revoked and no result is written."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        await asyncio.sleep(30)  # provider outlives the lease
        return ToolResult(ok=True, data={"refund_id": "r1"})

    # claim at T0 (lease T0+3); the heartbeat's first tick resolves T0+120 and
    # finds the lease already dead -> LeaseConflictError, promptly.
    clock = _AdvancingClock([_T0, _T0 + timedelta(seconds=120)])
    try:
        with pytest.raises(LeaseConflictError):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=3,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=clock,
            )
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["version"] == 2
        assert row["result_payload"] is None
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 "
                "AND event_type IN ('operation_succeeded', 'operation_failed')",
                run_id,
            )
            == 0
        )
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executor_heartbeat_failure_race_with_invoke_return_decided_by_fence() -> None:
    """A provider that returns while the lease is lost must still be fenced out."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")

    async def _invoke(operation: object) -> ToolResult:
        # returns just after the heartbeat's first (doomed) renewal
        await asyncio.sleep(1.4)
        return ToolResult(ok=True, data={"refund_id": "r1"})

    clock = _AdvancingClock([_T0, _T0 + timedelta(seconds=120)])
    try:
        with pytest.raises(LeaseConflictError):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=3,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=clock,
            )
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
        assert (
            await _count(
                "SELECT count(*) FROM run_events WHERE run_id = $1 "
                "AND event_type = 'operation_succeeded'",
                run_id,
            )
            == 0
        )
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executor_same_turn_failures_are_both_retrieved_and_lease_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simultaneous invoke/heartbeat failures must not orphan either exception."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    orphaned: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: orphaned.append(context))

    async def _invoke(operation: object) -> ToolResult:
        raise RuntimeError("provider failed in same turn")

    async def _failed_heartbeat(*args: object, **kwargs: object) -> None:
        raise LeaseConflictError("heartbeat failed in same turn")

    monkeypatch.setattr(executor_module, "_heartbeat_loop", _failed_heartbeat)
    try:
        with pytest.raises(LeaseConflictError, match="heartbeat failed in same turn"):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: _T0,
            )
        await asyncio.sleep(0)
        assert orphaned == []
        _assert_no_leftover_tasks()
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
    finally:
        loop.set_exception_handler(previous_handler)
        await _cleanup_runs([run_id])


async def test_executor_heartbeat_failure_during_invoke_cleanup_blocks_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat failing as invoke wins FIRST_COMPLETED must still fail closed."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    heartbeat_started = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        await heartbeat_started.wait()
        return ToolResult(ok=True, data={"refund_id": "must-not-commit"})

    async def _failed_during_stop(*args: object, **kwargs: object) -> None:
        heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise LeaseConflictError("heartbeat failed during cleanup") from None

    monkeypatch.setattr(executor_module, "_heartbeat_loop", _failed_during_stop)
    try:
        with pytest.raises(LeaseConflictError, match="heartbeat failed during cleanup"):
            await execute_operation(
                async_session_factory,
                op_id,
                owner="worker-1",
                lease_seconds=60,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: _T0,
            )
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
        _assert_no_leftover_tasks()
    finally:
        await _cleanup_runs([run_id])


async def test_executor_non_cancellable_invoke_does_not_block_lease_revocation() -> None:
    """A provider that ignores cancellation must not block the LeaseConflictError."""
    run_id = await _create_run()
    op_id = await _insert_operation_raw(run_id, status="READY")
    started = asyncio.Event()
    teardown = asyncio.Event()

    async def _invoke(operation: object) -> ToolResult:
        started.set()
        while not teardown.is_set():
            try:
                await teardown.wait()
            except asyncio.CancelledError:
                # Deliberately swallow every cancellation, not only the first.
                continue
        return ToolResult(ok=True, data={"refund_id": "r1"})

    clock = _AdvancingClock([_T0, _T0 + timedelta(seconds=120)])
    task = asyncio.create_task(
        execute_operation(
            async_session_factory,
            op_id,
            owner="worker-1",
            lease_seconds=3,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            now=clock,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # heartbeat's first renewal fails (lease already dead at T0+120) and must
        # surface even though the provider refuses to terminate.
        with pytest.raises(LeaseConflictError):
            await asyncio.wait_for(task, timeout=6)
        assert detached_provider_task_count() == 1
        row = await _fetch_operation(op_id)
        assert row["status"] == "EXECUTING"
        assert row["result_payload"] is None
    finally:
        teardown.set()  # let the abandoned provider finish; no task leaks
        await asyncio.wait_for(drain_detached_provider_tasks(), timeout=15)
        assert detached_provider_task_count() == 0
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=15)
        await _cleanup_runs([run_id])
