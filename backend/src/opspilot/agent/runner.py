"""Bounded agent loop that only calls registry-exposed tools.

The loop asks a decision provider (the "brain") for one action per round,
executes tool calls only through the ``ToolRegistry`` after Pydantic validation,
and stops after at most ``max_rounds`` rounds or ``max_tool_calls`` tool
invocations. It never persists or displays hidden thinking.

SIDE_EFFECT tools are never executed through their adapter: the runner hands the
decision to the injected ``side_effect_handler`` (the durable Operation flow) or
fails closed when none is wired. READ_ONLY tools keep the direct-invocation
contract.
"""

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from opspilot.agent.prompts import render_system_prompt, render_untrusted_tool_output
from opspilot.agent.state import AgentMessage, AgentState
from opspilot.generation.citations import ValidatedAnswer
from opspilot.retrieval.context_builder import BuiltContext, CitationSnapshot
from opspilot.tools.registry import ToolRegistry, validate_arguments
from opspilot.tools.types import ToolDefinition, ToolEffect, ToolResult

DecisionKind = Literal["answer", "clarify", "tool_call", "grounded_answer"]


@dataclass(frozen=True)
class AgentDecision:
    """The decider's chosen action for one round."""

    kind: DecisionKind
    answer: str | None = None
    question: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    search_call_id: str | None = None


@dataclass(frozen=True)
class GroundedSearchResult:
    """Server-owned search result; only ``summary`` is exposed to the decider."""

    summary: ToolResult
    context: BuiltContext


@dataclass(frozen=True)
class WorkflowFact:
    """Durable server fact returned by a SIDE_EFFECT operation handler."""

    operation_id: str
    tool: str
    status: str


@dataclass(frozen=True)
class AgentOutcome:
    final_answer: str | None = None
    clarification: str | None = None
    rounds: int = 0
    tool_calls: int = 0
    bounded: bool = False
    citation_snapshots: tuple[CitationSnapshot, ...] = ()
    workflow_facts: tuple[WorkflowFact, ...] = ()
    grounded: bool = False


class DecisionProvider(Protocol):
    async def decide(
        self, state: AgentState, tools: tuple[ToolDefinition, ...]
    ) -> AgentDecision: ...


SideEffectHandler = Callable[[ToolDefinition, Any], Awaitable[ToolResult]]
KnowledgeSearchHandler = Callable[[Any], Awaitable[GroundedSearchResult]]
GroundedAnswerHandler = Callable[[str, BuiltContext], Awaitable[ValidatedAnswer]]


class AgentRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        decision_provider: DecisionProvider,
        *,
        max_rounds: int = 8,
        max_tool_calls: int = 6,
        side_effect_handler: SideEffectHandler | None = None,
        knowledge_search_handler: KnowledgeSearchHandler | None = None,
        grounded_answer_handler: GroundedAnswerHandler | None = None,
    ) -> None:
        self._registry = registry
        self._decision_provider = decision_provider
        self._max_rounds = max_rounds
        self._max_tool_calls = max_tool_calls
        self._side_effect_handler = side_effect_handler
        self._knowledge_search_handler = knowledge_search_handler
        self._grounded_answer_handler = grounded_answer_handler

    async def run(self, user_message: str) -> AgentOutcome:
        state = AgentState(messages=[AgentMessage(role="user", content=user_message)])
        grounded_contexts: dict[str, BuiltContext] = {}
        workflow_facts: list[WorkflowFact] = []
        while True:
            if state.rounds >= self._max_rounds:
                return AgentOutcome(
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    bounded=True,
                    workflow_facts=tuple(workflow_facts),
                )
            state.rounds += 1
            decision = await self._decision_provider.decide(state, self._registry.all())
            if decision.kind == "answer":
                return AgentOutcome(
                    final_answer=decision.answer,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind == "clarify":
                return AgentOutcome(
                    clarification=decision.question,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind == "grounded_answer":
                call_id = decision.search_call_id
                context = grounded_contexts.get(call_id or "")
                if context is None:
                    raise DecisionError("grounded_answer references an unknown or expired search")
                if self._grounded_answer_handler is None:
                    raise DecisionError("grounded answer generation is not configured")
                validated = await self._grounded_answer_handler(user_message, context)
                return AgentOutcome(
                    final_answer=validated.answer,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    citation_snapshots=validated.snapshots,
                    workflow_facts=tuple(workflow_facts),
                    grounded=True,
                )
            if state.tool_calls >= self._max_tool_calls:
                return AgentOutcome(
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    bounded=True,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind != "tool_call":
                raise DecisionError(
                    f"decision provider returned unsupported decision kind: {decision.kind!r}"
                )
            tool_name = decision.tool
            if tool_name is None or not tool_name:
                raise DecisionError("decision provider returned a tool_call without a tool name")
            definition = self._registry.get(tool_name)
            arguments = validate_arguments(definition, decision.arguments or {})
            state.tool_calls += 1
            if definition.name == "search_knowledge" and self._knowledge_search_handler:
                grounded = await self._knowledge_search_handler(arguments)
                summary = dict(grounded.summary.data or {})
                summary.pop("search_call_id", None)
                if grounded.summary.ok:
                    call_id = secrets.token_urlsafe(24)
                    while call_id in grounded_contexts:
                        call_id = secrets.token_urlsafe(24)
                    if len(grounded_contexts) >= self._max_rounds:
                        raise DecisionError("grounded search context limit exceeded")
                    grounded_contexts[call_id] = grounded.context
                    summary["search_call_id"] = call_id
                result = ToolResult(
                    ok=grounded.summary.ok,
                    data=summary,
                    error=grounded.summary.error,
                    provider_not_called=grounded.summary.provider_not_called,
                    failure_kind=grounded.summary.failure_kind,
                )
            elif definition.effect is ToolEffect.SIDE_EFFECT:
                # The runner never executes a SIDE_EFFECT adapter directly: the
                # decision is handed to the durable-operation handler, or fails
                # closed when none is wired. Only READ_ONLY tools keep the Task 8
                # direct-invocation contract.
                if self._side_effect_handler is None:
                    result = ToolResult(
                        ok=False,
                        error=(
                            f"tool {definition.name!r} requires the durable "
                            "operation flow; refusing to call its adapter directly"
                        ),
                    )
                else:
                    result = await self._side_effect_handler(definition, arguments)
                    fact = _workflow_fact(definition, result)
                    if fact is not None:
                        workflow_facts.append(fact)
            else:
                result = await definition.invoke(arguments)
            state.messages.append(
                AgentMessage(
                    role="tool",
                    content=result.render(),
                    tool_name=definition.name,
                    tool_arguments=decision.arguments,
                )
            )


def _workflow_fact(definition: ToolDefinition, result: ToolResult) -> WorkflowFact | None:
    """Read only the server-produced durable Operation shape, never model text."""
    if not result.ok or result.data is None:
        return None
    operation_id = result.data.get("operation_id")
    status = result.data.get("status")
    if not isinstance(operation_id, str) or not operation_id:
        return None
    allowed_statuses = {
        "WAITING_APPROVAL",
        "READY",
        "EXECUTING",
        "OUTCOME_UNKNOWN",
        "RECONCILING",
        "MANUAL_REVIEW",
        "DENIED",
        "REJECTED",
        "FAILED",
        "SUCCEEDED",
    }
    if not isinstance(status, str) or status not in allowed_statuses:
        return None
    return WorkflowFact(operation_id=operation_id, tool=definition.name, status=status)


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
        proxy_url: str | None = None,
    ) -> None:
        self.model = model
        http_client = httpx.AsyncClient(proxy=proxy_url) if proxy_url else None
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            http_client=http_client,
        )

    async def decide(self, state: AgentState, tools: tuple[ToolDefinition, ...]) -> AgentDecision:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=_render_api_messages(state, tools),
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if content is None:
            raise DecisionError("decider returned an empty response")
        return _parse_decision(content)

    async def aclose(self) -> None:
        """Close the owned OpenAI-compatible HTTP client."""
        await self._client.close()


def _render_api_messages(
    state: AgentState, tools: tuple[ToolDefinition, ...]
) -> list[ChatCompletionMessageParam]:
    """Map the run history onto chat messages without a raw ``tool`` role.

    A native ``role: tool`` message requires a preceding assistant ``tool_call``
    with a ``tool_call_id``; the JSON decision protocol records no such call, so
    tool results are re-emitted as labeled user context that the model treats as
    untrusted tool output.
    """
    messages: list[ChatCompletionMessageParam] = [
        cast(
            ChatCompletionMessageParam,
            {"role": "system", "content": render_system_prompt(tools)},
        )
    ]
    for message in state.messages:
        if message.role == "tool":
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {
                        "role": "user",
                        "content": render_untrusted_tool_output(message.tool_name, message.content),
                    },
                )
            )
        else:
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {"role": message.role, "content": message.content},
                )
            )
    return messages


def _parse_decision(content: str) -> AgentDecision:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise DecisionError(f"decider returned invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise DecisionError("decider decision must be a JSON object")
    kind = payload.get("type")
    if kind == "answer":
        answer = payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise DecisionError("decider answer decision needs a non-empty 'answer' string")
        return AgentDecision(kind="answer", answer=answer)
    if kind == "clarify":
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise DecisionError("decider clarify decision needs a non-empty 'question' string")
        return AgentDecision(kind="clarify", question=question)
    if kind == "tool_call":
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise DecisionError("decider tool_call decision needs a non-empty 'tool' name")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise DecisionError("decider tool_call decision 'arguments' must be a JSON object")
        return AgentDecision(kind="tool_call", tool=tool, arguments=arguments)
    if kind == "grounded_answer":
        call_id = payload.get("search_call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise DecisionError(
                "decider grounded_answer decision needs a non-empty 'search_call_id' string"
            )
        return AgentDecision(kind="grounded_answer", search_call_id=call_id)
    raise DecisionError(f"decider returned unknown decision type: {kind!r}")
