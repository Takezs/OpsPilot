from opspilot.retrieval.fusion import reciprocal_rank_fusion
from opspilot.retrieval.types import RetrievalSource


def test_rrf_merges_rankings() -> None:
    result = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60)
    assert result[0].chunk_id == "b"
    assert result[0].source is RetrievalSource.RRF


def test_rrf_deduplicates_chunks_across_lists() -> None:
    result = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60)
    chunk_ids = [candidate.chunk_id for candidate in result]
    assert chunk_ids == ["b", "a", "c"]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_rrf_respects_limit() -> None:
    result = reciprocal_rank_fusion([["a", "b", "c", "d"]], k=60, limit=2)
    assert [candidate.chunk_id for candidate in result] == ["a", "b"]


def test_rrf_assigns_rank_and_monotonic_score() -> None:
    result = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60)
    assert [candidate.rank for candidate in result] == [1, 2, 3]
    scores = [candidate.score for candidate in result]
    assert scores == sorted(scores, reverse=True)
