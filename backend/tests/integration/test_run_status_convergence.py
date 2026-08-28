"""Run status is derived from durable workflow facts in PostgreSQL."""

import uuid

import asyncpg
import pytest

from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.models import Operation, OperationStatus
from opspilot.runs.models import Run, RunStatus
from opspilot.runs.status import recompute_run_status


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_status", "expected"),
    [
        (OperationStatus.WAITING_APPROVAL, RunStatus.RUNNING),
        (OperationStatus.OUTCOME_UNKNOWN, RunStatus.RUNNING),
        (OperationStatus.RECONCILING, RunStatus.RUNNING),
        (OperationStatus.MANUAL_REVIEW, RunStatus.RUNNING),
        (OperationStatus.SUCCEEDED, RunStatus.COMPLETED),
        (OperationStatus.FAILED, RunStatus.COMPLETED),
        (OperationStatus.DENIED, RunStatus.COMPLETED),
        (OperationStatus.REJECTED, RunStatus.COMPLETED),
    ],
)
async def test_recompute_run_status_from_operation_facts(
    operation_status: OperationStatus, expected: RunStatus
) -> None:
    run_id = uuid.uuid4()
    try:
        async with async_session_factory() as session:
            session.add(Run(id=run_id, status=RunStatus.RUNNING))
            await session.flush()
            session.add(
                Operation(
                    run_id=run_id,
                    tool_name="refund_order",
                    normalized_arguments={"order_number": "ORD-002", "amount": 350},
                    arguments_hash="a" * 64,
                    idempotency_key=f"refund:{run_id}",
                    status=operation_status,
                    version=1,
                )
            )
            await session.flush()
            assert await recompute_run_status(session, run_id) is expected
            await session.commit()
        async with async_session_factory() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            assert run.status is expected
    finally:
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            await connection.execute("DELETE FROM agent_runs WHERE id = $1", run_id)
        finally:
            await connection.close()
