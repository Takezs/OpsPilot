"""Add audited document retry attempts."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_document_retry_attempts"
down_revision: str | None = "0006_document_index_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "document_index_outbox_document_id_key",
        "document_index_outbox",
        type_="unique",
    )
    op.add_column(
        "document_index_outbox",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "document_index_outbox",
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "document_index_outbox",
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("document_index_outbox", sa.Column("audit_reason", sa.String(500), nullable=True))
    op.add_column(
        "document_index_outbox",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_document_index_attempt",
        "document_index_outbox",
        ["document_id", "attempt"],
    )


def downgrade() -> None:
    # The preceding schema permits only one outbox row per document, so manual retry history
    # cannot be represented after downgrade.
    op.execute("DELETE FROM document_index_outbox WHERE attempt > 0")
    op.drop_constraint("uq_document_index_attempt", "document_index_outbox", type_="unique")
    op.drop_column("document_index_outbox", "completed_at")
    op.drop_column("document_index_outbox", "audit_reason")
    op.drop_column("document_index_outbox", "requested_at")
    op.drop_column("document_index_outbox", "requested_by")
    op.drop_column("document_index_outbox", "attempt")
    op.create_unique_constraint(
        "document_index_outbox_document_id_key",
        "document_index_outbox",
        ["document_id"],
    )
