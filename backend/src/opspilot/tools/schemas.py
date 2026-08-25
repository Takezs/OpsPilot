"""Pydantic input schemas for the six registry-exposed tools.

Every tool argument set is validated through one of these models before the
underlying adapter runs; the agent never hands raw JSON to an external service.
"""

from pydantic import BaseModel, Field

_EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class GetOrderArgs(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=64)


class CheckRefundEligibilityArgs(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=64)


class RefundOrderArgs(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=64)
    # Task 9: the refund amount feeds the deterministic policy (ALLOW /
    # REQUIRE_APPROVAL / DENY); a non-positive amount is rejected by the schema.
    amount: float = Field(..., gt=0)


class GetRefundStatusArgs(BaseModel):
    order_number: str = Field(..., min_length=1, max_length=64)


class SendEmailArgs(BaseModel):
    to: str = Field(..., min_length=3, max_length=254, pattern=_EMAIL_PATTERN)
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=2000)
