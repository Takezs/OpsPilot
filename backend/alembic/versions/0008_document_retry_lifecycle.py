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
            nullable=False,
            server_default="QUEUED",
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


def downgrade() -> None:
    op.drop_column("document_index_outbox", "lease_expires_at")
    op.drop_column("document_index_outbox", "started_at")
    op.drop_column("document_index_outbox", "status")
    document_index_status.drop(op.get_bind(), checkfirst=True)
