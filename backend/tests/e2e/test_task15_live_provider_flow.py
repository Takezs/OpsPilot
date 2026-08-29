"""Task 15 live acceptance through public HTTP, real ARQ, and real providers."""

import asyncio
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from opspilot.auth.models import Role, User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.runs.models import Run, RunEvent

ROOT = Path(__file__).parents[3]
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ARQ = ROOT / ".venv" / "Scripts" / "arq.exe"
API = "http://127.0.0.1:18000"
PAYMENT = "http://127.0.0.1:18102"


async def _wait(url: str) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(100):
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"service unavailable: {url}")


@asynccontextmanager
async def _runtime():
    env = os.environ.copy()
    env.update(
        OPSPILOT_DEMO_E2E="true",
        OPSPILOT_DEMO_E2E_TIMEOUT_ORDER="ORD-002",
        ORDER_SERVICE_URL="http://127.0.0.1:18101",
        PAYMENT_SERVICE_URL=PAYMENT,
        EMAIL_SERVICE_URL="http://127.0.0.1:18103",
        PAYMENT_TIMEOUT_SECONDS="1",
    )
    services = [
        ("app:app", 18101, ROOT / "demo-services" / "order_service"),
        ("app:app", 18102, ROOT / "demo-services" / "payment_service"),
        ("app:app", 18103, ROOT / "demo-services" / "email_service"),
        ("opspilot.main:app", 18000, BACKEND),
    ]
    commands = [
        (
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                app,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd,
        )
        for app, port, cwd in services
    ] + [
        ([str(ARQ), "opspilot.worker.WorkerSettings"], BACKEND),
        ([str(ARQ), "opspilot.outbox_publisher.OutboxPublisherSettings"], BACKEND),
    ]
    processes = [
        subprocess.Popen(  # noqa: ASYNC220
            command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for command, cwd in commands
    ]
    try:
        await _wait(f"{API}/health")
        await _wait(f"{PAYMENT}/refunds/ORD-002/eligibility")
        yield
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _poll(client, run_id, headers, predicate, attempts=180):
    last = {}
    for _ in range(attempts):
        response = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(1)
    raise AssertionError(f"run timeout: {last}")


async def _poll_history(client, run_id, headers, predicate, attempts=180):
    history = []
    for _ in range(attempts):
        response = await client.get(f"/api/v1/runs/{run_id}/history?limit=500", headers=headers)
        response.raise_for_status()
        history = response.json()
        if predicate(history):
            return history
        await asyncio.sleep(1)
    raise AssertionError(f"history timeout: {history}")


@pytest.mark.integration
async def test_public_api_real_provider_refund_flow() -> None:
    if os.getenv("OPSPILOT_LIVE_PROVIDER_E2E") != "1":
        pytest.skip("live Provider acceptance is opt-in")
    settings = Settings()
    assert settings.deepseek_api_key
    suffix = uuid.uuid4().hex
    owner_id, reviewer_id = uuid.uuid4(), uuid.uuid4()
    owner_name, reviewer_name = f"live-owner-{suffix}", f"live-reviewer-{suffix}"
    password = f"Live-{uuid.uuid4().hex}!"
    run_id = None
    kb_id = None
    marker = f"LIVE-POLICY-{suffix[:12]}"
    text = (
        f"Policy marker {marker}: refunds above 100 require reviewer approval. "
        "ORD-002 is a 350 USD order "
        "and must be approved before the refund worker executes it."
    )
    embedding_provider = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    try:
        embedding = (await embedding_provider.embed([text]))[0]
        auth = AuthService(settings.jwt_secret)
        owner = User(
            id=owner_id,
            username=owner_name,
            password_hash=auth.hash_password(password),
            role=Role.USER,
            allowed_departments=["support"],
            max_access_level=1,
            is_active=True,
        )
        reviewer = User(
            id=reviewer_id,
            username=reviewer_name,
            password_hash=auth.hash_password(password),
            role=Role.REVIEWER,
            allowed_departments=["support"],
            max_access_level=1,
            is_active=True,
        )
        kb = KnowledgeBase(name=f"live-task15-{suffix}", department="support", access_level=1)
        document = Document(
            title="Refund approval policy",
            version=1,
            content_sha256=uuid.uuid4().hex,
            storage_path="live/refund.md",
            status=DocumentStatus.READY,
        )
        chunk = Chunk(
            position=0,
            content=text,
            section_path=["Refunds", "Approval"],
            token_count=28,
            page=1,
            embedding=embedding,
            embedding_cache_key=f"live:{suffix}",
        )
        document.chunks.append(chunk)
        kb.documents.append(document)
        async with async_session_factory() as session:
            session.add_all([owner, reviewer, kb])
            await session.commit()
            kb_id = kb.id

        async with _runtime(), httpx.AsyncClient(base_url=API, timeout=10) as client:
            owner_headers = await _login(client, owner_name, password)
            reviewer_headers = await _login(client, reviewer_name, password)
            response = await client.post("/api/v1/runs", headers=owner_headers)
            response.raise_for_status()
            run_id = uuid.UUID(response.json()["run_id"])
            response = await client.post(
                f"/api/v1/runs/{run_id}/messages",
                headers=owner_headers,
                json={
                    "content": (
                        f"Call search_knowledge for policy marker {marker}, explain its exact "
                        "meaning, then return "
                        "grounded_answer with the server-issued search_call_id. Do not use the "
                        "plain answer action."
                    )
                },
            )
            response.raise_for_status()
            history = await _poll_history(
                client,
                run_id,
                owner_headers,
                lambda rows: any(
                    event["event_type"] == "assistant_message_created" for event in rows
                ),
            )
            assistant = [
                event for event in history if event["event_type"] == "assistant_message_created"
            ]
            assert len(assistant) == 1
            citations = assistant[0]["payload"]["citations"]
            assert citations and citations[0]["document_id"] == str(document.id), assistant[0][
                "payload"
            ]
            assert citations[0]["document_version"] == 1
            assert citations[0]["chunk_id"] == str(chunk.id)

            response = await client.post(
                f"/api/v1/runs/{run_id}/messages",
                headers=owner_headers,
                json={
                    "content": (
                        "Call check_refund_eligibility for ORD-002, then create the "
                        "refund_order Operation for ORD-002 amount 350."
                    )
                },
            )
            response.raise_for_status()
            waiting = await _poll(
                client,
                run_id,
                owner_headers,
                lambda row: (
                    row.get("operations") and row["operations"][0]["status"] == "WAITING_APPROVAL"
                ),
            )
            operation_id = waiting["operations"][0]["id"]

            approvals = (
                await client.get(
                    "/api/v1/approval-requests?status=PENDING", headers=reviewer_headers
                )
            ).json()
            approval = next(row for row in approvals if row["operation_id"] == operation_id)
            response = await client.post(
                f"/api/v1/approval-requests/{approval['id']}/decisions",
                headers=reviewer_headers,
                json={"decision": "APPROVE", "comment": "live acceptance"},
            )
            response.raise_for_status()
            assert response.json()["operation_status"] == "READY"

            completed = await _poll(
                client,
                run_id,
                owner_headers,
                lambda row: (
                    row.get("status") == "COMPLETED"
                    and row.get("operations")
                    and row["operations"][0]["status"] == "SUCCEEDED"
                ),
            )
            assert completed["operations"][0]["provider_reference_id"]
            history = (
                await client.get(f"/api/v1/runs/{run_id}/history?limit=500", headers=owner_headers)
            ).json()
            assert [event["seq"] for event in history] == list(range(1, len(history) + 1))
            types = {event["event_type"] for event in history}
            assert {
                "operation_outcome_unknown",
                "operation_reconciliation_started",
                "operation_reconciled_succeeded",
            } <= types
            async with httpx.AsyncClient(base_url=PAYMENT, timeout=5) as payment:
                count = await payment.get("/__e2e/refunds/ORD-002/count")
                count.raise_for_status()
                assert count.json() == {"count": 1}

        async with async_session_factory() as session:
            events = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
            assert [event.seq for event in events] == list(range(1, len(events) + 1))
    finally:
        await embedding_provider.aclose()
        async with async_session_factory() as session:
            if run_id is not None:
                run = await session.get(Run, run_id)
                if run is not None:
                    await session.delete(run)
            if kb_id is not None:
                await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await session.execute(delete(User).where(User.id.in_([owner_id, reviewer_id])))
            await session.commit()
