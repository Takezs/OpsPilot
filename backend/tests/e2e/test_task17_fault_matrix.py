"""Process-level Task 17 fault matrix through public APIs and real infrastructure."""

import asyncio
import os
import statistics
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from opspilot.auth.models import Role, User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationFaultPoint,
    EvaluationRun,
    EvaluationRunStatus,
)
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.runs.models import Run

ROOT = Path(__file__).parents[3]
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ARQ = ROOT / ".venv" / "Scripts" / "arq.exe"
API = "http://127.0.0.1:18200"
PAYMENT = "http://127.0.0.1:18202"


async def _wait(url: str) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(120):
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"service unavailable: {url}")


def _spawn(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: ASYNC220
        command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


@asynccontextmanager
async def _runtime():
    env = os.environ.copy()
    env.update(
        OPSPILOT_EVAL_FAULT_MATRIX="1",
        ORDER_SERVICE_URL="http://127.0.0.1:18201",
        PAYMENT_SERVICE_URL=PAYMENT,
        EMAIL_SERVICE_URL="http://127.0.0.1:18203",
        OPERATION_LEASE_SECONDS="2",
        PAYMENT_TIMEOUT_SECONDS="1",
        ACCESS_TOKEN_TTL_SECONDS="7200",
    )
    services = [
        ("app:app", 18201, ROOT / "demo-services" / "order_service"),
        ("app:app", 18202, ROOT / "demo-services" / "payment_service"),
        ("app:app", 18203, ROOT / "demo-services" / "email_service"),
        ("opspilot.main:app", 18200, BACKEND),
    ]
    processes = [
        _spawn(
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
            env,
        )
        for app, port, cwd in services
    ]
    publisher = _spawn(
        [str(ARQ), "opspilot.outbox_publisher.OutboxPublisherSettings"], BACKEND, env
    )
    processes.append(publisher)
    try:
        await _wait(f"{API}/health")
        await _wait(f"{PAYMENT}/refunds/EVAL-001/eligibility")
        yield env, processes
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def _poll(
    client: httpx.AsyncClient, path: str, headers: dict[str, str], predicate, attempts: int = 180
):
    last = None
    for _ in range(attempts):
        response = await client.get(path, headers=headers)
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(1)
    raise AssertionError(f"poll timeout: {last}")


@pytest.mark.integration
async def test_process_level_fault_matrix() -> None:
    if os.getenv("OPSPILOT_FAULT_MATRIX_E2E") != "1":
        pytest.skip("process fault matrix is opt-in")
    trials = int(os.getenv("OPSPILOT_FAULT_MATRIX_TRIALS", "1"))
    preserve = os.getenv("OPSPILOT_FAULT_MATRIX_PRESERVE") == "1"
    resume_run_id = os.getenv("OPSPILOT_FAULT_MATRIX_RESUME_RUN_ID")
    assert 1 <= trials <= 20
    settings = Settings()
    assert settings.deepseek_api_key
    suffix = uuid.uuid4().hex
    admin_id = uuid.uuid4()
    username, password = f"fault-admin-{suffix}", f"Fault-{suffix}!"
    auth = AuthService(settings.jwt_secret)
    policy_text = (
        "Evaluation refund policy: every EVAL order has amount 350 USD and requires reviewer "
        "approval before refund_order may execute. After approval the worker must use the stable "
        "refund idempotency key and reconcile uncertain results."
    )
    embedding_provider = BgeM3EmbeddingProvider(
        settings.bge_base_url, settings.bge_api_key, settings.bge_embedding_model
    )
    embedding = (await embedding_provider.embed([policy_text]))[0]
    await embedding_provider.aclose()
    kb_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            User(
                id=admin_id,
                username=username,
                password_hash=auth.hash_password(password),
                role=Role.ADMIN,
                allowed_departments=["evaluation"],
                max_access_level=1,
                is_active=True,
            )
        )
        kb = KnowledgeBase(
            id=kb_id, name=f"fault-policy-{suffix}", department="evaluation", access_level=1
        )
        document = Document(
            title="Evaluation refund policy",
            version=1,
            content_sha256=uuid.uuid4().hex,
            storage_path=f"evaluation/{suffix}.md",
            status=DocumentStatus.READY,
        )
        document.chunks.append(
            Chunk(
                position=0,
                content=policy_text,
                section_path=["Evaluation", "Refunds"],
                token_count=34,
                page=1,
                embedding=embedding,
                embedding_cache_key=f"task17-fault:{suffix}",
            )
        )
        kb.documents.append(document)
        session.add(kb)
        await session.commit()
    created_runs: list[uuid.UUID] = []
    recovery_samples: dict[str, list[int]] = {point.value: [] for point in EvaluationFaultPoint}
    matrix_id = uuid.UUID(resume_run_id) if resume_run_id else uuid.uuid4()
    completed_case_ids: set[str] = set()
    async with async_session_factory() as session:
        if resume_run_id:
            matrix = await session.get(EvaluationRun, matrix_id)
            assert matrix is not None
            assert matrix.status == EvaluationRunStatus.RUNNING
            assert matrix.configuration == {"trials_per_point": trials}
            records = (
                await session.scalars(
                    select(EvaluationCaseRecord).where(
                        EvaluationCaseRecord.evaluation_run_id == matrix_id
                    )
                )
            ).all()
            for record in records:
                completed_case_ids.add(record.dataset_case_id)
                fault_point = str(record.actual_output["fault_point"])
                recovery_samples[fault_point].append(int(record.actual_output["recovery_ms"]))
        else:
            session.add(
                EvaluationRun(
                    id=matrix_id,
                    dataset_version="fault-matrix-dev",
                    dataset_sha256="f" * 64,
                    status=EvaluationRunStatus.RUNNING,
                    model="deepseek-chat",
                    embedding_model="bge-m3",
                    reranker_model="bge-reranker-v2-m3",
                    top_k=5,
                    prompt_version="task17-fault-v1",
                    random_parameters={"temperature": 0.0},
                    configuration={"trials_per_point": trials},
                    started_at=datetime.now(UTC),
                )
            )
        await session.commit()
    try:
        async with (
            _runtime() as (env, processes),
            httpx.AsyncClient(base_url=API, timeout=15) as client,
        ):
            login = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            login.raise_for_status()
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            points = tuple(EvaluationFaultPoint)
            for point_index, point in enumerate(points):
                for trial in range(trials):
                    case_id = f"fault:{point.value}:{trial + 1}"
                    if case_id in completed_case_ids:
                        continue
                    order = f"EVAL-{point_index * 20 + trial + 1:03d}"
                    reset = await client.post(f"{PAYMENT}/__e2e/refunds/{order}/reset")
                    reset.raise_for_status()
                    worker = _spawn([str(ARQ), "opspilot.worker.WorkerSettings"], BACKEND, env)
                    processes.append(worker)
                    create = await client.post("/api/v1/runs", headers=headers)
                    create.raise_for_status()
                    run_id = uuid.UUID(create.json()["run_id"])
                    created_runs.append(run_id)
                    message = await client.post(
                        f"/api/v1/runs/{run_id}/messages",
                        headers=headers,
                        json={
                            "content": (
                                f"Call check_refund_eligibility for {order}, then create the "
                                f"refund_order Operation for {order} amount 350."
                            )
                        },
                    )
                    message.raise_for_status()
                    detail = await _poll(
                        client,
                        f"/api/v1/runs/{run_id}",
                        headers,
                        lambda value: bool(value.get("operations")),
                        attempts=600,
                    )
                    operation = detail["operations"][-1]
                    operation_id = operation["id"]
                    plan = await client.post(
                        "/api/v1/evaluations/fault-plans",
                        headers=headers,
                        json={
                            "matrix_run_id": str(matrix_id),
                            "operation_id": operation_id,
                            "fault_point": point.value,
                        },
                    )
                    plan.raise_for_status()
                    approvals = await client.get(
                        "/api/v1/approval-requests?limit=100", headers=headers
                    )
                    approvals.raise_for_status()
                    approval = next(
                        item for item in approvals.json() if item["operation_id"] == operation_id
                    )
                    decided = await client.post(
                        f"/api/v1/approval-requests/{approval['id']}/decisions",
                        headers=headers,
                        json={"decision": "APPROVE"},
                    )
                    decided.raise_for_status()
                    recovery_started = time.monotonic()
                    for _ in range(120):
                        if worker.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    assert worker.poll() == 86
                    replacement = _spawn([str(ARQ), "opspilot.worker.WorkerSettings"], BACKEND, env)
                    processes.append(replacement)
                    completed = await _poll(
                        client,
                        f"/api/v1/runs/{run_id}",
                        headers,
                        lambda value: (
                            value.get("operations", [{}])[-1].get("status") == "SUCCEEDED"
                        ),
                    )
                    assert completed["operations"][-1]["status"] == "SUCCEEDED"
                    count = await client.get(f"{PAYMENT}/__e2e/refunds/{order}/count")
                    assert count.json() == {"count": 1}
                    history = await client.get(
                        f"/api/v1/runs/{run_id}/history?limit=500", headers=headers
                    )
                    history.raise_for_status()
                    seq = [event["seq"] for event in history.json()]
                    assert seq == list(range(1, len(seq) + 1))
                    recovery_ms = int((time.monotonic() - recovery_started) * 1000)
                    recovery_samples[point.value].append(recovery_ms)
                    async with async_session_factory() as session:
                        session.add(
                            EvaluationCaseRecord(
                                evaluation_run_id=matrix_id,
                                dataset_case_id=case_id,
                                repetition=1,
                                actual_output={
                                    "fault_point": point.value,
                                    "operation_id": operation_id,
                                    "worker_exit_code": 86,
                                    "terminal_status": "SUCCEEDED",
                                    "payment_count": 1,
                                    "journal_continuous": True,
                                    "lost_operation": False,
                                    "duplicate_side_effect": False,
                                    "recovery_ms": recovery_ms,
                                },
                                deterministic_scores={
                                    "recovered": 1.0,
                                    "duplicate_side_effect": 0.0,
                                    "lost_operation": 0.0,
                                },
                                latency_ms=recovery_ms,
                            )
                        )
                        await session.commit()
                    replacement.terminate()
                    replacement.wait(timeout=10)
            async with async_session_factory() as session:
                matrix = await session.get(EvaluationRun, matrix_id)
                assert matrix is not None
                matrix.status = EvaluationRunStatus.COMPLETED
                matrix.completed_at = datetime.now(UTC)
                all_recovery_ms = [
                    sample for samples in recovery_samples.values() for sample in samples
                ]
                sorted_recovery_ms = sorted(all_recovery_ms)
                p95_index = max(0, (95 * len(sorted_recovery_ms) + 99) // 100 - 1)
                matrix.metrics = {
                    "trials": len(points) * trials,
                    "recovery_rate": 1.0,
                    "duplicate_side_effect_rate": 0.0,
                    "lost_operation_rate": 0.0,
                    "recovery_mean_ms": statistics.fmean(all_recovery_ms),
                    "recovery_p95_ms": sorted_recovery_ms[p95_index],
                    "fault_points": {
                        point: {
                            "trials": len(samples),
                            "recovery_mean_ms": statistics.fmean(samples),
                        }
                        for point, samples in recovery_samples.items()
                    },
                }
                await session.commit()
            if preserve:
                print(f"preserved fault matrix evaluation_run_id={matrix_id}")
    finally:
        async with async_session_factory() as session:
            for run_id in created_runs:
                run = await session.get(Run, run_id)
                if run is not None:
                    await session.delete(run)
            user = await session.get(User, admin_id)
            if user is not None:
                await session.delete(user)
            if not preserve:
                matrix = await session.get(EvaluationRun, matrix_id)
                if matrix is not None:
                    await session.delete(matrix)
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await session.commit()
