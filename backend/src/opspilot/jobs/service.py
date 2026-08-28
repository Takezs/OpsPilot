import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.jobs.models import OperationJobOutbox, RunJobOutbox


def enqueue_run_job(session: AsyncSession, message_id: uuid.UUID) -> None:
    session.add(RunJobOutbox(message_id=message_id))


def enqueue_operation_job(
    session: AsyncSession,
    operation_id: uuid.UUID,
    expected_version: int,
    kind: str,
    *,
    available_at: datetime | None = None,
) -> None:
    values: dict[str, object] = {
        "operation_id": operation_id,
        "expected_version": expected_version,
        "kind": kind,
    }
    if available_at is not None:
        values["available_at"] = available_at
    session.add(OperationJobOutbox(**values))
