"""Exact tool-call precision, recall and F1."""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    ApprovalExpectation,
    ExpectedOutcome,
)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, object]

    def signature(self) -> str:
        arguments = json.dumps(
            self.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return f"{self.name}:{arguments}"


@dataclass(frozen=True)
class ToolMetrics:
    precision: float
    recall: float
    f1: float
    true_positives: int
    actual_count: int
    expected_count: int


@dataclass(frozen=True)
class ActualAgentOutcome:
    order_number: str | None
    amount: Decimal | None
    idempotency_key: str | None
    tool_calls: tuple[ToolCall, ...]
    approval: ApprovalExpectation | None
    final_state: ExpectedOutcome


@dataclass(frozen=True)
class AgentOutcomeMetrics:
    order_number_match: bool | None
    amount_match: bool | None
    idempotency_key_match: bool | None
    approval_match: bool | None
    final_state_match: bool
    tools: ToolMetrics
    passed: bool


def tool_metrics(actual: Sequence[ToolCall], expected: Sequence[ToolCall]) -> ToolMetrics:
    actual_counts = Counter(item.signature() for item in actual)
    expected_counts = Counter(item.signature() for item in expected)
    true_positives = sum(
        min(count, expected_counts[signature]) for signature, count in actual_counts.items()
    )
    actual_count = len(actual)
    expected_count = len(expected)
    precision = true_positives / actual_count if actual_count else float(expected_count == 0)
    recall = true_positives / expected_count if expected_count else float(actual_count == 0)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return ToolMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=true_positives,
        actual_count=actual_count,
        expected_count=expected_count,
    )


def evaluate_agent_outcome(
    actual: ActualAgentOutcome, expected: AgentEvaluationCase
) -> AgentOutcomeMetrics:
    expected_tools = tuple(ToolCall(item.name, item.arguments) for item in expected.expected_tools)
    tools = tool_metrics(actual.tool_calls, expected_tools)
    final_state_match = actual.final_state is expected.expected_final_state
    matches: tuple[bool | None, ...] = (
        actual.order_number == expected.order_number if expected.operation_expected else None,
        actual.amount == expected.amount if expected.operation_expected else None,
        actual.idempotency_key == expected.expected_idempotency_key
        if expected.operation_expected
        else None,
        actual.approval is expected.expected_approval if expected.operation_expected else None,
        final_state_match,
    )
    return AgentOutcomeMetrics(
        order_number_match=matches[0],
        amount_match=matches[1],
        idempotency_key_match=matches[2],
        approval_match=matches[3],
        final_state_match=final_state_match,
        tools=tools,
        passed=all(match for match in matches if match is not None)
        and tools.precision == 1.0
        and tools.recall == 1.0,
    )
