import uuid
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.knowledge.router import get_document_queue, get_file_storage
from opspilot.knowledge.schemas import AccessLevel
from opspilot.knowledge.storage import VolumeFileStorage
from opspilot.main import app


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def enqueue_document(self, document_id: str) -> None:
        self.calls.append(("index_document", {"document_id": document_id}))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_persists_versions_and_enqueues_id_only(tmp_path: Path) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"upload-kb-{knowledge_base_id}",
    )
    queue = RecordingQueue()
    principal = Principal(
        user_id=str(uuid.uuid4()),
        role=Role.USER,
        allowed_departments=frozenset({"support"}),
        max_access_level=AccessLevel.INTERNAL,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_file_storage] = lambda: VolumeFileStorage(tmp_path)
    app.dependency_overrides[get_document_queue] = lambda: queue
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "Refund policy"},
                files={"file": ("policy.md", b"version one", "text/markdown")},
            )
            second = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "Refund policy"},
                files={"file": ("policy.md", b"version two", "text/markdown")},
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert [first.json()["version"], second.json()["version"]] == [1, 2]
        assert len(queue.calls) == 2
        assert all(set(payload) == {"document_id"} for _, payload in queue.calls)
        rows = await connection.fetch(
            "SELECT version, storage_path FROM documents WHERE knowledge_base_id = $1 "
            "ORDER BY version",
            knowledge_base_id,
        )
        assert [row["version"] for row in rows] == [1, 2]
        assert all(
            Path(row["storage_path"]).parent == tmp_path.resolve()  # noqa: ASYNC240
            for row in rows
        )
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_duplicate_and_cross_department(tmp_path: Path) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'finance', 1)",
        knowledge_base_id,
        f"finance-kb-{knowledge_base_id}",
    )
    principal = Principal(
        user_id=str(uuid.uuid4()),
        role=Role.USER,
        allowed_departments=frozenset({"support"}),
        max_access_level=AccessLevel.INTERNAL,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_file_storage] = lambda: VolumeFileStorage(tmp_path)
    app.dependency_overrides[get_document_queue] = RecordingQueue
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "Finance"},
                files={"file": ("finance.md", b"secret", "text/markdown")},
            )
        assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_duplicate_sha_without_queueing(tmp_path: Path) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"dedup-kb-{knowledge_base_id}",
    )
    queue = RecordingQueue()
    principal = Principal(
        user_id=str(uuid.uuid4()),
        role=Role.USER,
        allowed_departments=frozenset({"support"}),
        max_access_level=AccessLevel.INTERNAL,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_file_storage] = lambda: VolumeFileStorage(tmp_path)
    app.dependency_overrides[get_document_queue] = lambda: queue
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "First title"},
                files={"file": ("first.md", b"identical", "text/markdown")},
            )
            duplicate = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "Other title"},
                files={"file": ("other.md", b"identical", "text/markdown")},
            )
        assert first.status_code == 202
        assert duplicate.status_code == 409
        assert len(queue.calls) == 1
        assert len(list(tmp_path.iterdir())) == 1  # noqa: ASYNC240
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_stream_enforces_size_limit_before_persisting(tmp_path: Path) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"limit-kb-{knowledge_base_id}",
    )
    queue = RecordingQueue()
    principal = Principal(
        user_id=str(uuid.uuid4()),
        role=Role.USER,
        allowed_departments=frozenset({"support"}),
        max_access_level=AccessLevel.INTERNAL,
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_file_storage] = lambda: VolumeFileStorage(tmp_path, max_bytes=10)
    app.dependency_overrides[get_document_queue] = lambda: queue
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/knowledge/{knowledge_base_id}/documents",
                data={"title": "Too large"},
                files={"file": ("large.md", b"more than ten bytes", "text/markdown")},
            )
        assert response.status_code == 422
        assert not queue.calls
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM documents WHERE knowledge_base_id = $1", knowledge_base_id
            )
            == 0
        )
        assert list(tmp_path.iterdir()) == []  # noqa: ASYNC240
    finally:
        app.dependency_overrides.clear()
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
