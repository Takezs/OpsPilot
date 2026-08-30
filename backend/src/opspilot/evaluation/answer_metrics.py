"""Deterministic required/forbidden fact checks."""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerFactMetrics:
    required_fact_recall: float
    forbidden_fact_hit_rate: float
    passed: bool


@dataclass(frozen=True)
class CitationMetrics:
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class SupplementedScore:
    deterministic: Mapping[str, float]
    judge_score: float | None
    judge_error: str | None


def evaluate_answer_facts(
    answer: str, *, required_facts: Sequence[str], forbidden_facts: Sequence[str]
) -> AnswerFactMetrics:
    normalized = answer.casefold()
    required_hits = sum(item.casefold() in normalized for item in required_facts)
    forbidden_hits = sum(item.casefold() in normalized for item in forbidden_facts)
    required_recall = required_hits / len(required_facts) if required_facts else 1.0
    forbidden_rate = forbidden_hits / len(forbidden_facts) if forbidden_facts else 0.0
    return AnswerFactMetrics(
        required_fact_recall=required_recall,
        forbidden_fact_hit_rate=forbidden_rate,
        passed=required_recall == 1.0 and forbidden_rate == 0.0,
    )


def evaluate_citation_ids(actual: Sequence[str], expected: Sequence[str]) -> CitationMetrics:
    actual_counts = Counter(actual)
    expected_counts = Counter(expected)
    matches = sum(
        min(count, expected_counts[citation]) for citation, count in actual_counts.items()
    )
    precision = matches / len(actual) if actual else float(len(expected) == 0)
    recall = matches / len(expected) if expected else float(len(actual) == 0)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return CitationMetrics(precision=precision, recall=recall, f1=f1)


def supplement_with_optional_judge(
    deterministic: Mapping[str, float], judge: Callable[[], float]
) -> SupplementedScore:
    try:
        score = judge()
    except Exception as error:
        return SupplementedScore(
            deterministic=dict(deterministic),
            judge_score=None,
            judge_error=type(error).__name__,
        )
    return SupplementedScore(deterministic=dict(deterministic), judge_score=score, judge_error=None)
