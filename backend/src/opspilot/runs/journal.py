"""Append-only Run Journal backed by the PostgreSQL ``run_events`` table."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.runs.models import EventOutbox, RunEvent


class RunNotFoundError(LookupError):
    """Raised when appending an event to a run that does not exist."""


async def append_event(
    session: AsyncSession,
    run_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    """Append an event to a run's journal inside the caller's transaction.

    The run's ``next_seq`` counter is incremented atomically with
    ``UPDATE ... RETURNING``, which also locks the run row so concurrent appends
    to the same run receive continuous, unique seq values. The ``run_events``
    and ``event_outbox`` rows are added to the session but never committed here:
    the caller owns the transaction and commits business state together with the
    journal and the outbox row in a single transaction.
    """
    result = await session.execute(
        text("UPDATE agent_runs SET next_seq = next_seq + 1 WHERE id = :run_id RETURNING next_seq"),
        {"run_id": run_id},
    )
    seq_row = result.first()
    if seq_row is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    seq = int(seq_row[0])
    session.add(RunEvent(run_id=run_id, seq=seq, event_type=event_type, payload=payload))
    session.add(EventOutbox(run_id=run_id, seq=seq))
    return seq
