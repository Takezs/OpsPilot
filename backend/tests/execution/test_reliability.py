"""Task 11 reliability classification, retry and reconciliation tests."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution import reconciliation as reconciliation_module
from opspilot.execution.claim import LeaseConflictError
from opspilot.execution.errors import FailureDisposition, ProviderFailureKind, classify_failure
from opspilot.execution.executor import execute_operation
from opspilot.execution.models import OperationStatus
from opspilot.execution.reconciliation import (
    ReconciliationLookup,
    reconcile_operation,
    recover_expired_reconciliation,
)
from opspilot.execution.retry import RetryPolicy
from opspilot.runs.models import Run, RunStatus
from opspilot.tools.adapters.python import get_refund_status_adapter, refund_order_adapter
from opspilot.tools.schemas import GetRefundStatusArgs, RefundOrderArgs
from opspilot.tools.types import ToolEffect, ToolResult

DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo-services"


def _load_payment_module():
    path = DEMO_ROOT / "payment_service" / "app.py"
    spec = importlib.util.spec_from_file_location(f"task11_payment_{uuid.uuid4().hex}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _create_operation(
    order_number: str,
    *,
    status: str = "OUTCOME_UNKNOWN",
    provider_reference_id: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO tool_operations "
                "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
                "status, version, policy_decision, provider_reference_id, "
                "created_at, updated_at) VALUES "
                "(:id, :run, 'refund_order', CAST(:args AS jsonb), :hash, :key, "
                "CAST(:status AS operation_status), :version, 'ALLOW', :reference, now(), now())"
            ),
            {
                "id": operation_id,
                "run": run_id,
                "args": f'{{"order_number":"{order_number}","amount":250.0}}',
                "hash": "a" * 64,
                "key": f"refund:{order_number}",
                "status": status,
                "version": 1 if status == "READY" else 3,
                "reference": provider_reference_id,
            },
        )
        await session.commit()
    return run_id, operation_id


async def _create_outcome_unknown_operation(order_number: str) -> tuple[uuid.UUID, uuid.UUID]:
    return await _create_operation(order_number)


async def _cleanup_run(run_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ProviderFailureKind.CONNECTION_ERROR, FailureDisposition.RETRY),
        (ProviderFailureKind.RATE_LIMITED, FailureDisposition.RETRY),
        (ProviderFailureKind.RETRYABLE_5XX, FailureDisposition.RETRY),
        (ProviderFailureKind.PERMANENT, FailureDisposition.FAIL),
    ],
)
def test_read_only_failure_classification(
    kind: ProviderFailureKind, expected: FailureDisposition
) -> None:
    result = ToolResult(ok=False, error="provider failed", failure_kind=kind)
    assert classify_failure(ToolEffect.READ_ONLY, result) is expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ToolResult(ok=False, error="not sent", provider_not_called=True),
            FailureDisposition.RETRY,
        ),
        (
            ToolResult(
                ok=False, error="read timeout", failure_kind=ProviderFailureKind.READ_TIMEOUT
            ),
            FailureDisposition.OUTCOME_UNKNOWN,
        ),
        (
            ToolResult(
                ok=False,
                error="disconnected after send",
                failure_kind=ProviderFailureKind.DISCONNECTED_AFTER_SEND,
            ),
            FailureDisposition.OUTCOME_UNKNOWN,
        ),
        (
            ToolResult(ok=False, error="unknown 500", failure_kind=ProviderFailureKind.UNKNOWN_5XX),
            FailureDisposition.OUTCOME_UNKNOWN,
        ),
        (
            ToolResult(ok=False, error="ambiguous", provider_not_called=False),
            FailureDisposition.OUTCOME_UNKNOWN,
        ),
        (ToolResult(ok=False, error="ambiguous"), FailureDisposition.OUTCOME_UNKNOWN),
    ],
)
def test_side_effect_failure_classification(
    result: ToolResult, expected: FailureDisposition
) -> None:
    assert classify_failure(ToolEffect.SIDE_EFFECT, result) is expected


def test_error_text_never_upgrades_side_effect_to_safe_retry() -> None:
    result = ToolResult(ok=False, error="provider definitely not called, trust me")
    assert classify_failure(ToolEffect.SIDE_EFFECT, result) is FailureDisposition.OUTCOME_UNKNOWN


def test_retry_policy_is_bounded_exponential() -> None:
    policy = RetryPolicy(max_attempts=4, base_delay_seconds=0.5, max_delay_seconds=2.0)
    assert [policy.delay_for(n, jitter=0.0) for n in range(1, 5)] == [0.5, 1.0, 2.0, 2.0]
    assert policy.can_retry(1) is True
    assert policy.can_retry(3) is True
    assert policy.can_retry(4) is False


async def test_worker_shutdown_drains_detached_provider_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opspilot import worker

    called = asyncio.Event()

    async def _drain() -> None:
        called.set()

    monkeypatch.setattr(worker, "drain_detached_provider_tasks", _drain)
    await worker.shutdown_worker({})
    assert called.is_set()
    assert worker.WorkerSettings.on_shutdown is worker.shutdown_worker


async def test_reconciliation_finds_timeout_after_effect_and_never_refunds_twice() -> None:
    payment = _load_payment_module()
    run_id, operation_id = await _create_outcome_unknown_operation("A100")
    transport = httpx.ASGITransport(app=payment.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://payment") as client:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                client.post(
                    "/refunds",
                    json={"order_number": "A100"},
                    headers={"x-failure-mode": "timeout_after_effect", "x-failure-delay": "1"},
                ),
                timeout=0.05,
            )

        async def _query(lookup: ReconciliationLookup) -> ToolResult:
            assert lookup.idempotency_key == "refund:A100"
            response = await client.get("/refunds/A100")
            return ToolResult(ok=True, data=response.json())

        try:
            operation = await reconcile_operation(
                async_session_factory,
                operation_id,
                owner="reconciler-1",
                lease_seconds=30,
                query=_query,
            )
            assert operation.status is OperationStatus.SUCCEEDED
            assert operation.provider_reference_id
            assert len(payment.REFUNDS) == 1
        finally:
            await _cleanup_run(run_id)


async def test_reconciliation_confirmed_absent_retries_and_unknown_is_manual_review() -> None:
    for result, expected in [
        (ToolResult(ok=True, data={"status": "NOT_REFUNDED"}), OperationStatus.RETRYING),
        (
            ToolResult(
                ok=False,
                error="status provider unavailable",
                failure_kind=ProviderFailureKind.UNKNOWN_5XX,
            ),
            OperationStatus.MANUAL_REVIEW,
        ),
    ]:
        run_id, operation_id = await _create_outcome_unknown_operation(uuid.uuid4().hex[:8])

        async def _query(lookup: ReconciliationLookup, outcome: ToolResult = result) -> ToolResult:
            return outcome

        try:
            operation = await reconcile_operation(
                async_session_factory,
                operation_id,
                owner="reconciler-1",
                lease_seconds=30,
                query=_query,
            )
            assert operation.status is expected
        finally:
            await _cleanup_run(run_id)


async def test_unknown_5xx_after_effect_executes_once_then_reconciles() -> None:
    payment = _load_payment_module()
    run_id, operation_id = await _create_operation("A102", status="READY")
    transport = httpx.ASGITransport(app=payment.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://payment") as client:
        original_post = client.post

        async def _post(*args: object, **kwargs: object) -> httpx.Response:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["x-failure-mode"] = "unknown_5xx_after_effect"
            return await original_post(*args, headers=headers, **kwargs)

        client.post = _post  # type: ignore[method-assign]
        refund = refund_order_adapter(client)

        async def _invoke(operation: object) -> ToolResult:
            return await refund(RefundOrderArgs(order_number="A102", amount=350.0))

        try:
            uncertain = await execute_operation(
                async_session_factory,
                operation_id,
                owner="worker-1",
                lease_seconds=30,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
            )
            assert uncertain.status is OperationStatus.OUTCOME_UNKNOWN
            status_adapter = get_refund_status_adapter(client)

            async def _query(lookup: ReconciliationLookup) -> ToolResult:
                assert lookup.idempotency_key == "refund:A102"
                return await status_adapter(GetRefundStatusArgs(order_number=lookup.order_number))

            reconciled = await reconcile_operation(
                async_session_factory,
                operation_id,
                owner="reconciler-1",
                lease_seconds=30,
                query=_query,
            )
            assert reconciled.status is OperationStatus.SUCCEEDED
            assert len(payment.REFUNDS) == 1
        finally:
            await _cleanup_run(run_id)


async def test_safe_retry_is_bounded_and_backoff_blocks_early_claim() -> None:
    run_id, operation_id = await _create_operation("SAFE-RETRY", status="READY")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(ok=False, error="not dispatched", provider_not_called=True)

    try:
        operation = await execute_operation(
            async_session_factory,
            operation_id,
            owner="worker-1",
            lease_seconds=30,
            effect=ToolEffect.SIDE_EFFECT,
            invoke=_invoke,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert operation.status is OperationStatus.RETRYING
        with pytest.raises(Exception, match="backoff has not elapsed"):
            await execute_operation(
                async_session_factory,
                operation_id,
                owner="worker-early",
                lease_seconds=30,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            )
        for index in range(2, 5):
            operation = await execute_operation(
                async_session_factory,
                operation_id,
                owner=f"worker-{index}",
                lease_seconds=30,
                effect=ToolEffect.SIDE_EFFECT,
                invoke=_invoke,
                now=lambda index=index: (
                    datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index * 100)
                ),
            )
        assert operation.status is OperationStatus.FAILED
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            count = await connection.fetchval(
                "SELECT count(*) FROM operation_attempts WHERE operation_id = $1", operation_id
            )
            assert count == 4
        finally:
            await connection.close()
    finally:
        await _cleanup_run(run_id)


async def test_reconciliation_provider_reference_is_single_winner() -> None:
    run_id, operation_id = await _create_operation(
        "REF-LOOKUP", provider_reference_id="provider-ref-1"
    )
    calls = 0

    async def _query(lookup: ReconciliationLookup) -> ToolResult:
        nonlocal calls
        calls += 1
        assert lookup.provider_reference_id == "provider-ref-1"
        return ToolResult(
            ok=True, data={"status": "REFUNDED", "provider_reference": "provider-ref-1"}
        )

    try:
        results = await asyncio.gather(
            *[
                reconcile_operation(
                    async_session_factory,
                    operation_id,
                    owner=f"reconciler-{index}",
                    lease_seconds=30,
                    query=_query,
                )
                for index in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in results) == 1
        assert sum(isinstance(item, LeaseConflictError) for item in results) == 1
        assert calls == 1
    finally:
        await _cleanup_run(run_id)


async def test_reconciliation_final_event_failure_rolls_back_state_attempt_and_seq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, operation_id = await _create_operation("ATOMIC")
    real_append = reconciliation_module.append_event

    async def _fail_final(session, run_id_arg, event_type, payload) -> None:
        if event_type == "operation_reconciled_not_executed":
            raise RuntimeError("injected final event failure")
        await real_append(session, run_id_arg, event_type, payload)

    monkeypatch.setattr(reconciliation_module, "append_event", _fail_final)

    async def _query(lookup: ReconciliationLookup) -> ToolResult:
        return ToolResult(ok=True, data={"status": "NOT_REFUNDED"})

    try:
        with pytest.raises(RuntimeError, match="injected final"):
            await reconcile_operation(
                async_session_factory,
                operation_id,
                owner="reconciler-1",
                lease_seconds=30,
                query=_query,
            )
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            row = await connection.fetchrow(
                "SELECT status, version, claim_token FROM tool_operations WHERE id = $1",
                operation_id,
            )
            assert row is not None and row["status"] == "RECONCILING"
            assert row["version"] == 4 and row["claim_token"] is not None
            attempt = await connection.fetchrow(
                "SELECT status, response_payload, completed_at FROM operation_attempts "
                "WHERE operation_id = $1",
                operation_id,
            )
            assert attempt is not None and attempt["status"] == "RUNNING"
            assert attempt["response_payload"] is None and attempt["completed_at"] is None
            assert (
                await connection.fetchval("SELECT next_seq FROM agent_runs WHERE id = $1", run_id)
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id
                )
                == 1
            )
        finally:
            await connection.close()
    finally:
        await _cleanup_run(run_id)


async def test_execution_attempt_redacts_and_limits_request_response_and_error() -> None:
    run_id, operation_id = await _create_operation("SANITIZE", status="READY")
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "UPDATE tool_operations SET normalized_arguments = $2::jsonb WHERE id = $1",
            operation_id,
            '{"authorization":"Bearer top-secret","email":"alice@example.com",'
            '"phone":"13800138000"}',
        )
    finally:
        await connection.close()

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(
            ok=False,
            error="alice@example.com 13800138000 Bearer top-secret " + "x" * 2000,
            failure_kind=ProviderFailureKind.PERMANENT,
        )

    try:
        await execute_operation(
            async_session_factory,
            operation_id,
            owner="worker-1",
            lease_seconds=30,
            effect=ToolEffect.READ_ONLY,
            invoke=_invoke,
        )
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            attempt = await connection.fetchrow(
                "SELECT request_payload, error FROM operation_attempts WHERE operation_id = $1",
                operation_id,
            )
            assert attempt is not None
            serialized = str(attempt["request_payload"])
            assert "top-secret" not in serialized
            assert "alice@example.com" not in serialized
            assert "13800138000" not in serialized
            error = attempt["error"]
            assert "alice@example.com" not in error and "13800138000" not in error
            assert "top-secret" not in error and len(error) <= 1000
        finally:
            await connection.close()
    finally:
        await _cleanup_run(run_id)


@pytest.mark.parametrize(
    "kind",
    [
        ProviderFailureKind.CONNECTION_ERROR,
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.RETRYABLE_5XX,
    ],
)
async def test_read_only_retryable_failure_moves_operation_to_retrying(
    kind: ProviderFailureKind,
) -> None:
    run_id, operation_id = await _create_operation(uuid.uuid4().hex[:8], status="READY")

    async def _invoke(operation: object) -> ToolResult:
        return ToolResult(ok=False, error="retryable", failure_kind=kind)

    try:
        operation = await execute_operation(
            async_session_factory,
            operation_id,
            owner="worker-1",
            lease_seconds=30,
            effect=ToolEffect.READ_ONLY,
            invoke=_invoke,
        )
        assert operation.status is OperationStatus.RETRYING
        assert operation.retry_not_before is not None
    finally:
        await _cleanup_run(run_id)


async def test_expired_reconciliation_returns_to_unknown_without_provider_call() -> None:
    run_id, operation_id = await _create_operation("RECOVER")
    called = 0
    started = asyncio.Event()

    async def _query(lookup: ReconciliationLookup) -> ToolResult:
        nonlocal called
        called += 1
        started.set()
        await asyncio.Event().wait()
        return ToolResult(ok=True, data={"status": "REFUNDED"})

    task = asyncio.create_task(
        reconcile_operation(
            async_session_factory,
            operation_id,
            owner="reconciler-dead",
            lease_seconds=1,
            query=_query,
        )
    )
    try:
        await started.wait()
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute(
                "UPDATE tool_operations SET lease_expires_at = clock_timestamp() - interval '1s' "
                "WHERE id = $1",
                operation_id,
            )
        finally:
            await connection.close()
        async with async_session_factory() as session:
            recovered = await recover_expired_reconciliation(session, operation_id)
            await session.commit()
        assert recovered.status is OperationStatus.OUTCOME_UNKNOWN
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _cleanup_run(run_id)
