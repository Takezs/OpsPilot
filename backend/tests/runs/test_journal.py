"""Run Journal tests: continuous unique seq and atomic rollback.

Task 7 guarantees:
- concurrent appends to one run yield a continuous, unique ``(run_id, seq)`` set;
- an event_outbox insert failure rolls back the event row and the run's seq
  counter together with the caller's business transaction.
"""

import asyncio
import uuid
from collections.abc import Sequence

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from opspilot.config import Settings
from opspilot.db import async_session_factory, engine
from opspilot.runs.journal import RunNotFoundError, append_event
from opspilot.runs.models import EventOutbox, Run, RunEvent, RunStatus
from opspilot.runs.sanitize import (
    MAX_PAYLOAD_BYTES,
    MAX_STRING_LENGTH,
    REDACTED,
    PayloadInvalidError,
    PayloadTooLargeError,
)


async def cleanup_runs(run_ids: Sequence[uuid.UUID]) -> None:
    """Delete runs with a fresh connection; FK CASCADE removes events + outbox."""
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        if run_ids:
            await connection.execute(
                "DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", list(run_ids)
            )
    finally:
        await connection.close()


async def _append_commit(run_id: uuid.UUID, marker: int) -> int:
    async with async_session_factory() as session:
        seq = await append_event(session, run_id, "retrieved", {"marker": marker})
        await session.commit()
        return seq


async def test_append_event_assigns_continuous_unique_seq() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        async with async_session_factory() as session:
            seqs = [await append_event(session, run_id, "retrieved", {"n": n}) for n in range(5)]
            await session.commit()

        assert seqs == [1, 2, 3, 4, 5]

        async with async_session_factory() as session:
            events = list(
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )

        assert [event.seq for event in events] == [1, 2, 3, 4, 5]
        assert {event.event_type for event in events} == {"retrieved"}
        assert len(outbox) == 5
    finally:
        await cleanup_runs([run_id])


async def test_concurrent_append_assigns_continuous_unique_seq() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        seqs = await asyncio.gather(*[_append_commit(run_id, n) for n in range(20)])

        assert sorted(seqs) == list(range(1, 21))
        assert len(set(seqs)) == 20
    finally:
        await cleanup_runs([run_id])


async def test_append_event_requires_existing_run() -> None:
    missing = uuid.uuid4()

    async with async_session_factory() as session:
        with pytest.raises(RunNotFoundError):
            await append_event(session, missing, "retrieved", {})
        await session.rollback()


class _FailOutboxTrigger:
    """Install a trigger that fails ``event_outbox`` inserts for a marker run."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.suffix = uuid.uuid4().hex[:8]

    async def install(self) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE FUNCTION _fail_outbox_{self.suffix}() RETURNS trigger AS $$"
                    "BEGIN RAISE EXCEPTION 'forced outbox insert failure'; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            # asyncpg rejects bound parameters in DDL, so the marker run_id is
            # interpolated as a literal (a test-owned UUID, safe to inline).
            await conn.execute(
                text(
                    f"CREATE TRIGGER _fail_outbox_trg_{self.suffix} BEFORE INSERT ON event_outbox "
                    f"FOR EACH ROW WHEN (NEW.run_id = '{self.run_id}') "
                    f"EXECUTE FUNCTION _fail_outbox_{self.suffix}()"
                )
            )

    async def drop(self) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"DROP TRIGGER IF EXISTS _fail_outbox_trg_{self.suffix} ON event_outbox")
            )
            await conn.execute(text(f"DROP FUNCTION IF EXISTS _fail_outbox_{self.suffix}()"))


async def test_outbox_insert_failure_rolls_back_events_and_seq() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    trigger = _FailOutboxTrigger(run_id)
    await trigger.install()
    try:
        async with async_session_factory() as session:
            await append_event(session, run_id, "retrieved", {"n": 1})
            with pytest.raises(SQLAlchemyError):
                await session.commit()

        async with async_session_factory() as session:
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )
            run = await session.get(Run, run_id)

        assert events == []
        assert outbox == []
        assert run is not None
        assert run.next_seq == 0
    finally:
        await trigger.drop()
        await cleanup_runs([run_id])


async def test_append_event_sanitizes_persisted_payload() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        payload = {
            "headers": {"Authorization": "Bearer secret-token", "X-Api-Key": "sk-123"},
            "contact": "email user@example.com phone 13812345678",
            "nested": {"password": "hunter2", "items": [{"token": "t-1"}]},
            "long": "z" * (MAX_STRING_LENGTH + 100),
        }
        async with async_session_factory() as session:
            await append_event(session, run_id, "retrieved", payload)
            await session.commit()

        async with async_session_factory() as session:
            event = await session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))

        assert event is not None
        persisted = event.payload
        assert persisted["headers"]["Authorization"] == REDACTED
        assert persisted["headers"]["X-Api-Key"] == REDACTED
        assert "user@example.com" not in persisted["contact"]
        assert "13812345678" not in persisted["contact"]
        assert persisted["nested"]["password"] == REDACTED
        assert persisted["nested"]["items"][0]["token"] == REDACTED
        assert len(persisted["long"]) == MAX_STRING_LENGTH
    finally:
        await cleanup_runs([run_id])


async def test_append_event_rejects_oversized_payload() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        # Many strings each under MAX_STRING_LENGTH so truncation cannot shrink
        # them; the combined serialized size exceeds MAX_PAYLOAD_BYTES.
        payload = {"items": ["a" * 500 for _ in range(MAX_PAYLOAD_BYTES // 250 + 1)]}
        async with async_session_factory() as session:
            with pytest.raises(PayloadTooLargeError):
                await append_event(session, run_id, "retrieved", payload)
            await session.rollback()

        async with async_session_factory() as session:
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )
            run = await session.get(Run, run_id)

        assert events == []
        assert outbox == []
        assert run is not None
        assert run.next_seq == 0
    finally:
        await cleanup_runs([run_id])


async def test_append_event_rejects_circular_payload() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        payload: dict[str, object] = {"name": "root"}
        payload["self"] = payload
        async with async_session_factory() as session:
            with pytest.raises(PayloadInvalidError):
                await append_event(session, run_id, "retrieved", payload)
            await session.rollback()

        async with async_session_factory() as session:
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )
            run = await session.get(Run, run_id)

        assert events == []
        assert outbox == []
        assert run is not None
        assert run.next_seq == 0
    finally:
        await cleanup_runs([run_id])


async def test_append_event_rejects_surrogate_in_key() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        payload = {"bad\ud800key": "value"}
        async with async_session_factory() as session:
            with pytest.raises(PayloadInvalidError):
                await append_event(session, run_id, "retrieved", payload)
            await session.rollback()

        async with async_session_factory() as session:
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )
            run = await session.get(Run, run_id)

        assert events == []
        assert outbox == []
        assert run is not None
        assert run.next_seq == 0
    finally:
        await cleanup_runs([run_id])


async def test_append_event_preserves_token_usage_metadata() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        payload = {
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
            "auth": {"access_token": "at-1", "refresh_token": "rt-1"},
        }
        async with async_session_factory() as session:
            await append_event(session, run_id, "retrieved", payload)
            await session.commit()

        async with async_session_factory() as session:
            event = await session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))

        assert event is not None
        persisted = event.payload
        assert persisted["usage"]["prompt_tokens"] == 12
        assert persisted["usage"]["completion_tokens"] == 34
        assert persisted["usage"]["total_tokens"] == 46
        assert persisted["auth"]["access_token"] == REDACTED
        assert persisted["auth"]["refresh_token"] == REDACTED
    finally:
        await cleanup_runs([run_id])


async def test_append_event_rejects_non_serializable_payload() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    try:
        payload = {"score": float("nan"), "metrics": {"ratio": float("inf")}}
        async with async_session_factory() as session:
            with pytest.raises(PayloadInvalidError):
                await append_event(session, run_id, "retrieved", payload)
            await session.rollback()

        async with async_session_factory() as session:
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            outbox = list(
                await session.scalars(select(EventOutbox).where(EventOutbox.run_id == run_id))
            )
            run = await session.get(Run, run_id)

        assert events == []
        assert outbox == []
        assert run is not None
        assert run.next_seq == 0
    finally:
        await cleanup_runs([run_id])


async def test_invalid_sse_event_types_fail_before_consuming_sequence() -> None:
    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Run(id=run_id, status=RunStatus.QUEUED))
        await session.commit()
    invalid = ["", "bad\revent", "bad\nevent", "bad\r\nevent", "x" * 65, "bad\u0085event"]
    try:
        async with async_session_factory() as session:
            for event_type in invalid:
                with pytest.raises(ValueError):
                    await append_event(session, run_id, event_type, {"line": "a\nb"})
            seq = await append_event(session, run_id, "valid.event-type_1", {"line": "a\nb"})
            await session.commit()
        assert seq == 1
        connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
        try:
            assert (
                await connection.fetchval("SELECT next_seq FROM agent_runs WHERE id = $1", run_id)
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM run_events WHERE run_id = $1", run_id
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM event_outbox WHERE run_id = $1", run_id
                )
                == 1
            )
            payload = await connection.fetchval(
                "SELECT payload::text FROM run_events WHERE run_id = $1", run_id
            )
            assert "\\n" in payload
        finally:
            await connection.close()
    finally:
        await cleanup_runs([run_id])
