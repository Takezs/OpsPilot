"""Derive Run status from durable message and Operation facts."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.execution.models import Operation, OperationStatus
from opspilot.runs.models import Run, RunMessage, RunStatus

_RUN_BLOCKING_OPERATION_STATUSES = (
    OperationStatus.CREATED,
    OperationStatus.WAITING_APPROVAL,
    OperationStatus.READY,
    OperationStatus.EXECUTING,
    OperationStatus.RETRYING,
    OperationStatus.OUTCOME_UNKNOWN,
    OperationStatus.RECONCILING,
    OperationStatus.MANUAL_REVIEW,
)


async def recompute_run_status(session: AsyncSession, run_id: uuid.UUID) -> RunStatus:
    """Recompute status in the caller's transaction without committing.

    MANUAL_REVIEW is deliberately blocking: it is an Operation terminal for
    mutation purposes, but the user-visible workflow still requires action.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"run {run_id} does not exist")
    blocking = await session.scalar(
        select(Operation.id)
        .where(
            Operation.run_id == run_id,
            Operation.status.in_(_RUN_BLOCKING_OPERATION_STATUSES),
        )
        .limit(1)
    )
    if blocking is not None:
        run.status = RunStatus.RUNNING
        return run.status
    completed_work = await session.scalar(
        select(Operation.id).where(Operation.run_id == run_id).limit(1)
    )
    if completed_work is None:
        completed_work = await session.scalar(
            select(RunMessage.id)
            .where(RunMessage.run_id == run_id, RunMessage.role == "ASSISTANT")
            .limit(1)
        )
    if completed_work is not None:
        run.status = RunStatus.COMPLETED
    return run.status
