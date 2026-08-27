"""Orchestrates Dense + PostgreSQL FTS retrieval and fuses them via RRF."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.embedding import EmbeddingProvider
from opspilot.knowledge.schemas import KnowledgeScope
from opspilot.retrieval.dense import DEFAULT_DENSE_LIMIT, dense_search
from opspilot.retrieval.fts import DEFAULT_FTS_LIMIT, fts_search
from opspilot.retrieval.fusion import DEFAULT_FUSION_LIMIT, reciprocal_rank_fusion
from opspilot.retrieval.types import RetrievalResult


class RetrievalService:
    def __init__(self, session: AsyncSession, embedding_provider: EmbeddingProvider) -> None:
        self.session = session
        self.embedding_provider = embedding_provider

    async def search(
        self,
        query: str,
        scope: KnowledgeScope,
        dense_limit: int = DEFAULT_DENSE_LIMIT,
        fts_limit: int = DEFAULT_FTS_LIMIT,
        fusion_limit: int = DEFAULT_FUSION_LIMIT,
        knowledge_base_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        """Run the Dense and FTS legs, then fuse them into a debug response.

        The result keeps each stage's ranking separate (Dense, FTS, RRF) and
        reports PostgreSQL FTS as ``fts``, never as BM25.
        """
        query_vector = (await self.embedding_provider.embed([query]))[0]
        dense = await dense_search(
            self.session, query_vector, scope, dense_limit, knowledge_base_id
        )
        fts = await fts_search(self.session, query, scope, fts_limit, knowledge_base_id)
        rrf = reciprocal_rank_fusion(
            [
                [candidate.chunk_id for candidate in dense],
                [candidate.chunk_id for candidate in fts],
            ],
            limit=fusion_limit,
        )
        return RetrievalResult(dense=dense, fts=fts, rrf=rrf)
