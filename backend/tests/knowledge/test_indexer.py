import uuid
from pathlib import Path

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.indexer import InMemoryChunkSink, index_chunks
from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock
from opspilot.knowledge.tasks import index_document


@pytest.mark.asyncio
async def test_indexer_batches_in_order_and_is_idempotent() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=4)
    sink = InMemoryChunkSink()
    document_id = uuid.uuid4()
    blocks = [ParsedBlock(BlockKind.PARAGRAPH, f"paragraph {index}") for index in range(7)]

    first_count = await index_chunks(document_id, blocks, provider, sink, batch_size=3)
    second_count = await index_chunks(document_id, blocks, provider, sink, batch_size=3)

    assert first_count == 7
    assert second_count == 0
    assert provider.batch_sizes == [3, 3, 1]
    assert [record.position for record in sink.records] == list(range(7))
    assert all(
        record.embedding_cache_key.startswith(f"{provider.model}:") for record in sink.records
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pgvector_chunk_and_fts_are_queryable() -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    vector = "[1," + ",".join(["0"] * 1023) + "]"
    try:
        await connection.execute(
            "INSERT INTO knowledge_bases (id, name, department, access_level) "
            "VALUES ($1, $2, 'support', 1)",
            knowledge_base_id,
            f"kb-{knowledge_base_id}",
        )
        await connection.execute(
            "INSERT INTO documents "
            "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
            "VALUES ($1, $2, 'policy', 1, $3, '/tmp/policy.md', 'UPLOADED')",
            document_id,
            knowledge_base_id,
            uuid.uuid4().hex.ljust(64, "0"),
        )
        await connection.execute(
            "INSERT INTO chunks "
            "(document_id, position, content, section_path, token_count, embedding, "
            "embedding_cache_key) VALUES ($1, 0, 'refund policy', $2, 2, $3::vector, 'key')",
            document_id,
            ["Refund"],
            vector,
        )
        matches = await connection.fetchval(
            "SELECT count(*) FROM chunks "
            "WHERE document_id = $1 AND search_vector @@ plainto_tsquery('simple', 'refund')",
            document_id,
        )
        distance = await connection.fetchval(
            "SELECT embedding <=> $2::vector FROM chunks WHERE document_id = $1",
            document_id,
            vector,
        )
        assert matches == 1
        assert distance == 0
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_reads_storage_path_and_reaches_ready(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Refund\nCustomers may request a refund.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    try:
        await connection.execute(
            "INSERT INTO knowledge_bases (id, name, department, access_level) "
            "VALUES ($1, $2, 'support', 1)",
            knowledge_base_id,
            f"worker-kb-{knowledge_base_id}",
        )
        await connection.execute(
            "INSERT INTO documents "
            "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
            "VALUES ($1, $2, 'worker policy', 1, $3, $4, 'UPLOADED')",
            document_id,
            knowledge_base_id,
            uuid.uuid4().hex.ljust(64, "0"),
            str(source),
        )
        await index_document(
            {"embedding_provider": DeterministicEmbeddingProvider(dimensions=1024)},
            str(document_id),
        )
        status = await connection.fetchval(
            "SELECT status FROM documents WHERE id = $1", document_id
        )
        chunk_count = await connection.fetchval(
            "SELECT count(*) FROM chunks WHERE document_id = $1", document_id
        )
        assert status == "READY"
        assert chunk_count == 1
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
