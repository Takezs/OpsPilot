"""Ensure one running execution attempt per operation.

Revision ID: 0016_running_execution_attempt
Revises: 0015_operation_attempts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_running_execution_attempt"
down_revision: str | None = "0015_operation_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_operation_attempt_running_execution",
        "operation_attempts",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'EXECUTION' AND status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_operation_attempt_running_execution",
        table_name="operation_attempts",
    )
