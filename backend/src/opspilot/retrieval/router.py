import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import get_session
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider, EmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.retrieval.reranker import (
    BgeReranker,
    RerankerProvider,
    RerankItem,
    RerankStatus,
    rerank_with_fallback,
)
from opspilot.retrieval.service import RetrievalService
from opspilot.retrieval.types import RetrievalCandidate, scope_conditions
from opspilot.runs.sanitize import sanitize_value

router = APIRouter(prefix="/retrieval", tags=["retrieval"])
EXCERPT_LENGTH = 500


class RetrievalDebugRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    knowledge_base_id: uuid.UUID | None = None
    top_k: int = Field(default=10, ge=1, le=20)


class RetrievalStageItem(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int
    document_title: str
    section_path: list[str]
    page: int | None
    rank: int
    score: float
    source: Literal["dense", "fts", "rrf", "reranker"]
    content_excerpt: str


class RetrievalDebugResponse(BaseModel):
    dense: list[RetrievalStageItem]
    fts: list[RetrievalStageItem]
    rrf: list[RetrievalStageItem]
    reranker: list[RetrievalStageItem]
    reranker_status: RerankStatus


async def get_embedding_provider() -> AsyncIterator[EmbeddingProvider]:
    settings = Settings()
    provider = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    try:
        yield provider
    finally:
        await provider.aclose()


async def get_reranker_provider() -> AsyncIterator[RerankerProvider]:
    settings = Settings()
    provider = BgeReranker(settings.bge_base_url, settings.bge_api_key)
    try:
        yield provider
    finally:
        await provider.aclose()


async def _metadata(
    session: AsyncSession,
    candidates: list[RetrievalCandidate],
    principal: Principal,
    knowledge_base_id: uuid.UUID | None,
) -> dict[str, tuple[Chunk, Document]]:
    ids = [uuid.UUID(candidate.chunk_id) for candidate in candidates]
    if not ids:
        return {}
    statement = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .where(Chunk.id.in_(ids), Document.status == DocumentStatus.READY)
        .where(scope_conditions(principal.knowledge_scope))
    )
    if knowledge_base_id is not None:
        statement = statement.where(Document.knowledge_base_id == knowledge_base_id)
    return {
        str(chunk.id): (chunk, document) for chunk, document in await session.execute(statement)
    }


def _stage(
    candidates: list[RetrievalCandidate],
    metadata: dict[str, tuple[Chunk, Document]],
    source: Literal["dense", "fts", "rrf", "reranker"],
    reindex: bool = False,
) -> list[RetrievalStageItem]:
    result: list[RetrievalStageItem] = []
    for candidate in candidates:
        item = metadata.get(candidate.chunk_id)
        if item is None:
            continue
        chunk, document = item
        safe_content = sanitize_value(chunk.content)
        excerpt = safe_content if isinstance(safe_content, str) else ""
        result.append(
            RetrievalStageItem(
                chunk_id=chunk.id,
                document_id=document.id,
                document_version=document.version,
                document_title=document.title,
                section_path=list(chunk.section_path),
                page=chunk.page,
                rank=len(result) + 1 if reindex else candidate.rank,
                score=candidate.score,
                source=source,
                content_excerpt=excerpt[:EXCERPT_LENGTH],
            )
        )
    return result


@router.post("/debug", response_model=RetrievalDebugResponse)
async def retrieval_debug(
    request: RetrievalDebugRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    embedding_provider: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    reranker_provider: Annotated[RerankerProvider, Depends(get_reranker_provider)],
) -> RetrievalDebugResponse:
    if request.knowledge_base_id is not None:
        visible = await session.scalar(
            select(KnowledgeBase.id)
            .where(KnowledgeBase.id == request.knowledge_base_id)
            .where(scope_conditions(principal.knowledge_scope))
        )
        if visible is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    result = await RetrievalService(session, embedding_provider).search(
        request.query,
        principal.knowledge_scope,
        dense_limit=request.top_k,
        fts_limit=request.top_k,
        fusion_limit=request.top_k,
        knowledge_base_id=request.knowledge_base_id,
    )
    all_candidates = [*result.dense, *result.fts, *result.rrf]
    metadata = await _metadata(session, all_candidates, principal, request.knowledge_base_id)
    rerank_items = [
        RerankItem(candidate=candidate, content=metadata[candidate.chunk_id][0].content)
        for candidate in result.rrf
        if candidate.chunk_id in metadata
    ]
    if rerank_items:
        reranked = await rerank_with_fallback(
            reranker_provider, request.query, rerank_items, timeout_seconds=5.0
        )
        reranker_candidates = list(reranked.candidates)
        reranker_status = reranked.status
    else:
        reranker_candidates = []
        reranker_status = RerankStatus.OK
    return RetrievalDebugResponse(
        dense=_stage(result.dense, metadata, "dense"),
        fts=_stage(result.fts, metadata, "fts"),
        rrf=_stage(result.rrf, metadata, "rrf"),
        reranker=_stage(reranker_candidates, metadata, "reranker", reindex=True),
        reranker_status=reranker_status,
    )
