from pydantic import BaseModel

from opspilot.auth.models import Role


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenSubject(BaseModel):
    user_id: str
    role: Role
