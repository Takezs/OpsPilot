"""Add durable Operations, idempotency occupancy, approvals and manual-review resolutions.

Task 9. The ``operation_idempotency_occupancy`` table holds the single current
occupant per ``(tool_name, idempotency_key)``; historical terminal Operations may
keep the same business key. Two BEFORE triggers prove the resolution gate so the
application layer can never release or re-point an active occupancy without the
right terminal status / ``RETRY_NEW_OPERATION`` resolution.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_operations_approvals"
down_revision: str | None = "0010_runs_journal_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


operation_status = sa.Enum(
    "CREATED",
    "WAITING_APPROVAL",
    "READY",
    "MANUAL_REVIEW",
    "DENIED",
    "REJECTED",
    "SUCCEEDED",
    "FAILED",
    name="operation_status",
)
approval_status = sa.Enum(
    "PENDING",
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    name="approval_status",
)
resolution_outcome = sa.Enum(
    "RESOLVED",
    "CANCELLED",
    "RETRY_NEW_OPERATION",
    name="resolution_outcome",
)


def upgrade() -> None:
    op.create_table(
        "tool_operations",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("normalized_arguments", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column(
            "status",
            operation_status,
            nullable=False,
            server_default=sa.text("'CREATED'::operation_status"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_decision", sa.String(16), nullable=True),
        sa.Column(
            "retry_of_operation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_tool_operations_run_id", "tool_operations", ["run_id"])
    op.create_index("ix_tool_operations_retry_of", "tool_operations", ["retry_of_operation_id"])
    op.create_index("ix_tool_operations_key", "tool_operations", ["tool_name", "idempotency_key"])

    op.create_table(
        "operation_idempotency_occupancy",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column(
            "operation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("tool_name", "idempotency_key", name="uq_occupancy_tool_key"),
    )
    op.create_index(
        "ix_operation_idempotency_occupancy_operation_id",
        "operation_idempotency_occupancy",
        ["operation_id"],
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            approval_status,
            nullable=False,
            server_default=sa.text("'PENDING'::approval_status"),
        ),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_approval_requests_operation_id", "approval_requests", ["operation_id"])

    op.create_table(
        "manual_review_resolutions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            resolution_outcome,
            nullable=False,
            server_default=sa.text("'RESOLVED'::resolution_outcome"),
        ),
        sa.Column("resolved_by", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_manual_review_resolutions_operation_id",
        "manual_review_resolutions",
        ["operation_id"],
    )

    op.execute(
        """
        CREATE FUNCTION guard_occupancy_repoint() RETURNS trigger AS $$
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
    )
    op.execute(
        """
        CREATE FUNCTION guard_occupancy_release() RETURNS trigger AS $$
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
    op.execute(
        """
        CREATE TRIGGER guard_occupancy_repoint
        BEFORE UPDATE OF operation_id ON operation_idempotency_occupancy
        FOR EACH ROW EXECUTE FUNCTION guard_occupancy_repoint()
        """
    )
    op.execute(
        """
        CREATE TRIGGER guard_occupancy_release
        BEFORE DELETE ON operation_idempotency_occupancy
        FOR EACH ROW EXECUTE FUNCTION guard_occupancy_release()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS guard_occupancy_release ON operation_idempotency_occupancy")
    op.execute("DROP TRIGGER IF EXISTS guard_occupancy_repoint ON operation_idempotency_occupancy")
    op.execute("DROP FUNCTION IF EXISTS guard_occupancy_release()")
    op.execute("DROP FUNCTION IF EXISTS guard_occupancy_repoint()")
    op.drop_index(
        "ix_manual_review_resolutions_operation_id", table_name="manual_review_resolutions"
    )
    op.drop_table("manual_review_resolutions")
    op.drop_index("ix_approval_requests_operation_id", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_index(
        "ix_operation_idempotency_occupancy_operation_id",
        table_name="operation_idempotency_occupancy",
    )
    op.drop_table("operation_idempotency_occupancy")
    op.drop_index("ix_tool_operations_key", table_name="tool_operations")
    op.drop_index("ix_tool_operations_retry_of", table_name="tool_operations")
    op.drop_index("ix_tool_operations_run_id", table_name="tool_operations")
    op.drop_table("tool_operations")
    resolution_outcome.drop(op.get_bind(), checkfirst=True)
    approval_status.drop(op.get_bind(), checkfirst=True)
    operation_status.drop(op.get_bind(), checkfirst=True)
