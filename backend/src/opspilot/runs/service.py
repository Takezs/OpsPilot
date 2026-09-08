"""Owned Run creation and fail-closed authorization."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal
from opspilot.runs.models import Run, RunStatus


async def create_agent_run(
    session: AsyncSession,
    principal: Principal,
    *,
    run_id: uuid.UUID | None = None,
    status_value: RunStatus = RunStatus.QUEUED,
    evaluation_correlation: str | None = None,
) -> Run:
    """Create a user Run; callers cannot supply or forge an owner."""
    run = Run(
        id=run_id or uuid.uuid4(),
        owner_user_id=uuid.UUID(principal.user_id),
        status=status_value,
        evaluation_correlation=evaluation_correlation,
    )
    session.add(run)
    await session.flush()
    return run


async def require_run_access(session: AsyncSession, run_id: uuid.UUID, principal: Principal) -> Run:
    run = await session.get(Run, run_id)
    principal_id = uuid.UUID(principal.user_id)
    if run is None or (principal.role is not Role.ADMIN and run.owner_user_id != principal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run
