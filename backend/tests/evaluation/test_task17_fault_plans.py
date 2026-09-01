import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from opspilot.db import async_session_factory
from opspilot.evaluation.faults import consume_fault_plan
from opspilot.evaluation.models import (
    EvaluationFaultPlan,
    EvaluationFaultPoint,
    EvaluationRun,
    EvaluationRunStatus,
)
from opspilot.execution.service import create_refund_operation
from opspilot.main import app
from opspilot.runs.models import Run, RunStatus


def test_default_application_does_not_register_fault_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPSPILOT_EVAL_FAULT_MATRIX", raising=False)
    assert "/api/v1/evaluations/fault-plans" not in {
        getattr(route, "path", None) for route in app.routes
    }


@pytest.mark.integration
async def test_fault_plan_is_disabled_by_default_and_consumed_once_across_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPSPILOT_EVAL_FAULT_MATRIX", raising=False)
    run_id, matrix_run_id = uuid.uuid4(), uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            EvaluationRun(
                id=matrix_run_id,
                dataset_version="fault-matrix-dev",
                dataset_sha256="f" * 64,
                status=EvaluationRunStatus.RUNNING,
                model="deepseek-chat",
                embedding_model="bge-m3",
                reranker_model="bge-reranker-v2-m3",
                top_k=5,
                prompt_version="task17-fault-v1",
                random_parameters={},
                configuration={"trials_per_point": 20},
                started_at=datetime.now(UTC),
            )
        )
        session.add(Run(id=run_id, status=RunStatus.RUNNING))
        await session.flush()
        operation = await create_refund_operation(session, run_id, "ORD-002", 350)
        plan = EvaluationFaultPlan(
            matrix_run_id=matrix_run_id,
            operation_id=operation.id,
            fault_point=EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT.value,
        )
        session.add(plan)
        await session.commit()
        operation_id = operation.id

    assert not await consume_fault_plan(
        operation_id, EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT, worker_id="disabled"
    )
    monkeypatch.setenv("OPSPILOT_EVAL_FAULT_MATRIX", "1")
    results = await asyncio.gather(
        consume_fault_plan(
            operation_id, EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT, worker_id="worker-a"
        ),
        consume_fault_plan(
            operation_id, EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT, worker_id="worker-b"
        ),
    )
    assert sorted(results) == [False, True]
    assert not await consume_fault_plan(
        operation_id, EvaluationFaultPoint.BEFORE_EXTERNAL_EFFECT, worker_id="restarted-worker"
    )
    async with async_session_factory() as session:
        stored = await session.scalar(
            select(EvaluationFaultPlan).where(EvaluationFaultPlan.operation_id == operation_id)
        )
        assert stored is not None and stored.consumed_at is not None
        assert stored.consumed_by in {"worker-a", "worker-b"}
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(EvaluationRun).where(EvaluationRun.id == matrix_run_id))
        await session.commit()
