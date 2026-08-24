"""Add Run Journal, run events and the transactional event outbox."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_runs_journal_outbox"
down_revision: str | None = "0009_document_effective_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


run_status = sa.Enum("QUEUED", "RUNNING", "COMPLETED", "FAILED", name="run_status")


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "status",
            run_status,
            nullable=False,
            server_default=sa.text("'QUEUED'::run_status"),
        ),
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_event_outbox_seq"),
    )
    op.create_index("ix_event_outbox_run_id", "event_outbox", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_event_outbox_run_id", table_name="event_outbox")
    op.drop_table("event_outbox")
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_table("agent_runs")
    run_status.drop(op.get_bind(), checkfirst=True)
