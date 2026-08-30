"""Hand-verifiable deterministic retrieval metrics."""

import math
from collections.abc import Sequence, Set


def _validate(ranked: Sequence[str], relevant: Set[str], k: int | None = None) -> None:
    if not relevant:
        raise ValueError("relevant set must be non-empty")
    if len(set(ranked)) != len(ranked):
        raise ValueError("ranked ids must be unique")
    if k is not None and k <= 0:
        raise ValueError("k must be positive")


def recall_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    _validate(ranked, relevant, k)
    return len(set(ranked[:k]) & set(relevant)) / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    _validate(ranked, relevant, k)
    return len(set(ranked[:k]) & set(relevant)) / k


def mean_reciprocal_rank(ranked: Sequence[str], relevant: Set[str]) -> float:
    _validate(ranked, relevant)
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Set[str], k: int) -> float:
    _validate(ranked, relevant, k)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(ranked[:k], start=1)
        if item in relevant
    )
    ideal_count = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / ideal
