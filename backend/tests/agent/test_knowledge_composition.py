import uuid
from datetime import UTC, datetime

from opspilot.agent.knowledge import build_grounded_search_result
from opspilot.retrieval.enrichment import CandidateMetadata
from opspilot.retrieval.reranker import RerankStatus
from opspilot.retrieval.types import RetrievalCandidate, RetrievalSource


def test_grounded_search_summary_is_bounded_and_context_uses_persisted_tokens() -> None:
    chunk_id = uuid.uuid4()
    document_id = uuid.uuid4()
    candidate = RetrievalCandidate(str(chunk_id), 0.5, 1, RetrievalSource.RRF)
    metadata = {
        str(chunk_id): CandidateMetadata(
            chunk_id=chunk_id,
            document_id=document_id,
            document_version=3,
            document_title="Refund guide",
            section_path=("Policy",),
            page=2,
            content="private full evidence",
            token_count=37,
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    }

    result = build_grounded_search_result(
        [candidate], metadata, RerankStatus.DEGRADED, token_budget=100
    )

    assert result.context.fragments[0].token_count == 37
    assert result.context.total_tokens == 53
    assert result.summary.data == {
        "result_count": 1,
        "reranker_status": "degraded",
        "citation_ids": [f"[DOC:{document_id}#{chunk_id}]"],
    }
    assert "private full evidence" not in result.summary.render()


def test_empty_built_context_is_not_reported_as_successful_grounding() -> None:
    result = build_grounded_search_result([], {}, RerankStatus.OK, token_budget=100)

    assert result.context.fragments == ()
    assert result.summary.ok is False
    assert result.summary.data == {
        "result_count": 0,
        "reranker_status": "ok",
        "citation_ids": [],
    }
    assert result.summary.error == "search returned no usable evidence"
