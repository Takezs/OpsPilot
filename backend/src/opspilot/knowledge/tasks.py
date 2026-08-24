import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings
from opspilot.db import engine
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider, EmbeddingProvider
from opspilot.knowledge.indexer import SqlAlchemyChunkSink, index_chunks
from opspilot.knowledge.models import Chunk, Document, DocumentIndexOutbox, DocumentStatus
from opspilot.knowledge.parsers.base import DocumentParser
from opspilot.knowledge.parsers.docx import DocxParser
from opspilot.knowledge.parsers.markdown import MarkdownParser
from opspilot.knowledge.parsers.pdf import PdfParser
from opspilot.knowledge.storage import FileStorage, VolumeFileStorage


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


async def index_document(ctx: dict[str, Any], document_id: str, retry_attempt: int = 0) -> None:
    """Index one immutable source document.

    Intermediate states are crash-recoverable and rebuild from scratch under the document lock.
    FAILED requires a persisted, audited retry intent matching ``retry_attempt``.
    """
    identifier = uuid.UUID(document_id)
    lock_statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:document_id, 0))")
    try:
        # The transaction-scoped lock is released by PostgreSQL on commit, rollback, connection
        # loss, or task cancellation. Keeping the rebuild atomic also prevents observers from
        # seeing partial chunks or intermediate ingestion states.
        async with engine.begin() as connection:
            await connection.execute(lock_statement, {"document_id": document_id})
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                document = await session.get(Document, identifier)
                if document is None:
                    raise ValueError("document not found")
                if document.status == DocumentStatus.READY:
                    return
                retry_intent: DocumentIndexOutbox | None = None
                if retry_attempt > 0:
                    retry_intent = await session.scalar(
                        select(DocumentIndexOutbox).where(
                            DocumentIndexOutbox.document_id == identifier,
                            DocumentIndexOutbox.attempt == retry_attempt,
                            DocumentIndexOutbox.requested_by.is_not(None),
                            DocumentIndexOutbox.completed_at.is_(None),
                        )
                    )
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
                    retry_intent.completed_at = datetime.now(UTC)
                await session.flush()
    except Exception as error:
        # The failed rebuild has already rolled back in full. Record the failure in a fresh,
        # fenced transaction without exposing partial chunks.
        async with engine.begin() as connection:
            await connection.execute(lock_statement, {"document_id": document_id})
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                document = await session.get(Document, identifier)
                if document is not None and document.status != DocumentStatus.READY:
                    document.status = DocumentStatus.FAILED
                    document.failure_reason = f"{type(error).__name__}: indexing failed"[:500]
                    await session.flush()
        raise
