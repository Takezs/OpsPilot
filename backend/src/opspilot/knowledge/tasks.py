import uuid
from pathlib import Path
from typing import Any

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider, EmbeddingProvider
from opspilot.knowledge.indexer import SqlAlchemyChunkSink, index_chunks
from opspilot.knowledge.models import Document, DocumentStatus
from opspilot.knowledge.parsers.base import DocumentParser
from opspilot.knowledge.parsers.docx import DocxParser
from opspilot.knowledge.parsers.markdown import MarkdownParser
from opspilot.knowledge.parsers.pdf import PdfParser


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


async def index_document(ctx: dict[str, Any], document_id: str) -> None:
    """ARQ entry point; the queue payload contains only document_id."""
    identifier = uuid.UUID(document_id)
    async with async_session_factory() as session:
        document = await session.get(Document, identifier)
        if document is None:
            raise ValueError("document not found")
        try:
            document.status = DocumentStatus.PARSING
            await session.commit()
            path = Path(document.storage_path)
            blocks = parser_for(path).parse(path)
            document.status = DocumentStatus.CHUNKING
            await session.commit()
            document.status = DocumentStatus.INDEXING
            await session.commit()
            settings = Settings()
            provider: EmbeddingProvider = ctx.get("embedding_provider") or BgeM3EmbeddingProvider(
                settings.bge_base_url,
                settings.bge_api_key,
                settings.bge_embedding_model,
            )
            await index_chunks(identifier, blocks, provider, SqlAlchemyChunkSink(session))
            document.status = DocumentStatus.READY
            document.failure_reason = None
            await session.commit()
        except Exception as error:
            await session.rollback()
            document.status = DocumentStatus.FAILED
            document.failure_reason = f"{type(error).__name__}: indexing failed"[:500]
            await session.commit()
            raise
