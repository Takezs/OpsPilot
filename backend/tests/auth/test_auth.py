import pytest

from opspilot.auth.models import Role
from opspilot.auth.schemas import TokenSubject
from opspilot.auth.service import AuthenticationError, AuthService


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
