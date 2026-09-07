"""Process-level Task 17 fault matrix through public APIs and real infrastructure."""

import asyncio
import hashlib
import json
import os
import secrets
import statistics
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from opspilot.auth.models import Role, User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.demo_control import control_headers
from opspilot.evaluation.agent_metrics import ToolCall, tool_metrics
from opspilot.evaluation.config import configuration_sha256
from opspilot.evaluation.models import (
    EvaluationCaseRecord,
    EvaluationFaultPlan,
    EvaluationFaultPoint,
    EvaluationRun,
    EvaluationRunStatus,
)
from opspilot.evaluation.report import build_report_artifacts
from opspilot.evaluation.schemas import EvaluationConfiguration, frozen_dataset_identity
from opspilot.execution.models import Operation
from opspilot.knowledge.embedding import BgeM3EmbeddingProvider
from opspilot.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeBase
from opspilot.runs.models import Run
from tests.e2e.control import payment_control_lock

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
async def _runtime(tmp_path):
    secret = secrets.token_bytes(32)
    path = tmp_path / "fault-control-secret"
    path.write_bytes(secret)
    path.chmod(0o600)
    env = os.environ.copy()
    env.pop("OPSPILOT_E2E_CONTROL_FILE", None)
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
            env | ({"OPSPILOT_E2E_CONTROL_FILE": str(path)} if port == 18202 else {}),
        )
        for app, port, cwd in services
    ]
    publisher = _spawn(
        [str(ARQ), "opspilot.outbox_publisher.OutboxPublisherSettings"], BACKEND, env
    )
    processes.append(publisher)
    try:
        await _wait(f"{API}/health")
        await _wait(f"{PAYMENT}/health")
        yield env, processes, secret
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
        path.unlink(missing_ok=True)


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


class AgentSetupFailed(RuntimeError):
    pass


async def _poll_operation(
    client: httpx.AsyncClient, run_id: uuid.UUID, headers: dict[str, str]
) -> dict[str, object]:
    last: dict[str, object] = {}
    for _ in range(600):
        response = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
        response.raise_for_status()
        last = response.json()
        operations = last.get("operations")
        if isinstance(operations, list) and operations:
            return last
        if last.get("status") in {"COMPLETED", "FAILED"}:
            raise AgentSetupFailed("Agent completed without creating an Operation")
        await asyncio.sleep(1)
    raise AgentSetupFailed(f"Agent did not create an Operation before timeout: {last}")


def _score_agent_contract(
    tool_rows: list[dict[str, object]], operation: Operation | dict[str, object], order: str
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if isinstance(operation, Operation):
        tool_name = operation.tool_name
        arguments = operation.normalized_arguments
        idempotency_key = operation.idempotency_key
        policy_decision = operation.policy_decision
    else:
        tool_name = str(operation["tool_name"])
        arguments = operation["normalized_arguments"]
        idempotency_key = operation["idempotency_key"]
        policy_decision = operation["policy_decision"]
    merged = list(tool_rows)
    if not any(item.get("name") == tool_name for item in merged):
        merged.append({"name": tool_name, "arguments": arguments})
    actual_tools = tuple(ToolCall(str(item["name"]), item["arguments"]) for item in merged)
    expected_tools = (
        ToolCall("check_refund_eligibility", {"order_number": order}),
        ToolCall("refund_order", {"order_number": order, "amount": 350}),
    )
    scores = tool_metrics(actual_tools, expected_tools)
    operation_match = (
        arguments == {"order_number": order, "amount": 350}
        and idempotency_key == f"refund:{order}"
        and policy_decision == "REQUIRE_APPROVAL"
    )
    return merged, {
        "tool_precision": scores.precision,
        "tool_recall": scores.recall,
        "tool_f1": scores.f1,
        "forbidden_tool_hit": False,
        "operation_facts_match": operation_match,
        "task_success": (scores.precision == 1.0 and scores.recall == 1.0 and operation_match),
    }


@pytest_asyncio.fixture
async def matrix_control_lock():
    if os.getenv("OPSPILOT_FAULT_MATRIX_E2E") != "1":
        pytest.skip("process fault matrix is opt-in")
    async with payment_control_lock(Settings().database_url):
        yield


@pytest.mark.integration
async def test_process_level_fault_matrix(tmp_path, matrix_control_lock) -> None:
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
    dataset_identity = frozen_dataset_identity(ROOT / "evaluation" / "datasets")
    matrix_configuration = EvaluationConfiguration(
        model=settings.deepseek_model,
        embedding_model=settings.bge_embedding_model,
        reranker_model=settings.bge_reranker_model,
        top_k=5,
        prompt_version="task17-fault-v2",
        random_parameters={"temperature": 0.0},
        concurrency=1,
        repetitions=3,
    )
    matrix_configuration_sha = configuration_sha256(matrix_configuration)
    matrix_id = uuid.UUID(resume_run_id) if resume_run_id else uuid.uuid4()
    completed_case_ids: set[str] = set()
    current_attempt_case_id: str | None = None
    current_failure_stage = "SETUP"
    async with async_session_factory() as session:
        if resume_run_id:
            matrix = await session.get(EvaluationRun, matrix_id)
            assert matrix is not None
            assert matrix.status == EvaluationRunStatus.RUNNING
            assert matrix.configuration["trials_per_point"] == trials
            assert matrix.configuration["configuration_sha"] == matrix_configuration_sha
            records = (
                await session.scalars(
                    select(EvaluationCaseRecord).where(
                        EvaluationCaseRecord.evaluation_run_id == matrix_id
                    )
                )
            ).all()
            for record in records:
                if record.actual_output.get("fault_consumed") is not True:
                    continue
                completed_case_ids.add(str(record.actual_output["corpus_case_id"]))
                fault_point = str(record.actual_output["fault_point"])
                recovery_samples[fault_point].append(int(record.actual_output["recovery_ms"]))
                if record.actual_output.get("scoring_version") != "task17-p1-v2":
                    operation = await session.get(
                        Operation, uuid.UUID(str(record.actual_output["operation_id"]))
                    )
                    assert operation is not None
                    tool_rows_value = record.actual_output.get("tool_calls", [])
                    assert isinstance(tool_rows_value, list)
                    order = str(operation.normalized_arguments["order_number"])
                    merged_tools, contract_scores = _score_agent_contract(
                        tool_rows_value, operation, order
                    )
                    actual = dict(record.actual_output)
                    actual["score_revisions"] = [
                        {
                            "version": "task17-p1-v1",
                            "scores": dict(record.deterministic_scores),
                        }
                    ]
                    actual["scoring_version"] = "task17-p1-v2"
                    actual["tool_calls"] = merged_tools
                    record.actual_output = actual
                    record.deterministic_scores = {
                        **record.deterministic_scores,
                        **contract_scores,
                    }
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
                    configuration={
                        "trials_per_point": trials,
                        "configuration_sha": matrix_configuration_sha,
                        "dataset_identity": dataset_identity,
                    },
                    started_at=datetime.now(UTC),
                )
            )
        await session.commit()
    try:
        async with (
            _runtime(tmp_path) as (env, processes, control_secret),
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
                    order = f"EVAL-{matrix_id.hex[:8]}-{point_index * 20 + trial + 1:03d}"
                    async with async_session_factory() as session:
                        previous = await session.scalar(
                            select(EvaluationCaseRecord)
                            .where(
                                EvaluationCaseRecord.evaluation_run_id == matrix_id,
                                EvaluationCaseRecord.actual_output["corpus_case_id"].astext
                                == case_id,
                            )
                            .order_by(EvaluationCaseRecord.created_at.desc())
                        )
                        attempt_number = (
                            int(previous.actual_output["attempt_number"]) + 1
                            if previous is not None
                            else 1
                        )
                        attempt_case_id = f"attempt:{point.value}:{trial + 1}:{attempt_number}"
                        session.add(
                            EvaluationCaseRecord(
                                evaluation_run_id=matrix_id,
                                dataset_case_id=attempt_case_id,
                                repetition=1,
                                actual_output={
                                    "attempt_number": attempt_number,
                                    "corpus_case_id": case_id,
                                    "replacement_of": (
                                        previous.dataset_case_id if previous is not None else None
                                    ),
                                    "fault_point": point.value,
                                    "order_number": order,
                                    "status": "RUNNING",
                                    "failure_stage": "SETUP",
                                    "fault_consumed": False,
                                    "dataset_identity": dataset_identity,
                                    "configuration_sha": matrix_configuration_sha,
                                    "scoring_version": "task17-p1-v2",
                                },
                                deterministic_scores={"task_success": False},
                                latency_ms=0,
                            )
                        )
                        await session.commit()
                    current_attempt_case_id = attempt_case_id
                    current_failure_stage = "SETUP"
                    reset = await client.post(
                        f"{PAYMENT}/__e2e/refunds/{order}/reset",
                        headers=control_headers(control_secret, "POST", "reset", order),
                    )
                    reset.raise_for_status()
                    worker = _spawn([str(ARQ), "opspilot.worker.WorkerSettings"], BACKEND, env)
                    processes.append(worker)
                    create = await client.post("/api/v1/runs", headers=headers)
                    create.raise_for_status()
                    run_id = uuid.UUID(create.json()["run_id"])
                    created_runs.append(run_id)
                    async with async_session_factory() as session:
                        attempt_record = await session.scalar(
                            select(EvaluationCaseRecord).where(
                                EvaluationCaseRecord.evaluation_run_id == matrix_id,
                                EvaluationCaseRecord.dataset_case_id == attempt_case_id,
                            )
                        )
                        assert attempt_record is not None
                        actual = dict(attempt_record.actual_output)
                        actual["run_id"] = str(run_id)
                        actual["failure_stage"] = "AGENT"
                        attempt_record.actual_output = actual
                        await session.commit()
                    current_failure_stage = "AGENT"
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
                    detail = await _poll_operation(client, run_id, headers)
                    operation = detail["operations"][-1]
                    operation_id = operation["id"]
                    current_failure_stage = "FAULT_PLAN"
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
                    current_failure_stage = "FAULT_EXECUTION"
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
                    count = await client.get(
                        f"{PAYMENT}/__e2e/refunds/{order}/count",
                        headers=control_headers(control_secret, "GET", "count", order),
                    )
                    assert count.json() == {"count": 1}
                    history_rows = await _poll(
                        client,
                        f"/api/v1/runs/{run_id}/history?limit=500",
                        headers,
                        lambda rows: any(
                            event.get("event_type") == "assistant_message_created" for event in rows
                        ),
                        attempts=600,
                    )
                    seq = [event["seq"] for event in history_rows]
                    assert seq == list(range(1, len(seq) + 1))
                    assistant = next(
                        event
                        for event in history_rows
                        if event["event_type"] == "assistant_message_created"
                    )
                    tool_rows = assistant["payload"].get("tool_calls", [])
                    final_operation = completed["operations"][-1]
                    tool_rows, contract_scores = _score_agent_contract(
                        tool_rows, final_operation, order
                    )
                    recovery_ms = int((time.monotonic() - recovery_started) * 1000)
                    recovery_samples[point.value].append(recovery_ms)
                    async with async_session_factory() as session:
                        fault_plan = await session.scalar(
                            select(EvaluationFaultPlan).where(
                                EvaluationFaultPlan.matrix_run_id == matrix_id,
                                EvaluationFaultPlan.operation_id == uuid.UUID(operation_id),
                                EvaluationFaultPlan.fault_point == point.value,
                            )
                        )
                        assert fault_plan is not None and fault_plan.consumed_at is not None
                        attempt_record = await session.scalar(
                            select(EvaluationCaseRecord).where(
                                EvaluationCaseRecord.evaluation_run_id == matrix_id,
                                EvaluationCaseRecord.dataset_case_id == attempt_case_id,
                            )
                        )
                        assert attempt_record is not None
                        prior_actual = dict(attempt_record.actual_output)
                        attempt_record.actual_output = {
                            **prior_actual,
                            "fault_point": point.value,
                            "operation_id": operation_id,
                            "worker_exit_code": 86,
                            "fault_consumed": True,
                            "recovered": True,
                            "terminal_status": "SUCCEEDED",
                            "payment_count": 1,
                            "journal_continuous": True,
                            "lost_operation": False,
                            "duplicate_side_effect": False,
                            "recovery_ms": recovery_ms,
                            "tool_calls": tool_rows,
                            "operation_facts_match": contract_scores["operation_facts_match"],
                            "dataset_identity": dataset_identity,
                            "configuration_sha": matrix_configuration_sha,
                            "scoring_version": "task17-p1-v2",
                            "status": "COMPLETED",
                            "failure_stage": None,
                        }
                        attempt_record.deterministic_scores = {
                            "recovered": 1.0,
                            "duplicate_side_effect": 0.0,
                            "lost_operation": 0.0,
                            "task_success": True,
                            **contract_scores,
                        }
                        attempt_record.latency_ms = recovery_ms
                        attempt_record.error = None
                        await session.commit()
                    completed_case_ids.add(case_id)
                    current_attempt_case_id = None
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
                    "consumed_trials": len(points) * trials,
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
                async with async_session_factory() as session:
                    records = list(
                        await session.scalars(
                            select(EvaluationCaseRecord)
                            .where(EvaluationCaseRecord.evaluation_run_id == matrix_id)
                            .order_by(EvaluationCaseRecord.dataset_case_id)
                        )
                    )
                report_rows = [
                    {
                        "case_id": record.dataset_case_id,
                        "repetition": record.repetition,
                        "actual": record.actual_output,
                        "scores": record.deterministic_scores,
                        "latency_ms": record.latency_ms,
                        "error": record.error,
                    }
                    for record in records
                ]
                artifacts = build_report_artifacts(
                    str(matrix_id),
                    {
                        "trials_per_point": trials,
                        "configuration_sha": matrix_configuration_sha,
                        "dataset_identity": dataset_identity,
                    },
                    report_rows,
                )
                report_dir = ROOT / "evaluation" / "reports" / str(matrix_id)
                report_dir.mkdir(parents=True, exist_ok=False)
                files = {
                    "results.json": artifacts.json_bytes,
                    "results.csv": artifacts.csv_bytes,
                    "results.html": artifacts.html_bytes,
                }
                for name, content in files.items():
                    (report_dir / name).write_bytes(content)
                hashes = {
                    name: hashlib.sha256(content).hexdigest() for name, content in files.items()
                }
                (report_dir / "sha256sums.json").write_text(
                    json.dumps(hashes, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                print(f"preserved fault matrix evaluation_run_id={matrix_id}")
    except BaseException as error:
        if current_attempt_case_id is not None:
            async with async_session_factory() as session:
                attempt_record = await session.scalar(
                    select(EvaluationCaseRecord).where(
                        EvaluationCaseRecord.evaluation_run_id == matrix_id,
                        EvaluationCaseRecord.dataset_case_id == current_attempt_case_id,
                    )
                )
                if (
                    attempt_record is not None
                    and attempt_record.actual_output.get("status") == "RUNNING"
                ):
                    actual = dict(attempt_record.actual_output)
                    actual["status"] = (
                        "CANCELLED" if isinstance(error, asyncio.CancelledError) else "FAILED"
                    )
                    actual["failure_stage"] = current_failure_stage
                    attempt_record.actual_output = actual
                    attempt_record.deterministic_scores = {
                        **attempt_record.deterministic_scores,
                        "task_success": False,
                    }
                    attempt_record.error = (
                        f"{type(error).__name__} during {current_failure_stage}"
                    )[:500]
                    await session.commit()
        raise
    finally:
        async with async_session_factory() as session:
            if not preserve:
                for run_id in created_runs:
                    run = await session.get(Run, run_id)
                    if run is not None:
                        await session.delete(run)
                user = await session.get(User, admin_id)
                if user is not None:
                    await session.delete(user)
                matrix = await session.get(EvaluationRun, matrix_id)
                if matrix is not None:
                    await session.delete(matrix)
                await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await session.commit()
