import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.knowledge.outbox import reconcile_expired_retry_attempts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconciler_closes_only_expired_running_attempts() -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    expired_document = uuid.uuid4()
    active_document = uuid.uuid4()
    now = datetime.now(UTC)
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"reconcile-kb-{knowledge_base_id}",
    )
    for document_id, title in ((expired_document, "expired"), (active_document, "active")):
        await connection.execute(
            "INSERT INTO documents "
            "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
            "VALUES ($1, $2, $3, 1, $4, '/tmp/retry.md', 'FAILED')",
            document_id,
            knowledge_base_id,
            title,
            uuid.uuid4().hex.ljust(64, "0"),
        )
    await connection.execute(
        "INSERT INTO document_index_outbox "
        "(document_id, job_id, attempt, requested_by, status, started_at, lease_expires_at) "
        "VALUES ($1, $2, 1, $3, 'RUNNING', $4, $5), "
        "($6, $7, 1, $3, 'RUNNING', $4, $8)",
        expired_document,
        f"document-index:{expired_document}:retry:1",
        uuid.uuid4(),
        now - timedelta(minutes=10),
        now - timedelta(minutes=1),
        active_document,
        f"document-index:{active_document}:retry:1",
        now + timedelta(minutes=10),
    )
    try:
        assert await reconcile_expired_retry_attempts(now=now) == 1
        assert (
            await connection.fetchval(
                "SELECT status FROM document_index_outbox WHERE document_id = $1", expired_document
            )
            == "FAILED"
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM document_index_outbox WHERE document_id = $1", active_document
            )
            == "RUNNING"
        )
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
