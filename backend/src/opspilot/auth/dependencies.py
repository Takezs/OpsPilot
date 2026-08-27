import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.auth.models import User
from opspilot.auth.schemas import Principal
from opspilot.auth.service import AuthenticationError, AuthService
from opspilot.config import Settings
from opspilot.db import get_session
from opspilot.knowledge.schemas import AccessLevel

bearer = HTTPBearer(auto_error=False)


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    settings = Settings()
    try:
        subject = AuthService(settings.jwt_secret).decode_token(credentials.credentials)
        user = await session.get(User, uuid.UUID(subject.user_id))
    except (AuthenticationError, ValueError) as error:
        raise unauthorized from error
    if user is None or not user.is_active:
        raise unauthorized
    principal = Principal(
        user_id=str(user.id),
        role=user.role,
        allowed_departments=frozenset(user.allowed_departments),
        max_access_level=AccessLevel(user.max_access_level),
    )
    await session.rollback()
    return principal
