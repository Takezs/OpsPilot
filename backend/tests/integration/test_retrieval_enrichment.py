import uuid

import pytest
from sqlalchemy import delete

from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.knowledge.schemas import AccessLevel, KnowledgeScope
from opspilot.retrieval.enrichment import context_fragments, load_candidate_metadata
from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource


def candidate(chunk_id: uuid.UUID, rank: int) -> RetrievalCandidate:
    return RetrievalCandidate(str(chunk_id), 1.0 / rank, rank, RetrievalSource.RRF)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_filters_scope_ready_and_optional_knowledge_base_in_sql() -> None:
    provider = DeterministicEmbeddingProvider()
    embedding = (await provider.embed(["refund evidence"]))[0]
    department = f"enrichment-{uuid.uuid4()}"
    included_kb = KnowledgeBase(
        name=f"included-{uuid.uuid4()}", department=department, access_level=1
    )
    other_kb = KnowledgeBase(name=f"other-{uuid.uuid4()}", department=department, access_level=1)
    forbidden_kb = KnowledgeBase(
        name=f"forbidden-{uuid.uuid4()}", department="finance", access_level=2
    )

    def document(kb: KnowledgeBase, title: str, status: DocumentStatus, tokens: int) -> Chunk:
        row = Document(
            title=title,
            version=tokens,
            content_sha256=uuid.uuid4().hex,
            storage_path=f"{title}.md",
            status=status,
        )
        chunk = Chunk(
            position=0,
            content=f"{title} evidence",
            section_path=["Refund", title],
            token_count=tokens,
            page=tokens,
            embedding=embedding,
            embedding_cache_key=f"det:{uuid.uuid4().hex}",
        )
        row.chunks.append(chunk)
        kb.documents.append(row)
        return chunk

    allowed = document(included_kb, "allowed", DocumentStatus.READY, 37)
    wrong_kb = document(other_kb, "wrong-kb", DocumentStatus.READY, 23)
    nonready = document(included_kb, "nonready", DocumentStatus.INDEXING, 19)
    forbidden = document(forbidden_kb, "forbidden", DocumentStatus.READY, 17)
    kb_ids = [included_kb.id, other_kb.id, forbidden_kb.id]
    async with async_session_factory() as session:
        session.add_all([included_kb, other_kb, forbidden_kb])
        await session.commit()
        kb_ids = [included_kb.id, other_kb.id, forbidden_kb.id]

    candidates = [
        candidate(allowed.id, 1),
        candidate(wrong_kb.id, 2),
        candidate(nonready.id, 3),
        candidate(forbidden.id, 4),
        candidate(uuid.uuid4(), 5),
    ]
    scope = KnowledgeScope(frozenset({department}), AccessLevel.INTERNAL)
    try:
        async with async_session_factory() as session:
            metadata = await load_candidate_metadata(
                session, candidates, scope, knowledge_base_id=included_kb.id
            )
        assert list(metadata) == [str(allowed.id)]
        item = metadata[str(allowed.id)]
        assert item.document_id == allowed.document_id
        assert item.document_version == 37
        assert item.token_count == 37

        fragments = context_fragments(candidates, metadata)
        assert [fragment.chunk_id for fragment in fragments] == [str(allowed.id)]
        assert fragments[0].token_count == 37
        assert fragments[0].content == "allowed evidence"
        assert fragments[0].section_path == ("Refund", "allowed")
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
            await session.commit()
