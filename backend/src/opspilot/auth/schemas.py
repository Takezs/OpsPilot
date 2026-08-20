from pydantic import BaseModel

from opspilot.auth.models import Role
from opspilot.knowledge.schemas import AccessLevel, KnowledgeScope


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenSubject(BaseModel):
    user_id: str
    role: Role


class Principal(BaseModel):
    user_id: str
    role: Role
    allowed_departments: frozenset[str]
    max_access_level: AccessLevel

    @property
    def knowledge_scope(self) -> KnowledgeScope:
        return KnowledgeScope(self.allowed_departments, self.max_access_level)
