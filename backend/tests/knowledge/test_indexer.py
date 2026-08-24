import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.knowledge.embedding import DeterministicEmbeddingProvider
from opspilot.knowledge.indexer import InMemoryChunkSink, index_chunks
from opspilot.knowledge.parsers.base import BlockKind, ParsedBlock
from opspilot.knowledge.storage import InvalidFile, VolumeFileStorage
from opspilot.knowledge.tasks import index_document


@pytest.mark.asyncio
async def test_indexer_batches_in_order_and_is_idempotent() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=4)
    sink = InMemoryChunkSink()
    document_id = uuid.uuid4()
    blocks = [
        ParsedBlock(BlockKind.PARAGRAPH, f"paragraph {index}", page=index) for index in range(7)
    ]

    first_count = await index_chunks(document_id, blocks, provider, sink, batch_size=3)
    second_count = await index_chunks(document_id, blocks, provider, sink, batch_size=3)

    assert first_count == 7
    assert second_count == 0
    assert provider.batch_sizes == [3, 3, 1]
    assert [record.position for record in sink.records] == list(range(7))
    assert all(
        record.embedding_cache_key.startswith(f"{provider.model}:") for record in sink.records
    )


@pytest.mark.asyncio
async def test_indexer_preserves_source_page() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=4)
    sink = InMemoryChunkSink()

    await index_chunks(
        uuid.uuid4(),
        [ParsedBlock(BlockKind.PARAGRAPH, "page citation", page=7)],
        provider,
        sink,
    )

    assert sink.records[0].page == 7


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
        indexes = {
            row["indexname"]
            for row in await connection.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'"
            )
        }
        assert {"ix_chunks_embedding_hnsw", "ix_chunks_search_vector"} <= indexes
        await connection.execute("SET enable_seqscan = off")
        vector_plan = "\n".join(
            row[0]
            for row in await connection.fetch(
                "EXPLAIN SELECT id FROM chunks ORDER BY embedding <=> $1::vector LIMIT 1",
                vector,
            )
        )
        fts_plan = "\n".join(
            row[0]
            for row in await connection.fetch(
                "EXPLAIN SELECT id FROM chunks "
                "WHERE search_vector @@ plainto_tsquery('simple', 'refund')"
            )
        )
        assert "ix_chunks_embedding_hnsw" in vector_plan
        assert "ix_chunks_search_vector" in fts_plan
        vector_index_definition = await connection.fetchval(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'chunks' AND indexname = 'ix_chunks_embedding_hnsw'"
        )
        assert "vector_cosine_ops" in vector_index_definition
        await connection.execute("RESET enable_seqscan")
        await connection.execute(
            "UPDATE chunks SET content = 'exchange policy' WHERE document_id = $1", document_id
        )
        assert (
            await connection.fetchval(
                "SELECT search_vector @@ plainto_tsquery('simple', 'exchange') "
                "FROM chunks WHERE document_id = $1",
                document_id,
            )
            is True
        )
        assert (
            await connection.fetchval("SELECT page FROM chunks WHERE document_id = $1", document_id)
            is None
        )
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
            {
                "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
                "file_storage": VolumeFileStorage(tmp_path),
            },
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_duplicate_jobs_converge_to_ready(tmp_path: Path) -> None:
    source = tmp_path / "concurrent.md"
    source.write_text("# Policy\nConcurrent indexing is safe.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"concurrent-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'concurrent', 1, $3, $4, 'UPLOADED')",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(source),
    )
    context = {
        "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
        "file_storage": VolumeFileStorage(tmp_path),
    }
    try:
        results = await asyncio.gather(
            index_document(context, str(document_id)),
            index_document(context, str(document_id)),
            return_exceptions=True,
        )
        assert results == [None, None]
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_rejects_storage_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.md"
    outside.write_text("must not be read", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"escape-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'escape', 1, $3, $4, 'UPLOADED')",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(outside),
    )
    try:
        with pytest.raises(InvalidFile, match="escapes"):
            await index_document(
                {
                    "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
                    "file_storage": VolumeFileStorage(root),
                },
                str(document_id),
            )
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "FAILED"
        )
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("crashed_status", ["PARSING", "CHUNKING", "INDEXING"])
async def test_worker_recovers_intermediate_state_without_duplicate_chunks(
    tmp_path: Path, crashed_status: str
) -> None:
    source = tmp_path / f"recover-{crashed_status.lower()}.md"
    source.write_text("# Recovery\nRebuild safely.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"recover-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'recover', 1, $3, $4, $5)",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(source),
        crashed_status,
    )
    vector = "[1," + ",".join(["0"] * 1023) + "]"
    await connection.execute(
        "INSERT INTO chunks "
        "(document_id, position, content, section_path, token_count, embedding, "
        "embedding_cache_key) VALUES ($1, 0, 'partial', $2, 1, $3::vector, 'partial')",
        document_id,
        ["Partial"],
        vector,
    )
    try:
        await index_document(
            {
                "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
                "file_storage": VolumeFileStorage(tmp_path),
            },
            str(document_id),
        )
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "READY"
        )
        rows = await connection.fetch(
            "SELECT position, content FROM chunks WHERE document_id = $1 ORDER BY position",
            document_id,
        )
        assert len(rows) == 1
        assert rows[0]["position"] == 0
        assert "Rebuild safely" in rows[0]["content"]
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_document_requires_explicit_retry(tmp_path: Path) -> None:
    source = tmp_path / "failed.md"
    source.write_text("# Retry\nExplicit retry.", encoding="utf-8")  # noqa: ASYNC240
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    knowledge_base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO knowledge_bases (id, name, department, access_level) "
        "VALUES ($1, $2, 'support', 1)",
        knowledge_base_id,
        f"failed-kb-{knowledge_base_id}",
    )
    await connection.execute(
        "INSERT INTO documents "
        "(id, knowledge_base_id, title, version, content_sha256, storage_path, status) "
        "VALUES ($1, $2, 'failed', 1, $3, $4, 'FAILED')",
        document_id,
        knowledge_base_id,
        uuid.uuid4().hex.ljust(64, "0"),
        str(source),
    )
    context = {
        "embedding_provider": DeterministicEmbeddingProvider(dimensions=1024),
        "file_storage": VolumeFileStorage(tmp_path),
    }
    try:
        await index_document(context, str(document_id))
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "FAILED"
        )
        await index_document({**context, "allow_failed_retry": True}, str(document_id))
        assert (
            await connection.fetchval("SELECT status FROM documents WHERE id = $1", document_id)
            == "READY"
        )
    finally:
        await connection.execute("DELETE FROM knowledge_bases WHERE id = $1", knowledge_base_id)
        await connection.close()
