"""Make the replacement binding immutable and the replacement undeletable.

Task 9 review (P2). ``manual_review_resolutions.replacement_operation_id`` was
created with ``ON DELETE SET NULL`` and no database-level protection: raw SQL
could clear or repoint an already-consumed retry authorization, and deleting a
bound replacement (e.g. a DENIED one that holds no occupancy) reset the binding
to NULL so the same authorization could be consumed again.

This migration replaces the foreign key with ``ON DELETE RESTRICT`` (a bound
replacement can never be deleted, keeping the Operation audit trail intact) and
installs a BEFORE UPDATE trigger that makes the binding immutable once set: the
NULL -> value transition (first consumption) is the only allowed change.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_resolution_binding"
down_revision: str | None = "0012_resolution_consumption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "manual_review_resolutions_replacement_operation_id_fkey"


def _binding_guard_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION guard_resolution_binding() RETURNS trigger AS $$
        BEGIN
            IF OLD.replacement_operation_id IS NOT NULL
               AND NEW.replacement_operation_id IS DISTINCT FROM OLD.replacement_operation_id
            THEN
                RAISE EXCEPTION
                    'manual_review_resolutions binding is immutable: '
                    'replacement_operation_id cannot be cleared or repointed once bound';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "manual_review_resolutions", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "manual_review_resolutions",
        "tool_operations",
        ["replacement_operation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(_binding_guard_function())
    op.execute(
        """
        CREATE TRIGGER guard_resolution_binding
        BEFORE UPDATE OF replacement_operation_id ON manual_review_resolutions
        FOR EACH ROW EXECUTE FUNCTION guard_resolution_binding()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS guard_resolution_binding ON manual_review_resolutions")
    op.execute("DROP FUNCTION IF EXISTS guard_resolution_binding()")
    op.drop_constraint(_CONSTRAINT, "manual_review_resolutions", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "manual_review_resolutions",
        "tool_operations",
        ["replacement_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
