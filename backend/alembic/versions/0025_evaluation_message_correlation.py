"""Persist idempotency correlation for evaluation user messages."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_eval_msg_corr"
down_revision: str | None = "0024_evaluation_run_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_messages", sa.Column("evaluation_correlation", sa.String(255), nullable=True)
    )
    op.create_unique_constraint(
        "uq_run_messages_evaluation_correlation",
        "run_messages",
        ["run_id", "evaluation_correlation"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_run_messages_evaluation_correlation", "run_messages", type_="unique")
    op.drop_column("run_messages", "evaluation_correlation")
