"""Idempotent release seed; it never touches frozen evaluation receipts."""

import asyncio
import os

from sqlalchemy import select

from opspilot.auth.models import Role, User
from opspilot.auth.service import AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.schemas import AccessLevel

RELEASE_ADMIN_ACCESS_LEVEL = AccessLevel.CONFIDENTIAL


def validate_existing_admin(user: User) -> None:
    """Reject a colliding seed identity without mutating its security facts."""
    compatible = (
        user.is_active
        and user.role is Role.ADMIN
        and user.allowed_departments == ["*"]
        and user.max_access_level == int(RELEASE_ADMIN_ACCESS_LEVEL)
    )
    if not compatible:
        raise RuntimeError("existing release administrator identity is incompatible")


async def seed() -> None:
    password = os.getenv("OPSPILOT_SEED_ADMIN_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("OPSPILOT_SEED_ADMIN_PASSWORD must contain at least 12 characters")
    username = os.getenv("OPSPILOT_SEED_ADMIN_USERNAME", "opspilot-admin")
    async with async_session_factory() as session:
        existing = await session.scalar(select(User).where(User.username == username))
        if existing is None:
            session.add(
                User(
                    username=username,
                    password_hash=AuthService(Settings().jwt_secret).hash_password(password),
                    role=Role.ADMIN,
                    allowed_departments=["*"],
                    max_access_level=int(RELEASE_ADMIN_ACCESS_LEVEL),
                )
            )
            await session.commit()
        else:
            validate_existing_admin(existing)


if __name__ == "__main__":
    asyncio.run(seed())
