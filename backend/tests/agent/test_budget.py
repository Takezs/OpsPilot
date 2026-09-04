import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from opspilot.agent.runner import (
    AgentBudget,
    AgentDecision,
    AgentRunner,
    GroundedSearchResult,
    detached_agent_task_count,
    drain_detached_agent_tasks,
)
from opspilot.generation.citations import ValidatedAnswer
from opspilot.retrieval.context_builder import BuiltContext, ContextFragment
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.types import ToolDefinition, ToolEffect, ToolResult


class EmptyArgs(BaseModel):
    pass


class AnswerProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, _state: object, _tools: object) -> AgentDecision:
        self.calls += 1
        return AgentDecision(kind="answer", answer="unsafe success")


@pytest.mark.parametrize(
    "budget",
    [
        AgentBudget(max_model_calls=0),
        AgentBudget(max_input_tokens=1),
        AgentBudget(max_duration_seconds=0),
    ],
)
async def test_agent_budget_exhaustion_fails_closed_before_provider(budget: AgentBudget) -> None:
    provider = AnswerProvider()
    runner = AgentRunner(ToolRegistry(()), provider, budget=budget)
    outcome = await runner.run("this prompt exceeds a one token budget")
    assert outcome.bounded is True
    assert outcome.final_answer is None
    assert provider.calls == 0


async def test_total_duration_budget_stops_between_model_rounds() -> None:
    class SlowToolProvider:
        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            await asyncio.sleep(0.02)
            return AgentDecision(kind="tool_call", tool="missing", arguments={})

    runner = AgentRunner(
        ToolRegistry(()),
        SlowToolProvider(),
        budget=AgentBudget(max_duration_seconds=0.001),
    )
    outcome = await runner.run("hello")
    assert outcome.bounded is True


async def test_inflight_decision_is_cancelled_at_monotonic_deadline() -> None:
    cancelled = asyncio.Event()

    class HangingProvider:
        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    runner = AgentRunner(
        ToolRegistry(()), HangingProvider(), budget=AgentBudget(max_duration_seconds=0.01)
    )
    outcome = await asyncio.wait_for(runner.run("hello"), 0.5)
    assert outcome.bounded is True
    assert cancelled.is_set()


async def test_grounded_generation_consumes_model_budget() -> None:
    class Decisions:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            self.calls += 1
            if self.calls == 2:
                return AgentDecision(
                    kind="answer", answer="must be replaced by grounded generation"
                )
            return AgentDecision(kind="tool_call", tool="search_knowledge", arguments={})

    generation_calls = 0

    async def search(_args: object) -> GroundedSearchResult:
        fragment = ContextFragment(
            document_id="00000000-0000-0000-0000-000000000001",
            chunk_id="00000000-0000-0000-0000-000000000002",
            title="title",
            document_version=1,
            section_path=(),
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            page=None,
            content="evidence",
            token_count=1,
        )
        context = BuiltContext(
            fragments=(fragment,), token_budget=100, total_tokens=17, truncated=False
        )
        return GroundedSearchResult(ToolResult(ok=True), context)

    async def generate(_query: str, _context: BuiltContext) -> ValidatedAnswer:
        nonlocal generation_calls
        generation_calls += 1
        return ValidatedAnswer(answer="x", snapshots=())

    async def unused_invoke(_args: EmptyArgs) -> ToolResult:
        raise AssertionError("knowledge_search_handler must own this call")

    tool = ToolDefinition(
        name="search_knowledge",
        description="",
        input_schema=EmptyArgs,
        effect=ToolEffect.READ_ONLY,
        idempotency_capable=True,
        supports_reconciliation=True,
        invoke=unused_invoke,
    )
    provider = Decisions()
    runner = AgentRunner(
        ToolRegistry([tool]),
        provider,
        knowledge_search_handler=search,
        grounded_answer_handler=generate,
        budget=AgentBudget(max_model_calls=2),
    )
    outcome = await runner.run("q")
    assert outcome.bounded is True
    assert provider.calls == 2
    assert generation_calls == 0


async def test_deadline_supervises_processor_that_ignores_cancellation() -> None:
    release = asyncio.Event()

    class IgnoringProvider:
        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return AgentDecision(kind="answer", answer="late")

    runner = AgentRunner(
        ToolRegistry([]),
        IgnoringProvider(),
        budget=AgentBudget(max_duration_seconds=0.01),
    )
    try:
        outcome = await asyncio.wait_for(runner.run("hello"), 2.5)
        assert outcome.bounded is True
        assert detached_agent_task_count() == 1
        await asyncio.wait_for(drain_detached_agent_tasks(), 1.5)
        assert detached_agent_task_count() == 1
    finally:
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if detached_agent_task_count() == 0:
                break
    assert detached_agent_task_count() == 0


@pytest.mark.parametrize("effect", [ToolEffect.READ_ONLY, ToolEffect.SIDE_EFFECT])
async def test_inflight_tool_path_is_cancelled_at_deadline(effect: ToolEffect) -> None:
    cancelled = asyncio.Event()

    async def hanging(_arg: object, *_extra: object) -> ToolResult:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    class ToolDecision:
        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            return AgentDecision(kind="tool_call", tool="bounded_tool", arguments={})

    tool = ToolDefinition(
        name="bounded_tool",
        description="",
        input_schema=EmptyArgs,
        effect=effect,
        idempotency_capable=True,
        supports_reconciliation=True,
        invoke=hanging,
    )
    runner = AgentRunner(
        ToolRegistry([tool]),
        ToolDecision(),
        side_effect_handler=hanging if effect is ToolEffect.SIDE_EFFECT else None,
        budget=AgentBudget(max_duration_seconds=0.01),
    )
    outcome = await asyncio.wait_for(runner.run("hello"), 0.5)
    assert outcome.bounded is True
    assert cancelled.is_set()
    assert outcome.workflow_facts == ()


async def test_inflight_retrieval_is_cancelled_at_deadline() -> None:
    cancelled = asyncio.Event()

    async def hanging_search(_arg: object) -> GroundedSearchResult:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    class SearchDecision:
        async def decide(self, _state: object, _tools: object) -> AgentDecision:
            return AgentDecision(kind="tool_call", tool="search_knowledge", arguments={})

    async def unused(_arg: EmptyArgs) -> ToolResult:
        raise AssertionError

    search = ToolDefinition(
        name="search_knowledge",
        description="",
        input_schema=EmptyArgs,
        effect=ToolEffect.READ_ONLY,
        idempotency_capable=True,
        supports_reconciliation=True,
        invoke=unused,
    )
    runner = AgentRunner(
        ToolRegistry([search]),
        SearchDecision(),
        knowledge_search_handler=hanging_search,
        budget=AgentBudget(max_duration_seconds=0.01),
    )
    outcome = await asyncio.wait_for(runner.run("hello"), 0.5)
    assert outcome.bounded is True
    assert outcome.final_answer is not None
    assert cancelled.is_set()
