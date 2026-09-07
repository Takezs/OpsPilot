"""Task 15 live acceptance through public HTTP, real ARQ, and real providers."""

import asyncio
import json
import os
import secrets
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from opspilot.agent.knowledge import RunKnowledgeSearch
from opspilot.auth.models import Role, User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.demo_control import control_headers
from opspilot.generation.citations import validate_citations
from opspilot.generation.provider import DeepSeekGenerationProvider, render_generation_prompt
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.retrieval.reranker import BgeReranker
from opspilot.runs.models import Run, RunEvent
from opspilot.tools.schemas import SearchKnowledgeArgs
from tests.e2e import control

ROOT = Path(__file__).parents[3]
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ARQ = ROOT / ".venv" / "Scripts" / "arq.exe"
COMPOSE_E2E = os.getenv("OPSPILOT_LIVE_COMPOSE_E2E") == "1"
API = (
    os.getenv("OPSPILOT_LIVE_COMPOSE_API_URL", "http://127.0.0.1:8000")
    if COMPOSE_E2E
    else "http://127.0.0.1:18000"
)
PAYMENT = (
    os.getenv("OPSPILOT_LIVE_COMPOSE_PAYMENT_URL", "http://127.0.0.1:18102")
    if COMPOSE_E2E
    else "http://127.0.0.1:18102"
)


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
async def _runtime(order: str, secret: bytes, secret_path: Path):
    if COMPOSE_E2E:
        await _wait(f"{API}/health")
        await _wait(f"{PAYMENT}/health")
        async with httpx.AsyncClient(base_url=PAYMENT, timeout=5) as payment:
            reset = await payment.post(
                f"/__e2e/refunds/{order}/reset",
                headers=control_headers(secret, "POST", "reset", order),
            )
            reset.raise_for_status()
        yield
        return
    env = os.environ.copy()
    env.pop("OPSPILOT_E2E_CONTROL_FILE", None)
    env.update(
        OPSPILOT_DEMO_E2E="true",
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
            command,
            cwd=cwd,
            env=env
            | (
                {"OPSPILOT_E2E_CONTROL_FILE": str(secret_path)}
                if cwd == ROOT / "demo-services" / "payment_service"
                else {}
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for command, cwd in commands
    ]
    try:
        await _wait(f"{API}/health")
        await _wait(f"{PAYMENT}/health")
        async with httpx.AsyncClient(base_url=PAYMENT) as payment:
            reset = await payment.post(
                f"/__e2e/refunds/{order}/reset",
                headers=control_headers(secret, "POST", "reset", order),
            )
            reset.raise_for_status()
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


@pytest_asyncio.fixture
async def live_runtime(tmp_path):
    if os.getenv("OPSPILOT_LIVE_PROVIDER_E2E") != "1":
        pytest.skip("live Provider acceptance is opt-in")
    secret = control.COMPOSE_SECRET if COMPOSE_E2E else secrets.token_bytes(32)
    if secret is None:
        raise RuntimeError("Compose control secret must be injected in memory by the test launcher")
    order = f"E2E-{uuid.uuid4().hex}"
    path = tmp_path / "payment-control"
    if not COMPOSE_E2E:
        path.write_bytes(secret)
        path.chmod(0o600)
    try:
        async with control.payment_control_lock(Settings().database_url):
            async with _runtime(order, secret, path):
                yield order, secret
    finally:
        if not COMPOSE_E2E:
            path.unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.parametrize("live_iteration", range(3))
async def test_public_api_real_provider_refund_flow(live_iteration: int, live_runtime) -> None:
    if os.getenv("OPSPILOT_LIVE_PROVIDER_E2E") != "1":
        pytest.skip("live Provider acceptance is opt-in")
    settings = Settings()
    assert settings.deepseek_api_key
    suffix = uuid.uuid4().hex
    order, control_secret = live_runtime
    evidence = {
        "attempt_id": suffix,
        "started_at": datetime.now(UTC).isoformat(),
        "iteration": live_iteration,
        "compose": COMPOSE_E2E,
        "order_id": order,
        "stage": "SETUP",
        "run_created": False,
        "operation_created": False,
        "complete": False,
    }
    owner_id, reviewer_id = uuid.uuid4(), uuid.uuid4()
    owner_name, reviewer_name = f"live-owner-{suffix}", f"live-reviewer-{suffix}"
    password = f"Live-{uuid.uuid4().hex}!"
    run_id = None
    kb_id = None
    marker = f"LIVE-POLICY-{suffix[:12]}"
    text = (
        f"Policy marker {marker}: refunds above 100 require reviewer approval. "
        f"{order} is a 350 USD order "
        "and must be approved before the refund worker executes it."
    )
    embedding_provider = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    reranker_provider = BgeReranker(
        settings.bge_base_url, settings.bge_api_key, settings.bge_reranker_model
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

        async with httpx.AsyncClient(base_url=API, timeout=10) as client:
            owner_headers = await _login(client, owner_name, password)
            reviewer_headers = await _login(client, reviewer_name, password)
            response = await client.post("/api/v1/runs", headers=owner_headers)
            response.raise_for_status()
            run_id = uuid.UUID(response.json()["run_id"])
            evidence.update(run_created=True, run_id=str(run_id), stage="CITATIONS")

            # Prove the exact production retrieval composition has selected the
            # unique evidence before asking the Agent. This prevents a lucky
            # model response from masquerading as grounded acceptance and makes
            # an embedding/FTS miss an explicit test failure.
            grounded = await RunKnowledgeSearch(
                async_session_factory,
                run_id,
                embedding_provider,
                reranker_provider,
                context_token_budget=settings.generation_context_token_budget,
                reranker_timeout_seconds=settings.retrieval_reranker_timeout_seconds,
            )(SearchKnowledgeArgs(query=marker, top_k=5))
            assert grounded.summary.ok is True
            assert grounded.summary.data is not None
            assert grounded.summary.data["result_count"] >= 1
            assert any(item.chunk_id == str(chunk.id) for item in grounded.context.fragments)

            generation_provider = DeepSeekGenerationProvider(
                settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                proxy_url=settings.deepseek_proxy_url,
            )
            try:
                diagnostic_query = (
                    f"According to our knowledge base, what exactly does policy marker "
                    f"{marker} say? Include its supporting evidence."
                )
                raw_grounded = await generation_provider.answer(
                    prompt=render_generation_prompt(diagnostic_query, grounded.context)
                )
                validated_grounded = validate_citations(raw_grounded, grounded.context)
                assert validated_grounded.snapshots, {
                    "citations": raw_grounded.citations,
                    "insufficient_evidence": raw_grounded.insufficient_evidence,
                    "has_follow_up": raw_grounded.follow_up_question is not None,
                    "available_ids": list(grounded.context.citation_ids),
                }
            finally:
                await generation_provider.aclose()

            response = await client.post(
                f"/api/v1/runs/{run_id}/messages",
                headers=owner_headers,
                json={"content": (diagnostic_query)},
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
            evidence.update(exact_citations=True)
            assert (
                "Unable to answer from verified knowledge evidence."
                not in assistant[0]["payload"]["content"]
            )

            evidence.update(stage="OPERATION")
            response = await client.post(
                f"/api/v1/runs/{run_id}/messages",
                headers=owner_headers,
                json={
                    "content": (
                        f"Call check_refund_eligibility for {order}, then create the "
                        f"refund_order Operation for {order} amount 350."
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
            evidence.update(
                operation_created=True, operation_id=operation_id, stage="RECONCILIATION"
            )

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
                count = await payment.get(
                    f"/__e2e/refunds/{order}/count",
                    headers=control_headers(control_secret, "GET", "count", order),
                )
                count.raise_for_status()
                assert count.json() == {"count": 1}
            evidence.update(refund_count=1, reconciliation=True, public_seq_contiguous=True)

        async with async_session_factory() as session:
            events = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
            assert [event.seq for event in events] == list(range(1, len(events) + 1))
            assert [event.seq for event in events] == [event["seq"] for event in history]
            evidence.update(
                pg_seq_contiguous=True, seq_count=len(events), complete=True, stage="SUCCEEDED"
            )
    finally:
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        print("LIVE_EVIDENCE " + json.dumps(evidence, sort_keys=True))
        await embedding_provider.aclose()
        await reranker_provider.aclose()
        async with async_session_factory() as session:
            if run_id is not None:
                run = await session.get(Run, run_id)
                if run is not None:
                    await session.delete(run)
            if kb_id is not None:
                await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await session.execute(delete(User).where(User.id.in_([owner_id, reviewer_id])))
            await session.commit()
