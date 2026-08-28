"""Add durable execution claims for run message jobs.

Revision ID: 0019_run_job_claims
Revises: 0018_task15_jobs_messages
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_run_job_claims"
down_revision: str | None = "0018_task15_jobs_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_job_outbox",
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
    )
    op.add_column("run_job_outbox", sa.Column("claim_token", sa.String(64), nullable=True))
    op.add_column("run_job_outbox", sa.Column("lease_owner", sa.String(64), nullable=True))
    op.add_column(
        "run_job_outbox", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "run_job_outbox", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_run_job_status",
        "run_job_outbox",
        "status IN ('PENDING', 'RUNNING', 'COMPLETED')",
    )
    op.create_index(
        "ix_run_job_recovery", "run_job_outbox", ["status", "lease_expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_run_job_recovery", table_name="run_job_outbox")
    op.drop_constraint("ck_run_job_status", "run_job_outbox", type_="check")
    op.drop_column("run_job_outbox", "completed_at")
    op.drop_column("run_job_outbox", "lease_expires_at")
    op.drop_column("run_job_outbox", "lease_owner")
    op.drop_column("run_job_outbox", "claim_token")
    op.drop_column("run_job_outbox", "status")
