import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.outbox import publish_pending_document_jobs
from opspilot.knowledge.storage import VolumeFileStorage
from opspilot.knowledge.tasks import index_document
from opspilot.outbox_publisher import OutboxPublisherSettings


class FlakyQueue:
    def __init__(self, index_context: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.index_context = index_context

    async def enqueue_document(self, document_id: str, job_id: str) -> None:
        self.calls.append((document_id, job_id))
        if len(self.calls) == 1:
            raise ConnectionError("redis unavailable")
        if self.index_context is not None:
            await index_document(self.index_context, document_id)


def test_outbox_publisher_has_periodic_recovery_schedule() -> None:
    assert OutboxPublisherSettings.cron_jobs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_enqueue_is_recovered_with_stable_job_id(tmp_path: Path) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    source = tmp_path / "outbox.md"
    source.write_text("# Outbox\nIndex exactly once.", encoding="utf-8")  # noqa: ASYNC240
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"outbox-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'outbox', 1, $3, $4, 'UPLOADED')",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(source),
    )
    await connection.execute(
        "INSERT INTO document_index_outbox (document_id, job_id) VALUES ($1, $2)",
        document_id,
        f"document-index:{document_id}",
    )
    queue = FlakyQueue(
        {
            "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
            "file_storage": VolumeFileStorage(tmp_path),
        }
    )
    try:
        assert await publish_pending_document_jobs(queue) == 0
        assert await publish_pending_document_jobs(queue) == 1
        assert await publish_pending_document_jobs(queue) == 0
        assert queue.calls == [
            (str(document_id), f"document-index:{document_id}"),
            (str(document_id), f"document-index:{document_id}"),
        ]
        row = await connection.fetchrow(
            "SELECT delivered_at, attempts FROM document_index_outbox WHERE document_id = $1",
            document_id,
        )
        assert row is not None and row["delivered_at"] is not None
        assert row["attempts"] == 2
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
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
