"""Authenticated Run SSE endpoint."""

import uuid
from typing import Annotated

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.execution.models import Operation, OperationAttempt
from opspilot.jobs.service import enqueue_run_job
from opspilot.runs.journal import append_event
from opspilot.runs.models import Run, RunEvent, RunMessage, RunStatus
from opspilot.runs.sanitize import sanitize_payload
from opspilot.runs.schemas import (
    AttemptResponse,
    MessageCreateRequest,
    MessageCreateResponse,
    OperationResponse,
    RunCreateResponse,
    RunDetailResponse,
    RunEventResponse,
)
from opspilot.runs.service import create_agent_run, require_run_access
from opspilot.runs.sse import RedisSSEStream, validate_last_event_id

router = APIRouter(prefix="/runs", tags=["runs"])


def _safe_diagnostic(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = sanitize_payload({"value": value})["value"]
    return sanitized if isinstance(sanitized, str) else None


@router.post("", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    principal: Annotated[Principal, Depends(get_current_principal)],
    evaluation_correlation: Annotated[str | None, Header(alias="X-Evaluation-Correlation")] = None,
) -> RunCreateResponse:
    async with async_session_factory() as session:
        try:
            run = await create_agent_run(
                session, principal, evaluation_correlation=evaluation_correlation
            )
        except IntegrityError:
            await session.rollback()
            if evaluation_correlation is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "run creation conflict") from None
            adopted = await session.scalar(
                select(Run).where(Run.evaluation_correlation == evaluation_correlation)
            )
            if adopted is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "run correlation conflict") from None
            run = adopted
            return RunCreateResponse(run_id=run.id, status=run.status.value)
        await append_event(session, run.id, "run_created", {"owner_user_id": principal.user_id})
        await session.commit()
    return RunCreateResponse(run_id=run.id, status=run.status.value)


@router.get("/by-correlation/{correlation}", response_model=RunCreateResponse)
async def find_correlated_run(
    correlation: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> RunCreateResponse:
    async with async_session_factory() as session:
        run = await session.scalar(select(Run).where(Run.evaluation_correlation == correlation))
        if run is None or (
            principal.role is not Role.ADMIN and run.owner_user_id != uuid.UUID(principal.user_id)
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return RunCreateResponse(run_id=run.id, status=run.status.value)


@router.post(
    "/{run_id}/messages",
    response_model=MessageCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_message(
    run_id: uuid.UUID,
    request: MessageCreateRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    evaluation_correlation: Annotated[str | None, Header(alias="X-Evaluation-Correlation")] = None,
) -> MessageCreateResponse:
    async with async_session_factory() as session:
        run = await require_run_access(session, run_id, principal)
        safe = sanitize_payload({"content": request.content})["content"]
        if not isinstance(safe, str):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid message")
        if evaluation_correlation:
            existing = await session.scalar(
                select(RunMessage).where(
                    RunMessage.run_id == run.id,
                    RunMessage.evaluation_correlation == evaluation_correlation,
                )
            )
            if existing is not None:
                return MessageCreateResponse(
                    message_id=existing.id, run_id=run.id, status=run.status.value
                )
        message = RunMessage(
            run_id=run.id, role="USER", content=safe, evaluation_correlation=evaluation_correlation
        )
        session.add(message)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(RunMessage).where(
                    RunMessage.run_id == run.id,
                    RunMessage.evaluation_correlation == evaluation_correlation,
                )
            )
            if existing is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "message correlation conflict"
                ) from None
            return MessageCreateResponse(
                message_id=existing.id, run_id=run.id, status=run.status.value
            )
        run.status = RunStatus.RUNNING
        await append_event(
            session,
            run.id,
            "user_message_created",
            {"message_id": str(message.id), "content": safe},
        )
        enqueue_run_job(session, message.id)
        await session.commit()
    return MessageCreateResponse(message_id=message.id, run_id=run.id, status=run.status.value)


@router.get("/{run_id}/history", response_model=list[RunEventResponse])
async def get_history(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[RunEventResponse]:
    async with async_session_factory() as session:
        await require_run_access(session, run_id, principal)
        rows = list(
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                .order_by(RunEvent.seq)
                .limit(limit)
            )
        )
    return [
        RunEventResponse(
            run_id=row.run_id,
            seq=row.seq,
            event_type=row.event_type,
            payload=row.payload,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> RunDetailResponse:
    async with async_session_factory() as session:
        run = await require_run_access(session, run_id, principal)
        operations = list(
            await session.scalars(
                select(Operation).where(Operation.run_id == run_id).order_by(Operation.created_at)
            )
        )
        operation_rows: list[OperationResponse] = []
        for operation in operations:
            attempts = list(
                await session.scalars(
                    select(OperationAttempt)
                    .where(OperationAttempt.operation_id == operation.id)
                    .order_by(OperationAttempt.attempt_number)
                )
            )
            operation_rows.append(
                OperationResponse(
                    id=operation.id,
                    tool_name=operation.tool_name,
                    status=operation.status.value,
                    version=operation.version,
                    policy_decision=operation.policy_decision,
                    provider_reference_id=_safe_diagnostic(operation.provider_reference_id),
                    normalized_arguments=sanitize_payload(operation.normalized_arguments),
                    idempotency_key=operation.idempotency_key,
                    attempts=[
                        AttemptResponse(
                            id=attempt.id,
                            attempt_number=attempt.attempt_number,
                            kind=attempt.kind,
                            status=attempt.status,
                            error=_safe_diagnostic(attempt.error),
                            completed_at=attempt.completed_at,
                        )
                        for attempt in attempts
                    ],
                )
            )
    return RunDetailResponse(
        id=run.id,
        status=run.status.value,
        next_seq=run.next_seq,
        created_at=run.created_at,
        operations=operation_rows,
    )


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    async with async_session_factory() as session:
        run = await require_run_access(session, run_id, principal)
        try:
            after_seq = await validate_last_event_id(run, last_event_id)
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    client: redis_async.Redis = redis_async.from_url(  # type: ignore[no-untyped-call]
        Settings().redis_url
    )
    stream = RedisSSEStream(run_id, client)
    try:
        await stream.open()
    except Exception as error:
        await stream.close()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "run event stream unavailable"
        ) from error
    return StreamingResponse(
        stream.events(after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
