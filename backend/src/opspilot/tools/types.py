"""Shared types for the Tool Gateway's registry surface."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ToolEffect(StrEnum):
    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool invocation, safe for journaling."""

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None

    def render(self) -> str:
        if self.ok:
            return json.dumps(self.data, ensure_ascii=False, default=str)
        return f"error: {self.error}"


@dataclass(frozen=True)
class ToolDefinition:
    """Immutable contract describing one registry-exposed tool.

    ``effect``, ``idempotency_capable`` and ``supports_reconciliation`` drive
    the failure handling policy (task 9/10): a side-effect tool that may have
    reached the provider must be reconciled, never blindly retried.
    """

    name: str
    description: str
    input_schema: type[BaseModel]
    effect: ToolEffect
    idempotency_capable: bool
    supports_reconciliation: bool
    invoke: Callable[..., Awaitable[ToolResult]]


class ToolNotFoundError(LookupError):
    """Raised when the registry has no tool with the requested name."""


class ToolArgumentError(ValueError):
    """Raised when tool arguments fail Pydantic schema validation."""
