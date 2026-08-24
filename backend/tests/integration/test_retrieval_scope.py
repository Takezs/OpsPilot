import uuid
from collections.abc import Sequence

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
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
        status=DocumentStatus.READY,
    )
    support_doc = Document(
        title="support policy",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="/tmp/support.md",
        status=DocumentStatus.READY,
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


async def cleanup_knowledge_bases(kb_ids: Sequence[uuid.UUID]) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if kb_ids:
            await connection.execute(
                "DELETE FROM knowledge_bases WHERE id = ANY($1::uuid[])", list(kb_ids)
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


ALL_STATUSES = [
    DocumentStatus.UPLOADED,
    DocumentStatus.PARSING,
    DocumentStatus.CHUNKING,
    DocumentStatus.INDEXING,
    DocumentStatus.READY,
    DocumentStatus.FAILED,
]


async def seed_status_fixture() -> tuple[uuid.UUID, uuid.UUID, set[uuid.UUID]]:
    """Seed one support KB with six documents (one per status), each holding one chunk.

    Every chunk carries the query term so the SQL ``Document.status == READY``
    predicate is what excludes non-READY documents from candidate results.
    """
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    content = "refund policy for status verification"
    embedding = (await _embed(provider, [content]))[0]
    knowledge_base = KnowledgeBase(
        name=f"status-kb-{uuid.uuid4()}",
        department="support",
        access_level=int(AccessLevel.INTERNAL),
    )
    ready_chunk_id = uuid.uuid4()
    non_ready_chunk_ids: set[uuid.UUID] = set()
    for position, status in enumerate(ALL_STATUSES):
        chunk_id = ready_chunk_id if status is DocumentStatus.READY else uuid.uuid4()
        if status is not DocumentStatus.READY:
            non_ready_chunk_ids.add(chunk_id)
        document = Document(
            id=uuid.uuid4(),
            title=f"status-doc-{status.value.lower()}",
            version=1,
            content_sha256=uuid.uuid4().hex,
            storage_path="/tmp/status.md",
            status=status,
        )
        chunk = Chunk(
            id=chunk_id,
            position=position,
            content=content,
            section_path=[],
            token_count=10,
            page=1,
            embedding=embedding,
            embedding_cache_key=f"det:{uuid.uuid4().hex}",
        )
        document.chunks.append(chunk)
        knowledge_base.documents.append(document)

    async with async_session_factory() as session:
        session.add(knowledge_base)
        await session.commit()
    return knowledge_base.id, ready_chunk_id, non_ready_chunk_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_only_ready_documents_are_retrievable() -> None:
    knowledge_base_id, ready_chunk_id, non_ready_chunk_ids = await seed_status_fixture()
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    scope = KnowledgeScope(frozenset({"support"}), AccessLevel.INTERNAL)
    query_vector = (await _embed(provider, [QUERY]))[0]
    try:
        async with async_session_factory() as session:
            dense = await dense_search(session, query_vector, scope)
            fts = await fts_search(session, QUERY, scope)

        assert str(ready_chunk_id) in chunk_ids(dense)
        assert str(ready_chunk_id) in chunk_ids(fts)
        assert not (chunk_ids(dense) & {str(cid) for cid in non_ready_chunk_ids})
        assert not (chunk_ids(fts) & {str(cid) for cid in non_ready_chunk_ids})
    finally:
        await cleanup_knowledge_bases([knowledge_base_id])


async def seed_tie_fixture() -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Seed two READY documents with byte-identical chunks so Dense/FTS scores tie."""
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    content = "exact duplicate refund policy text"
    embedding = (await _embed(provider, [content]))[0]
    chunk_ids = [uuid.uuid4(), uuid.uuid4()]
    knowledge_base = KnowledgeBase(
        name=f"tie-kb-{uuid.uuid4()}",
        department="support",
        access_level=int(AccessLevel.INTERNAL),
    )
    for position, chunk_id in enumerate(chunk_ids):
        document = Document(
            id=uuid.uuid4(),
            title=f"tie-doc-{position}",
            version=1,
            content_sha256=uuid.uuid4().hex,
            storage_path="/tmp/tie.md",
            status=DocumentStatus.READY,
        )
        chunk = Chunk(
            id=chunk_id,
            position=0,
            content=content,
            section_path=[],
            token_count=10,
            page=1,
            embedding=embedding,
            embedding_cache_key=f"det:{uuid.uuid4().hex}",
        )
        document.chunks.append(chunk)
        knowledge_base.documents.append(document)

    async with async_session_factory() as session:
        session.add(knowledge_base)
        await session.commit()
        knowledge_base_id = knowledge_base.id
    return knowledge_base_id, chunk_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tied_scores_order_stably_by_chunk_id() -> None:
    knowledge_base_id, chunk_ids = await seed_tie_fixture()
    provider = DeterministicEmbeddingProvider(dimensions=1024)
    scope = KnowledgeScope(frozenset({"support"}), AccessLevel.INTERNAL)
    query_vector = (await _embed(provider, ["exact duplicate refund policy text"]))[0]
    expected = [str(chunk_id) for chunk_id in sorted(chunk_ids)]
    try:
        async with async_session_factory() as session:
            dense = await dense_search(session, query_vector, scope)
            fts = await fts_search(session, "exact duplicate refund policy text", scope)

        assert [candidate.chunk_id for candidate in dense] == expected
        assert [candidate.chunk_id for candidate in fts] == expected
    finally:
        await cleanup_knowledge_bases([knowledge_base_id])
