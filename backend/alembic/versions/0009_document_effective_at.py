"""Add document effective date."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_document_effective_at"
down_revision: str | None = "0008_document_retry_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "effective_at")
