"""ToolDefinition surface, registry lookup and Pydantic argument validation.

Task 8 guarantees:
- the registry exposes exactly the six allowed tools;
- every tool carries ``effect`` (read_only | side_effect), idempotency capability
  and ``supports_reconciliation`` so failure handling (task 9/10) can decide
  retry versus reconciliation;
- every tool argument set is validated through its Pydantic input schema.
"""

import pytest

from opspilot.tools.registry import (
    ToolDependencies,
    ToolNotFoundError,
    build_tool_registry,
    validate_arguments,
)
from opspilot.tools.types import ToolArgumentError, ToolEffect, ToolResult

ALLOWED_TOOLS = frozenset(
    {
        "search_knowledge",
        "get_order",
        "check_refund_eligibility",
        "refund_order",
        "get_refund_status",
        "send_email",
    }
)


async def _stub_ok(*args: object, **kwargs: object) -> ToolResult:
    return ToolResult(ok=True, data={"stub": True})


def _registry():
    deps = ToolDependencies(
        search_knowledge=_stub_ok,
        get_order=_stub_ok,
        check_refund_eligibility=_stub_ok,
        refund_order=_stub_ok,
        get_refund_status=_stub_ok,
        send_email=_stub_ok,
    )
    return build_tool_registry(deps)


def test_registry_exposes_exactly_the_six_allowed_tools() -> None:
    assert set(_registry().names()) == ALLOWED_TOOLS


def test_refund_order_is_side_effect_idempotent_and_reconciliable() -> None:
    definition = _registry().get("refund_order")
    assert definition.effect == ToolEffect.SIDE_EFFECT
    assert definition.idempotency_capable is True
    assert definition.supports_reconciliation is True


def test_send_email_is_side_effect() -> None:
    assert _registry().get("send_email").effect == ToolEffect.SIDE_EFFECT


def test_read_only_tools_are_idempotent_and_reconciliable() -> None:
    for name in ("search_knowledge", "get_order", "check_refund_eligibility", "get_refund_status"):
        definition = _registry().get(name)
        assert definition.effect == ToolEffect.READ_ONLY
        assert definition.idempotency_capable is True
        assert definition.supports_reconciliation is True


def test_registry_get_unknown_tool_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        _registry().get("delete_everything")


def test_arguments_validate_through_pydantic_schema() -> None:
    definition = _registry().get("get_order")
    parsed = validate_arguments(definition, {"order_number": "A100"})
    assert parsed.order_number == "A100"


def test_missing_required_argument_raises_tool_argument_error() -> None:
    definition = _registry().get("get_order")
    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {})


def test_send_email_rejects_invalid_recipient() -> None:
    definition = _registry().get("send_email")
    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"to": "not-an-email", "subject": "hi", "body": "hello"})
