"""Bounded agent loop: decisions, tool execution and termination limits.

Task 8 guarantees:
- pure Q&A answers without any tool call;
- missing order numbers produce a clarification instead of a guess;
- tool calls are executed only through the registry with Pydantic-validated
  arguments and the result is handed back to the decider;
- the loop stops after at most 8 rounds or 6 tool invocations.

Task 9 fix (side-effect bypass closed): the runner never executes a SIDE_EFFECT
tool's adapter directly. ``refund_order`` decisions are routed to the durable
Operation creation flow (the injected side-effect handler); with no handler the
runner returns a fail-closed result. READ_ONLY tools keep the Task 8 contract.
"""

import json
import uuid
from typing import Any

import pytest

from opspilot.agent.prompts import render_system_prompt
from opspilot.agent.runner import (
    AgentDecision,
    AgentRunner,
    DecisionError,
    GroundedSearchResult,
    _parse_decision,
    _render_api_messages,
)
from opspilot.agent.state import AgentMessage, AgentState
from opspilot.generation.citations import ValidatedAnswer
from opspilot.retrieval.context_builder import BuiltContext, CitationSnapshot, ContextFragment
from opspilot.tools.registry import (
    ToolArgumentError,
    ToolDependencies,
    build_tool_registry,
)
from opspilot.tools.schemas import (
    CheckRefundEligibilityArgs,
    GetOrderArgs,
    GetRefundStatusArgs,
    RefundOrderArgs,
    SearchKnowledgeArgs,
    SendEmailArgs,
)
from opspilot.tools.types import ToolResult


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search_knowledge(self, args: SearchKnowledgeArgs) -> ToolResult:
        self.calls.append(("search_knowledge", {"query": args.query, "top_k": args.top_k}))
        return ToolResult(ok=True, data={"candidates": [{"chunk_id": "c1"}]})

    async def get_order(self, args: GetOrderArgs) -> ToolResult:
        self.calls.append(("get_order", {"order_number": args.order_number}))
        return ToolResult(ok=True, data={"order_number": args.order_number, "status": "OPEN"})

    async def check_refund_eligibility(self, args: CheckRefundEligibilityArgs) -> ToolResult:
        self.calls.append(("check_refund_eligibility", {"order_number": args.order_number}))
        return ToolResult(ok=True, data={"order_number": args.order_number, "eligible": True})

    async def refund_order(self, args: RefundOrderArgs) -> ToolResult:
        self.calls.append(("refund_order", {"order_number": args.order_number}))
        return ToolResult(ok=True, data={"order_number": args.order_number, "status": "REFUNDED"})

    async def get_refund_status(self, args: GetRefundStatusArgs) -> ToolResult:
        self.calls.append(("get_refund_status", {"order_number": args.order_number}))
        return ToolResult(
            ok=True, data={"order_number": args.order_number, "status": "NOT_REFUNDED"}
        )

    async def send_email(self, args: SendEmailArgs) -> ToolResult:
        self.calls.append(
            ("send_email", {"to": args.to, "subject": args.subject, "body": args.body})
        )
        return ToolResult(ok=True, data={"delivered": True})


def _registry(fake: FakeTools):
    deps = ToolDependencies(
        search_knowledge=fake.search_knowledge,
        get_order=fake.get_order,
        check_refund_eligibility=fake.check_refund_eligibility,
        refund_order=fake.refund_order,
        get_refund_status=fake.get_refund_status,
        send_email=fake.send_email,
    )
    return build_tool_registry(deps)


def _answer(text: str) -> AgentDecision:
    return AgentDecision(kind="answer", answer=text)


def _clarify(question: str) -> AgentDecision:
    return AgentDecision(kind="clarify", question=question)


def _tool(name: str, **arguments: Any) -> AgentDecision:
    return AgentDecision(kind="tool_call", tool=name, arguments=arguments)


def _grounded(search_call_id: str) -> AgentDecision:
    return AgentDecision(kind="grounded_answer", search_call_id=search_call_id)


class ScriptedDecisionProvider:
    """Deterministic decider that records the tool results it observed."""

    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = decisions
        self.tool_results_seen: list[str] = []

    async def decide(self, state: AgentState, tools: tuple[Any, ...]) -> AgentDecision:
        self.tool_results_seen.extend(
            message.content for message in state.messages if message.role == "tool"
        )
        return self.decisions.pop(0)


async def test_pure_qa_returns_answer_without_tools() -> None:
    fake = FakeTools()
    runner = AgentRunner(
        _registry(fake), ScriptedDecisionProvider([_answer("The refund window is 30 days.")])
    )

    outcome = await runner.run("What is the refund window?")

    assert outcome.final_answer == "The refund window is 30 days."
    assert outcome.tool_calls == 0
    assert outcome.bounded is False
    assert fake.calls == []


async def test_missing_order_number_asks_for_clarification() -> None:
    fake = FakeTools()
    runner = AgentRunner(
        _registry(fake), ScriptedDecisionProvider([_clarify("Please provide the order number.")])
    )

    outcome = await runner.run("Can I get a refund?")

    assert outcome.clarification == "Please provide the order number."
    assert outcome.final_answer is None
    assert outcome.tool_calls == 0
    assert fake.calls == []


async def test_correct_tool_invoked_with_validated_arguments() -> None:
    fake = FakeTools()
    provider = ScriptedDecisionProvider(
        [_tool("get_order", order_number="A100"), _answer("Order A100 is OPEN.")]
    )
    runner = AgentRunner(_registry(fake), provider)

    outcome = await runner.run("What is the status of order A100?")

    assert fake.calls == [("get_order", {"order_number": "A100"})]
    assert outcome.final_answer == "Order A100 is OPEN."
    assert outcome.tool_calls == 1
    assert provider.tool_results_seen != []
    assert "A100" in provider.tool_results_seen[0]


async def test_invalid_arguments_raise_tool_argument_error() -> None:
    fake = FakeTools()
    runner = AgentRunner(_registry(fake), ScriptedDecisionProvider([_tool("get_order")]))

    with pytest.raises(ToolArgumentError):
        await runner.run("status of ?")
    assert fake.calls == []


async def test_stops_after_max_rounds() -> None:
    fake = FakeTools()
    decisions = [_tool("get_order", order_number=f"A{i}") for i in range(9)]
    runner = AgentRunner(
        _registry(fake),
        ScriptedDecisionProvider(decisions),
        max_rounds=8,
        max_tool_calls=100,
    )

    outcome = await runner.run("check several orders")

    assert outcome.bounded is True
    assert outcome.rounds == 8
    assert outcome.tool_calls == 8


async def test_stops_after_max_tool_calls_with_defaults() -> None:
    fake = FakeTools()
    decisions = [_tool("get_order", order_number=f"A{i}") for i in range(7)]
    runner = AgentRunner(_registry(fake), ScriptedDecisionProvider(decisions))

    outcome = await runner.run("check several orders")

    assert outcome.bounded is True
    assert outcome.tool_calls == 6
    assert outcome.rounds == 7


def test_parse_decision_rejects_non_object_json() -> None:
    for content in ("[1, 2]", '"hello"', "42", "null", "true"):
        with pytest.raises(DecisionError):
            _parse_decision(content)


def test_parse_decision_rejects_missing_or_empty_answer() -> None:
    for payload in ({"type": "answer"}, {"type": "answer", "answer": ""}):
        with pytest.raises(DecisionError):
            _parse_decision(json.dumps(payload))


def test_parse_decision_rejects_non_string_answer() -> None:
    with pytest.raises(DecisionError):
        _parse_decision(json.dumps({"type": "answer", "answer": 42}))


def test_parse_decision_rejects_missing_or_empty_question() -> None:
    for payload in ({"type": "clarify"}, {"type": "clarify", "question": "   "}):
        with pytest.raises(DecisionError):
            _parse_decision(json.dumps(payload))


def test_parse_decision_rejects_missing_or_empty_tool() -> None:
    for payload in ({"type": "tool_call"}, {"type": "tool_call", "tool": ""}):
        with pytest.raises(DecisionError):
            _parse_decision(json.dumps(payload))


def test_parse_decision_rejects_missing_or_non_object_arguments() -> None:
    for payload in (
        {"type": "tool_call", "tool": "get_order"},
        {"type": "tool_call", "tool": "get_order", "arguments": ["A100"]},
        {"type": "tool_call", "tool": "get_order", "arguments": "A100"},
    ):
        with pytest.raises(DecisionError):
            _parse_decision(json.dumps(payload))


def test_parse_decision_rejects_unknown_type() -> None:
    with pytest.raises(DecisionError):
        _parse_decision(json.dumps({"type": "hack", "answer": "x"}))


def test_parse_decision_accepts_valid_decisions() -> None:
    answer = _parse_decision(json.dumps({"type": "answer", "answer": "The window is 30 days."}))
    assert answer.kind == "answer"
    assert answer.answer == "The window is 30 days."

    clarify = _parse_decision(json.dumps({"type": "clarify", "question": "Which order?"}))
    assert clarify.kind == "clarify"
    assert clarify.question == "Which order?"

    tool_call = _parse_decision(
        json.dumps(
            {"type": "tool_call", "tool": "get_order", "arguments": {"order_number": "A100"}}
        )
    )
    assert tool_call.kind == "tool_call"
    assert tool_call.tool == "get_order"
    assert tool_call.arguments == {"order_number": "A100"}


def test_render_api_messages_never_emits_tool_role() -> None:
    state = AgentState(
        messages=[
            AgentMessage(role="user", content="status of A100"),
            AgentMessage(role="tool", content='{"status": "REFUNDED"}', tool_name="get_order"),
        ]
    )

    messages = _render_api_messages(state, ())

    assert "tool" not in [message["role"] for message in messages]


def test_render_api_messages_labels_tool_output_as_untrusted() -> None:
    state = AgentState(
        messages=[
            AgentMessage(role="user", content="status of A100"),
            AgentMessage(role="tool", content='{"status": "REFUNDED"}', tool_name="get_order"),
        ]
    )

    messages = _render_api_messages(state, ())
    untrusted = [
        message["content"]
        for message in messages
        if message["role"] == "user" and "[UNTRUSTED TOOL OUTPUT" in message["content"]
    ]

    assert len(untrusted) == 1
    assert "get_order" in untrusted[0]
    assert '{"status": "REFUNDED"}' in untrusted[0]


def test_decision_prompt_requires_grounded_answer_after_successful_search() -> None:
    prompt = render_system_prompt(())

    assert "MUST use grounded_answer" in prompt
    assert "MUST NOT use answer" in prompt


def test_render_api_messages_preserves_regular_roles() -> None:
    state = AgentState(
        messages=[
            AgentMessage(role="user", content="hello"),
            AgentMessage(role="assistant", content="I will check."),
        ]
    )

    messages = _render_api_messages(state, ())

    assert [message["role"] for message in messages] == ["system", "user", "assistant"]


def test_render_api_messages_keeps_runner_control_separate_from_untrusted_tool_data() -> None:
    state = AgentState(
        messages=[
            AgentMessage(role="tool", content='{"result_count":1}', tool_name="search_knowledge"),
            AgentMessage(
                role="control",
                content="verified search_call_id: server-only-id",
                tool_name="search_knowledge",
            ),
        ]
    )

    messages = _render_api_messages(state, ())

    assert messages[1]["role"] == "user"
    assert "UNTRUSTED TOOL OUTPUT" in messages[1]["content"]
    assert "server-only-id" not in messages[1]["content"]
    assert messages[2] == {
        "role": "system",
        "content": "verified search_call_id: server-only-id",
    }


async def test_runner_raises_decision_error_for_tool_call_without_tool_name() -> None:
    fake = FakeTools()
    runner = AgentRunner(
        _registry(fake),
        ScriptedDecisionProvider([AgentDecision(kind="tool_call")]),
    )

    with pytest.raises(DecisionError):
        await runner.run("status of ?")
    assert fake.calls == []


# --- Task 9 fix: SIDE_EFFECT tools are never executed through their adapter ---


async def test_refund_order_decisions_never_invoke_payment_adapter() -> None:
    """Any refund_order decision routes to the operation handler, never the adapter."""
    fake = FakeTools()
    received: list[tuple[str, dict[str, Any]]] = []

    async def handler(definition: Any, arguments: Any) -> ToolResult:
        received.append((definition.name, arguments.model_dump(mode="json")))
        return ToolResult(
            ok=True, data={"operation_id": str(uuid.uuid4()), "status": "WAITING_APPROVAL"}
        )

    amounts = [50.0, 250.0, 5000.0]
    decisions = [_tool("refund_order", order_number="A100", amount=a) for a in amounts]
    decisions.append(_answer("created operations"))
    runner = AgentRunner(
        _registry(fake),
        ScriptedDecisionProvider(decisions),
        side_effect_handler=handler,
    )

    outcome = await runner.run("refund A100")

    # the payment adapter was never invoked, for any amount
    assert all(name != "refund_order" for name, _ in fake.calls)
    # every refund_order decision was routed to the durable operation handler
    assert [name for name, _ in received] == ["refund_order"] * len(amounts)
    assert outcome.final_answer == "created operations"
    assert outcome.tool_calls == len(amounts)
    assert len(outcome.workflow_facts) == len(amounts)
    assert all(fact.tool == "refund_order" for fact in outcome.workflow_facts)
    assert all(fact.status == "WAITING_APPROVAL" for fact in outcome.workflow_facts)


async def test_side_effect_without_handler_is_fail_closed() -> None:
    """With no handler wired, a SIDE_EFFECT decision must not call its adapter."""
    fake = FakeTools()
    runner = AgentRunner(
        _registry(fake),
        ScriptedDecisionProvider(
            [
                _tool("refund_order", order_number="A100", amount=250.0),
                _answer("blocked"),
            ]
        ),
    )

    outcome = await runner.run("refund A100")

    assert all(name != "refund_order" for name, _ in fake.calls)
    assert outcome.final_answer == "blocked"


async def test_grounded_answer_uses_runner_owned_id_and_exact_built_context() -> None:
    fake = FakeTools()
    fragment = ContextFragment(
        document_id="d1",
        chunk_id="c1",
        title="Policy",
        document_version=3,
        section_path=("Refunds",),
        effective_at=__import__("datetime").datetime(2026, 1, 1),
        page=2,
        content="Refunds require approval.",
        token_count=5,
    )
    context = BuiltContext((fragment,), token_budget=100, total_tokens=21, truncated=False)
    observed: list[tuple[str, BuiltContext]] = []

    async def search(_: Any) -> GroundedSearchResult:
        return GroundedSearchResult(
            summary=ToolResult(
                ok=True,
                data={"result_count": 1, "search_call_id": "provider-chosen-id"},
            ),
            context=context,
        )

    async def generate(query: str, exact: BuiltContext) -> ValidatedAnswer:
        observed.append((query, exact))
        return ValidatedAnswer(
            answer="Approval is required [DOC:d1#c1].",
            citations=("[DOC:d1#c1]",),
            snapshots=(CitationSnapshot("d1", 3, "c1", ("Refunds",), 2),),
            insufficient_evidence=False,
            follow_up_question=None,
        )

    async def capture_search(arguments: Any) -> GroundedSearchResult:
        result = await search(arguments)
        return result

    class CapturingDecider:
        call_id: str | None = None
        attempted_plain_answer = False

        async def decide(self, state: AgentState, tools: tuple[Any, ...]) -> AgentDecision:
            if len(state.messages) == 1:
                return _tool("search_knowledge", query="refund policy", top_k=5)
            if self.call_id is None:
                tool_message = next(message for message in state.messages if message.role == "tool")
                control = next(message for message in state.messages if message.role == "control")
                payload = json.loads(tool_message.content)
                assert "search_call_id" not in payload
                self.call_id = control.content.rsplit(": ", 1)[1]
                assert self.call_id not in {"", "provider-chosen-id"}
                assert "Refunds require approval" not in tool_message.content
                self.attempted_plain_answer = True
                return _answer("uncited factual answer")
            assert self.call_id in state.messages[-1].content
            return _grounded(self.call_id)

    runner = AgentRunner(
        _registry(fake),
        CapturingDecider(),
        knowledge_search_handler=capture_search,
        grounded_answer_handler=generate,
    )
    outcome = await runner.run("What is the refund policy?")

    assert observed == [("What is the refund policy?", context)]
    # The model's uncited plain answer is discarded; with one unambiguous
    # server-owned context the runner immediately performs validated generation.
    assert outcome.rounds == 2
    assert outcome.final_answer == "Approval is required [DOC:d1#c1]."
    assert outcome.citation_snapshots == (CitationSnapshot("d1", 3, "c1", ("Refunds",), 2),)


async def test_grounded_answer_rejects_unknown_or_cross_run_search_id_fail_closed() -> None:
    fake = FakeTools()
    runner = AgentRunner(
        _registry(fake),
        ScriptedDecisionProvider([_grounded("another-run-id")]),
        max_rounds=1,
    )

    outcome = await runner.run("question")

    assert outcome.final_answer == "Unable to answer from verified knowledge evidence."
    assert outcome.citation_snapshots == ()


async def test_failed_search_does_not_issue_a_grounded_call_id() -> None:
    fake = FakeTools()

    async def failed(_: Any) -> GroundedSearchResult:
        return GroundedSearchResult(
            summary=ToolResult(
                ok=False,
                data={"search_call_id": "provider-chosen-id"},
                error="search unavailable",
            ),
            context=BuiltContext((), token_budget=100, total_tokens=0, truncated=False),
        )

    provider = ScriptedDecisionProvider(
        [
            _tool("search_knowledge", query="refund", top_k=5),
            _answer("uncited policy fact"),
            _answer("another uncited policy fact"),
        ]
    )
    outcome = await AgentRunner(
        _registry(fake),
        provider,
        knowledge_search_handler=failed,
        max_rounds=3,
    ).run("question")

    assert outcome.final_answer == "Unable to answer from verified knowledge evidence."
    assert "policy fact" not in outcome.final_answer
    assert outcome.citation_snapshots == ()
    assert "search_call_id" not in provider.tool_results_seen[0]


async def test_empty_context_is_not_a_successful_grounded_search() -> None:
    fake = FakeTools()

    async def empty(_: Any) -> GroundedSearchResult:
        return GroundedSearchResult(
            summary=ToolResult(ok=True, data={"result_count": 0}),
            context=BuiltContext((), token_budget=100, total_tokens=0, truncated=False),
        )

    provider = ScriptedDecisionProvider(
        [
            _tool("search_knowledge", query="missing marker", top_k=5),
            _answer("invented policy fact"),
        ]
    )
    outcome = await AgentRunner(
        _registry(fake), provider, knowledge_search_handler=empty, max_rounds=2
    ).run("question")

    assert "search_call_id" not in provider.tool_results_seen[0]
    assert outcome.final_answer == "Unable to answer from verified knowledge evidence."
    assert outcome.citation_snapshots == ()


async def test_wrong_grounded_id_cannot_escape_to_plain_uncited_answer() -> None:
    fake = FakeTools()
    fragment = ContextFragment(
        document_id="d1",
        chunk_id="c1",
        title="Policy",
        document_version=1,
        section_path=("Refunds",),
        effective_at=__import__("datetime").datetime(2026, 1, 1),
        page=1,
        content="Verified policy fact.",
        token_count=4,
    )
    context = BuiltContext((fragment,), token_budget=100, total_tokens=20, truncated=False)

    async def search(_: Any) -> GroundedSearchResult:
        return GroundedSearchResult(summary=ToolResult(ok=True), context=context)

    class WrongThenPlain:
        async def decide(self, state: AgentState, tools: tuple[Any, ...]) -> AgentDecision:
            if len(state.messages) == 1:
                return _tool("search_knowledge", query="policy", top_k=5)
            if len(state.messages) == 2:
                return _grounded("wrong-id")
            return _answer("uncited policy fact")

    outcome = await AgentRunner(
        _registry(fake),
        WrongThenPlain(),
        knowledge_search_handler=search,
        max_rounds=3,
    ).run("question")

    assert outcome.final_answer == "Unable to answer from verified knowledge evidence."
    assert "uncited policy fact" not in outcome.final_answer
    assert outcome.citation_snapshots == ()


async def test_insufficient_validated_answer_cannot_persist_provider_factual_text() -> None:
    fake = FakeTools()
    fragment = ContextFragment(
        document_id="d1",
        chunk_id="c1",
        title="Policy",
        document_version=1,
        section_path=("Refunds",),
        effective_at=__import__("datetime").datetime(2026, 1, 1),
        page=1,
        content="Verified policy fact.",
        token_count=4,
    )
    context = BuiltContext((fragment,), token_budget=100, total_tokens=20, truncated=False)

    async def search(_: Any) -> GroundedSearchResult:
        return GroundedSearchResult(summary=ToolResult(ok=True), context=context)

    async def insufficient(_: str, __: BuiltContext) -> ValidatedAnswer:
        return ValidatedAnswer(
            answer="provider supplied uncited policy fact",
            citations=(),
            snapshots=(),
            insufficient_evidence=True,
            follow_up_question=None,
        )

    class GroundedDecider:
        async def decide(self, state: AgentState, tools: tuple[Any, ...]) -> AgentDecision:
            if len(state.messages) == 1:
                return _tool("search_knowledge", query="policy", top_k=5)
            control = next(message for message in state.messages if message.role == "control")
            return _grounded(control.content.rsplit(": ", 1)[1])

    outcome = await AgentRunner(
        _registry(fake),
        GroundedDecider(),
        knowledge_search_handler=search,
        grounded_answer_handler=insufficient,
    ).run("question")

    assert outcome.final_answer == "Unable to answer from verified knowledge evidence."
    assert "provider supplied" not in outcome.final_answer
    assert outcome.citation_snapshots == ()


async def test_plain_answer_and_clarification_never_have_citations() -> None:
    fake = FakeTools()
    answer = await AgentRunner(
        _registry(fake), ScriptedDecisionProvider([_answer("unverified")])
    ).run("question")
    clarification = await AgentRunner(
        _registry(fake), ScriptedDecisionProvider([_clarify("which policy?")])
    ).run("question")

    assert answer.citation_snapshots == ()
    assert clarification.citation_snapshots == ()


def test_parse_grounded_answer_requires_non_empty_search_call_id() -> None:
    decision = _parse_decision(
        json.dumps({"type": "grounded_answer", "search_call_id": "server-id"})
    )
    assert decision == AgentDecision(kind="grounded_answer", search_call_id="server-id")
    for value in (None, "", "   ", 3):
        with pytest.raises(DecisionError):
            _parse_decision(json.dumps({"type": "grounded_answer", "search_call_id": value}))
