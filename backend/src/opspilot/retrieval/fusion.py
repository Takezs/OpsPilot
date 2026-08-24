"""Reciprocal Rank Fusion over ranked chunk identifier lists."""

from collections.abc import Sequence

from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource

DEFAULT_RRF_K = 60
DEFAULT_FUSION_LIMIT = 20


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    k: int = DEFAULT_RRF_K,
    limit: int = DEFAULT_FUSION_LIMIT,
) -> list[RetrievalCandidate]:
    """Fuse ranked chunk-id lists into a single deduplicated ranking.

    Each chunk scores ``1 / (k + rank)`` per list in which it appears, with
    rank starting at 1. Results are ordered by descending score; ties are
    broken deterministically by ascending chunk id so output is stable.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        RetrievalCandidate(
            chunk_id=chunk_id,
            score=score,
            rank=rank,
            source=RetrievalSource.RRF,
        )
        for rank, (chunk_id, score) in enumerate(ordered[:limit], start=1)
    ]
