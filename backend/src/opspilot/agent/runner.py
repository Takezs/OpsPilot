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

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from opspilot.agent.prompts import render_system_prompt, render_untrusted_tool_output
from opspilot.agent.state import AgentMessage, AgentState
from opspilot.generation.citations import ValidatedAnswer
from opspilot.observability.tracing import traced_stage
from opspilot.retrieval.context_builder import BuiltContext, CitationSnapshot
from opspilot.tools.registry import ToolRegistry, validate_arguments
from opspilot.tools.types import ToolDefinition, ToolEffect, ToolResult

DecisionKind = Literal["answer", "clarify", "tool_call", "grounded_answer"]
INSUFFICIENT_EVIDENCE_REPLY = "Unable to answer from verified knowledge evidence."
_CANCEL_GRACE_SECONDS = 1.0
_GROUNDED_CONTROL_TOKEN_OVERHEAD = 32
_DETACHED_AGENT_TASKS: set[asyncio.Task[Any]] = set()


class AgentBudgetExceeded(RuntimeError):
    """An in-flight Agent operation crossed its monotonic deadline."""


def _consume_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled() or not task.done():
        return
    try:
        task.result()
    except BaseException:
        pass


def _forget_task(task: asyncio.Task[Any]) -> None:
    _consume_task(task)
    _DETACHED_AGENT_TASKS.discard(task)


def _supervise_detached(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume_task(task)
        return
    _DETACHED_AGENT_TASKS.add(task)
    task.add_done_callback(_forget_task)


async def drain_detached_agent_tasks() -> None:
    """Cancel and retrieve cancellation-resistant Agent operations at shutdown."""
    tasks = tuple(_DETACHED_AGENT_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        done, _ = await asyncio.wait(tasks, timeout=_CANCEL_GRACE_SECONDS)
        for task in done:
            _consume_task(task)


def detached_agent_task_count() -> int:
    """Return cancellation-resistant operations still owned by the supervisor."""
    return len(_DETACHED_AGENT_TASKS)


async def _cancel_bounded(task: asyncio.Task[Any]) -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
    if done:
        _consume_task(task)
    else:
        _supervise_detached(task)


async def _await_before_deadline(awaitable: Awaitable[Any], deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise AgentBudgetExceeded
    task: asyncio.Task[Any] = asyncio.create_task(cast(Coroutine[Any, Any, Any], awaitable))
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        await _cancel_bounded(task)
        raise
    if task not in done:
        await _cancel_bounded(task)
        raise AgentBudgetExceeded
    return task.result()


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
class ExecutedToolCall:
    name: str
    arguments: dict[str, Any]


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
    executed_tool_calls: tuple[ExecutedToolCall, ...] = ()
    model_calls: int = 0
    input_tokens: int = 0


@dataclass(frozen=True)
class AgentBudget:
    max_model_calls: int = 8
    max_tool_calls: int = 6
    max_input_tokens: int = 16_000
    max_duration_seconds: float = 300.0


def _estimated_tokens(messages: list[AgentMessage]) -> int:
    return sum(max(1, len(message.content.encode("utf-8")) // 4) for message in messages)


def _grounded_prompt_tokens(query: str, context: BuiltContext) -> int:
    if context.total_tokens < 0:
        raise DecisionError("grounded context token count cannot be negative")
    query_tokens = max(1, len(query.encode("utf-8")) // 4)
    return query_tokens + context.total_tokens + _GROUNDED_CONTROL_TOKEN_OVERHEAD


class DecisionProvider(Protocol):
    async def decide(
        self, state: AgentState, tools: tuple[ToolDefinition, ...]
    ) -> AgentDecision: ...


SideEffectHandler = Callable[[ToolDefinition, Any], Awaitable[ToolResult]]
KnowledgeSearchHandler = Callable[[Any], Awaitable[GroundedSearchResult]]
ModelCallReservation = Callable[[], None]
GroundedAnswerHandler = Callable[
    [str, BuiltContext, ModelCallReservation], Awaitable[ValidatedAnswer]
]


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
        budget: AgentBudget | None = None,
        run_id: str = "unpersisted",
    ) -> None:
        self._registry = registry
        self._decision_provider = decision_provider
        self._max_rounds = max_rounds
        self._max_tool_calls = max_tool_calls
        self._side_effect_handler = side_effect_handler
        self._knowledge_search_handler = knowledge_search_handler
        self._grounded_answer_handler = grounded_answer_handler
        self._budget = budget or AgentBudget(
            max_model_calls=max_rounds,
            max_tool_calls=max_tool_calls,
        )
        self._run_id = run_id

    async def run(self, user_message: str) -> AgentOutcome:
        started_at = time.monotonic()
        deadline = started_at + self._budget.max_duration_seconds
        model_calls = 0
        input_tokens = 0
        state = AgentState(messages=[AgentMessage(role="user", content=user_message)])
        grounded_contexts: dict[str, BuiltContext] = {}
        workflow_facts: list[WorkflowFact] = []
        executed_tool_calls: list[ExecutedToolCall] = []
        knowledge_search_attempted = False

        def outcome(**values: Any) -> AgentOutcome:
            return AgentOutcome(
                executed_tool_calls=tuple(executed_tool_calls),
                model_calls=model_calls,
                input_tokens=input_tokens,
                **values,
            )

        def reserve_model_call(prompt_tokens: int) -> None:
            nonlocal input_tokens, model_calls
            if (
                model_calls >= self._budget.max_model_calls
                or input_tokens + prompt_tokens > self._budget.max_input_tokens
            ):
                raise AgentBudgetExceeded
            model_calls += 1
            input_tokens += prompt_tokens

        async def grounded_outcome(context: BuiltContext) -> AgentOutcome:
            if self._grounded_answer_handler is None:
                raise DecisionError("grounded answer generation is not configured")
            prompt_tokens = _grounded_prompt_tokens(user_message, context)
            with traced_stage(
                "llm",
                self._run_id,
                {"decision": "grounded_answer", "input_tokens_per_attempt": prompt_tokens},
            ):
                try:
                    validated = await _await_before_deadline(
                        self._grounded_answer_handler(
                            user_message,
                            context,
                            lambda: reserve_model_call(prompt_tokens),
                        ),
                        deadline,
                    )
                except AgentBudgetExceeded:
                    return outcome(
                        final_answer=INSUFFICIENT_EVIDENCE_REPLY,
                        rounds=state.rounds,
                        tool_calls=state.tool_calls,
                        bounded=True,
                        workflow_facts=tuple(workflow_facts),
                    )
            if validated.follow_up_question is not None:
                return outcome(
                    clarification=validated.follow_up_question,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                    grounded=True,
                )
            if validated.insufficient_evidence or not validated.snapshots:
                return outcome(
                    final_answer=INSUFFICIENT_EVIDENCE_REPLY,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                    grounded=True,
                )
            return outcome(
                final_answer=validated.answer,
                rounds=state.rounds,
                tool_calls=state.tool_calls,
                citation_snapshots=validated.snapshots,
                workflow_facts=tuple(workflow_facts),
                grounded=True,
            )

        while True:
            budget_exhausted = (
                state.rounds >= self._max_rounds
                or model_calls >= self._budget.max_model_calls
                or time.monotonic() - started_at >= self._budget.max_duration_seconds
            )
            if budget_exhausted:
                if knowledge_search_attempted:
                    return outcome(
                        final_answer=INSUFFICIENT_EVIDENCE_REPLY,
                        rounds=state.rounds,
                        tool_calls=state.tool_calls,
                        bounded=True,
                        workflow_facts=tuple(workflow_facts),
                    )
                return outcome(
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    bounded=True,
                    workflow_facts=tuple(workflow_facts),
                )
            state.rounds += 1
            decision_input_tokens = _estimated_tokens(state.messages)
            try:
                reserve_model_call(decision_input_tokens)
            except AgentBudgetExceeded:
                return outcome(
                    rounds=state.rounds - 1,
                    tool_calls=state.tool_calls,
                    bounded=True,
                    workflow_facts=tuple(workflow_facts),
                )
            with traced_stage(
                "llm",
                self._run_id,
                {"decision": "agent_decision", "input_tokens": decision_input_tokens},
            ):
                try:
                    decision = await _await_before_deadline(
                        self._decision_provider.decide(state, self._registry.all()), deadline
                    )
                except AgentBudgetExceeded:
                    return outcome(
                        rounds=state.rounds,
                        tool_calls=state.tool_calls,
                        bounded=True,
                        workflow_facts=tuple(workflow_facts),
                    )
            if time.monotonic() - started_at >= self._budget.max_duration_seconds:
                return outcome(
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    bounded=True,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind == "answer":
                if knowledge_search_attempted:
                    if len(grounded_contexts) == 1 and self._grounded_answer_handler is not None:
                        # Model prose is never persisted here. Once there is one
                        # unambiguous server-owned context, a plain final action
                        # is converted into the same validated generation path.
                        return await grounded_outcome(next(iter(grounded_contexts.values())))
                    valid_ids = ", ".join(sorted(grounded_contexts)) or "none"
                    state.messages.append(
                        AgentMessage(
                            role="control",
                            tool_name="search_knowledge",
                            content=(
                                "error: answer is forbidden after a knowledge search attempt. "
                                "Use a valid grounded decision when evidence exists, otherwise "
                                "retry search_knowledge. Grounded decision shape: "
                                '{"type":"grounded_answer","search_call_id":"<valid-id>"}. '
                                f"Valid server-issued ids for this run: {valid_ids}"
                            ),
                        )
                    )
                    continue
                return outcome(
                    final_answer=decision.answer,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind == "clarify":
                if knowledge_search_attempted:
                    return outcome(
                        final_answer=INSUFFICIENT_EVIDENCE_REPLY,
                        rounds=state.rounds,
                        tool_calls=state.tool_calls,
                        workflow_facts=tuple(workflow_facts),
                    )
                return outcome(
                    clarification=decision.question,
                    rounds=state.rounds,
                    tool_calls=state.tool_calls,
                    workflow_facts=tuple(workflow_facts),
                )
            if decision.kind == "grounded_answer":
                knowledge_search_attempted = True
                call_id = decision.search_call_id
                context = grounded_contexts.get(call_id or "")
                if context is None:
                    state.messages.append(
                        AgentMessage(
                            role="control",
                            tool_name="search_knowledge",
                            content=(
                                "error: grounded_answer references an unknown or expired "
                                "server search id; retry search_knowledge and use only the "
                                "new server-issued id"
                            ),
                        )
                    )
                    continue
                return await grounded_outcome(context)
            if state.tool_calls >= min(self._max_tool_calls, self._budget.max_tool_calls):
                return outcome(
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
            with traced_stage("tool.policy", self._run_id, {"tool": tool_name}):
                definition = self._registry.get(tool_name)
                arguments = validate_arguments(definition, decision.arguments or {})
            executed_tool_calls.append(
                ExecutedToolCall(
                    name=definition.name,
                    arguments=arguments.model_dump(mode="json"),
                )
            )
            state.tool_calls += 1
            issued_search_call_id: str | None = None
            if definition.name == "search_knowledge" and self._knowledge_search_handler:
                knowledge_search_attempted = True
                with traced_stage("retrieval", self._run_id, {"tool": definition.name}):
                    try:
                        grounded = await _await_before_deadline(
                            self._knowledge_search_handler(arguments), deadline
                        )
                    except AgentBudgetExceeded:
                        return outcome(
                            final_answer=INSUFFICIENT_EVIDENCE_REPLY,
                            rounds=state.rounds,
                            tool_calls=state.tool_calls,
                            bounded=True,
                            workflow_facts=tuple(workflow_facts),
                        )
                summary = dict(grounded.summary.data or {})
                summary.pop("search_call_id", None)
                has_evidence = bool(grounded.context.fragments)
                if grounded.summary.ok and has_evidence:
                    call_id = secrets.token_urlsafe(24)
                    while call_id in grounded_contexts:
                        call_id = secrets.token_urlsafe(24)
                    if len(grounded_contexts) >= self._max_rounds:
                        raise DecisionError("grounded search context limit exceeded")
                    grounded_contexts[call_id] = grounded.context
                    issued_search_call_id = call_id
                result = ToolResult(
                    ok=grounded.summary.ok and has_evidence,
                    data=summary,
                    error=(
                        grounded.summary.error
                        if grounded.summary.error or has_evidence
                        else "search returned no usable evidence"
                    ),
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
                    with traced_stage("tool.execute", self._run_id, {"tool": definition.name}):
                        try:
                            result = await _await_before_deadline(
                                self._side_effect_handler(definition, arguments), deadline
                            )
                        except AgentBudgetExceeded:
                            return outcome(
                                rounds=state.rounds,
                                tool_calls=state.tool_calls,
                                bounded=True,
                                workflow_facts=tuple(workflow_facts),
                            )
                    fact = _workflow_fact(definition, result)
                    if fact is not None:
                        workflow_facts.append(fact)
            else:
                with traced_stage("tool.execute", self._run_id, {"tool": definition.name}):
                    try:
                        result = await _await_before_deadline(
                            definition.invoke(arguments), deadline
                        )
                    except AgentBudgetExceeded:
                        return outcome(
                            rounds=state.rounds,
                            tool_calls=state.tool_calls,
                            bounded=True,
                            workflow_facts=tuple(workflow_facts),
                        )
            state.messages.append(
                AgentMessage(
                    role="tool",
                    content=result.render(),
                    tool_name=definition.name,
                    tool_arguments=decision.arguments,
                )
            )
            if issued_search_call_id is not None:
                state.messages.append(
                    AgentMessage(
                        role="control",
                        tool_name="search_knowledge",
                        content=f"verified search_call_id: {issued_search_call_id}",
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
        elif message.role == "control":
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {"role": "system", "content": message.content},
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
