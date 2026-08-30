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
        UniqueConstraint("evaluation_run_id", "dataset_case_id", name="uq_evaluation_run_case"),
        CheckConstraint("latency_ms >= 0", name="ck_evaluation_case_latency"),
        Index("ix_evaluation_cases_run", "evaluation_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="CASCADE")
    )
    dataset_case_id: Mapped[str] = mapped_column(String(128))
    actual_output: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    deterministic_scores: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
