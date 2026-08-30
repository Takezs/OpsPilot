import math
from decimal import Decimal

import pytest

from opspilot.evaluation.agent_metrics import (
    ActualAgentOutcome,
    ToolCall,
    evaluate_agent_outcome,
    tool_metrics,
)
from opspilot.evaluation.answer_metrics import (
    evaluate_answer_facts,
    evaluate_citation_ids,
    supplement_with_optional_judge,
)
from opspilot.evaluation.reliability_metrics import (
    ReliabilityObservation,
    duplicate_side_effect_rate,
    unapproved_execution_rate,
)
from opspilot.evaluation.retrieval_metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    ApprovalExpectation,
    ExpectedOutcome,
)


def test_hand_calculated_retrieval_metrics() -> None:
    ranked = ["a", "x", "b", "c", "y"]
    relevant = {"a", "b", "d"}

    assert recall_at_k(ranked, relevant, 5) == pytest.approx(2 / 3)
    assert precision_at_k(ranked, relevant, 5) == pytest.approx(2 / 5)
    assert mean_reciprocal_rank(ranked, relevant) == 1.0
    expected_dcg = 1.0 + 1.0 / math.log2(4)
    ideal_dcg = 1.0 + 1.0 / math.log2(3) + 1.0 / math.log2(4)
    assert ndcg_at_k(ranked, relevant, 5) == pytest.approx(expected_dcg / ideal_dcg)


def test_retrieval_metrics_reject_undefined_inputs() -> None:
    for metric in (recall_at_k, precision_at_k, ndcg_at_k):
        with pytest.raises(ValueError):
            metric(["a"], set(), 5)
        with pytest.raises(ValueError):
            metric(["a"], {"a"}, 0)


def test_tool_precision_recall_f1_use_exact_name_and_arguments_with_multiplicity() -> None:
    expected = [
        ToolCall("check_refund_eligibility", {"order_number": "ORD-002"}),
        ToolCall("refund_order", {"order_number": "ORD-002", "amount": 350}),
    ]
    actual = [
        expected[0],
        expected[1],
        ToolCall("send_email", {"to": "safe@example.invalid"}),
    ]

    score = tool_metrics(actual, expected)
    assert score.precision == pytest.approx(2 / 3)
    assert score.recall == 1.0
    assert score.f1 == pytest.approx(0.8)

    duplicate = tool_metrics([expected[1], expected[1]], [expected[1]])
    assert duplicate.precision == 0.5
    assert duplicate.recall == 1.0
    assert duplicate.f1 == pytest.approx(2 / 3)


def test_answer_fact_checks_are_deterministic_and_case_insensitive() -> None:
    score = evaluate_answer_facts(
        "Refunds ABOVE 100 require reviewer approval. Never automatic.",
        required_facts=["refunds above 100 require reviewer approval"],
        forbidden_facts=["refunds are always automatic"],
    )

    assert score.required_fact_recall == 1.0
    assert score.forbidden_fact_hit_rate == 0.0
    assert score.passed is True

    citations = evaluate_citation_ids(
        ["[DOC:doc-1#chunk-1]", "[DOC:wrong#chunk-x]"],
        ["[DOC:doc-1#chunk-1]"],
    )
    assert citations.precision == 0.5
    assert citations.recall == 1.0


def test_optional_judge_failure_never_blocks_deterministic_score() -> None:
    deterministic = {"required_fact_recall": 1.0, "citation_recall": 1.0}

    def unavailable() -> float:
        raise TimeoutError("judge unavailable")

    result = supplement_with_optional_judge(deterministic, unavailable)

    assert result.deterministic == deterministic
    assert result.judge_score is None
    assert result.judge_error == "TimeoutError"


def test_unapproved_and_duplicate_side_effect_rates_are_case_rates() -> None:
    observations = [
        ReliabilityObservation(
            approval_required=True, approved=False, side_effect_expected=True, side_effect_count=1
        ),
        ReliabilityObservation(
            approval_required=True, approved=True, side_effect_expected=True, side_effect_count=2
        ),
        ReliabilityObservation(
            approval_required=False, approved=False, side_effect_expected=True, side_effect_count=1
        ),
    ]

    assert unapproved_execution_rate(observations) == 0.5
    assert duplicate_side_effect_rate(observations) == pytest.approx(1 / 3)


def test_agent_outcome_uses_exact_business_and_database_facts() -> None:
    expected = AgentEvaluationCase(
        schema_version="1.0.0",
        case_id="agent-hand-001",
        category="refund",
        query="Refund ORD-002 for 350.",
        relevant_chunk_ids=(),
        expected_citation_ids=(),
        required_facts=(),
        forbidden_facts=("refund succeeded",),
        expected_tools=(
            {"name": "refund_order", "arguments": {"order_number": "ORD-002", "amount": 350}},
        ),
        forbidden_tools=(),
        follow_up_required=False,
        approval_required=True,
        expected_final_state=ExpectedOutcome.WAITING_APPROVAL,
        order_number="ORD-002",
        amount=Decimal("350.00"),
        expected_idempotency_key="refund:ORD-002",
        expected_approval=ApprovalExpectation.PENDING,
    )
    actual = ActualAgentOutcome(
        order_number="ORD-002",
        amount=Decimal("350.00"),
        idempotency_key="refund:ORD-002",
        tool_calls=(ToolCall("refund_order", {"order_number": "ORD-002", "amount": 350}),),
        approval=ApprovalExpectation.PENDING,
        final_state=ExpectedOutcome.WAITING_APPROVAL,
    )

    score = evaluate_agent_outcome(actual, expected)

    assert score.passed is True
    wrong = evaluate_agent_outcome(
        ActualAgentOutcome(
            order_number="ORD-003",
            amount=Decimal("350.01"),
            idempotency_key="refund:ORD-003",
            tool_calls=actual.tool_calls,
            approval=ApprovalExpectation.APPROVED,
            final_state=ExpectedOutcome.SUCCEEDED,
        ),
        expected,
    )
    assert wrong.passed is False
    assert (
        wrong.order_number_match,
        wrong.amount_match,
        wrong.idempotency_key_match,
        wrong.approval_match,
        wrong.final_state_match,
    ) == (False, False, False, False, False)
