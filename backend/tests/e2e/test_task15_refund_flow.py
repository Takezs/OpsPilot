import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy import select

from opspilot.approvals.models import ApprovalRequest
from opspilot.approvals.service import ApprovalDecision, decide_approval
from opspilot.auth.models import Role
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.demo_control import control_headers
from opspilot.execution.models import Operation, OperationStatus
from opspilot.execution.service import create_refund_operation
from opspilot.jobs.tasks import process_operation_job
from opspilot.runs.models import Run, RunEvent, RunStatus
from tests.e2e.control import payment_control_lock

ROOT = Path(__file__).parents[3]


async def _wait_payment() -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8102") as client:
        for _ in range(50):
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("payment demo did not start")


@pytest.mark.integration
async def test_real_http_timeout_after_effect_reconciles_once(tmp_path) -> None:
    async with payment_control_lock(Settings().database_url):
        await _exercise_refund(tmp_path)


async def _exercise_refund(tmp_path) -> None:
    secret = secrets.token_bytes(32)
    order = f"E2E-{uuid.uuid4().hex}"
    path = tmp_path / "payment-secret"
    path.write_bytes(secret)
    path.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        OPSPILOT_DEMO_E2E="true",
        OPSPILOT_E2E_CONTROL_FILE=str(path),
    )
    process = subprocess.Popen(  # noqa: ASYNC220 - bounded real-HTTP E2E child process
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8102",
            "--log-level",
            "warning",
        ],
        cwd=ROOT / "demo-services" / "payment_service",
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run_id = uuid.uuid4()
    try:
        await _wait_payment()
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8102") as client:
            reset = await client.post(
                f"/__e2e/refunds/{order}/reset",
                headers=control_headers(secret, "POST", "reset", order),
            )
            reset.raise_for_status()
        async with async_session_factory() as session:
            session.add(Run(id=run_id, status=RunStatus.RUNNING))
            await session.flush()
            operation = await create_refund_operation(session, run_id, order, 350)
            approval = await session.scalar(
                select(ApprovalRequest).where(ApprovalRequest.operation_id == operation.id)
            )
            assert approval is not None
            await decide_approval(
                session,
                approval.id,
                ApprovalDecision.APPROVE,
                decided_by="task15-reviewer",
                role=Role.REVIEWER,
            )
            await session.commit()
            operation_id = operation.id
            execute_version = operation.version

        await process_operation_job({}, str(operation_id), execute_version, "EXECUTE")
        async with async_session_factory() as session:
            uncertain = await session.get(Operation, operation_id)
            assert uncertain is not None
            assert uncertain.status is OperationStatus.OUTCOME_UNKNOWN
            reconcile_version = uncertain.version

        await process_operation_job({}, str(operation_id), reconcile_version, "RECONCILE")
        async with async_session_factory() as session:
            succeeded = await session.get(Operation, operation_id)
            assert succeeded is not None
            assert succeeded.status is OperationStatus.SUCCEEDED
            completed_run = await session.get(Run, run_id)
            assert completed_run is not None
            assert completed_run.status is RunStatus.COMPLETED
            events = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
            assert [event.seq for event in events] == list(range(1, len(events) + 1))

        async with httpx.AsyncClient(base_url="http://127.0.0.1:8102") as client:
            count = await client.get(
                f"/__e2e/refunds/{order}/count",
                headers=control_headers(secret, "GET", "count", order),
            )
            assert count.json() == {"count": 1}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
        finally:
            await connection.close()
            path.unlink(missing_ok=True)
