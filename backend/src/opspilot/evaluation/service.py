"""Evaluation authorization and durable orchestration services."""

import json
import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.evaluation.config import canonical_configuration, configuration_sha256
from opspilot.evaluation.models import (
    EvaluationExecutionAttempt,
    EvaluationExecutionStatus,
    EvaluationJobOutbox,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationTestExecution,
)
from opspilot.evaluation.schemas import EvaluationConfiguration, FreezeEvaluationRequest


class EvaluationPermissionError(PermissionError):
    pass


class EvaluationConflictError(RuntimeError):
    pass


class EvaluationNotFoundError(LookupError):
    pass


def require_evaluation_admin(principal: Principal) -> None:
    if principal.role is not Role.ADMIN:
        raise EvaluationPermissionError("ADMIN role required")


def require_evaluation_reader(principal: Principal) -> None:
    if principal.role not in {Role.REVIEWER, Role.ADMIN}:
        raise EvaluationPermissionError("evaluation read role required")


async def freeze_test_execution(
    session: AsyncSession,
    principal: Principal,
    request: FreezeEvaluationRequest,
) -> EvaluationTestExecution:
    require_evaluation_admin(principal)
    canonical = canonical_configuration(request.configuration)
    configuration_sha = configuration_sha256(request.configuration)
    run = EvaluationRun(
        dataset_version=request.dataset_version,
        dataset_sha256=request.dataset_sha,
        status=EvaluationRunStatus.PENDING,
        model=request.configuration.model,
        embedding_model=request.configuration.embedding_model,
        reranker_model=request.configuration.reranker_model,
        top_k=request.configuration.top_k,
        prompt_version=request.configuration.prompt_version,
        random_parameters=request.configuration.random_parameters,
        configuration=json.loads(canonical),
    )
    session.add(run)
    await session.flush()
    execution = EvaluationTestExecution(
        evaluation_run_id=run.id,
        dataset_version=request.dataset_version,
        dataset_sha=request.dataset_sha,
        configuration=json.loads(canonical),
        configuration_sha=configuration_sha,
        status=EvaluationExecutionStatus.FROZEN,
        frozen_by_user_id=uuid.UUID(principal.user_id),
    )
    session.add(execution)
    try:
        await session.flush()
    except IntegrityError as error:
        raise EvaluationConflictError("dataset test execution is already frozen") from error
    return execution


async def start_test_execution(
    session: AsyncSession,
    principal: Principal,
    execution_id: uuid.UUID,
) -> EvaluationTestExecution:
    require_evaluation_admin(principal)
    execution = await session.scalar(
        select(EvaluationTestExecution)
        .where(EvaluationTestExecution.id == execution_id)
        .with_for_update()
    )
    if execution is None:
        raise EvaluationNotFoundError("evaluation execution not found")
    if execution.status != EvaluationExecutionStatus.FROZEN:
        raise EvaluationConflictError("evaluation execution is not frozen")
    configuration = EvaluationConfiguration.model_validate(execution.configuration)
    if configuration_sha256(configuration) != execution.configuration_sha:
        raise EvaluationConflictError("frozen configuration hash mismatch")
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    execution.status = EvaluationExecutionStatus.RUNNING
    execution.started_by_user_id = uuid.UUID(principal.user_id)
    execution.started_at = now
    execution.version += 1
    run = await session.get(EvaluationRun, execution.evaluation_run_id, with_for_update=True)
    if run is None or run.dataset_sha256 != execution.dataset_sha:
        raise EvaluationConflictError("evaluation run facts do not match receipt")
    run.status = EvaluationRunStatus.RUNNING
    run.started_at = now
    session.add(EvaluationJobOutbox(execution_id=execution.id, available_at=now))
    session.add(
        EvaluationExecutionAttempt(
            execution_id=execution.id,
            attempt_number=1,
            status="RUNNING",
            started_by_user_id=uuid.UUID(principal.user_id),
            started_at=now,
        )
    )
    await session.flush()
    return execution


async def cancel_test_execution(
    session: AsyncSession, principal: Principal, execution_id: uuid.UUID
) -> EvaluationTestExecution:
    require_evaluation_admin(principal)
    execution = await session.scalar(
        select(EvaluationTestExecution)
        .where(EvaluationTestExecution.id == execution_id)
        .with_for_update()
    )
    if execution is None:
        raise EvaluationNotFoundError("evaluation execution not found")
    if execution.status != EvaluationExecutionStatus.RUNNING:
        raise EvaluationConflictError("only a running execution can be cancelled")
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    execution.status = EvaluationExecutionStatus.CANCELLED
    execution.completed_at = now
    execution.claim_token = None
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.version += 1
    attempt = await session.scalar(
        select(EvaluationExecutionAttempt)
        .where(
            EvaluationExecutionAttempt.execution_id == execution.id,
            EvaluationExecutionAttempt.status == "RUNNING",
        )
        .order_by(EvaluationExecutionAttempt.attempt_number.desc())
        .with_for_update()
    )
    if attempt is not None:
        attempt.status = "CANCELLED"
        attempt.completed_at = now
    return execution


async def resume_test_execution(
    session: AsyncSession, principal: Principal, execution_id: uuid.UUID
) -> EvaluationTestExecution:
    require_evaluation_admin(principal)
    execution = await session.scalar(
        select(EvaluationTestExecution)
        .where(EvaluationTestExecution.id == execution_id)
        .with_for_update()
    )
    if execution is None:
        raise EvaluationNotFoundError("evaluation execution not found")
    if execution.status not in {
        EvaluationExecutionStatus.FAILED,
        EvaluationExecutionStatus.CANCELLED,
    }:
        raise EvaluationConflictError("only failed or cancelled execution can resume")
    configuration = EvaluationConfiguration.model_validate(execution.configuration)
    if configuration_sha256(configuration) != execution.configuration_sha:
        raise EvaluationConflictError("frozen configuration hash mismatch")
    now = cast(datetime, await session.scalar(select(func.clock_timestamp())))
    last_attempt = await session.scalar(
        select(func.coalesce(func.max(EvaluationExecutionAttempt.attempt_number), 0)).where(
            EvaluationExecutionAttempt.execution_id == execution.id
        )
    )
    execution.status = EvaluationExecutionStatus.RUNNING
    execution.completed_at = None
    execution.claim_token = None
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.version += 1
    session.add(
        EvaluationExecutionAttempt(
            execution_id=execution.id,
            attempt_number=int(last_attempt or 0) + 1,
            status="RUNNING",
            started_by_user_id=uuid.UUID(principal.user_id),
            started_at=now,
        )
    )
    outbox = await session.scalar(
        select(EvaluationJobOutbox)
        .where(EvaluationJobOutbox.execution_id == execution.id)
        .with_for_update()
    )
    if outbox is None:
        session.add(EvaluationJobOutbox(execution_id=execution.id, available_at=now))
    else:
        outbox.delivered_at = None
        outbox.available_at = now
        outbox.last_error = None
    run = await session.get(EvaluationRun, execution.evaluation_run_id, with_for_update=True)
    if run is not None:
        run.status = EvaluationRunStatus.RUNNING
        run.completed_at = None
        run.error = None
    await session.flush()
    return execution
