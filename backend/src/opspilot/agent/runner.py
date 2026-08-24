"""Bounded agent loop that only calls registry-exposed tools.

The loop asks a decision provider (the "brain") for one action per round,
executes tool calls only through the ``ToolRegistry`` after Pydantic validation,
and stops after at most ``max_rounds`` rounds or ``max_tool_calls`` tool
invocations. It never persists or displays hidden thinking.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from opspilot.agent.prompts import render_system_prompt
from opspilot.agent.state import AgentMessage, AgentState
from opspilot.tools.registry import ToolRegistry, validate_arguments
from opspilot.tools.types import ToolDefinition

DecisionKind = Literal["answer", "clarify", "tool_call"]


@dataclass(frozen=True)
class AgentDecision:
    """The decider's chosen action for one round."""

    kind: DecisionKind
    answer: str | None = None
    question: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentOutcome:
    final_answer: str | None = None
    clarification: str | None = None
    rounds: int = 0
    tool_calls: int = 0
    bounded: bool = False


class DecisionProvider(Protocol):
    async def decide(
        self, state: AgentState, tools: tuple[ToolDefinition, ...]
    ) -> AgentDecision: ...


class AgentRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        decision_provider: DecisionProvider,
        *,
        max_rounds: int = 8,
        max_tool_calls: int = 6,
    ) -> None:
        self._registry = registry
        self._decision_provider = decision_provider
        self._max_rounds = max_rounds
        self._max_tool_calls = max_tool_calls

    async def run(self, user_message: str) -> AgentOutcome:
        state = AgentState(messages=[AgentMessage(role="user", content=user_message)])
        while True:
            if state.rounds >= self._max_rounds:
                return AgentOutcome(rounds=state.rounds, tool_calls=state.tool_calls, bounded=True)
            state.rounds += 1
            decision = await self._decision_provider.decide(state, self._registry.all())
            if decision.kind == "answer":
                return AgentOutcome(
                    final_answer=decision.answer,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                )
            if decision.kind == "clarify":
                return AgentOutcome(
                    clarification=decision.question,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                )
            if state.tool_calls >= self._max_tool_calls:
                return AgentOutcome(rounds=state.rounds, tool_calls=state.tool_calls, bounded=True)
            assert decision.tool is not None
            definition = self._registry.get(decision.tool)
            arguments = validate_arguments(definition, decision.arguments or {})
            state.tool_calls += 1
            result = await definition.invoke(arguments)
            state.messages.append(
                AgentMessage(
                    role="tool",
                    content=result.render(),
                    tool_name=definition.name,
                    tool_arguments=decision.arguments,
                )
            )


class DecisionError(RuntimeError):
    """Raised when the decider returns malformed JSON or no valid action."""


class DeepSeekAgentDecider:
    """DeepSeek V4 Flash decider speaking the JSON decision contract."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)

    async def decide(self, state: AgentState, tools: tuple[ToolDefinition, ...]) -> AgentDecision:
        messages: list[ChatCompletionMessageParam] = [
            cast(
                ChatCompletionMessageParam,
                {"role": "system", "content": render_system_prompt(tools)},
            ),
            *[
                cast(
                    ChatCompletionMessageParam,
                    {"role": message.role, "content": message.content},
                )
                for message in state.messages
            ],
        ]
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if content is None:
            raise DecisionError("decider returned an empty response")
        return _parse_decision(content)


def _parse_decision(content: str) -> AgentDecision:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise DecisionError(f"decider returned invalid JSON: {error}") from error
    kind = payload.get("type")
    if kind == "answer":
        return AgentDecision(kind="answer", answer=str(payload.get("answer", "")))
    if kind == "clarify":
        return AgentDecision(kind="clarify", question=str(payload.get("question", "")))
    if kind == "tool_call":
        arguments = payload.get("arguments")
        return AgentDecision(
            kind="tool_call",
            tool=str(payload.get("tool", "")),
            arguments=dict(arguments) if isinstance(arguments, dict) else {},
        )
    raise DecisionError(f"decider returned unknown decision type: {kind!r}")
