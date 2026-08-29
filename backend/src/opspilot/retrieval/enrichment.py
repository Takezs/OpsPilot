"""Permission-scoped metadata enrichment for retrieval candidates.

Candidate identifiers are untrusted inputs at this boundary.  The database
query repeats the READY and KnowledgeScope predicates used by both retrieval
legs so stale, forged, or no-longer-visible candidates never become context.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.knowledge.schemas import KnowledgeScope
from opspilot.retrieval.context_builder import ContextFragment
from opspilot.retrieval.types import RetrievalCandidate, scope_conditions


@dataclass(frozen=True)
class CandidateMetadata:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int
    document_title: str
    section_path: tuple[str, ...]
    page: int | None
    content: str
    token_count: int
    effective_at: datetime


async def load_candidate_metadata(
    session: AsyncSession,
    candidates: list[RetrievalCandidate],
    scope: KnowledgeScope,
    knowledge_base_id: uuid.UUID | None = None,
) -> dict[str, CandidateMetadata]:
    """Load visible READY candidate metadata in a single scoped SQL query."""
    ids: list[uuid.UUID] = []
    for candidate in candidates:
        try:
            ids.append(uuid.UUID(candidate.chunk_id))
        except (ValueError, AttributeError):
            continue
    if not ids:
        return {}

    statement = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(Chunk.id.in_(ids), Document.status == DocumentStatus.READY)
        .where(scope_conditions(scope))
    )
    if knowledge_base_id is not None:
        statement = statement.where(Document.knowledge_base_id == knowledge_base_id)

    return {
        str(chunk.id): CandidateMetadata(
            chunk_id=chunk.id,
            document_id=document.id,
            document_version=document.version,
            document_title=document.title,
            section_path=tuple(chunk.section_path),
            page=chunk.page,
            content=chunk.content,
            token_count=chunk.token_count,
            effective_at=document.effective_at,
        )
        for chunk, document in await session.execute(statement)
    }


def context_fragments(
    candidates: list[RetrievalCandidate], metadata: dict[str, CandidateMetadata]
) -> list[ContextFragment]:
    """Map enriched candidates to Task 6 fragments in candidate rank order."""
    result: list[ContextFragment] = []
    for candidate in candidates:
        item = metadata.get(candidate.chunk_id)
        if item is None:
            continue
        result.append(
            ContextFragment(
                document_id=str(item.document_id),
                chunk_id=str(item.chunk_id),
                title=item.document_title,
                document_version=item.document_version,
                section_path=item.section_path,
                effective_at=item.effective_at,
                page=item.page,
                content=item.content,
                token_count=item.token_count,
            )
        )
    return result
