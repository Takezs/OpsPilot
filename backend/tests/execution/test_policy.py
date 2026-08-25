"""Deterministic refund policy and server-side idempotency derivation (unit).

Task 9 acceptance: the policy must be deterministic and unbypassable by the
prompt/agent (pure function over the normalized amount), the idempotency key
must be derived server-side from the business parameter (``refund:{order_id}``,
no round/timestamp/id suffix), and the arguments hash must be stable across
calls for the same normalized arguments.
"""

import pytest

from opspilot.execution.idempotency import (
    derive_refund_idempotency_key,
    normalize_refund_arguments,
    stable_arguments_hash,
)
from opspilot.execution.policy import PolicyDecision, PolicyError, decide_refund_policy


def test_amount_at_or_below_100_allows() -> None:
    for amount in (0.01, 50.0, 99.99, 100.0):
        assert decide_refund_policy(amount) is PolicyDecision.ALLOW


def test_amount_above_100_requires_approval() -> None:
    for amount in (100.01, 250.0, 999.99, 1000.0):
        assert decide_refund_policy(amount) is PolicyDecision.REQUIRE_APPROVAL


def test_amount_above_1000_denies() -> None:
    for amount in (1000.01, 1200.0, 10_000.0):
        assert decide_refund_policy(amount) is PolicyDecision.DENY


def test_non_finite_or_non_positive_amount_fails_closed() -> None:
    for amount in (float("nan"), float("inf"), 0.0, -5.0):
        with pytest.raises(PolicyError):
            decide_refund_policy(amount)


def test_idempotency_key_is_stable_and_server_derived() -> None:
    assert derive_refund_idempotency_key("A100") == "refund:A100"
    assert derive_refund_idempotency_key("A100") == derive_refund_idempotency_key("A100")
    assert derive_refund_idempotency_key("A100") != derive_refund_idempotency_key("A101")


def test_normalize_refund_arguments_uses_pydantic_schema() -> None:
    normalized = normalize_refund_arguments(order_number="A100", amount=250.0)
    assert normalized == {"order_number": "A100", "amount": 250.0}


def test_arguments_hash_is_stable_and_param_sensitive() -> None:
    first = stable_arguments_hash({"order_number": "A100", "amount": 250.0})
    assert first == stable_arguments_hash({"order_number": "A100", "amount": 250.0})
    assert len(first) == 64
    assert stable_arguments_hash({"order_number": "A100", "amount": 300.0}) != first
    assert stable_arguments_hash({"order_number": "A101", "amount": 250.0}) != first


def test_arguments_hash_is_key_order_insensitive() -> None:
    assert stable_arguments_hash(
        {"order_number": "A100", "amount": 250.0}
    ) == stable_arguments_hash({"amount": 250.0, "order_number": "A100"})
