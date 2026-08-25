"""Deterministic refund policy.

The policy is a pure function over the normalized refund amount and can never
be bypassed by a prompt or the agent: every Operation whose amount lands in the
REQUIRE_APPROVAL band is durably bound to a pending ApprovalRequest before it
becomes executable, and a DENY is recorded as a terminal Operation without an
occupancy. Non-finite and non-positive amounts fail closed.
"""

import math
from enum import StrEnum


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class PolicyError(ValueError):
    """Raised when an amount cannot be judged by the refund policy."""


def decide_refund_policy(amount: float) -> PolicyDecision:
    """Judge a refund amount deterministically (inclusive thresholds).

    ``<= 100`` is allowed outright, ``100 < amount <= 1000`` requires approval,
    and ``> 1000`` is denied. The thresholds mirror the demo order catalogue
    (A101 = 50, A100 = 250, A102 = 350, A103 = 1200).
    """
    if not math.isfinite(amount) or amount <= 0:
        raise PolicyError(f"invalid refund amount: {amount!r}")
    if amount <= 100.0:
        return PolicyDecision.ALLOW
    if amount <= 1000.0:
        return PolicyDecision.REQUIRE_APPROVAL
    return PolicyDecision.DENY
