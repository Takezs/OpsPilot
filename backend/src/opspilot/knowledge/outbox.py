from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select

from opspilot.db import async_session_factory
from opspilot.knowledge.models import DocumentIndexOutbox


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
                .where(DocumentIndexOutbox.delivered_at.is_(None))
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
