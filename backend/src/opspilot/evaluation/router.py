"""Role-protected evaluation freeze, execution and audit API."""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.evaluation.models import EvaluationCaseRecord, EvaluationRun, EvaluationTestExecution
from opspilot.evaluation.report import build_report_artifacts
from opspilot.evaluation.schemas import (
    DatasetManifest,
    EvaluationExecutionResponse,
    FreezeEvaluationRequest,
)
from opspilot.evaluation.service import (
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationPermissionError,
    cancel_test_execution,
    freeze_test_execution,
    require_evaluation_reader,
    resume_test_execution,
    start_test_execution,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _response(row: EvaluationTestExecution) -> EvaluationExecutionResponse:
    return EvaluationExecutionResponse(
        id=row.id,
        evaluation_run_id=row.evaluation_run_id,
        dataset_version=row.dataset_version,
        dataset_sha=row.dataset_sha,
        configuration_sha=row.configuration_sha,
        status=str(row.status),
        frozen_at=row.frozen_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        failure_summary=row.failure_summary,
    )


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, EvaluationPermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(error))
    if isinstance(error, EvaluationNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    return HTTPException(status.HTTP_409_CONFLICT, str(error))


@router.post(
    "/test/freeze",
    response_model=EvaluationExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def freeze_test(
    request: FreezeEvaluationRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EvaluationExecutionResponse:
    manifest = DatasetManifest.model_validate_json(
        (Path(Settings().evaluation_dataset_root) / "manifest.json").read_text(encoding="utf-8")
    )
    if (
        request.dataset_version != manifest.corpus_version
        or request.dataset_sha != manifest.test_sha256
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "dataset does not match the published frozen test manifest",
        )
    async with async_session_factory() as session:
        try:
            row = await freeze_test_execution(session, principal, request)
            await session.commit()
        except (EvaluationPermissionError, EvaluationConflictError) as error:
            await session.rollback()
            raise _http_error(error) from error
    return _response(row)


@router.post("/{execution_id}/start", response_model=EvaluationExecutionResponse)
async def start_test(
    execution_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EvaluationExecutionResponse:
    async with async_session_factory() as session:
        try:
            row = await start_test_execution(session, principal, execution_id)
            await session.commit()
        except (
            EvaluationPermissionError,
            EvaluationConflictError,
            EvaluationNotFoundError,
        ) as error:
            await session.rollback()
            raise _http_error(error) from error
    return _response(row)


@router.post("/{execution_id}/cancel", response_model=EvaluationExecutionResponse)
async def cancel_test(
    execution_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EvaluationExecutionResponse:
    async with async_session_factory() as session:
        try:
            row = await cancel_test_execution(session, principal, execution_id)
            await session.commit()
        except (
            EvaluationPermissionError,
            EvaluationConflictError,
            EvaluationNotFoundError,
        ) as error:
            await session.rollback()
            raise _http_error(error) from error
    return _response(row)


@router.post("/{execution_id}/resume", response_model=EvaluationExecutionResponse)
async def resume_test(
    execution_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EvaluationExecutionResponse:
    async with async_session_factory() as session:
        try:
            row = await resume_test_execution(session, principal, execution_id)
            await session.commit()
        except (
            EvaluationPermissionError,
            EvaluationConflictError,
            EvaluationNotFoundError,
        ) as error:
            await session.rollback()
            raise _http_error(error) from error
    return _response(row)


@router.get("", response_model=list[EvaluationExecutionResponse])
async def list_evaluations(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> list[EvaluationExecutionResponse]:
    try:
        require_evaluation_reader(principal)
    except EvaluationPermissionError as error:
        raise _http_error(error) from error
    async with async_session_factory() as session:
        rows = list(
            await session.scalars(
                select(EvaluationTestExecution)
                .order_by(EvaluationTestExecution.frozen_at.desc())
                .limit(100)
            )
        )
    return [_response(row) for row in rows]


@router.get("/{execution_id}", response_model=EvaluationExecutionResponse)
async def get_evaluation(
    execution_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> EvaluationExecutionResponse:
    try:
        require_evaluation_reader(principal)
    except EvaluationPermissionError as error:
        raise _http_error(error) from error
    async with async_session_factory() as session:
        row = await session.get(EvaluationTestExecution, execution_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evaluation execution not found")
    return _response(row)


@router.get("/{execution_id}/report.{report_format}")
async def get_evaluation_report(
    execution_id: uuid.UUID,
    report_format: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    try:
        require_evaluation_reader(principal)
    except EvaluationPermissionError as error:
        raise _http_error(error) from error
    async with async_session_factory() as session:
        execution = await session.get(EvaluationTestExecution, execution_id)
        if execution is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "evaluation execution not found")
        run = await session.get(EvaluationRun, execution.evaluation_run_id)
        rows = list(
            await session.scalars(
                select(EvaluationCaseRecord)
                .where(EvaluationCaseRecord.evaluation_run_id == execution.evaluation_run_id)
                .order_by(EvaluationCaseRecord.dataset_case_id, EvaluationCaseRecord.repetition)
            )
        )
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "evaluation run facts are unavailable")
    artifacts = build_report_artifacts(
        str(run.id),
        run.configuration,
        [
            {
                "case_id": row.dataset_case_id,
                "repetition": row.repetition,
                "actual": row.actual_output,
                "scores": row.deterministic_scores,
                "latency_ms": row.latency_ms,
                "error": row.error,
            }
            for row in rows
        ],
    )
    formats = {
        "json": (artifacts.json_bytes, "application/json"),
        "csv": (artifacts.csv_bytes, "text/csv; charset=utf-8"),
        "html": (artifacts.html_bytes, "text/html; charset=utf-8"),
    }
    selected = formats.get(report_format)
    if selected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report format not found")
    return Response(content=selected[0], media_type=selected[1])
