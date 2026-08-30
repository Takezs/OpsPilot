"""Mutable state for one bounded agent run."""

from dataclasses import dataclass, field
from typing import Any, Literal

MessageRole = Literal["user", "assistant", "tool", "control"]


@dataclass(frozen=True)
class AgentMessage:
    role: MessageRole
    content: str
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None


@dataclass
class AgentState:
    messages: list[AgentMessage] = field(default_factory=list)
    rounds: int = 0
    tool_calls: int = 0
