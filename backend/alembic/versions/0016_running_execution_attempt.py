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
    op.execute(
        sa.text(
            """
            WITH ranked_running AS (
                SELECT
                    attempt.id,
                    operation.status::text AS operation_status,
                    row_number() OVER (
                        PARTITION BY attempt.operation_id
                        ORDER BY attempt.attempt_number DESC, attempt.id DESC
                    ) AS running_rank
                FROM operation_attempts AS attempt
                JOIN tool_operations AS operation ON operation.id = attempt.operation_id
                WHERE attempt.kind = 'EXECUTION' AND attempt.status = 'RUNNING'
            )
            UPDATE operation_attempts AS attempt
            SET
                status = CASE
                    WHEN ranked.operation_status IN ('OUTCOME_UNKNOWN', 'RECONCILING')
                        THEN 'OUTCOME_UNKNOWN'
                    ELSE 'ABANDONED'
                END,
                completed_at = clock_timestamp(),
                error = 'closed by 0016 running execution attempt backfill'
            FROM ranked_running AS ranked
            WHERE attempt.id = ranked.id
              AND NOT (
                  ranked.operation_status = 'EXECUTING'
                  AND ranked.running_rank = 1
              )
            """
        )
    )
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
