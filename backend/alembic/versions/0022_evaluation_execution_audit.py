"""Add exactly-once frozen test execution audit and job intent.

Revision ID: 0022_evaluation_execution_audit
Revises: 0021_evaluation_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_evaluation_execution_audit"
down_revision: str | None = "0021_evaluation_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_cases",
        sa.Column("repetition", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint("uq_evaluation_run_case", "evaluation_cases", type_="unique")
    op.create_unique_constraint(
        "uq_evaluation_run_case_repetition",
        "evaluation_cases",
        ["evaluation_run_id", "dataset_case_id", "repetition"],
    )
    op.create_table(
        "evaluation_test_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evaluation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("dataset_sha", sa.String(64), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("dataset_identity", postgresql.JSONB(), nullable=False),
        sa.Column("configuration_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="FROZEN"),
        sa.Column(
            "frozen_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "started_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "frozen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_summary", sa.String(500), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("version >= 0", name="ck_evaluation_test_version"),
        sa.CheckConstraint(
            "configuration_sha ~ '^[0-9a-f]{64}$' AND dataset_sha ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_test_hashes",
        ),
        sa.CheckConstraint(
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
        sa.UniqueConstraint("dataset_sha", name="uq_evaluation_test_dataset_sha"),
        sa.UniqueConstraint("evaluation_run_id", name="uq_evaluation_test_run"),
    )
    op.create_index(
        "ix_evaluation_test_status",
        "evaluation_test_executions",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "evaluation_job_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_test_executions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_evaluation_job_attempts"),
    )
    op.create_index(
        "ix_evaluation_job_pending",
        "evaluation_job_outbox",
        ["delivered_at", "available_at"],
    )
    op.create_table(
        "evaluation_execution_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_test_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
        sa.Column(
            "started_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_evaluation_attempt_number"),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED','FAILED','CANCELLED') AND completed_at IS NOT NULL)",
            name="ck_evaluation_attempt_lifecycle",
        ),
        sa.UniqueConstraint(
            "execution_id", "attempt_number", name="uq_evaluation_execution_attempt"
        ),
    )
    op.create_index(
        "ix_evaluation_attempt_execution",
        "evaluation_execution_attempts",
        ["execution_id", "attempt_number"],
    )
    op.create_table(
        "evaluation_fault_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "matrix_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fault_point", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "fault_point IN ('BEFORE_EXTERNAL_EFFECT',"
            "'AFTER_EXTERNAL_EFFECT_BEFORE_LOCAL_COMMIT',"
            "'AFTER_LOCAL_COMMIT_BEFORE_JOB_ACK')",
            name="ck_evaluation_fault_point",
        ),
        sa.UniqueConstraint(
            "matrix_run_id", "operation_id", "fault_point", name="uq_evaluation_fault_plan"
        ),
    )
    op.create_index(
        "ix_evaluation_fault_pending",
        "evaluation_fault_plans",
        ["operation_id", "fault_point", "consumed_at"],
    )


def downgrade() -> None:
    # Task 17 development builds briefly stamped 0022 before the attempt audit
    # table was added.  Keep downgrade usable for those databases as well as
    # for a clean installation of the final migration.
    op.execute("DROP INDEX IF EXISTS ix_evaluation_fault_pending")
    op.execute("DROP TABLE IF EXISTS evaluation_fault_plans")
    op.execute("DROP INDEX IF EXISTS ix_evaluation_attempt_execution")
    op.execute("DROP TABLE IF EXISTS evaluation_execution_attempts")
    op.drop_index("ix_evaluation_job_pending", table_name="evaluation_job_outbox")
    op.drop_table("evaluation_job_outbox")
    op.drop_index("ix_evaluation_test_status", table_name="evaluation_test_executions")
    op.drop_table("evaluation_test_executions")
    op.drop_constraint("uq_evaluation_run_case_repetition", "evaluation_cases", type_="unique")
    op.create_unique_constraint(
        "uq_evaluation_run_case",
        "evaluation_cases",
        ["evaluation_run_id", "dataset_case_id"],
    )
    op.drop_column("evaluation_cases", "repetition")
