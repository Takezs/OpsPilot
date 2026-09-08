import asyncio

import httpx
import pytest

from opspilot.evaluation.runner import PublicEvaluationApi
from tests.evaluation.test_task17_worker_fencing import _case


@pytest.mark.asyncio
async def test_late_public_run_is_collected_after_old_sixty_second_window(monkeypatch):
    polls = 0
    creates = 0

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def handler(request):
        nonlocal polls, creates
        if request.method == "POST" and request.url.path == "/runs":
            creates += 1
            return httpx.Response(202, json={"run_id": "late-run"})
        if request.method == "POST":
            return httpx.Response(202, json={"message_id": "late-message"})
        if request.url.path.endswith("by-correlation/evaluation:late:1"):
            return httpx.Response(404, json={"detail": "run not found"})
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json=[]
                if polls < 245
                else [
                    {
                        "event_type": "assistant_message_created",
                        "payload": {"content": "late answer", "citations": []},
                    }
                ],
            )
        polls += 1
        return httpx.Response(
            200,
            json={
                "status": "RUNNING" if polls < 245 else "COMPLETED",
                "operations": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        result = await PublicEvaluationApi(client, "unit-token")(_case("late"), 1)
    assert result.actual_output["run_id"] == "late-run"
    assert creates == 1


@pytest.mark.asyncio
async def test_correlated_empty_run_gets_one_idempotent_message():
    messages = 0

    def handler(request):
        nonlocal messages
        if request.url.path.endswith("by-correlation/evaluation:empty:1"):
            return httpx.Response(200, json={"run_id": "empty-run", "status": "RUNNING"})
        if request.url.path.endswith("/history"):
            if messages == 0:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {"event_type": "user_message_created", "payload": {}},
                    {
                        "event_type": "assistant_message_created",
                        "payload": {"content": "ok", "citations": []},
                    },
                ],
            )
        if request.method == "POST" and request.url.path.endswith("/messages"):
            messages += 1
            return httpx.Response(202, json={"message_id": "one"})
        return httpx.Response(200, json={"status": "COMPLETED", "operations": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        await PublicEvaluationApi(client, "token", poll_seconds=0)(_case("empty"), 1)
        assert messages == 1
