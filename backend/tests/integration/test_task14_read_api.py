import uuid

import httpx
import pytest
from sqlalchemy import delete

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app
from opspilot.retrieval.reranker import DeterministicReranker, RerankItem
from opspilot.retrieval.router import get_embedding_provider, get_reranker_provider


def principal(departments: frozenset[str] = frozenset({"support"})) -> Principal:
    return Principal(
        user_id=str(uuid.uuid4()),
        role=Role.USER,
        allowed_departments=departments,
        max_access_level=AccessLevel.INTERNAL,
    )


async def seed_catalog() -> tuple[KnowledgeBase, KnowledgeBase, Document, Document, Chunk]:
    embedding = (await DeterministicEmbeddingProvider().embed(["refund policy snapshot v1"]))[0]
    department = f"support-{uuid.uuid4()}"
    visible = KnowledgeBase(name=department, department=department, access_level=1)
    hidden = KnowledgeBase(name=f"finance-{uuid.uuid4()}", department="finance", access_level=2)
    old = Document(
        title="Refund guide",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="/private/token-file.md",
        status=DocumentStatus.FAILED,
        failure_reason="Bearer secret-token /private/a.txt alice@example.com 13800138000",
    )
    current = Document(
        title="Refund guide",
        version=2,
        content_sha256=uuid.uuid4().hex,
        storage_path="/private/new.md",
        status=DocumentStatus.READY,
    )
    chunk = Chunk(
        position=0,
        content="refund policy snapshot v1",
        section_path=["Policy", "Refund"],
        token_count=5,
        page=7,
        embedding=embedding,
        embedding_cache_key=f"det:{uuid.uuid4().hex}",
    )
    old.chunks.append(chunk)
    visible.documents.extend([old, current])
    hidden.documents.append(
        Document(
            title="Secret",
            version=1,
            content_sha256=uuid.uuid4().hex,
            storage_path="/private/secret.md",
            status=DocumentStatus.READY,
        )
    )
    async with async_session_factory() as session:
        session.add_all([visible, hidden])
        await session.commit()
    return visible, hidden, old, current, chunk


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scoped_catalog_status_and_failure_message() -> None:
    visible, hidden, old, current, _ = await seed_catalog()
    app.dependency_overrides[get_current_principal] = lambda: principal(
        frozenset({visible.department})
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/knowledge-bases?limit=1")
            assert response.status_code == 200
            assert [row["id"] for row in response.json()] == [str(visible.id)]

            response = await client.get(f"/api/v1/knowledge-bases/{visible.id}/documents?limit=10")
            assert response.status_code == 200
            rows = response.json()
            assert [(row["version"], row["status"]) for row in rows] == [
                (2, "READY"),
                (1, "FAILED"),
            ]
            failed = rows[1]
            assert failed["failure_message"] == "Document processing failed"
            serialized = response.text
            for secret in ("secret-token", "/private", "alice@example.com", "13800138000"):
                assert secret not in serialized

            response = await client.get(f"/api/v1/knowledge/documents/{current.id}")
            assert response.status_code == 200
            assert response.json()["status"] == "READY"

            for path in (
                f"/api/v1/knowledge-bases/{hidden.id}/documents",
                f"/api/v1/knowledge/documents/{hidden.documents[0].id}",
                f"/api/v1/knowledge/documents/{uuid.uuid4()}",
            ):
                assert (await client.get(path)).status_code == 404
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id.in_([visible.id, hidden.id]))
            )
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_citation_detail_requires_exact_version_chunk_and_scope() -> None:
    visible, hidden, old, _, chunk = await seed_catalog()
    app.dependency_overrides[get_current_principal] = lambda: principal(
        frozenset({visible.department})
    )
    exact = f"/api/v1/knowledge/documents/{old.id}/versions/{old.version}/chunks/{chunk.id}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(exact)
            assert response.status_code == 200
            assert response.json() | {} == {
                "document_id": str(old.id),
                "document_version": 1,
                "chunk_id": str(chunk.id),
                "document_title": "Refund guide",
                "section_path": ["Policy", "Refund"],
                "page": 7,
                "content": "refund policy snapshot v1",
                "effective_at": response.json()["effective_at"],
            }
            for path in (
                exact.replace("/versions/1/", "/versions/2/"),
                exact.replace(str(chunk.id), str(uuid.uuid4())),
                exact.replace(str(old.id), str(hidden.documents[0].id)),
            ):
                assert (await client.get(path)).status_code == 404
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id.in_([visible.id, hidden.id]))
            )
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_debug_exposes_four_real_stages() -> None:
    provider = DeterministicEmbeddingProvider()
    department = f"retrieval-{uuid.uuid4()}"
    visible = KnowledgeBase(name=department, department=department, access_level=1)
    hidden = KnowledgeBase(name=f"hidden-{uuid.uuid4()}", department="finance", access_level=2)
    ready = Document(
        title="Visible ready",
        version=3,
        content_sha256=uuid.uuid4().hex,
        storage_path="safe.md",
        status=DocumentStatus.READY,
    )
    nonready = Document(
        title="Visible parsing",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="pending.md",
        status=DocumentStatus.PARSING,
    )
    secret = Document(
        title="Hidden ready",
        version=1,
        content_sha256=uuid.uuid4().hex,
        storage_path="secret.md",
        status=DocumentStatus.READY,
    )
    contents = ["refund policy visible", "refund policy pending", "refund policy secret"]
    embeddings = await provider.embed(contents)
    chunks = [
        Chunk(
            position=0,
            content=content,
            section_path=["Refund"],
            token_count=5,
            page=index + 1,
            embedding=embedding,
            embedding_cache_key=f"det:{uuid.uuid4().hex}",
        )
        for index, (content, embedding) in enumerate(zip(contents, embeddings, strict=True))
    ]
    ready.chunks.append(chunks[0])
    nonready.chunks.append(chunks[1])
    secret.chunks.append(chunks[2])
    visible.documents.extend([ready, nonready])
    hidden.documents.append(secret)
    async with async_session_factory() as session:
        session.add_all([visible, hidden])
        await session.commit()

    app.dependency_overrides[get_current_principal] = lambda: principal(frozenset({department}))
    app.dependency_overrides[get_embedding_provider] = lambda: provider
    app.dependency_overrides[get_reranker_provider] = lambda: DeterministicReranker()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/retrieval/debug", json={"query": "refund policy", "top_k": 5}
            )
            assert response.status_code == 200
            assert set(response.json()) == {
                "dense",
                "fts",
                "rrf",
                "reranker",
                "reranker_status",
            }
            data = response.json()
            assert data["reranker_status"] == "ok"
            for stage in ("dense", "fts", "rrf", "reranker"):
                assert [item["chunk_id"] for item in data[stage]] == [str(chunks[0].id)]
                assert data[stage][0]["document_id"] == str(ready.id)
                assert data[stage][0]["source"] == stage
                assert "secret" not in str(data[stage])
                assert "pending" not in str(data[stage])

            response = await client.post(
                "/api/v1/retrieval/debug",
                json={
                    "query": "refund policy",
                    "top_k": 5,
                    "knowledge_base_id": str(hidden.id),
                },
            )
            assert response.status_code == 404
            for body in ({"query": "", "top_k": 5}, {"query": "x", "top_k": 21}):
                assert (await client.post("/api/v1/retrieval/debug", json=body)).status_code == 422
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id.in_([visible.id, hidden.id]))
            )
            await session.commit()


class TimeoutReranker:
    async def rerank(self, query: str, items: list[RerankItem]) -> object:
        del query, items
        raise TimeoutError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_debug_reports_reranker_degraded() -> None:
    visible, hidden, old, _, _ = await seed_catalog()
    old.status = DocumentStatus.READY
    async with async_session_factory() as session:
        merged = await session.merge(old)
        merged.status = DocumentStatus.READY
        await session.commit()
    app.dependency_overrides[get_current_principal] = lambda: principal(
        frozenset({visible.department})
    )
    app.dependency_overrides[get_embedding_provider] = lambda: DeterministicEmbeddingProvider()
    app.dependency_overrides[get_reranker_provider] = lambda: TimeoutReranker()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/retrieval/debug", json={"query": "refund policy", "top_k": 5}
            )
            assert response.status_code == 200
            assert response.json()["reranker_status"] == "degraded"
            assert [item["chunk_id"] for item in response.json()["reranker"]] == [
                item["chunk_id"] for item in response.json()["rrf"]
            ]
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id.in_([visible.id, hidden.id]))
            )
            await session.commit()
