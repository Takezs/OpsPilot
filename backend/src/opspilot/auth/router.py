from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import User
from opspilot.auth.schemas import LoginRequest, Principal, TokenResponse, TokenSubject
from opspilot.auth.service import AuthenticationError, AuthService
from opspilot.config import Settings
from opspilot.db import get_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=Principal)
async def current_session(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    return principal


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    user = await session.scalar(select(User).where(User.username == request.username))
    settings = Settings()
    service = AuthService(settings.jwt_secret, settings.access_token_ttl_seconds)
    try:
        if user is None or not user.is_active:
            raise AuthenticationError("invalid credentials")
        service.authenticate_password(request.password, user.password_hash)
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        ) from error
    token = service.issue_token(TokenSubject(user_id=str(user.id), role=user.role))
    return TokenResponse(access_token=token)
