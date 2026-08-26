"""Sanitized Operation attempt persistence inside caller-owned transactions."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.models import Operation, OperationAttempt
from opspilot.runs.sanitize import sanitize_payload, sanitize_value


async def start_attempt(
    session: AsyncSession,
    operation: Operation,
    *,
    kind: str,
    request: dict[str, object],
) -> OperationAttempt:
    number = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(OperationAttempt.attempt_number), 0)).where(
                    OperationAttempt.operation_id == operation.id
                )
            )
            or 0
        )
        + 1
    )
    attempt = OperationAttempt(
        id=uuid.uuid4(),
        operation_id=operation.id,
        attempt_number=number,
        kind=kind,
        status="RUNNING",
        request_payload=sanitize_payload(request),
    )
    session.add(attempt)
    await session.flush()
    return attempt


def finish_attempt(
    attempt: OperationAttempt,
    *,
    status: str,
    response: dict[str, object] | None,
    error: str | None,
    completed_at: datetime,
) -> None:
    attempt.status = status
    attempt.response_payload = sanitize_payload(response) if response is not None else None
    sanitized_error = sanitize_value(error) if error is not None else None
    attempt.error = sanitized_error if isinstance(sanitized_error, str) else None
    attempt.completed_at = completed_at
