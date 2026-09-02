import asyncio

import pytest

from opspilot.agent.runner import AgentBudget, AgentDecision, AgentRunner
from opspilot.tools.registry import ToolRegistry


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
