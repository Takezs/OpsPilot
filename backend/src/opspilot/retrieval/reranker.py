"""BGE reranker with an explicit degraded fallback to the RRF ordering.

A reranker timeout must never be swallowed: ``rerank_with_fallback`` imposes a
wall-clock timeout and, on expiry, returns the input (RRF) ordering with
``RerankStatus.DEGRADED`` so the pipeline records the degradation event instead
of silently losing ranking signal.
"""

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from opspilot.retrieval.types import RetrievalCandidate


class RerankStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class RerankItem:
    candidate: RetrievalCandidate
    content: str


@dataclass(frozen=True)
class RerankedResult:
    status: RerankStatus
    candidates: tuple[RetrievalCandidate, ...]


class RerankerTimeoutError(TimeoutError):
    """Raised by a reranker provider when the upstream call times out.

    The domain fallback treats this like any other timeout (including
    ``asyncio.TimeoutError`` from the wall-clock guard) so remote and local
    providers degrade identically.
    """


class RerankerProvider(Protocol):
    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankedResult: ...


async def rerank_with_fallback(
    provider: RerankerProvider,
    query: str,
    items: Sequence[RerankItem],
    timeout_seconds: float,
) -> RerankedResult:
    """Rerank ``items``, degrading to the input ordering when the call times out.

    The timeout is applied uniformly with ``asyncio.wait_for`` so every provider
    (remote HTTP or local) degrades the same way. Provider-level timeouts that
    surface as ``RerankerTimeoutError`` (a ``TimeoutError`` subclass) are caught
    too; unrelated exceptions propagate to the caller.
    """
    try:
        return await asyncio.wait_for(provider.rerank(query, items), timeout=timeout_seconds)
    except TimeoutError:
        return RerankedResult(
            status=RerankStatus.DEGRADED,
            candidates=tuple(item.candidate for item in items),
        )


class BgeReranker:
    """BGE reranker exposed over an OpenAI-compatible ``/rerank`` endpoint.

    HTTP timeouts are converted into ``RerankerTimeoutError`` at the provider
    boundary so the fallback logic treats remote and local providers uniformly.
    Unrelated HTTP errors are not swallowed and propagate to the caller.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankedResult:
        try:
            response = await self._client.post(
                f"{self.base_url}/rerank",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [item.content for item in items],
                },
            )
        except httpx.TimeoutException as exc:
            raise RerankerTimeoutError("reranker request timed out") from exc
        response.raise_for_status()
        data = response.json()
        scored: list[tuple[float, RerankItem]] = []
        for entry in data.get("results", []):
            item = items[entry["index"]]
            scored.append((float(entry["score"]), item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].candidate.chunk_id))
        return RerankedResult(
            status=RerankStatus.OK,
            candidates=tuple(item.candidate for _, item in scored),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class DeterministicReranker:
    """Deterministic reranker for unit tests; never calls an external service.

    Scores come from an optional ``chunk_id -> score`` mapping and fall back to
    a content hash so results are reproducible. Ties break by ascending chunk id
    so the output is stable across runs.
    """

    def __init__(self, scores: Mapping[str, float] | None = None) -> None:
        self._scores = dict(scores or {})

    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankedResult:
        scored: list[tuple[float, RerankItem]] = []
        for item in items:
            score = self._scores.get(item.candidate.chunk_id, _content_score(item.content))
            scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].candidate.chunk_id))
        return RerankedResult(
            status=RerankStatus.OK,
            candidates=tuple(item.candidate for _, item in scored),
        )


def _content_score(content: str) -> float:
    digest = hashlib.sha256(content.encode()).digest()
    return float(int.from_bytes(digest[:8], "big")) / float(2**64)
