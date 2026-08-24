import uuid

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, KnowledgeBase
from opspilot.knowledge.schemas import AccessLevel, KnowledgeScope
from opspilot.retrieval.dense import dense_search
from opspilot.retrieval.fts import fts_search
from opspilot.retrieval.service import RetrievalService
from opspilot.retrieval.types import RetrievalCandidate

QUERY = "refund policy"


async def _embed(provider: DeterministicEmbeddingProvider, texts: list[str]) -> list[list[float]]:
    return await provider.embed(texts)


async def seed_scope_fixture() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    finance_content = "confidential refund policy for finance team only"
    support_content = "refund policy for support customers"
    finance_embedding, support_embedding = await _embed(
        provider, [finance_content, support_content]
    )

    finance_kb = KnowledgeBase(
        name=f"finance-kb-{uuid.uuid4()}",
        department="finance",
        access_level=int(AccessLevel.CONFIDENTIAL),
    )
    support_kb = KnowledgeBase(
        name=f"support-kb-{uuid.uuid4()}",
        department="support",
        access_level=int(AccessLevel.INTERNAL),
    )
    finance_doc = Document(
        title="finance policy",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="/tmp/finance.md",
    )
    support_doc = Document(
        title="support policy",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="/tmp/support.md",
    )
    finance_chunk = Chunk(
        position=0,
        content=finance_content,
        section_path=[],
        token_count=10,
        page=1,
        embedding=finance_embedding,
        embedding_cache_key=f"det:{uuid.uuid4().hex}",
    )
    support_chunk = Chunk(
        position=0,
        content=support_content,
        section_path=[],
        token_count=10,
        page=1,
        embedding=support_embedding,
        embedding_cache_key=f"det:{uuid.uuid4().hex}",
    )
    finance_kb.documents.append(finance_doc)
    support_kb.documents.append(support_doc)
    finance_doc.chunks.append(finance_chunk)
    support_doc.chunks.append(support_chunk)

    async with async_session_factory() as session:
        session.add_all([finance_kb, support_kb])
        await session.commit()
    return finance_chunk.id, support_chunk.id, finance_kb.id, support_kb.id


async def cleanup_knowledge_bases(kb_ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "DELETE FROM knowledge_bases WHERE id IN ($1, $2)", kb_ids[0], kb_ids[1]
        )
    finally:
        await connection.close()


def chunk_ids(candidates: list[RetrievalCandidate]) -> set[str]:
    return {candidate.chunk_id for candidate in candidates}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_scope_excludes_forbidden_chunks_at_candidate_sql() -> None:
    finance_chunk_id, support_chunk_id, finance_kb_id, support_kb_id = await seed_scope_fixture()
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    scope = KnowledgeScope(frozenset({"support"}), AccessLevel.INTERNAL)
    query_vector = (await _embed(provider, [QUERY]))[0]
    try:
        async with async_session_factory() as session:
            dense = await dense_search(session, query_vector, scope)
            fts = await fts_search(session, QUERY, scope)
            result = await RetrievalService(session, provider).search(QUERY, scope)

        support = str(support_chunk_id)
        forbidden = str(finance_chunk_id)

        assert support in chunk_ids(dense)
        assert forbidden not in chunk_ids(dense)
        assert support in chunk_ids(fts)
        assert forbidden not in chunk_ids(fts)
        assert support in chunk_ids(result.dense)
        assert forbidden not in chunk_ids(result.fts)
        assert support in chunk_ids(result.rrf)
        assert forbidden not in chunk_ids(result.rrf)
        # The forbidden chunk genuinely contains the query term, so its absence
        # proves filtering happens in the candidate SQL, not in Python.
    finally:
        await cleanup_knowledge_bases((finance_kb_id, support_kb_id))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_elevated_scope_returns_confidential_chunks() -> None:
    finance_chunk_id, support_chunk_id, finance_kb_id, support_kb_id = await seed_scope_fixture()
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    scope = KnowledgeScope(frozenset({"finance", "support"}), AccessLevel.CONFIDENTIAL)
    query_vector = (await _embed(provider, [QUERY]))[0]
    try:
        async with async_session_factory() as session:
            dense = await dense_search(session, query_vector, scope)
            fts = await fts_search(session, QUERY, scope)

        assert str(finance_chunk_id) in chunk_ids(dense)
        assert str(finance_chunk_id) in chunk_ids(fts)
        assert str(support_chunk_id) in chunk_ids(dense)
        assert str(support_chunk_id) in chunk_ids(fts)
    finally:
        await cleanup_knowledge_bases((finance_kb_id, support_kb_id))
