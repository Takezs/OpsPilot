"""Unit tests for reranker fallback and the context builder budget.

Task 6 guarantees:
- a reranker timeout keeps the RRF ordering and records a degraded status;
- the built context never exceeds the token budget and citation ids are stable.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from opspilot.retrieval.context_builder import (
    BuiltContext,
    ContextFragment,
    build_context,
    citation_id,
)
from opspilot.retrieval.reranker import (
    BgeReranker,
    DeterministicReranker,
    RerankItem,
    RerankStatus,
    rerank_with_fallback,
)
from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource


def candidate(chunk_id: str, rank: int) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=chunk_id,
        score=float(rank),
        rank=rank,
        source=RetrievalSource.RRF,
    )


def fragment(
    chunk_id: str,
    document_id: str,
    token_count: int,
    *,
    title: str = "T",
    version: int = 1,
    section_path: tuple[str, ...] = ("section",),
    page: int | None = 1,
) -> ContextFragment:
    return ContextFragment(
        document_id=document_id,
        chunk_id=chunk_id,
        title=title,
        document_version=version,
        section_path=section_path,
        effective_at=datetime(2026, 8, 24, tzinfo=UTC),
        page=page,
        content=f"content-{chunk_id}",
        token_count=token_count,
    )


class _TimeoutReranker:
    """Stub provider that always times out, for the degraded-path test."""

    async def rerank(self, query: str, items: list[RerankItem]) -> object:
        raise TimeoutError()


class _SlowReranker:
    """Stub provider that outlives the wall-clock guard to trigger cancellation."""

    async def rerank(self, query: str, items: list[RerankItem]) -> object:
        await asyncio.sleep(10)
        raise AssertionError("must not finish within the wall-clock timeout")


async def test_reranker_timeout_keeps_rrf_order_and_marks_degraded() -> None:
    rrf_order = [candidate("b", 1), candidate("a", 2), candidate("c", 3)]
    items = [RerankItem(candidate=item, content=f"content-{item.chunk_id}") for item in rrf_order]

    result = await rerank_with_fallback(_TimeoutReranker(), "query", items, timeout_seconds=1.0)

    assert result.status is RerankStatus.DEGRADED
    assert [candidate.chunk_id for candidate in result.candidates] == ["b", "a", "c"]


async def test_reranker_degrades_on_real_http_timeout() -> None:
    def _raise_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated upstream timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_raise_timeout))
    reranker = BgeReranker(base_url="http://reranker.local", api_key="test", client=client)
    rrf_order = [candidate("b", 1), candidate("a", 2)]
    items = [RerankItem(candidate=item, content=f"content-{item.chunk_id}") for item in rrf_order]

    result = await rerank_with_fallback(reranker, "query", items, timeout_seconds=5.0)

    assert result.status is RerankStatus.DEGRADED
    assert [candidate.chunk_id for candidate in result.candidates] == ["b", "a"]
    await client.aclose()


async def test_reranker_degrades_on_wall_clock_timeout() -> None:
    rrf_order = [candidate("b", 1), candidate("a", 2), candidate("c", 3)]
    items = [RerankItem(candidate=item, content=f"content-{item.chunk_id}") for item in rrf_order]

    result = await rerank_with_fallback(_SlowReranker(), "query", items, timeout_seconds=0.001)

    assert result.status is RerankStatus.DEGRADED
    assert [candidate.chunk_id for candidate in result.candidates] == ["b", "a", "c"]


class _BoomReranker:
    """Stub provider that raises an unrelated error, for the propagation test."""

    async def rerank(self, query: str, items: list[RerankItem]) -> object:
        raise RuntimeError("upstream exploded")


async def test_rerank_with_fallback_propagates_unrelated_errors() -> None:
    items = [RerankItem(candidate=candidate("b", 1), content="x")]

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await rerank_with_fallback(_BoomReranker(), "query", items, timeout_seconds=1.0)


async def test_deterministic_reranker_reorders_candidates() -> None:
    rrf_order = [candidate("a", 1), candidate("b", 2), candidate("c", 3)]
    items = [RerankItem(candidate=item, content=f"content-{item.chunk_id}") for item in rrf_order]

    reranker = DeterministicReranker(scores={"a": 0.1, "b": 0.5, "c": 0.9})
    result = await reranker.rerank("query", items)

    assert result.status is RerankStatus.OK
    assert [candidate.chunk_id for candidate in result.candidates] == ["c", "b", "a"]


def test_deterministic_reranker_breaks_ties_by_chunk_id() -> None:
    rrf_order = [candidate("z", 1), candidate("a", 2)]
    items = [RerankItem(candidate=item, content=f"content-{item.chunk_id}") for item in rrf_order]

    reranker = DeterministicReranker(scores={"z": 0.5, "a": 0.5})
    result = asyncio.run(reranker.rerank("query", items))

    assert [candidate.chunk_id for candidate in result.candidates] == ["a", "z"]


def test_citation_id_uses_document_and_chunk() -> None:
    assert citation_id("doc-1", "chunk-2") == "[DOC:doc-1#chunk-2]"


def test_context_stays_within_token_budget() -> None:
    doc_id = str(uuid.uuid4())
    # 16 tokens of header overhead are charged per fragment.
    first = fragment("chunk-a", doc_id, token_count=30)
    oversized = fragment("chunk-b", doc_id, token_count=40)
    last = fragment("chunk-c", doc_id, token_count=20)

    built = build_context([first, oversized, last], token_budget=100)

    assert built.total_tokens <= 100
    assert [item.chunk_id for item in built.fragments] == ["chunk-a", "chunk-c"]
    assert built.total_tokens == (30 + 16) + (20 + 16)
    assert built.truncated is True


def test_context_reports_full_when_budget_sufficient() -> None:
    doc_id = str(uuid.uuid4())
    built = build_context([fragment("chunk-a", doc_id, token_count=20)], token_budget=100)
    assert built.truncated is False
    assert built.total_tokens == 20 + 16


def test_context_citation_ids_are_stable_and_ordered() -> None:
    doc_id = str(uuid.uuid4())
    built = build_context(
        [
            fragment("chunk-a", doc_id, token_count=10),
            fragment("chunk-b", doc_id, token_count=10),
        ],
        token_budget=200,
    )

    assert built.citation_ids == (
        f"[DOC:{doc_id}#chunk-a]",
        f"[DOC:{doc_id}#chunk-b]",
    )

    rebuilt = build_context(
        [
            fragment("chunk-a", doc_id, token_count=10),
            fragment("chunk-b", doc_id, token_count=10),
        ],
        token_budget=200,
    )
    assert rebuilt.citation_ids == built.citation_ids


def test_empty_context_reports_zero_tokens() -> None:
    built = build_context([], token_budget=100)
    assert isinstance(built, BuiltContext)
    assert built.fragments == ()
    assert built.total_tokens == 0
    assert built.citation_ids == ()
    assert built.truncated is False
