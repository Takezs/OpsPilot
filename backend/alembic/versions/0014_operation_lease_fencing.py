"""Add claim/lease fencing columns and extend the operation status enum.

Task 10. The ``tool_operations`` table gains the lease state that a worker's
conditional PostgreSQL UPDATE populates on claim: ``claim_token`` (one-shot,
non-reusable), ``lease_owner`` and ``lease_expires_at``, plus the optional
``provider_reference_id`` and ``result_payload`` a fenced result write records.

The ``operation_status`` enum is extended with EXECUTING, RETRYING,
OUTCOME_UNKNOWN and RECONCILING. ``guard_occupancy_release`` is also hardened so
the in-flight statuses (EXECUTING / RETRYING / OUTCOME_UNKNOWN / RECONCILING)
join the non-terminal set whose occupancy row may not be deleted, keeping task
9's occupancy invariant true for the whole lifecycle.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_operation_lease_fencing"
down_revision: str | None = "0013_resolution_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NON_TERMINAL = (
    "'CREATED','WAITING_APPROVAL','READY','MANUAL_REVIEW','EXECUTING',"
    "'RETRYING','OUTCOME_UNKNOWN','RECONCILING'"
)


def upgrade() -> None:
    # Adding values to an existing enum keeps the same type OID; the migration
    # runs on its own connection before any app connection caches the type, so
    # the asyncpg enum codec stays consistent for the running suite.
    op.execute("ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'EXECUTING'")
    op.execute("ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'RETRYING'")
    op.execute("ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'OUTCOME_UNKNOWN'")
    op.execute("ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'RECONCILING'")

    op.add_column("tool_operations", sa.Column("claim_token", sa.String(64), nullable=True))
    op.add_column("tool_operations", sa.Column("lease_owner", sa.String(64), nullable=True))
    op.add_column(
        "tool_operations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_operations",
        sa.Column("provider_reference_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "tool_operations",
        sa.Column("result_payload", sa.dialects.postgresql.JSONB(), nullable=True),
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION guard_occupancy_release() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = OLD.operation_id
                  AND o.status IN ({_NON_TERMINAL})
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy release denied: '
                    'occupant is not terminal';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.drop_column("tool_operations", "result_payload")
    op.drop_column("tool_operations", "provider_reference_id")
    op.drop_column("tool_operations", "lease_expires_at")
    op.drop_column("tool_operations", "lease_owner")
    op.drop_column("tool_operations", "claim_token")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_occupancy_release() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM tool_operations o
                WHERE o.id = OLD.operation_id
                  AND o.status IN ('CREATED', 'WAITING_APPROVAL', 'READY', 'MANUAL_REVIEW')
            ) THEN
                RAISE EXCEPTION
                    'operation_idempotency_occupancy release denied: '
                    'occupant is not terminal';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    # PostgreSQL cannot remove a single enum value, so rebuild the type without
    # the task-10 values. This changes the type OID; callers must dispose their
    # asyncpg pools (the migration roundtrip checks do).
    op.execute("ALTER TABLE tool_operations ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE tool_operations ALTER COLUMN status TYPE VARCHAR(20) USING status::text"
    )
    op.execute("DROP TYPE IF EXISTS operation_status")
    op.execute(
        "CREATE TYPE operation_status AS ENUM "
        "('CREATED','WAITING_APPROVAL','READY','MANUAL_REVIEW','DENIED','REJECTED',"
        "'SUCCEEDED','FAILED')"
    )
    op.execute(
        "ALTER TABLE tool_operations ALTER COLUMN status TYPE operation_status "
        "USING status::operation_status"
    )
    op.execute(
        "ALTER TABLE tool_operations ALTER COLUMN status SET DEFAULT 'CREATED'::operation_status"
    )
