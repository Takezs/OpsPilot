import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config

from alembic import command
from opspilot.config import Settings


def alembic_config() -> Config:
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


async def seed_0007_retry_history() -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    documents = {name: uuid.uuid4() for name in ("completed", "incomplete", "ready", "failed")}
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"migration-kb-{knowledge_base_id}",
    )
    for position, (name, document_id) in enumerate(documents.items()):
        document_status = "READY" if name in {"completed", "ready"} else "FAILED"
        await connection.execute(
            "INSERT INTO documents "
            "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
            "VALUES ($1, $2, $3, 1, $4, '/tmp/migration.md', $5)",
            document_id,
            knowledge_base_id,
            name,
            f"{position:064x}",
            document_status,
        )
    now = datetime.now(UTC)
    await connection.execute(
        "INSERT INTO document_index_outbox "
        "(document_id, job_id, attempt, requested_by, delivered_at, completed_at) VALUES "
        "($1, $2, 1, $3, $4, $4), ($5, $6, 1, $3, $4, NULL), "
        "($7, $8, 0, NULL, $4, NULL), ($9, $10, 0, NULL, $4, NULL)",
        documents["completed"],
        f"document-index:{documents['completed']}:retry:1",
        uuid.uuid4(),
        now,
        documents["incomplete"],
        f"document-index:{documents['incomplete']}:retry:1",
        documents["ready"],
        f"document-index:{documents['ready']}",
        documents["failed"],
        f"document-index:{documents['failed']}",
    )
    await connection.close()
    return knowledge_base_id, documents


async def assert_0008_backfill_and_create_next_attempt(
    documents: dict[str, uuid.UUID],
) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    rows = await connection.fetch(
        "SELECT document_id, attempt, status, completed_at, lease_expires_at "
        "FROM document_index_outbox WHERE document_id = ANY($1::uuid[])",
        list(documents.values()),
    )
    by_document = {row["document_id"]: row for row in rows}
    assert by_document[documents["completed"]]["status"] == "SUCCEEDED"
    assert by_document[documents["incomplete"]]["status"] == "FAILED"
    assert by_document[documents["incomplete"]]["completed_at"] is not None
    assert by_document[documents["incomplete"]]["lease_expires_at"] is None
    assert by_document[documents["ready"]]["status"] == "SUCCEEDED"
    assert by_document[documents["failed"]]["status"] == "FAILED"
    assert not any(row["attempt"] > 0 and row["status"] in {"QUEUED", "RUNNING"} for row in rows)
    next_attempt = await connection.fetchval(
        "INSERT INTO document_index_outbox "
        "(document_id, job_id, attempt, requested_by, audit_reason) "
        "VALUES ($1, $2, 2, $3, 'migration retry') RETURNING attempt",
        documents["incomplete"],
        f"document-index:{documents['incomplete']}:retry:2",
        uuid.uuid4(),
    )
    assert next_attempt == 2
    await connection.close()


async def cleanup(knowledge_base_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
    await connection.close()


@pytest.mark.integration
def test_0007_retry_history_is_backfilled_during_0008_upgrade() -> None:
    config = alembic_config()
    command.downgrade(config, "0007_document_retry_attempts")
    knowledge_base_id, documents = asyncio.run(seed_0007_retry_history())
    try:
        command.upgrade(config, "0008_document_retry_lifecycle")
        asyncio.run(assert_0008_backfill_and_create_next_attempt(documents))
        asyncio.run(cleanup(knowledge_base_id))
        command.downgrade(config, "0006_document_index_outbox")
        command.upgrade(config, "head")
    finally:
        command.upgrade(config, "head")
        asyncio.run(cleanup(knowledge_base_id))
