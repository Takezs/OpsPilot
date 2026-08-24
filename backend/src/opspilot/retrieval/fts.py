"""PostgreSQL full-text search retrieval with SQL-side scope filtering."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.knowledge.schemas import KnowledgeScope
from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource, scope_conditions

DEFAULT_FTS_LIMIT = 30


async def fts_search(
    session: AsyncSession,
    query: str,
    scope: KnowledgeScope,
    limit: int = DEFAULT_FTS_LIMIT,
) -> list[RetrievalCandidate]:
    """Return chunks matching ``query`` via PostgreSQL FTS, scoped in SQL.

    Uses the same ``'simple'`` text search configuration as the persisted
    ``chunks.search_vector`` column. This is PostgreSQL FTS, not BM25.
    Only chunks belonging to READY documents are candidates; documents in any
    other ingestion state (or FAILED) are excluded in the candidate SQL.
    """
    tsquery = func.plainto_tsquery("simple", query)
    rank_expr = func.ts_rank(Chunk.search_vector, tsquery)
    statement = (
        select(Chunk.id, rank_expr.label("rank_score"))
        .join(Document, Chunk.document_id == Document.id)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(Chunk.search_vector.op("@@")(tsquery))
        .where(Document.status == DocumentStatus.READY)
        .where(scope_conditions(scope))
        .order_by(rank_expr.desc(), Chunk.id)
        .limit(limit)
    )
    rows = (await session.execute(statement)).all()
    return [
        RetrievalCandidate(
            chunk_id=str(row[0]),
            score=float(row[1]),
            rank=rank,
            source=RetrievalSource.FTS,
        )
        for rank, row in enumerate(rows, start=1)
    ]
