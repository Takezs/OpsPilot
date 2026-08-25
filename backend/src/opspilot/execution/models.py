"""Durable Operation model and its idempotency occupancy record.

An Operation is persisted before any approval happens; the ``arguments_hash``
snapshot, ``version`` and ``retry_of_operation_id`` make approval binding and
manual-review lineage immutable and auditable. The current idempotency occupancy
(``operation_idempotency_occupancy``) holds the single active Operation per
business key; historical terminal Operations keep the same key as audit rows.
The occupancy is guarded by database triggers (see migration 0011) so the
manual-review resolution gate cannot be bypassed from the application layer.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base


class OperationStatus(StrEnum):
    CREATED = "CREATED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DENIED = "DENIED"
    REJECTED = "REJECTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXECUTING = "EXECUTING"
    RETRYING = "RETRYING"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"


class Operation(Base):
    __tablename__ = "tool_operations"
    __table_args__ = (
        Index("ix_tool_operations_run_id", "run_id"),
        Index("ix_tool_operations_retry_of", "retry_of_operation_id"),
        Index("ix_tool_operations_key", "tool_name", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    tool_name: Mapped[str] = mapped_column(String(64))
    normalized_arguments: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus, name="operation_status"), default=OperationStatus.CREATED
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    policy_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    retry_of_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_operations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Task 10 lease state: set by the winning claim, cleared on terminal writes
    # and on expiry recovery. ``claim_token`` is one-shot and non-reusable.
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )


class OperationIdempotencyOccupancy(Base):
    __tablename__ = "operation_idempotency_occupancy"
    __table_args__ = (
        UniqueConstraint("tool_name", "idempotency_key", name="uq_occupancy_tool_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tool_name: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tool_operations.id", ondelete="CASCADE"), index=True
    )
