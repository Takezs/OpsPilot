from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from opspilot.auth.schemas import TokenSubject


class AuthenticationError(ValueError):
    pass


class AuthService:
    def __init__(self, secret: str, token_ttl_seconds: int = 900) -> None:
        self._secret = secret
        self._token_ttl = timedelta(seconds=token_ttl_seconds)
        self._passwords = PasswordHash.recommended()

    def hash_password(self, password: str) -> str:
        return self._passwords.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        return self._passwords.verify(password, password_hash)

    def authenticate_password(self, password: str, password_hash: str) -> None:
        if not self.verify_password(password, password_hash):
            raise AuthenticationError("invalid credentials")

    def issue_token(self, subject: TokenSubject) -> str:
        now = datetime.now(UTC)
        payload = {
            "sub": subject.user_id,
            "role": subject.role.value,
            "iat": now,
            "exp": now + self._token_ttl,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def decode_token(self, token: str) -> TokenSubject:
        try:
            payload = jwt.decode(token, self._secret, algorithms=["HS256"])
            return TokenSubject(user_id=payload["sub"], role=payload["role"])
        except (jwt.PyJWTError, KeyError, ValueError) as error:
            raise AuthenticationError("invalid token") from error
