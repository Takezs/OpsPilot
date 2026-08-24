"""Tool Registry: the restricted surface an agent loop is allowed to call."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from opspilot.tools.schemas import (
    CheckRefundEligibilityArgs,
    GetOrderArgs,
    GetRefundStatusArgs,
    RefundOrderArgs,
    SearchKnowledgeArgs,
    SendEmailArgs,
)
from opspilot.tools.types import (
    ToolArgumentError,
    ToolDefinition,
    ToolEffect,
    ToolNotFoundError,
    ToolResult,
)


@dataclass(frozen=True)
class ToolDependencies:
    """Wiring for the six tools; adapters implement the actual I/O.

    Each slot receives the already-validated arguments model so adapters never
    re-parse raw JSON.
    """

    search_knowledge: Callable[[SearchKnowledgeArgs], Awaitable[ToolResult]]
    get_order: Callable[[GetOrderArgs], Awaitable[ToolResult]]
    check_refund_eligibility: Callable[[CheckRefundEligibilityArgs], Awaitable[ToolResult]]
    refund_order: Callable[[RefundOrderArgs], Awaitable[ToolResult]]
    get_refund_status: Callable[[GetRefundStatusArgs], Awaitable[ToolResult]]
    send_email: Callable[[SendEmailArgs], Awaitable[ToolResult]]


class ToolRegistry:
    """Ordered set of tool definitions, looked up by name."""

    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._by_name = {definition.name: definition for definition in definitions}

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._by_name[name]
        except KeyError as error:
            raise ToolNotFoundError(f"unknown tool: {name}") from error

    def all(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._by_name.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)


def validate_arguments(definition: ToolDefinition, arguments: dict[str, Any]) -> BaseModel:
    """Validate raw tool arguments through the definition's Pydantic schema."""
    try:
        return definition.input_schema.model_validate(arguments)
    except ValidationError as error:
        raise ToolArgumentError(f"invalid arguments for {definition.name}: {error}") from error


def build_tool_registry(deps: ToolDependencies) -> ToolRegistry:
    """Build the restricted six-tool registry shared by the agent loop."""
    return ToolRegistry(
        [
            ToolDefinition(
                name="search_knowledge",
                description="Search the permitted knowledge bases and return ranked chunks.",
                input_schema=SearchKnowledgeArgs,
                effect=ToolEffect.READ_ONLY,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.search_knowledge,
            ),
            ToolDefinition(
                name="get_order",
                description="Look up an order by its order number.",
                input_schema=GetOrderArgs,
                effect=ToolEffect.READ_ONLY,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.get_order,
            ),
            ToolDefinition(
                name="check_refund_eligibility",
                description="Check whether an order can be refunded and its current refund status.",
                input_schema=CheckRefundEligibilityArgs,
                effect=ToolEffect.READ_ONLY,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.check_refund_eligibility,
            ),
            ToolDefinition(
                name="refund_order",
                description="Refund an order through the payment provider.",
                input_schema=RefundOrderArgs,
                effect=ToolEffect.SIDE_EFFECT,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.refund_order,
            ),
            ToolDefinition(
                name="get_refund_status",
                description="Return the payment provider's refund status for an order.",
                input_schema=GetRefundStatusArgs,
                effect=ToolEffect.READ_ONLY,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.get_refund_status,
            ),
            ToolDefinition(
                name="send_email",
                description="Send an email notification.",
                input_schema=SendEmailArgs,
                effect=ToolEffect.SIDE_EFFECT,
                idempotency_capable=True,
                supports_reconciliation=True,
                invoke=deps.send_email,
            ),
        ]
    )
