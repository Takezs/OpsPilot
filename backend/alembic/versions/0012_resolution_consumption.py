"""Bind retry authorizations to one replacement and harden the repoint trigger.

Task 9 review fix. ``manual_review_resolutions.replacement_operation_id`` is the
durable, one-shot binding between a ``RETRY_NEW_OPERATION`` resolution and the
single replacement Operation it authorizes; the claim is a conditional UPDATE in
the retry transaction, so a resolution can never create two replacements (even a
DENIED replacement is the definitive result). ``guard_occupancy_repoint`` is
rewritten to validate the new occupant at the database level: it must exist,
match the occupancy's ``tool_name`` / ``idempotency_key``, retry the old
MANUAL_REVIEW Operation, and be the exact replacement bound to a valid
``RETRY_NEW_OPERATION`` resolution.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_resolution_consumption"
down_revision: str | None = "0011_operations_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hardened_repoint_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION guard_occupancy_repoint() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = OLD.operation_id
                  AND o.status = 'MANUAL_REVIEW'
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'old occupant must be MANUAL_REVIEW';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = NEW.operation_id
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'new occupant must exist';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = NEW.operation_id
                  AND o.tool_name = NEW.tool_name
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'new occupant tool does not match the occupancy';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = NEW.operation_id
                  AND o.idempotency_key = NEW.idempotency_key
                  AND o.retry_of_operation_id = OLD.operation_id
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'new occupant idempotency key does not match the occupancy '
                    'or must be a retry of the old MANUAL_REVIEW operation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM manual_review_resolutions r
                WHERE r.operation_id = OLD.operation_id
                  AND r.outcome = 'RETRY_NEW_OPERATION'
                  AND r.replacement_operation_id = NEW.operation_id
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'no RETRY_NEW_OPERATION resolution bound to the new occupant';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """


def _original_repoint_function() -> str:
    return """
        CREATE OR REPLACE FUNCTION guard_occupancy_repoint() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = OLD.operation_id
                  AND o.status = 'MANUAL_REVIEW'
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'occupant must be MANUAL_REVIEW';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM manual_review_resolutions r
                WHERE r.operation_id = OLD.operation_id
                  AND r.outcome = 'RETRY_NEW_OPERATION'
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy repoint denied: '
                    'missing RETRY_NEW_OPERATION manual_review_resolution';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """


def upgrade() -> None:
    op.add_column(
        "manual_review_resolutions",
        # nullable by default; a resolution is bound only when consumed
        sa.Column(
            "replacement_operation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_manual_review_resolutions_replacement",
        "manual_review_resolutions",
        ["replacement_operation_id"],
    )
    op.execute(_hardened_repoint_function())


def downgrade() -> None:
    op.execute(_original_repoint_function())
    op.drop_index(
        "ix_manual_review_resolutions_replacement",
        table_name="manual_review_resolutions",
    )
    op.drop_column("manual_review_resolutions", "replacement_operation_id")
