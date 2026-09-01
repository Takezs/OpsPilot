"""Evaluation-only, PostgreSQL-backed one-shot worker crash plans."""

import os
import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.models import EvaluationFaultPlan, EvaluationFaultPoint
from opspilot.evaluation.schemas import EvaluationFaultPlanRequest
from opspilot.evaluation.service import EvaluationPermissionError, require_evaluation_admin

router = APIRouter(prefix="/evaluations/fault-plans", tags=["evaluation-faults"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_fault_plan(
    request: EvaluationFaultPlanRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, str]:
    try:
        require_evaluation_admin(principal)
        point = EvaluationFaultPoint(request.fault_point)
    except EvaluationPermissionError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid fault point") from error
    async with async_session_factory() as session:
        row = EvaluationFaultPlan(
            matrix_run_id=request.matrix_run_id,
            operation_id=request.operation_id,
            fault_point=point.value,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "fault plan already exists") from error
    return {"id": str(row.id), "fault_point": point.value}


async def consume_fault_plan(
    operation_id: uuid.UUID, fault_point: EvaluationFaultPoint, *, worker_id: str
) -> bool:
    """Atomically consume at most one matching plan across all workers/restarts."""
    if not Settings().evaluation_fault_matrix:
        return False
    async with async_session_factory() as session:
        changed = await session.execute(
            update(EvaluationFaultPlan)
            .where(
                EvaluationFaultPlan.operation_id == operation_id,
                EvaluationFaultPlan.fault_point == fault_point.value,
                EvaluationFaultPlan.consumed_at.is_(None),
            )
            .values(consumed_at=func.clock_timestamp(), consumed_by=worker_id[:128])
        )
        if (changed.rowcount or 0) != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return False
        await session.commit()
        return True


async def crash_if_planned(
    operation_id: uuid.UUID,
    fault_point: EvaluationFaultPoint,
    *,
    worker_id: str,
    terminate: Callable[[int], object] = os._exit,
) -> None:
    if await consume_fault_plan(operation_id, fault_point, worker_id=worker_id):
        terminate(86)
