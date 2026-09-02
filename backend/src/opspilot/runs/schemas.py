import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_serializer


class RunCreateResponse(BaseModel):
    run_id: uuid.UUID
    status: str


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class MessageCreateResponse(BaseModel):
    message_id: uuid.UUID
    run_id: uuid.UUID
    status: str


class RunEventResponse(BaseModel):
    run_id: uuid.UUID
    seq: int
    event_type: str
    payload: dict[str, object]
    created_at: datetime


class AttemptResponse(BaseModel):
    id: uuid.UUID
    attempt_number: int
    kind: str
    status: str
    error: str | None
    completed_at: datetime | None

    @field_serializer("completed_at")
    def serialize_completed_at(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None


class OperationResponse(BaseModel):
    id: uuid.UUID
    tool_name: str
    status: str
    version: int
    policy_decision: str | None
    provider_reference_id: str | None
    normalized_arguments: dict[str, object]
    idempotency_key: str
    attempts: list[AttemptResponse]


class RunDetailResponse(BaseModel):
    id: uuid.UUID
    status: str
    next_seq: int
    created_at: datetime
    operations: list[OperationResponse]
