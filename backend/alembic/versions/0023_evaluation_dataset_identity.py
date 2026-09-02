"""Bind frozen evaluation executions to verified dataset artifact bytes.

Revision ID: 0023_evaluation_dataset_identity
Revises: 0022_evaluation_execution_audit
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_evaluation_dataset_identity"
down_revision: str | None = "0022_evaluation_execution_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS also repairs development databases that briefly applied an
    # unpublished 0022 draft containing this column. Existing verified values
    # are preserved byte-for-byte.
    op.execute(
        "ALTER TABLE evaluation_test_executions ADD COLUMN IF NOT EXISTS dataset_identity JSONB"
    )
    op.execute(
        "UPDATE evaluation_test_executions SET dataset_identity = "
        "jsonb_build_object('legacy_unverified', true, "
        "'schema_version', 'legacy-unverified', 'test_sha256', dataset_sha) "
        "WHERE dataset_identity IS NULL"
    )
    op.execute("ALTER TABLE evaluation_test_executions ALTER COLUMN dataset_identity SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_test_executions DROP COLUMN IF EXISTS dataset_identity")
