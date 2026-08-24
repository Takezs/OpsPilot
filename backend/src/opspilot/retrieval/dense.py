"""Dense (vector) retrieval over pgvector with SQL-side scope filtering."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.knowledge.schemas import KnowledgeScope
from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource, scope_conditions

DEFAULT_DENSE_LIMIT = 30


async def dense_search(
    session: AsyncSession,
    query_vector: Sequence[float],
    scope: KnowledgeScope,
    limit: int = DEFAULT_DENSE_LIMIT,
) -> list[RetrievalCandidate]:
    """Return the nearest chunks to ``query_vector`` allowed by ``scope``.

    The scope predicate is applied in the candidate SQL (joined through the
    knowledge base) so unauthorized chunks are excluded before any result
    reaches Python. Distance uses the cosine operator backed by the HNSW index.
    Only chunks belonging to READY documents are candidates: a document in any
    other ingestion state (or FAILED) is not visible to retrieval.
    """
    distance = Chunk.embedding.cosine_distance(list(query_vector))
    statement = (
        select(Chunk.id, distance.label("distance"))
        .join(Document, Chunk.document_id == Document.id)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(Document.status == DocumentStatus.READY)
        .where(scope_conditions(scope))
        .order_by(distance, Chunk.id)
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        RetrievalCandidate(
            chunk_id=str(row[0]),
            score=1.0 - float(row[1]),
            rank=rank,
            source=RetrievalSource.DENSE,
        )
        for rank, row in enumerate(rows, start=1)
    ]
