"""Add sanitized operation attempt journal for Task 11.

Revision ID: 0015_operation_attempts
Revises: 0014_operation_lease_fencing
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_operation_attempts"
down_revision: str | None = "0014_operation_lease_fencing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_operations",
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "operation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["operation_id"], ["tool_operations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", "attempt_number", name="uq_operation_attempt_number"),
    )
    op.create_index("ix_operation_attempts_operation", "operation_attempts", ["operation_id"])


def downgrade() -> None:
    op.drop_index("ix_operation_attempts_operation", table_name="operation_attempts")
    op.drop_table("operation_attempts")
    op.drop_column("tool_operations", "retry_not_before")
