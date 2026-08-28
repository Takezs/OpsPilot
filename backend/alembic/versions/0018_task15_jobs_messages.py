"""Add durable run messages and task job outboxes.

Revision ID: 0018_task15_jobs_messages
Revises: 0017_run_ownership
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018_task15_jobs_messages"
down_revision: str | None = "0017_run_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.String(8000), nullable=False),
        sa.Column("citation_snapshots", postgresql.JSONB(), nullable=True),
        sa.Column("in_reply_to_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["in_reply_to_message_id"], ["run_messages.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("in_reply_to_message_id", name="uq_run_message_reply"),
        sa.CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_run_messages_role"),
    )
    op.create_index("ix_run_messages_run_created", "run_messages", ["run_id", "created_at"])
    op.create_table(
        "run_job_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["message_id"], ["run_messages.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_run_job_pending", "run_job_outbox", ["delivered_at", "available_at"])
    op.create_table(
        "operation_job_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["operation_id"], ["tool_operations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "operation_id", "expected_version", "kind", name="uq_operation_job_identity"
        ),
        sa.CheckConstraint("kind IN ('EXECUTE', 'RECONCILE')", name="ck_operation_job_kind"),
    )
    op.create_index(
        "ix_operation_job_pending", "operation_job_outbox", ["delivered_at", "available_at"]
    )


def downgrade() -> None:
    op.drop_table("operation_job_outbox")
    op.drop_table("run_job_outbox")
    op.drop_table("run_messages")
