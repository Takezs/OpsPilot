"""Authenticated Run SSE endpoint."""

import uuid
from typing import Annotated

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.schemas import Principal
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.runs.service import require_run_access
from opspilot.runs.sse import RedisSSEStream, validate_last_event_id

router = APIRouter(prefix="/runs", tags=["runs"])


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
