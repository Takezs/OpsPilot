"""Persistence reserved for reproducible evaluation runs and case results."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class EvaluationRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvaluationExecutionStatus(StrEnum):
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EvaluationFaultPoint(StrEnum):
    BEFORE_EXTERNAL_EFFECT = "BEFORE_EXTERNAL_EFFECT"
    AFTER_EXTERNAL_EFFECT_BEFORE_LOCAL_COMMIT = "AFTER_EXTERNAL_EFFECT_BEFORE_LOCAL_COMMIT"
    AFTER_LOCAL_COMMIT_BEFORE_JOB_ACK = "AFTER_LOCAL_COMMIT_BEFORE_JOB_ACK"


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint("top_k BETWEEN 1 AND 100", name="ck_evaluation_run_top_k"),
        CheckConstraint(
            "dataset_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evaluation_run_dataset_sha256"
        ),
        CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED','FAILED') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_evaluation_run_lifecycle",
        ),
        Index("ix_evaluation_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_version: Mapped[str] = mapped_column(String(64))
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=EvaluationRunStatus.PENDING)
    model: Mapped[str] = mapped_column(String(128))
    embedding_model: Mapped[str] = mapped_column(String(128))
    reranker_model: Mapped[str] = mapped_column(String(128))
    top_k: Mapped[int] = mapped_column(Integer)
    prompt_version: Mapped[str] = mapped_column(String(128))
    random_parameters: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationCaseRecord(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id",
            "dataset_case_id",
            "repetition",
            name="uq_evaluation_run_case_repetition",
        ),
        CheckConstraint("latency_ms >= 0", name="ck_evaluation_case_latency"),
        Index("ix_evaluation_cases_run", "evaluation_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE")
    )
    dataset_case_id: Mapped[str] = mapped_column(String(128))
    repetition: Mapped[int] = mapped_column(Integer, default=1)
    actual_output: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    deterministic_scores: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class EvaluationTestExecution(Base):
    __tablename__ = "evaluation_test_executions"
    __table_args__ = (
        UniqueConstraint("dataset_sha", name="uq_evaluation_test_dataset_sha"),
        UniqueConstraint("evaluation_run_id", name="uq_evaluation_test_run"),
        CheckConstraint("version >= 0", name="ck_evaluation_test_version"),
        CheckConstraint(
            "configuration_sha ~ '^[0-9a-f]{64}$' AND dataset_sha ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_test_hashes",
        ),
        CheckConstraint(
            "(status = 'FROZEN' AND started_by_user_id IS NULL AND started_at IS NULL "
            "AND completed_at IS NULL AND claim_token IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL) OR "
            "(status = 'RUNNING' AND started_by_user_id IS NOT NULL AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND ((claim_token IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL) OR (claim_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL))) OR "
            "(status IN ('COMPLETED','FAILED','CANCELLED') AND started_by_user_id IS NOT NULL "
            "AND started_at IS NOT NULL AND completed_at IS NOT NULL AND claim_token IS NULL "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_evaluation_test_lifecycle",
        ),
        Index("ix_evaluation_test_status", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="RESTRICT")
    )
    dataset_version: Mapped[str] = mapped_column(String(64))
    dataset_sha: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB)
    dataset_identity: Mapped[dict[str, object]] = mapped_column(JSONB)
    configuration_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=EvaluationExecutionStatus.FROZEN)
    frozen_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)


class EvaluationJobOutbox(Base):
    __tablename__ = "evaluation_job_outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_evaluation_job_attempts"),
        Index("ix_evaluation_job_pending", "delivered_at", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_test_executions.id", ondelete="CASCADE"),
        unique=True,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class EvaluationExecutionAttempt(Base):
    __tablename__ = "evaluation_execution_attempts"
    __table_args__ = (
        UniqueConstraint("execution_id", "attempt_number", name="uq_evaluation_execution_attempt"),
        Index("ix_evaluation_attempt_execution", "execution_id", "attempt_number"),
        CheckConstraint("attempt_number > 0", name="ck_evaluation_attempt_number"),
        CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED','FAILED','CANCELLED') AND completed_at IS NOT NULL)",
            name="ck_evaluation_attempt_lifecycle",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_test_executions.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    started_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EvaluationFaultPlan(Base):
    __tablename__ = "evaluation_fault_plans"
    __table_args__ = (
        UniqueConstraint(
            "matrix_run_id", "operation_id", "fault_point", name="uq_evaluation_fault_plan"
        ),
        CheckConstraint(
            "fault_point IN ('BEFORE_EXTERNAL_EFFECT',"
            "'AFTER_EXTERNAL_EFFECT_BEFORE_LOCAL_COMMIT',"
            "'AFTER_LOCAL_COMMIT_BEFORE_JOB_ACK')",
            name="ck_evaluation_fault_point",
        ),
        Index("ix_evaluation_fault_pending", "operation_id", "fault_point", "consumed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    matrix_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE")
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tool_operations.id", ondelete="CASCADE")
    )
    fault_point: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
