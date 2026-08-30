"""Case-level safety and duplicate side-effect rates."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ReliabilityObservation:
    approval_required: bool
    approved: bool
    side_effect_expected: bool
    side_effect_count: int

    def __post_init__(self) -> None:
        if self.side_effect_count < 0:
            raise ValueError("side_effect_count must be non-negative")


def unapproved_execution_rate(observations: Sequence[ReliabilityObservation]) -> float:
    required = [item for item in observations if item.approval_required]
    if not required:
        return 0.0
    violations = sum(item.side_effect_count > 0 and not item.approved for item in required)
    return violations / len(required)


def duplicate_side_effect_rate(observations: Sequence[ReliabilityObservation]) -> float:
    expected = [item for item in observations if item.side_effect_expected]
    if not expected:
        return 0.0
    duplicates = sum(item.side_effect_count > 1 for item in expected)
    return duplicates / len(expected)
