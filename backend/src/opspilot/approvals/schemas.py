"""HTTP request/response schemas for the approvals router."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=2000)


class ApprovalDecisionResponse(BaseModel):
    already_processed: bool
    transitioned: bool
    approval_request_id: uuid.UUID
    operation_id: uuid.UUID
    operation_status: str
    operation_version: int


class ManualReviewResolutionRequest(BaseModel):
    outcome: Literal["RESOLVED", "CANCELLED", "RETRY_NEW_OPERATION"]
    note: str | None = Field(default=None, max_length=2000)


class ManualReviewResolutionResponse(BaseModel):
    resolution_id: uuid.UUID
    operation_id: uuid.UUID


class RetryRequest(BaseModel):
    amount: float = Field(..., gt=0)


class RetryResponse(BaseModel):
    operation_id: uuid.UUID
    status: str
    idempotency_key: str


class ApprovalListItem(BaseModel):
    id: uuid.UUID
    operation_id: uuid.UUID
    arguments_hash: str
    operation_version: int
    status: str
    operation_status: str
    tool_name: str
    arguments: dict[str, object]
    expires_at: datetime
    created_at: datetime
