import uuid

import asyncpg
import httpx
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from opspilot.auth.dependencies import get_current_principal
from opspilot.auth.models import Role
from opspilot.auth.schemas import Principal, TokenSubject
from opspilot.auth.service import AuthenticationError, AuthService
from opspilot.config import Settings
from opspilot.db import async_session_factory
from opspilot.knowledge.schemas import AccessLevel
from opspilot.main import app


@pytest.mark.parametrize("role", list(Role))
def test_password_login_and_token_round_trip(role: Role) -> None:
    service = AuthService(secret="test-secret-that-is-at-least-32-bytes", token_ttl_seconds=60)
    password_hash = service.hash_password("correct horse")

    assert service.verify_password("correct horse", password_hash)
    token = service.issue_token(TokenSubject(user_id="user-1", role=role))
    assert service.decode_token(token) == TokenSubject(user_id="user-1", role=role)


def test_wrong_password_is_rejected() -> None:
    service = AuthService(secret="test-secret-that-is-at-least-32-bytes")
    password_hash = service.hash_password("correct horse")

    with pytest.raises(AuthenticationError):
        service.authenticate_password("wrong", password_hash)


@pytest.mark.parametrize(
    ("role", "departments", "max_level"),
    [
        (Role.USER, frozenset({"support"}), AccessLevel.INTERNAL),
        (Role.REVIEWER, frozenset({"support", "finance"}), AccessLevel.CONFIDENTIAL),
        (Role.ADMIN, frozenset({"*"}), AccessLevel.CONFIDENTIAL),
    ],
)
def test_principal_restores_knowledge_scope(
    role: Role, departments: frozenset[str], max_level: AccessLevel
) -> None:
    principal = Principal(
        user_id="user-1",
        role=role,
        allowed_departments=departments,
        max_access_level=max_level,
    )

    scope = principal.knowledge_scope

    assert scope.departments == departments
    assert scope.max_access_level is max_level


def test_production_rejects_default_or_weak_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT secret"):
        Settings(environment="production")
    with pytest.raises(ValidationError, match="JWT secret"):
        Settings(environment="production", jwt_secret="development-only-change-me")
    with pytest.raises(ValidationError, match="JWT secret"):
        Settings(environment="production", jwt_secret="short")


def test_test_environment_explicitly_allows_test_secret() -> None:
    settings = Settings(environment="test", jwt_secret="test-secret")
    assert settings.environment == "test"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "departments", "max_level"),
    [
        (Role.USER, ["support"], AccessLevel.INTERNAL),
        (Role.REVIEWER, ["support", "finance"], AccessLevel.CONFIDENTIAL),
        (Role.ADMIN, ["*"], AccessLevel.CONFIDENTIAL),
    ],
)
async def test_bearer_identity_restores_scope_from_database(
    role: Role, departments: list[str], max_level: AccessLevel
) -> None:
    settings = Settings()
    user_id = uuid.uuid4()
    connection = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    await connection.execute(
        "INSERT INTO users "
        "(id, username, password_hash, role, allowed_departments, max_access_level, is_active) "
        "VALUES ($1, $2, 'hash', $3, $4, $5, true)",
        user_id,
        f"scope-{user_id}",
        role.value,
        departments,
        int(max_level),
    )
    token = AuthService(settings.jwt_secret).issue_token(
        TokenSubject(user_id=str(user_id), role=role)
    )
    try:
        async with async_session_factory() as session:
            principal = await get_current_principal(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), session
            )
        assert principal.role is role
        assert principal.knowledge_scope.departments == frozenset(departments)
        assert principal.knowledge_scope.max_access_level is max_level
    finally:
        await connection.execute("DELETE FROM users WHERE id = $1", user_id)
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_verifies_token_and_uses_current_database_identity() -> None:
    settings = Settings()
    user_id = uuid.uuid4()
    connection = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    await connection.execute(
        "INSERT INTO users "
        "(id, username, password_hash, role, allowed_departments, max_access_level, is_active) "
        "VALUES ($1, $2, 'hash', 'USER', ARRAY['support'], 1, true)",
        user_id,
        f"me-{user_id}",
    )
    service = AuthService(settings.jwt_secret, token_ttl_seconds=60)
    user_token = service.issue_token(TokenSubject(user_id=str(user_id), role=Role.USER))
    expired_token = AuthService(settings.jwt_secret, token_ttl_seconds=-1).issue_token(
        TokenSubject(user_id=str(user_id), role=Role.USER)
    )
    pieces = user_token.split(".")
    forged_token = f"{pieces[0]}.{pieces[1]}.forged-signature"
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"}
            )
            assert response.status_code == 200
            assert response.json() == {
                "user_id": str(user_id),
                "role": "USER",
                "allowed_departments": ["support"],
                "max_access_level": 1,
            }
            assert "token" not in response.text

            for invalid in (forged_token, expired_token):
                response = await client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {invalid}"}
                )
                assert response.status_code == 401

            await connection.execute(
                "UPDATE users SET role = 'ADMIN', allowed_departments = ARRAY['*'], "
                "max_access_level = 2 WHERE id = $1",
                user_id,
            )
            response = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"}
            )
            assert response.status_code == 200
            assert response.json()["role"] == "ADMIN"

            await connection.execute("UPDATE users SET is_active = false WHERE id = $1", user_id)
            response = await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"}
            )
            assert response.status_code == 401
    finally:
        await connection.execute("DELETE FROM users WHERE id = $1", user_id)
        await connection.close()
