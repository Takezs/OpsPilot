import uuid

import httpx
import pytest

from opspilot.evaluation.runner import PublicEvaluationApi
from tests.evaluation.test_task17_worker_fencing import _case


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403, 503])
async def test_missing_evidence_blocks_model_and_run_creation(status):
    calls = []
    document, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    case = _case("synthetic-evidence").model_copy(
        update={
            "expected_citation_ids": (f"[DOC:{document}#{chunk}]",),
            "relevant_chunk_ids": (chunk,),
        }
    )

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(status, json={"detail": "unavailable"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        with pytest.raises(ValueError, match="evaluation evidence unavailable"):
            await PublicEvaluationApi(client, "unit-token")(case, 1)
    assert calls == [("GET", f"/knowledge/documents/{document}")]


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["valid", "wrong_chunk", "empty", "not_ready"])
async def test_evidence_requires_ready_exact_nonempty_chunk(variant):
    document, chunk = str(uuid.uuid4()), str(uuid.uuid4())
    case = _case("synthetic").model_copy(
        update={
            "expected_citation_ids": (f"[DOC:{document}#{chunk}]",),
        }
    )

    def handler(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer unit-token"
        if "/chunks/" not in request.url.path:
            return httpx.Response(
                200, json={"version": 1, "status": "FAILED" if variant == "not_ready" else "READY"}
            )
        return httpx.Response(
            200,
            json={
                "document_id": document,
                "document_version": 1,
                "chunk_id": str(uuid.uuid4()) if variant == "wrong_chunk" else chunk,
                "content": "" if variant == "empty" else "synthetic evidence",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        runner = PublicEvaluationApi(client, "unit-token")
        if variant == "valid":
            await runner.validate_evidence(case)
        else:
            with pytest.raises(ValueError, match="evaluation evidence unavailable"):
                await runner.validate_evidence(case)
