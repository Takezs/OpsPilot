"""Persist evaluation case to public Run correlation."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0024_evaluation_run_correlation"
down_revision: str | None = "0023_evaluation_dataset_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("evaluation_correlation", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_agent_runs_evaluation_correlation", "agent_runs", ["evaluation_correlation"])

def downgrade() -> None:
    op.drop_constraint("uq_agent_runs_evaluation_correlation", "agent_runs", type_="unique")
    op.drop_column("agent_runs", "evaluation_correlation")
