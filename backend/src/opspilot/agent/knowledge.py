"""Production composition for a Run-scoped grounded knowledge search."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.agent.runner import GroundedSearchResult
from opspilot.auth.models import User
from opspilot.auth.schemas import Principal
from opspilot.knowledge.embedding import EmbeddingProvider
from opspilot.knowledge.schemas import AccessLevel
from opspilot.observability.tracing import traced_stage
from opspilot.retrieval.context_builder import build_context
from opspilot.retrieval.enrichment import (
    CandidateMetadata,
    context_fragments,
    load_candidate_metadata,
)
from opspilot.retrieval.reranker import (
    RerankerProvider,
    RerankItem,
    RerankStatus,
    rerank_with_fallback,
)
from opspilot.retrieval.service import RetrievalService
from opspilot.retrieval.types import RetrievalCandidate
from opspilot.runs.models import Run
from opspilot.tools.schemas import SearchKnowledgeArgs
from opspilot.tools.types import ToolResult


class RunKnowledgeAccessError(RuntimeError):
    """The Run has no active database owner from which to derive scope."""


class RunKnowledgeSearch:
    """Resolve database identity and run the approved Task 5/6 retrieval path."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: uuid.UUID,
        embedding_provider: EmbeddingProvider,
        reranker_provider: RerankerProvider,
        *,
        context_token_budget: int,
        reranker_timeout_seconds: float = 5.0,
    ) -> None:
        if context_token_budget <= 0:
            raise ValueError("context token budget must be positive")
        self._session_factory = session_factory
        self._run_id = run_id
        self._embedding_provider = embedding_provider
        self._reranker_provider = reranker_provider
        self._context_token_budget = context_token_budget
        self._reranker_timeout_seconds = reranker_timeout_seconds

    async def __call__(self, arguments: SearchKnowledgeArgs) -> GroundedSearchResult:
        async with self._session_factory() as session:
            principal = await _load_run_principal(session, self._run_id)
            result = await RetrievalService(session, self._embedding_provider).search(
                arguments.query,
                principal.knowledge_scope,
                dense_limit=arguments.top_k,
                fts_limit=arguments.top_k,
                fusion_limit=arguments.top_k,
            )
            metadata = await load_candidate_metadata(session, result.rrf, principal.knowledge_scope)
            rerank_items = [
                RerankItem(candidate=item, content=metadata[item.chunk_id].content)
                for item in result.rrf
                if item.chunk_id in metadata
            ]
            if rerank_items:
                with traced_stage(
                    "rerank", str(self._run_id), {"candidate_count": len(rerank_items)}
                ):
                    reranked = await rerank_with_fallback(
                        self._reranker_provider,
                        arguments.query,
                        rerank_items,
                        self._reranker_timeout_seconds,
                    )
                candidates = list(reranked.candidates)
                status = reranked.status
            else:
                candidates = []
                status = RerankStatus.OK
            return build_grounded_search_result(
                candidates,
                metadata,
                status,
                token_budget=self._context_token_budget,
            )


async def _load_run_principal(session: AsyncSession, run_id: uuid.UUID) -> Principal:
    user = await session.scalar(
        select(User)
        .join(Run, Run.owner_user_id == User.id)
        .where(Run.id == run_id, User.is_active.is_(True))
    )
    if user is None:
        raise RunKnowledgeAccessError("run has no active database owner")
    try:
        access_level = AccessLevel(user.max_access_level)
    except ValueError as error:
        raise RunKnowledgeAccessError("run owner has an invalid knowledge scope") from error
    return Principal(
        user_id=str(user.id),
        role=user.role,
        allowed_departments=frozenset(user.allowed_departments),
        max_access_level=access_level,
    )


def build_grounded_search_result(
    candidates: Sequence[RetrievalCandidate],
    metadata: dict[str, CandidateMetadata],
    reranker_status: RerankStatus,
    *,
    token_budget: int,
) -> GroundedSearchResult:
    """Bind selected evidence to context while exposing only a bounded summary."""
    fragments = context_fragments(list(candidates), metadata)
    context = build_context(fragments, token_budget)
    has_evidence = bool(context.fragments)
    return GroundedSearchResult(
        summary=ToolResult(
            ok=has_evidence,
            data={
                "result_count": len(context.fragments),
                "reranker_status": reranker_status.value,
                "citation_ids": list(context.citation_ids),
            },
            error=None if has_evidence else "search returned no usable evidence",
        ),
        context=context,
    )
