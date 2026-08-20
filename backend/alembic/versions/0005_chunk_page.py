"""Persist source page for citation positioning."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_chunk_page"
down_revision: str | None = "0004_user_knowledge_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "page")
