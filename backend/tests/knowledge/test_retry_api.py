import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.outbox import publish_pending_document_jobs
from opspilot.knowledge.schemas import AccessLevel
from opspilot.knowledge.storage import VolumeFileStorage
from opspilot.knowledge.tasks import index_document
from opspilot.main import app


def principal(role: Role) -> Principal:
    return Principal(
        user_id=str(uuid.uuid4()),
        role=role,
        allowed_departments=frozenset({"support"}),
        max_access_level=AccessLevel.CONFIDENTIAL,
    )


async def create_document(
    connection: asyncpg.Connection, source: Path, status: str
) -> tuple[uuid.UUID, uuid.UUID]:
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"retry-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'retry', 1, $3, $4, $5)",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(source),
        status,
    )
    return knowledge_base_id, document_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_requires_reviewer_or_admin(tmp_path: Path) -> None:
    source = tmp_path / "unauthorized.md"
    source.write_text("retry", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.USER)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry",
                json={"reason": "manual review"},
            )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_rejects_non_failed_document(tmp_path: Path) -> None:
    source = tmp_path / "ready.md"
    source.write_text("ready", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "READY")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.REVIEWER)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry", json={"reason": "wrong state"}
            )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_retry_clicks_create_one_audited_attempt(tmp_path: Path) -> None:
    source = tmp_path / "concurrent-retry.md"
    source.write_text("retry", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    reviewer = principal(Role.REVIEWER)
    app.dependency_overrides[get_current_principal] = lambda: reviewer
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(
                client.post(
                    f"/api/v1/knowledge/documents/{document_id}/retry",
                    json={"reason": "first"},
                ),
                client.post(
                    f"/api/v1/knowledge/documents/{document_id}/retry",
                    json={"reason": "second"},
                ),
            )
        assert sorted(response.status_code for response in responses) == [202, 409]
        rows = await connection.fetch(
            "SELECT attempt, requested_by, audit_reason FROM document_index_outbox "
            "WHERE document_id = $1",
            document_id,
        )
        assert len(rows) == 1
        assert rows[0]["attempt"] == 1
        assert str(rows[0]["requested_by"]) == reviewer.user_id
        assert rows[0]["audit_reason"] in {"first", "second"}
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


class DirectRetryQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, int]] = []

    async def enqueue_document(self, document_id: str, job_id: str, retry_attempt: int) -> None:
        assert job_id.endswith(f":retry:{retry_attempt}")
        self.jobs.append((document_id, retry_attempt))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_outbox_reaches_ready_without_duplicate_chunks(tmp_path: Path) -> None:
    source = tmp_path / "end-to-end.md"
    source.write_text("# Retry\nRebuild once.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    await connection.execute(
        "UPDATE document_index_outbox SET delivered_at = now() WHERE delivered_at IS NULL"
    )
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.ADMIN)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry",
                json={"reason": "admin retry"},
            )
        assert response.status_code == 202
        queue = DirectRetryQueue()
        assert await publish_pending_document_jobs(queue) == 1
        assert await publish_pending_document_jobs(queue) == 0
        assert queue.jobs == [(str(document_id), 1)]
        await index_document(
            {
                "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
                "file_storage": VolumeFileStorage(tmp_path),
            },
            *queue.jobs[0],
        )
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "READY"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM chunks WHERE document_id = $1", document_id
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT completed_at IS NOT NULL FROM document_index_outbox "
                "WHERE document_id = $1 AND attempt = 1",
                document_id,
            )
            is True
        )
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
