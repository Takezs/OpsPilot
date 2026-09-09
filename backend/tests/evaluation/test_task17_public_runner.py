import json

import httpx
import pytest

from opspilot.evaluation.runner import PublicEvaluationApi
from opspilot.evaluation.schemas import (
    AgentEvaluationCase,
    ApprovalExpectation,
    EvaluationCase,
    ExpectedOutcome,
    ToolExpectation,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["ANSWERED", "CLARIFICATION"])
async def test_runner_uses_only_public_authorized_http_contract(response_kind) -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(200, json={"run_id": "run-1"})
        if request.method == "POST":
            assert json.loads(request.content)["content"] == "policy question"
            return httpx.Response(202, json={"message_id": "message-1"})
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json=[
                    {
                        "event_type": "assistant_message_created",
                        "payload": {
                            "content": "answer",
                            "citations": [],
                            "response_kind": response_kind,
                        },
                    }
                ],
            )
        return httpx.Response(200, json={"status": "COMPLETED", "operations": []})

    case = EvaluationCase(
        schema_version="1.0.0",
        case_id="dev-public-http",
        category="knowledge",
        query="policy question",
        relevant_chunk_ids=(),
        expected_citation_ids=(),
        required_facts=(),
        forbidden_facts=(),
        expected_tools=(),
        forbidden_tools=(),
        follow_up_required=response_kind == "CLARIFICATION",
        approval_required=False,
        expected_final_state=ExpectedOutcome(response_kind),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        result = await PublicEvaluationApi(client, "opaque-token", poll_seconds=0)(case, 1)
    assert result.actual_output["run_id"] == "run-1"
    assert result.actual_output["final_state"] == response_kind
    assert result.deterministic_scores["task_success"] is True
    assert [item[:2] for item in seen] == [
        ("GET", "/runs/by-correlation/evaluation:dev-public-http:1"),
        ("POST", "/runs"),
        ("POST", "/runs/run-1/messages"),
        ("GET", "/runs/run-1/history"),
        ("GET", "/runs/run-1"),
    ]
    assert all(item[2] == "Bearer opaque-token" for item in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempts", "duplicate"),
    [
        ([], False),
        ([{"kind": "RECONCILIATION", "status": "SUCCEEDED"}] * 2, False),
        ([{"kind": "EXECUTION", "status": "SUCCEEDED"}] * 2, True),
        (
            [
                {"kind": "EXECUTION", "status": "SUCCEEDED"},
                {"kind": "RECONCILIATION", "status": "SUCCEEDED"},
            ],
            False,
        ),
    ],
)
async def test_agent_case_rejects_wrong_tool_arguments_and_durable_facts(
    attempts, duplicate
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/runs":
            return httpx.Response(200, json={"run_id": "run-2"})
        if request.method == "POST":
            return httpx.Response(202, json={"message_id": "message-2"})
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json=[
                    {
                        "event_type": "assistant_message_created",
                        "payload": {
                            "content": "waiting",
                            "citations": [],
                            "tool_calls": [
                                {
                                    "name": "refund_order",
                                    "arguments": {"order_number": "ORD-999", "amount": 1},
                                }
                            ],
                        },
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "status": "RUNNING",
                "operations": [
                    {
                        "id": "op-1",
                        "tool_name": "refund_order",
                        "status": "WAITING_APPROVAL",
                        "version": 0,
                        "policy_decision": "REQUIRE_APPROVAL",
                        "provider_reference_id": None,
                        "normalized_arguments": {"order_number": "ORD-999", "amount": 1},
                        "idempotency_key": "refund:ORD-999",
                        "attempts": attempts,
                    }
                ],
            },
        )

    case = AgentEvaluationCase(
        schema_version="1.0.0",
        case_id="agent-strict",
        category="approval_required",
        query="refund",
        relevant_chunk_ids=(),
        expected_citation_ids=(),
        required_facts=(),
        forbidden_facts=(),
        expected_tools=(
            ToolExpectation(
                name="refund_order", arguments={"order_number": "ORD-1003", "amount": 350}
            ),
        ),
        forbidden_tools=(),
        follow_up_required=False,
        approval_required=True,
        expected_final_state=ExpectedOutcome.WAITING_APPROVAL,
        operation_expected=True,
        order_number="ORD-1003",
        amount="350.00",
        expected_idempotency_key="refund:ORD-1003",
        expected_approval=ApprovalExpectation.PENDING,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        result = await PublicEvaluationApi(client, "opaque-token", poll_seconds=0)(case, 1)
    assert result.deterministic_scores["tool_precision"] == 0.0
    assert result.deterministic_scores["order_number_match"] is False
    assert result.deterministic_scores["task_success"] is False
    assert result.actual_output["duplicate_side_effect"] is duplicate
