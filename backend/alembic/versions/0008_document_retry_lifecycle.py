"""Add retry attempt lifecycle and lease."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_document_retry_lifecycle"
down_revision: str | None = "0007_document_retry_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


document_index_status = sa.Enum(
    "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="document_index_status"
)


def upgrade() -> None:
    document_index_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "document_index_outbox",
        sa.Column(
            "status",
            document_index_status,
            nullable=True,
        ),
    )
    op.add_column(
        "document_index_outbox",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "document_index_outbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Legacy manual attempts had only completed_at: a value meant success, while a delivered
    # unfinished job can no longer be proven alive during deployment. Close the latter as FAILED
    # so an operator can create a fresh, uniquely identified attempt.
    op.execute(
        "UPDATE document_index_outbox SET status = 'SUCCEEDED' "
        "WHERE attempt > 0 AND completed_at IS NOT NULL"
    )
    op.execute(
        "UPDATE document_index_outbox SET status = 'FAILED', completed_at = now(), "
        "lease_expires_at = NULL WHERE attempt > 0 AND completed_at IS NULL"
    )
    # Initial-ingestion rows share the table but are not manual retry attempts. Backfill their
    # audit status from the document fact, and lease non-terminal rows so none can remain pending
    # forever after upgrade.
    op.execute(
        "UPDATE document_index_outbox AS o SET status = 'SUCCEEDED', "
        "completed_at = COALESCE(o.completed_at, now()), lease_expires_at = NULL "
        "FROM documents AS d WHERE o.attempt = 0 AND o.document_id = d.id "
        "AND d.status = 'READY'"
    )
    op.execute(
        "UPDATE document_index_outbox AS o SET status = 'FAILED', "
        "completed_at = COALESCE(o.completed_at, now()), lease_expires_at = NULL "
        "FROM documents AS d WHERE o.attempt = 0 AND o.document_id = d.id "
        "AND d.status = 'FAILED'"
    )
    op.execute(
        "UPDATE document_index_outbox AS o SET status = 'RUNNING', "
        "started_at = COALESCE(o.started_at, o.delivered_at, o.requested_at), "
        "lease_expires_at = now() + interval '360 seconds' "
        "FROM documents AS d WHERE o.attempt = 0 AND o.document_id = d.id "
        "AND d.status NOT IN ('READY', 'FAILED') AND o.delivered_at IS NOT NULL"
    )
    op.execute(
        "UPDATE document_index_outbox AS o SET status = 'QUEUED', "
        "lease_expires_at = now() + interval '360 seconds' "
        "FROM documents AS d WHERE o.attempt = 0 AND o.document_id = d.id "
        "AND d.status NOT IN ('READY', 'FAILED') AND o.delivered_at IS NULL"
    )
    op.alter_column(
        "document_index_outbox",
        "status",
        existing_type=document_index_status,
        nullable=False,
        server_default=sa.text("'QUEUED'::document_index_status"),
    )


def downgrade() -> None:
    op.drop_column("document_index_outbox", "lease_expires_at")
    op.drop_column("document_index_outbox", "started_at")
    op.drop_column("document_index_outbox", "status")
    document_index_status.drop(op.get_bind(), checkfirst=True)
