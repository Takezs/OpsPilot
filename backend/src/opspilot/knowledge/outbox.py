from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text

from opspilot.db import async_session_factory
from opspilot.knowledge.models import (
    Document,
    DocumentIndexOutbox,
    DocumentIndexStatus,
    DocumentStatus,
)
from opspilot.knowledge.tasks import document_index_lock_key


class DocumentJobQueue(Protocol):
    async def enqueue_document(
        self, document_id: str, job_id: str, retry_attempt: int = 0
    ) -> None: ...


async def publish_pending_document_jobs(queue: DocumentJobQueue, batch_size: int = 100) -> int:
    delivered = 0
    for _ in range(batch_size):
        async with async_session_factory() as session:
            row = await session.scalar(
                select(DocumentIndexOutbox)
                .where(
                    DocumentIndexOutbox.delivered_at.is_(None),
                    DocumentIndexOutbox.status == DocumentIndexStatus.QUEUED,
                )
                .order_by(DocumentIndexOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            row.attempts += 1
            try:
                await queue.enqueue_document(str(row.document_id), row.job_id, row.attempt)
            except Exception as error:
                row.last_error = f"{type(error).__name__}: enqueue failed"[:500]
                await session.commit()
                break
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            await session.commit()
            delivered += 1
    return delivered


async def reconcile_expired_retry_attempts(
    now: datetime | None = None, batch_size: int = 100
) -> int:
    cutoff = now or datetime.now(UTC)
    async with async_session_factory() as session:
        candidate_ids = list(
            await session.scalars(
                select(DocumentIndexOutbox.id)
                .where(
                    DocumentIndexOutbox.attempt > 0,
                    DocumentIndexOutbox.status.in_(
                        [DocumentIndexStatus.QUEUED, DocumentIndexStatus.RUNNING]
                    ),
                    DocumentIndexOutbox.lease_expires_at < cutoff,
                )
                .order_by(DocumentIndexOutbox.lease_expires_at)
                .limit(batch_size)
            )
        )
    reconciled = 0
    for candidate_id in candidate_ids:
        async with async_session_factory() as session:
            candidate = await session.get(DocumentIndexOutbox, candidate_id)
            if candidate is None:
                continue
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": document_index_lock_key(candidate.document_id)},
            )
            candidate = await session.scalar(
                select(DocumentIndexOutbox)
                .where(DocumentIndexOutbox.id == candidate_id)
                .with_for_update()
            )
            if (
                candidate is None
                or candidate.status not in {DocumentIndexStatus.QUEUED, DocumentIndexStatus.RUNNING}
                or candidate.lease_expires_at is None
                or candidate.lease_expires_at >= cutoff
            ):
                continue
            document = await session.get(Document, candidate.document_id)
            candidate.completed_at = cutoff
            candidate.lease_expires_at = None
            if document is not None and document.status == DocumentStatus.READY:
                candidate.status = DocumentIndexStatus.SUCCEEDED
            else:
                candidate.status = DocumentIndexStatus.FAILED
                if document is not None:
                    document.status = DocumentStatus.FAILED
                    document.failure_reason = "RetryLeaseExpired: indexing attempt expired"
            await session.commit()
            reconciled += 1
    return reconciled
