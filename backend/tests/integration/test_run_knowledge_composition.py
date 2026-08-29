import uuid

import pytest
from sqlalchemy import delete

from opspilot.agent.knowledge import RunKnowledgeAccessError, RunKnowledgeSearch
from opspilot.auth.models import Role, User
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.retrieval.reranker import DeterministicReranker
from opspilot.runs.models import Run
from opspilot.tools.schemas import SearchKnowledgeArgs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_knowledge_uses_active_database_owner_scope() -> None:
    provider = DeterministicEmbeddingProvider()
    embedding = (await provider.embed(["refund policy evidence"]))[0]
    owner_id = uuid.uuid4()
    owner = User(
        id=owner_id,
        username=f"run-search-{uuid.uuid4()}",
        password_hash="not-used",
        role=Role.USER,
        allowed_departments=["support"],
        max_access_level=1,
        is_active=True,
    )
    run = Run(owner_user_id=owner_id)
    visible_kb = KnowledgeBase(name=f"visible-{uuid.uuid4()}", department="support", access_level=1)
    hidden_kb = KnowledgeBase(name=f"hidden-{uuid.uuid4()}", department="finance", access_level=2)

    def add_chunk(kb: KnowledgeBase, title: str, status: DocumentStatus) -> Chunk:
        document = Document(
            title=title,
            version=4,
            content_sha256=uuid.uuid4().hex,
            storage_path=f"{title}.md",
            status=status,
        )
        chunk = Chunk(
            position=0,
            content="refund policy evidence",
            section_path=[title],
            token_count=13,
            page=1,
            embedding=embedding,
            embedding_cache_key=f"det:{uuid.uuid4().hex}",
        )
        document.chunks.append(chunk)
        kb.documents.append(document)
        return chunk

    visible = add_chunk(visible_kb, "visible", DocumentStatus.READY)
    add_chunk(visible_kb, "pending", DocumentStatus.INDEXING)
    add_chunk(hidden_kb, "secret", DocumentStatus.READY)
    async with async_session_factory() as session:
        session.add_all([owner, run, visible_kb, hidden_kb])
        await session.commit()
    try:
        search = RunKnowledgeSearch(
            async_session_factory,
            run.id,
            provider,
            DeterministicReranker(),
            context_token_budget=100,
        )
        result = await search(SearchKnowledgeArgs(query="refund policy", top_k=10))
        assert [item.chunk_id for item in result.context.fragments] == [str(visible.id)]
        assert result.summary.data is not None
        assert result.summary.data["result_count"] == 1
        assert "secret" not in result.summary.render()
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(Run).where(Run.id == run.id))
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id.in_([visible_kb.id, hidden_kb.id]))
            )
            await session.execute(delete(User).where(User.id == owner.id))
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_knowledge_rejects_legacy_inactive_and_missing_identity() -> None:
    inactive_id = uuid.uuid4()
    inactive = User(
        id=inactive_id,
        username=f"inactive-search-{uuid.uuid4()}",
        password_hash="not-used",
        role=Role.ADMIN,
        allowed_departments=["*"],
        max_access_level=2,
        is_active=False,
    )
    inactive_run = Run(owner_user_id=inactive_id)
    legacy_run = Run(owner_user_id=None)
    async with async_session_factory() as session:
        session.add_all([inactive, inactive_run, legacy_run])
        await session.commit()
    provider = DeterministicEmbeddingProvider()
    try:
        for run_id in (inactive_run.id, legacy_run.id, uuid.uuid4()):
            search = RunKnowledgeSearch(
                async_session_factory,
                run_id,
                provider,
                DeterministicReranker(),
                context_token_budget=100,
            )
            with pytest.raises(RunKnowledgeAccessError):
                await search(SearchKnowledgeArgs(query="refund", top_k=5))
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(Run).where(Run.id.in_([inactive_run.id, legacy_run.id])))
            await session.execute(delete(User).where(User.id == inactive.id))
            await session.commit()
