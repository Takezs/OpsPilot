import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest
from arq.worker import Retry
from httpx import ASGITransport, AsyncClient

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.outbox import publish_pending_document_jobs
from opspilot.knowledge.schemas import AccessLevel
from opspilot.knowledge.storage import VolumeFileStorage
from opspilot.knowledge.tasks import DOCUMENT_INDEX_MAX_TRIES, index_document
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
async def test_retry_requires_non_blank_audit_reason(tmp_path: Path) -> None:
    source = tmp_path / "blank-reason.md"
    source.write_text("retry", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.REVIEWER)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry", json={"reason": "   "}
            )
        assert response.status_code == 422
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM document_index_outbox WHERE document_id = $1", document_id
            )
            == 0
        )
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


class FailingEmbeddingProvider:
    model = "failing-test-embedding"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider unavailable")


class BlockingRetryEmbeddingProvider:
    model = "blocking-retry-embedding"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_failed_attempt_allows_next_attempt_to_reach_ready(tmp_path: Path) -> None:
    source = tmp_path / "retry-twice.md"
    source.write_text("# Retry\nTry again safely.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    await connection.execute(
        "UPDATE document_index_outbox SET delivered_at = now() WHERE delivered_at IS NULL"
    )
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.ADMIN)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry",
                json={"reason": "first attempt"},
            )
            assert first.status_code == 202
            first_queue = DirectRetryQueue()
            assert await publish_pending_document_jobs(first_queue) == 1
            assert first_queue.jobs == [(str(document_id), 1)]
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await index_document(
                    {
                        "embedding_provider": FailingEmbeddingProvider(),
                        "file_storage": VolumeFileStorage(tmp_path),
                        "job_try": DOCUMENT_INDEX_MAX_TRIES,
                    },
                    str(document_id),
                    1,
                )
            assert (
                await connection.fetchval(
                    "SELECT status FROM document_index_outbox "
                    "WHERE document_id = $1 AND attempt = 1",
                    document_id,
                )
                == "FAILED"
            )
            second = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry",
                json={"reason": "second attempt"},
            )
            assert second.status_code == 202
            assert second.json()["attempt"] == 2
        second_queue = DirectRetryQueue()
        assert await publish_pending_document_jobs(second_queue) == 1
        assert second_queue.jobs == [(str(document_id), 2)]
        await index_document(
            {
                "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
                "file_storage": VolumeFileStorage(tmp_path),
                "job_try": 1,
            },
            *second_queue.jobs[0],
        )
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "READY"
        )
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retryable_failure_keeps_attempt_running_and_raises_retry(tmp_path: Path) -> None:
    source = tmp_path / "retryable.md"
    source.write_text("retry later", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    reviewer = principal(Role.REVIEWER)
    app.dependency_overrides[get_current_principal] = lambda: reviewer
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.post(
                    f"/api/v1/knowledge/documents/{document_id}/retry",
                    json={"reason": "retryable"},
                )
            ).status_code == 202
            with pytest.raises(Retry):
                await index_document(
                    {
                        "embedding_provider": FailingEmbeddingProvider(),
                        "file_storage": VolumeFileStorage(tmp_path),
                        "job_try": 1,
                    },
                    str(document_id),
                    1,
                )
            duplicate = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/retry",
                json={"reason": "duplicate"},
            )
        assert duplicate.status_code == 409
        assert (
            await connection.fetchval(
                "SELECT status FROM document_index_outbox WHERE document_id = $1 AND attempt = 1",
                document_id,
            )
            == "RUNNING"
        )
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_click_during_running_attempt_returns_conflict(tmp_path: Path) -> None:
    source = tmp_path / "running.md"
    source.write_text("running", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id, document_id = await create_document(connection, source, "FAILED")
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.ADMIN)
    provider = BlockingRetryEmbeddingProvider()
    task: asyncio.Task[None] | None = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.post(
                    f"/api/v1/knowledge/documents/{document_id}/retry",
                    json={"reason": "running attempt"},
                )
            ).status_code == 202
            task = asyncio.create_task(
                index_document(
                    {
                        "embedding_provider": provider,
                        "file_storage": VolumeFileStorage(tmp_path),
                        "job_try": 1,
                    },
                    str(document_id),
                    1,
                )
            )
            await asyncio.wait_for(provider.started.wait(), timeout=5)
            duplicate = await asyncio.wait_for(
                client.post(
                    f"/api/v1/knowledge/documents/{document_id}/retry",
                    json={"reason": "must reject now"},
                ),
                timeout=1,
            )
        assert duplicate.status_code == 409
    finally:
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
