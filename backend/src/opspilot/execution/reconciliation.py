"""Fenced reconciliation of uncertain side-effect Operations (Task 11)."""

import secrets
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.attempts import finish_attempt, start_attempt
from opspilot.execution.claim import LeaseConflictError
from opspilot.execution.models import Operation, OperationAttempt, OperationStatus
from opspilot.jobs.service import enqueue_operation_job
from opspilot.runs.journal import append_event
from opspilot.runs.sanitize import sanitize_payload
from opspilot.tools.types import ToolResult

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class ReconciliationLookup:
    provider_reference_id: str | None
    idempotency_key: str
    order_number: str


ReconciliationQuery = Callable[[ReconciliationLookup], Coroutine[Any, Any, ToolResult]]


async def _database_now(session: AsyncSession) -> datetime:
    timestamp: datetime | None = await session.scalar(select(func.clock_timestamp()))
    if timestamp is None:  # pragma: no cover
        raise RuntimeError("PostgreSQL clock_timestamp returned NULL")
    return timestamp


async def _load(session: AsyncSession, operation_id: uuid.UUID) -> Operation:
    operation = await session.get(Operation, operation_id)
    if operation is None:
        raise LeaseConflictError(f"operation {operation_id} does not exist")
    return operation


async def _begin_reconciliation(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int,
) -> tuple[Operation, OperationAttempt, ReconciliationLookup]:
    operation = await _load(session, operation_id)
    expected_version = operation.version
    timestamp = await _database_now(session)
    token = secrets.token_urlsafe(32)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.OUTCOME_UNKNOWN,
            Operation.version == expected_version,
        )
        .values(
            status=OperationStatus.RECONCILING,
            version=Operation.version + 1,
            claim_token=token,
            lease_owner=owner,
            lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
        )
    )
    if (updated.rowcount or 0) != 1:  # type: ignore[attr-defined]
        raise LeaseConflictError(f"operation {operation_id} is not reconcilable")
    await session.refresh(operation)
    order_number = operation.normalized_arguments.get("order_number")
    if not isinstance(order_number, str) or not order_number:
        raise ValueError("refund operation lacks a normalized order_number")
    lookup = ReconciliationLookup(
        provider_reference_id=operation.provider_reference_id,
        idempotency_key=operation.idempotency_key,
        order_number=order_number,
    )
    attempt = await start_attempt(
        session,
        operation,
        kind="RECONCILIATION",
        request={
            "provider_reference_id": lookup.provider_reference_id,
            "idempotency_key": lookup.idempotency_key,
            "order_number": lookup.order_number,
        },
    )
    await append_event(
        session,
        operation.run_id,
        "operation_reconciliation_started",
        {"operation_id": str(operation.id), "attempt_number": attempt.attempt_number},
    )
    return operation, attempt, lookup


async def _finish_reconciliation(
    session: AsyncSession,
    operation_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    owner: str,
    token: str,
    expected_version: int,
    outcome: ToolResult,
) -> Operation:
    timestamp = await _database_now(session)
    target = OperationStatus.MANUAL_REVIEW
    event_type = "operation_manual_review_required"
    provider_reference: str | None = None
    result_payload: dict[str, object] | None = None
    if outcome.ok and outcome.data is not None:
        status = outcome.data.get("status")
        if status == "REFUNDED":
            target = OperationStatus.SUCCEEDED
            event_type = "operation_reconciled_succeeded"
            result_payload = sanitize_payload(outcome.data)
            reference = outcome.data.get("provider_reference")
            provider_reference = reference if isinstance(reference, str) else None
        elif status == "NOT_REFUNDED":
            target = OperationStatus.RETRYING
            event_type = "operation_reconciled_not_executed"

    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RECONCILING,
            Operation.version == expected_version,
            Operation.claim_token == token,
            Operation.lease_owner == owner,
            Operation.lease_expires_at > timestamp,
        )
        .values(
            status=target,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
            provider_reference_id=func.coalesce(
                provider_reference, Operation.provider_reference_id
            ),
            result_payload=result_payload,
        )
    )
    if (updated.rowcount or 0) != 1:  # type: ignore[attr-defined]
        raise LeaseConflictError(f"reconciliation write denied for operation {operation_id}")
    attempt = await session.get(OperationAttempt, attempt_id)
    if attempt is None:
        raise LeaseConflictError(f"reconciliation attempt {attempt_id} disappeared")
    finish_attempt(
        attempt,
        status="SUCCEEDED" if outcome.ok else "FAILED",
        response=outcome.data,
        error=outcome.error,
        completed_at=timestamp,
    )
    operation = await _load(session, operation_id)
    await append_event(
        session,
        operation.run_id,
        event_type,
        {
            "operation_id": str(operation_id),
            "attempt_number": attempt.attempt_number,
            "status": target.value,
            "error": outcome.error,
        },
    )
    if target is OperationStatus.RETRYING:
        enqueue_operation_job(session, operation_id, expected_version + 1, "EXECUTE")
    await session.refresh(operation)
    return operation


async def reconcile_operation(
    session_factory: SessionFactory,
    operation_id: uuid.UUID,
    *,
    owner: str,
    lease_seconds: int,
    query: ReconciliationQuery,
) -> Operation:
    """Claim OUTCOME_UNKNOWN, query Provider once, and atomically persist verdict."""
    async with session_factory() as session:
        operation, attempt, lookup = await _begin_reconciliation(
            session,
            operation_id,
            owner=owner,
            lease_seconds=lease_seconds,
        )
        await session.commit()
        token = operation.claim_token
        if token is None:
            raise LeaseConflictError("reconciliation claim has no token")
        version = operation.version
        attempt_id = attempt.id

    try:
        outcome = await query(lookup)
    except Exception as error:
        outcome = ToolResult(ok=False, error=str(error))

    async with session_factory() as session:
        operation = await _finish_reconciliation(
            session,
            operation_id,
            attempt_id,
            owner=owner,
            token=token,
            expected_version=version,
            outcome=outcome,
        )
        await session.commit()
        return operation


async def recover_expired_reconciliation(
    session: AsyncSession, operation_id: uuid.UUID
) -> Operation:
    """Return an expired reconciliation lease to OUTCOME_UNKNOWN without invoking Provider."""
    timestamp = await _database_now(session)
    operation = await _load(session, operation_id)
    updated = await session.execute(
        update(Operation)
        .where(
            Operation.id == operation_id,
            Operation.status == OperationStatus.RECONCILING,
            Operation.version == operation.version,
            Operation.lease_expires_at <= timestamp,
        )
        .values(
            status=OperationStatus.OUTCOME_UNKNOWN,
            version=Operation.version + 1,
            claim_token=None,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if (updated.rowcount or 0) != 1:  # type: ignore[attr-defined]
        raise LeaseConflictError(f"reconciliation {operation_id} is not expired")
    attempt = await session.scalar(
        select(OperationAttempt)
        .where(
            OperationAttempt.operation_id == operation_id,
            OperationAttempt.kind == "RECONCILIATION",
            OperationAttempt.status == "RUNNING",
        )
        .order_by(OperationAttempt.attempt_number.desc())
        .limit(1)
    )
    if attempt is not None:
        finish_attempt(
            attempt,
            status="FAILED",
            response=None,
            error="reconciliation lease expired",
            completed_at=timestamp,
        )
    await append_event(
        session,
        operation.run_id,
        "operation_reconciliation_lease_expired",
        {"operation_id": str(operation_id)},
    )
    recovered = await _load(session, operation_id)
    await session.refresh(recovered)
    return recovered
