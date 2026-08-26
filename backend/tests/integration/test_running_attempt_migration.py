"""0016 dirty-data backfill and transactional migration tests."""

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
    database_name = f"opspilot_0016_{uuid.uuid4().hex}"
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


async def _seed_dirty_attempts(database_url: str) -> dict[str, uuid.UUID]:
    connection = await asyncpg.connect(database_url)
    try:
        run_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO agent_runs (id, status) VALUES ($1, 'QUEUED')", run_id
        )
        operation_ids = {
            status: uuid.uuid4()
            for status in (
                "EXECUTING",
                "RETRYING",
                "OUTCOME_UNKNOWN",
                "RECONCILING",
                "SUCCEEDED",
                "FAILED",
                "MANUAL_REVIEW",
            )
        }
        for status, operation_id in operation_ids.items():
            await connection.execute(
                "INSERT INTO tool_operations "
                "(id, run_id, tool_name, normalized_arguments, arguments_hash, idempotency_key, "
                "status, version, policy_decision, created_at, updated_at) VALUES "
                "($1, $2, 'refund_order', '{}'::jsonb, $3, $4, "
                "$5::operation_status, 3, 'ALLOW', now(), now())",
                operation_id,
                run_id,
                "a" * 64,
                f"refund:{status.lower()}",
                status,
            )
            await connection.executemany(
                "INSERT INTO operation_attempts "
                "(id, operation_id, attempt_number, kind, status, request_payload) "
                "VALUES ($1, $2, $3, 'EXECUTION', 'RUNNING', '{}'::jsonb)",
                [
                    (uuid.uuid4(), operation_id, 1),
                    (uuid.uuid4(), operation_id, 2),
                ],
            )
            await connection.execute(
                "INSERT INTO operation_attempts "
                "(id, operation_id, attempt_number, kind, status, request_payload, completed_at) "
                "VALUES ($1, $2, 3, 'RECONCILIATION', 'SUCCEEDED', '{}'::jsonb, now())",
                uuid.uuid4(),
                operation_id,
            )
        return operation_ids
    finally:
        await connection.close()


async def _fetch_attempts(database_url: str, operation_id: uuid.UUID) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(database_url)
    try:
        return await connection.fetch(
            "SELECT attempt_number, kind, status, completed_at, error "
            "FROM operation_attempts WHERE operation_id = $1 ORDER BY attempt_number",
            operation_id,
        )
    finally:
        await connection.close()


@pytest.mark.integration
def test_0016_backfills_duplicate_running_execution_attempts_before_unique_index() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0015_operation_attempts")
        operation_ids = asyncio.run(_seed_dirty_attempts(database_url))

        command.upgrade(_config(), "0016_running_execution_attempt")

        for operation_status, operation_id in operation_ids.items():
            attempts = asyncio.run(_fetch_attempts(database_url, operation_id))
            execution = attempts[:2]
            assert attempts[2]["status"] == "SUCCEEDED"
            if operation_status == "EXECUTING":
                assert [row["status"] for row in execution] == ["ABANDONED", "RUNNING"]
                assert execution[0]["completed_at"] is not None
                assert execution[1]["completed_at"] is None
            elif operation_status in {"OUTCOME_UNKNOWN", "RECONCILING"}:
                assert [row["status"] for row in execution] == [
                    "OUTCOME_UNKNOWN",
                    "OUTCOME_UNKNOWN",
                ]
            else:
                assert [row["status"] for row in execution] == ["ABANDONED", "ABANDONED"]
            for row in execution:
                if row["status"] != "RUNNING":
                    assert row["completed_at"] is not None
                    assert row["error"] == "closed by 0016 running execution attempt backfill"

        executing_id = operation_ids["EXECUTING"]

        async def _assert_index() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                with pytest.raises(asyncpg.UniqueViolationError):
                    await connection.execute(
                        "INSERT INTO operation_attempts "
                        "(id, operation_id, attempt_number, kind, status, request_payload) "
                        "VALUES ($1, $2, 4, 'EXECUTION', 'RUNNING', '{}'::jsonb)",
                        uuid.uuid4(),
                        executing_id,
                    )
            finally:
                await connection.close()

        asyncio.run(_assert_index())

        command.downgrade(_config(), "0015_operation_attempts")
        command.upgrade(_config(), "0016_running_execution_attempt")
        for operation_status, operation_id in operation_ids.items():
            attempts = asyncio.run(_fetch_attempts(database_url, operation_id))
            if operation_status == "EXECUTING":
                assert [row["status"] for row in attempts[:2]] == ["ABANDONED", "RUNNING"]
            elif operation_status in {"OUTCOME_UNKNOWN", "RECONCILING"}:
                assert [row["status"] for row in attempts[:2]] == [
                    "OUTCOME_UNKNOWN",
                    "OUTCOME_UNKNOWN",
                ]
            else:
                assert [row["status"] for row in attempts[:2]] == [
                    "ABANDONED",
                    "ABANDONED",
                ]
        asyncio.run(_assert_index())


@pytest.mark.integration
def test_0016_backfill_and_index_creation_are_atomic() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0015_operation_attempts")
        operation_ids = asyncio.run(_seed_dirty_attempts(database_url))

        async def _inject_conflicting_index() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                await connection.execute(
                    "CREATE INDEX uq_operation_attempt_running_execution "
                    "ON operation_attempts (attempt_number)"
                )
            finally:
                await connection.close()

        asyncio.run(_inject_conflicting_index())
        with pytest.raises(Exception, match="uq_operation_attempt_running_execution"):
            command.upgrade(_config(), "0016_running_execution_attempt")

        for operation_id in operation_ids.values():
            attempts = asyncio.run(_fetch_attempts(database_url, operation_id))
            assert [row["status"] for row in attempts[:2]] == ["RUNNING", "RUNNING"]
            assert all(row["completed_at"] is None and row["error"] is None for row in attempts[:2])

        async def _assert_migration_rolled_back() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                    "0015_operation_attempts"
                )
                index_definition = await connection.fetchval(
                    "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indexrelid = 'uq_operation_attempt_running_execution'::regclass"
                )
                assert "attempt_number" in index_definition
                assert "UNIQUE" not in index_definition
            finally:
                await connection.close()

        asyncio.run(_assert_migration_rolled_back())
