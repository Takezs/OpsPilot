"""0020 run-job lifecycle dirty-history migration tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config

from alembic import command
from opspilot.config import Settings


def _config() -> Config:
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


@contextmanager
def _migration_database() -> Iterator[str]:
    database_name = f"opspilot_0020_{uuid.uuid4().hex}"
    sqlalchemy_url = Settings().database_url
    asyncpg_url = sqlalchemy_url.replace("+asyncpg", "")
    admin_url = asyncpg_url.rsplit("/", 1)[0] + "/postgres"

    async def _create() -> None:
        connection = await asyncpg.connect(admin_url)
        try:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await connection.close()

    async def _drop() -> None:
        connection = await asyncpg.connect(admin_url)
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        finally:
            await connection.close()

    asyncio.run(_create())
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sqlalchemy_url.rsplit("/", 1)[0] + f"/{database_name}"
    try:
        yield os.environ["DATABASE_URL"].replace("+asyncpg", "")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop())


async def _seed_dirty_jobs(database_url: str) -> dict[str, uuid.UUID]:
    connection = await asyncpg.connect(database_url)
    try:
        run_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO agent_runs (id, status) VALUES ($1, 'RUNNING')", run_id
        )
        message_ids = {
            name: uuid.uuid4() for name in ("reply", "delivered", "pending", "running", "completed")
        }
        for message_id in message_ids.values():
            await connection.execute(
                "INSERT INTO run_messages (id, run_id, role, content) "
                "VALUES ($1, $2, 'USER', 'migration input')",
                message_id,
                run_id,
            )
        await connection.execute(
            "INSERT INTO run_messages (id, run_id, role, content, in_reply_to_message_id) "
            "VALUES ($1, $2, 'ASSISTANT', 'done', $3)",
            uuid.uuid4(),
            run_id,
            message_ids["reply"],
        )
        rows = [
            (message_ids["reply"], "PENDING", None, None, None, None),
            (message_ids["delivered"], "PENDING", None, None, None, None),
            (message_ids["pending"], "PENDING", "dirty-token", "dirty-owner", "1 hour", None),
            (message_ids["running"], "RUNNING", None, None, None, None),
            (message_ids["completed"], "COMPLETED", "dirty-token", "dirty-owner", "1 hour", None),
        ]
        for message_id, status, token, owner, lease_delta, completed_at in rows:
            await connection.execute(
                "INSERT INTO run_job_outbox "
                "(id, message_id, available_at, delivered_at, status, claim_token, lease_owner, "
                "lease_expires_at, completed_at) VALUES "
                "($1, $2, clock_timestamp(), clock_timestamp(), $3, $4, $5, "
                "CASE WHEN $6::text IS NULL THEN NULL "
                "ELSE clock_timestamp() + $6::interval END, $7)",
                uuid.uuid4(),
                message_id,
                status,
                token,
                owner,
                lease_delta,
                completed_at,
            )
        return message_ids
    finally:
        await connection.close()


async def _fetch_jobs(database_url: str) -> dict[uuid.UUID, asyncpg.Record]:
    connection = await asyncpg.connect(database_url)
    try:
        rows = await connection.fetch(
            "SELECT message_id, status, delivered_at, claim_token, lease_owner, "
            "lease_expires_at, completed_at FROM run_job_outbox"
        )
        return {row["message_id"]: row for row in rows}
    finally:
        await connection.close()


@pytest.mark.integration
def test_0020_backfills_dirty_delivered_jobs_and_enforces_lifecycle() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0019_run_job_claims")
        message_ids = asyncio.run(_seed_dirty_jobs(database_url))

        command.upgrade(_config(), "0020_run_job_lifecycle")

        jobs = asyncio.run(_fetch_jobs(database_url))
        replied = jobs[message_ids["reply"]]
        assert replied["status"] == "COMPLETED"
        assert replied["completed_at"] is not None
        assert replied["claim_token"] is None
        assert replied["lease_owner"] is None
        assert replied["lease_expires_at"] is None

        for name in ("delivered", "pending", "running", "completed"):
            job = jobs[message_ids[name]]
            assert job["status"] == "PENDING"
            assert job["delivered_at"] is None
            assert job["claim_token"] is None
            assert job["lease_owner"] is None
            assert job["lease_expires_at"] is None
            assert job["completed_at"] is None

        async def _assert_constraint() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                job_id = await connection.fetchval("SELECT id FROM run_job_outbox LIMIT 1")
                invalid_updates = (
                    "status = 'PENDING', claim_token = 'x'",
                    "status = 'RUNNING', claim_token = NULL, "
                    "lease_owner = NULL, lease_expires_at = NULL",
                    "status = 'COMPLETED', completed_at = NULL",
                )
                for update in invalid_updates:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                f"UPDATE run_job_outbox SET {update} WHERE id = $1", job_id
                            )
            finally:
                await connection.close()

        asyncio.run(_assert_constraint())

        command.downgrade(_config(), "0019_run_job_claims")
        command.upgrade(_config(), "0020_run_job_lifecycle")


@pytest.mark.integration
def test_0020_backfill_and_constraint_are_atomic() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0019_run_job_claims")
        message_ids = asyncio.run(_seed_dirty_jobs(database_url))

        async def _inject_conflict() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                await connection.execute(
                    "ALTER TABLE run_job_outbox ADD CONSTRAINT ck_run_job_lifecycle "
                    "CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED'))"
                )
            finally:
                await connection.close()

        asyncio.run(_inject_conflict())
        with pytest.raises(Exception, match="ck_run_job_lifecycle"):
            command.upgrade(_config(), "0020_run_job_lifecycle")

        jobs = asyncio.run(_fetch_jobs(database_url))
        assert jobs[message_ids["reply"]]["status"] == "PENDING"
        assert jobs[message_ids["reply"]]["delivered_at"] is not None
        assert jobs[message_ids["pending"]]["claim_token"] == "dirty-token"

        async def _assert_revision() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                    "0019_run_job_claims"
                )
            finally:
                await connection.close()

        asyncio.run(_assert_revision())
