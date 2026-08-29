"""Backfill and constrain durable run-job lifecycle.

Revision ID: 0020_run_job_lifecycle
Revises: 0019_run_job_claims
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_run_job_lifecycle"
down_revision: str | None = "0019_run_job_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A persisted reply is the durable completion fact. Everything else from
    # the pre-claim-aware era is made safely publishable again; no owner/token
    # or delivery acknowledgement is guessed.
    op.execute(
        """
        UPDATE run_job_outbox AS job
        SET status = 'COMPLETED',
            claim_token = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            completed_at = clock_timestamp(),
            last_error = NULL
        WHERE EXISTS (
            SELECT 1 FROM run_messages AS reply
            WHERE reply.in_reply_to_message_id = job.message_id
              AND reply.role = 'ASSISTANT'
        )
        """
    )
    op.execute(
        """
        UPDATE run_job_outbox AS job
        SET status = 'PENDING',
            claim_token = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            completed_at = NULL,
            delivered_at = NULL,
            available_at = clock_timestamp(),
            last_error = 'migration reset unconfirmed delivery'
        WHERE NOT EXISTS (
            SELECT 1 FROM run_messages AS reply
            WHERE reply.in_reply_to_message_id = job.message_id
              AND reply.role = 'ASSISTANT'
        )
        """
    )
    op.create_check_constraint(
        "ck_run_job_lifecycle",
        "run_job_outbox",
        """
        (status = 'PENDING'
          AND claim_token IS NULL AND lease_owner IS NULL
          AND lease_expires_at IS NULL AND completed_at IS NULL)
        OR
        (status = 'RUNNING'
          AND claim_token IS NOT NULL AND lease_owner IS NOT NULL
          AND lease_expires_at IS NOT NULL AND completed_at IS NULL)
        OR
        (status = 'COMPLETED'
          AND claim_token IS NULL AND lease_owner IS NULL
          AND lease_expires_at IS NULL AND completed_at IS NOT NULL)
        """,
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_job_lifecycle", "run_job_outbox", type_="check")
