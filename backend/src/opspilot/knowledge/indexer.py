import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.knowledge.chunking import chunk_blocks
from opspilot.knowledge.embedding import EmbeddingProvider, embedding_cache_key
from opspilot.knowledge.models import Chunk
from opspilot.knowledge.parsers.base import ParsedBlock


@dataclass(frozen=True)
class ChunkRecord:
    document_id: uuid.UUID
    position: int
    content: str
    section_path: list[str]
    token_count: int
    embedding: list[float]
    embedding_cache_key: str


class ChunkSink(Protocol):
    async def existing_positions(self, document_id: uuid.UUID) -> set[int]: ...

    async def add_many(self, records: list[ChunkRecord]) -> None: ...


class InMemoryChunkSink:
    def __init__(self) -> None:
        self.records: list[ChunkRecord] = []

    async def existing_positions(self, document_id: uuid.UUID) -> set[int]:
        return {record.position for record in self.records if record.document_id == document_id}

    async def add_many(self, records: list[ChunkRecord]) -> None:
        self.records.extend(records)


class SqlAlchemyChunkSink:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def existing_positions(self, document_id: uuid.UUID) -> set[int]:
        positions = await self.session.scalars(
            select(Chunk.position).where(Chunk.document_id == document_id)
        )
        return set(positions)

    async def add_many(self, records: list[ChunkRecord]) -> None:
        self.session.add_all(
            Chunk(
                document_id=record.document_id,
                position=record.position,
                content=record.content,
                section_path=record.section_path,
                token_count=record.token_count,
                embedding=record.embedding,
                embedding_cache_key=record.embedding_cache_key,
            )
            for record in records
        )
        await self.session.flush()


async def index_chunks(
    document_id: uuid.UUID,
    blocks: list[ParsedBlock],
    provider: EmbeddingProvider,
    sink: ChunkSink,
    batch_size: int = 32,
) -> int:
    chunks = chunk_blocks(blocks)
    existing = await sink.existing_positions(document_id)
    pending = [
        (position, chunk) for position, chunk in enumerate(chunks) if position not in existing
    ]
    count = 0
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        embeddings = await provider.embed([chunk.content for _, chunk in batch])
        records = [
            ChunkRecord(
                document_id=document_id,
                position=position,
                content=chunk.content,
                section_path=chunk.section_path,
                token_count=chunk.token_count,
                embedding=embedding,
                embedding_cache_key=embedding_cache_key(provider.model, chunk.content),
            )
            for (position, chunk), embedding in zip(batch, embeddings, strict=True)
        ]
        await sink.add_many(records)
        count += len(records)
    return count
