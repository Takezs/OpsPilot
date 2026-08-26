"""Add fail-closed ownership to agent runs.

Revision ID: 0017_run_ownership
Revises: 0016_running_execution_attempt
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_run_ownership"
down_revision: str | None = "0016_running_execution_attempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("owner_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_runs_owner_user",
        "agent_runs",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_agent_runs_owner_user_id", "agent_runs", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_owner_user_id", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_owner_user", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "owner_user_id")
