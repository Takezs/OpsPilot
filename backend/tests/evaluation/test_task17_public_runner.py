import json

import httpx
import pytest

from opspilot.evaluation.runner import PublicEvaluationApi
from opspilot.evaluation.schemas import EvaluationCase, ExpectedOutcome


@pytest.mark.asyncio
async def test_runner_uses_only_public_authorized_http_contract() -> None:
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
                        "payload": {"content": "answer", "citations": []},
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
        follow_up_required=False,
        approval_required=False,
        expected_final_state=ExpectedOutcome.ANSWERED,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        result = await PublicEvaluationApi(client, "opaque-token", poll_seconds=0)(case, 1)
    assert result.actual_output["run_id"] == "run-1"
    assert [item[:2] for item in seen] == [
        ("POST", "/runs"),
        ("POST", "/runs/run-1/messages"),
        ("GET", "/runs/run-1/history"),
        ("GET", "/runs/run-1"),
    ]
    assert all(item[2] == "Bearer opaque-token" for item in seen)
