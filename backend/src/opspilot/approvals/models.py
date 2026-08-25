"""Approval binding and manual-review resolution models.

``ApprovalRequest`` freezes the operation's ``arguments_hash`` and
``operation_version`` so a reviewer's decision is provably tied to the exact
arguments that were pending. ``ManualReviewResolution`` is an audit record only —
it never mutates the terminal MANUAL_REVIEW Operation; the system releases the
idempotency occupancy and creates a retry Operation only when a resolution with
outcome ``RETRY_NEW_OPERATION`` exists (enforced by database triggers).
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ResolutionOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    RETRY_NEW_OPERATION = "RETRY_NEW_OPERATION"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_operation_id", "operation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tool_operations.id", ondelete="CASCADE")
    )
    arguments_hash: Mapped[str] = mapped_column(String(64))
    operation_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"), default=ApprovalStatus.PENDING
    )
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class ManualReviewResolution(Base):
    __tablename__ = "manual_review_resolutions"
    __table_args__ = (Index("ix_manual_review_resolutions_operation_id", "operation_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tool_operations.id", ondelete="CASCADE")
    )
    outcome: Mapped[ResolutionOutcome] = mapped_column(
        Enum(ResolutionOutcome, name="resolution_outcome")
    )
    resolved_by: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
