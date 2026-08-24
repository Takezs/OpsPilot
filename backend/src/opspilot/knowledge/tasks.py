import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arq.worker import Retry
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings
from opspilot.db import engine
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider, EmbeddingProvider
from opspilot.knowledge.indexer import SqlAlchemyChunkSink, index_chunks
from opspilot.knowledge.models import (
    Chunk,
    Document,
    DocumentIndexOutbox,
    DocumentIndexStatus,
    DocumentStatus,
)
from opspilot.knowledge.parsers.base import DocumentParser
from opspilot.knowledge.parsers.docx import DocxParser
from opspilot.knowledge.parsers.markdown import MarkdownParser
from opspilot.knowledge.parsers.pdf import PdfParser
from opspilot.knowledge.storage import FileStorage, VolumeFileStorage

DOCUMENT_INDEX_MAX_TRIES = 3
DOCUMENT_INDEX_JOB_TIMEOUT = 300
DOCUMENT_INDEX_LEASE = timedelta(seconds=DOCUMENT_INDEX_JOB_TIMEOUT + 60)
DOCUMENT_INDEX_RETRY_DEFER_SECONDS = 10


def document_index_lock_key(document_id: uuid.UUID | str) -> str:
    return f"document-index:{document_id}"


def parser_for(path: Path) -> DocumentParser:
    parsers: dict[str, DocumentParser] = {
        ".md": MarkdownParser(),
        ".txt": MarkdownParser(),
        ".pdf": PdfParser(),
        ".docx": DocxParser(),
    }
    try:
        return parsers[path.suffix.lower()]
    except KeyError as error:
        raise ValueError("unsupported stored document") from error


async def _active_retry_intent(
    session: AsyncSession, document_id: uuid.UUID, retry_attempt: int
) -> DocumentIndexOutbox | None:
    result = await session.scalar(
        select(DocumentIndexOutbox).where(
            DocumentIndexOutbox.document_id == document_id,
            DocumentIndexOutbox.attempt == retry_attempt,
            DocumentIndexOutbox.requested_by.is_not(None),
            DocumentIndexOutbox.status.in_(
                [DocumentIndexStatus.QUEUED, DocumentIndexStatus.RUNNING]
            ),
        )
    )
    if result is None:
        return None
    if not isinstance(result, DocumentIndexOutbox):
        raise TypeError("retry intent query returned an unexpected model")
    return result


async def index_document(ctx: dict[str, Any], document_id: str, retry_attempt: int = 0) -> None:
    """Index one immutable source document.

    Intermediate states are crash-recoverable and rebuild from scratch under the document lock.
    FAILED requires a persisted, audited retry intent matching ``retry_attempt``.
    """
    identifier = uuid.UUID(document_id)
    lock_statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")
    lock_parameters = {"lock_key": document_index_lock_key(document_id)}
    if retry_attempt > 0:
        # Persist the claim before doing slow parsing/embedding work. If the process disappears,
        # the lease reconciler can observe and close this attempt.
        async with engine.begin() as connection:
            await connection.execute(lock_statement, lock_parameters)
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                claimed_intent = await _active_retry_intent(session, identifier, retry_attempt)
                if claimed_intent is None:
                    return
                document = await session.get(Document, identifier)
                if document is None:
                    return
                now = datetime.now(UTC)
                if document.status == DocumentStatus.READY:
                    claimed_intent.status = DocumentIndexStatus.SUCCEEDED
                    claimed_intent.completed_at = now
                    claimed_intent.lease_expires_at = None
                    await session.flush()
                    return
                claimed_intent.status = DocumentIndexStatus.RUNNING
                claimed_intent.started_at = claimed_intent.started_at or now
                claimed_intent.lease_expires_at = now + DOCUMENT_INDEX_LEASE
                await session.flush()
    try:
        # The transaction-scoped lock is released by PostgreSQL on commit, rollback, connection
        # loss, or task cancellation. Keeping the rebuild atomic also prevents observers from
        # seeing partial chunks or intermediate ingestion states.
        async with engine.begin() as connection:
            await connection.execute(lock_statement, lock_parameters)
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                document = await session.get(Document, identifier)
                if document is None:
                    if retry_attempt > 0:
                        return
                    raise ValueError("document not found")
                retry_intent: DocumentIndexOutbox | None = None
                if retry_attempt > 0:
                    retry_intent = await _active_retry_intent(session, identifier, retry_attempt)
                    if retry_intent is None:
                        return
                    now = datetime.now(UTC)
                    retry_intent.status = DocumentIndexStatus.RUNNING
                    retry_intent.started_at = retry_intent.started_at or now
                    retry_intent.lease_expires_at = now + DOCUMENT_INDEX_LEASE
                if document.status == DocumentStatus.READY:
                    if retry_intent is not None:
                        retry_intent.status = DocumentIndexStatus.SUCCEEDED
                        retry_intent.completed_at = datetime.now(UTC)
                        retry_intent.lease_expires_at = None
                        await session.flush()
                    return
                recoverable = {
                    DocumentStatus.UPLOADED,
                    DocumentStatus.PARSING,
                    DocumentStatus.CHUNKING,
                    DocumentStatus.INDEXING,
                }
                if document.status == DocumentStatus.FAILED and retry_intent is not None:
                    recoverable.add(DocumentStatus.FAILED)
                if document.status not in recoverable:
                    return
                await session.execute(delete(Chunk).where(Chunk.document_id == identifier))
                document.status = DocumentStatus.PARSING
                document.failure_reason = None
                await session.flush()
                settings = Settings()
                storage: FileStorage = ctx.get("file_storage") or VolumeFileStorage(
                    Path(settings.storage_root)
                )
                path = Path(document.storage_path)
                async with storage.open(document.storage_path) as stream:
                    blocks = parser_for(path).parse(stream)
                document.status = DocumentStatus.CHUNKING
                await session.flush()
                document.status = DocumentStatus.INDEXING
                await session.flush()
                provider: EmbeddingProvider = ctx.get(
                    "embedding_provider"
                ) or BgeM3EmbeddingProvider(
                    settings.bge_base_url,
                    settings.bge_api_key,
                    settings.bge_embedding_model,
                )
                await index_chunks(identifier, blocks, provider, SqlAlchemyChunkSink(session))
                document.status = DocumentStatus.READY
                document.failure_reason = None
                if retry_intent is not None:
                    retry_intent.status = DocumentIndexStatus.SUCCEEDED
                    retry_intent.completed_at = datetime.now(UTC)
                    retry_intent.lease_expires_at = None
                await session.flush()
    except Exception as error:
        # The failed rebuild has already rolled back in full. Record the failure in a fresh,
        # fenced transaction without exposing partial chunks.
        async with engine.begin() as connection:
            await connection.execute(lock_statement, lock_parameters)
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                document = await session.get(Document, identifier)
                if document is not None and document.status != DocumentStatus.READY:
                    document.status = DocumentStatus.FAILED
                    document.failure_reason = f"{type(error).__name__}: indexing failed"[:500]
                retry_intent = None
                if retry_attempt > 0:
                    retry_intent = await _active_retry_intent(session, identifier, retry_attempt)
                if retry_intent is not None:
                    now = datetime.now(UTC)
                    if int(ctx.get("job_try", 1)) < DOCUMENT_INDEX_MAX_TRIES:
                        retry_intent.status = DocumentIndexStatus.RUNNING
                        retry_intent.started_at = retry_intent.started_at or now
                        retry_intent.lease_expires_at = now + DOCUMENT_INDEX_LEASE
                    else:
                        retry_intent.status = DocumentIndexStatus.FAILED
                        retry_intent.completed_at = now
                        retry_intent.lease_expires_at = None
                await session.flush()
        if retry_attempt > 0 and int(ctx.get("job_try", 1)) < DOCUMENT_INDEX_MAX_TRIES:
            raise Retry(defer=DOCUMENT_INDEX_RETRY_DEFER_SECONDS) from error
        raise
