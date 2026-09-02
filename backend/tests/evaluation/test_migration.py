from __future__ import annotations

import asyncio
import json
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
from opspilot.evaluation.models import EvaluationCaseRecord, EvaluationRun
from tests.migration_database import MIGRATION_DATABASE_PREFIX, assert_safe_migration_database


def _config() -> Config:
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


@contextmanager
def _migration_database() -> Iterator[str]:
    database_name = f"{MIGRATION_DATABASE_PREFIX}{uuid.uuid4().hex}"
    sqlalchemy_url = Settings().database_url
    asyncpg_url = sqlalchemy_url.replace("+asyncpg", "")
    scratch_url = sqlalchemy_url.rsplit("/", 1)[0] + f"/{database_name}"
    assert_safe_migration_database(scratch_url)
    admin_url = asyncpg_url.rsplit("/", 1)[0] + "/postgres"

    async def create() -> None:
        connection = await asyncpg.connect(admin_url)
        try:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await connection.close()

    async def drop() -> None:
        connection = await asyncpg.connect(admin_url)
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        finally:
            await connection.close()

    asyncio.run(create())
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scratch_url
    try:
        yield os.environ["DATABASE_URL"].replace("+asyncpg", "")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(drop())


async def _table_names(database_url: str) -> set[str]:
    connection = await asyncpg.connect(database_url)
    try:
        rows = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row["tablename"] for row in rows}
    finally:
        await connection.close()


async def _column_names(database_url: str, table: str) -> set[str]:
    connection = await asyncpg.connect(database_url)
    try:
        rows = await connection.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
        return {row["column_name"] for row in rows}
    finally:
        await connection.close()


async def _seed_0022_execution(database_url: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, run_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(
            "INSERT INTO users (id,username,password_hash,role) VALUES ($1,$2,'unused','ADMIN')",
            user_id,
            f"migration-{user_id}",
        )
        await connection.execute(
            "INSERT INTO evaluation_runs "
            "(id,dataset_version,dataset_sha256,status,model,embedding_model,reranker_model,"
            "top_k,prompt_version,random_parameters,configuration) "
            "VALUES ($1,'legacy',$2,'PENDING','m','e','r',5,'p','{}'::jsonb,'{}'::jsonb)",
            run_id,
            "a" * 64,
        )
        await connection.execute(
            "INSERT INTO evaluation_test_executions "
            "(id,evaluation_run_id,dataset_version,dataset_sha,configuration,configuration_sha,"
            "status,frozen_by_user_id) "
            "VALUES ($1,$2,'legacy',$3,'{}'::jsonb,$4,'FROZEN',$5)",
            execution_id,
            run_id,
            "b" * 64,
            "c" * 64,
            user_id,
        )
        return user_id, run_id, execution_id
    finally:
        await connection.close()


def test_evaluation_models_use_reserved_tables() -> None:
    assert EvaluationRun.__tablename__ == "evaluation_runs"
    assert EvaluationCaseRecord.__tablename__ == "evaluation_cases"


@pytest.mark.integration
def test_0021_full_chain_constraints_and_round_trip() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "head")
        assert {"evaluation_runs", "evaluation_cases"} <= asyncio.run(_table_names(database_url))


@pytest.mark.integration
def test_0022_published_schema_upgrades_dataset_identity_only_in_0023() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0022_evaluation_execution_audit")
        assert "dataset_identity" not in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )
        command.upgrade(_config(), "head")
        assert "dataset_identity" in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )
        command.downgrade(_config(), "0022_evaluation_execution_audit")
        assert "dataset_identity" not in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )
        command.upgrade(_config(), "head")
        assert "dataset_identity" in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )

        async def verify_constraints() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                run_id = uuid.uuid4()
                await connection.execute(
                    "INSERT INTO evaluation_runs "
                    "(id,dataset_version,dataset_sha256,status,model,embedding_model,"
                    "reranker_model,top_k,prompt_version,random_parameters,configuration) "
                    "VALUES ($1,'v1',$2,'PENDING','deepseek','bge-m3','bge-reranker',5,'p1',"
                    "'{}'::jsonb,'{}'::jsonb)",
                    run_id,
                    "a" * 64,
                )
                await connection.execute(
                    "INSERT INTO evaluation_cases "
                    "(id,evaluation_run_id,dataset_case_id,actual_output,"
                    "deterministic_scores,latency_ms) "
                    "VALUES ($1,$2,'case-1','{}'::jsonb,'{}'::jsonb,10)",
                    uuid.uuid4(),
                    run_id,
                )
                with pytest.raises(asyncpg.UniqueViolationError):
                    await connection.execute(
                        "INSERT INTO evaluation_cases "
                        "(id,evaluation_run_id,dataset_case_id,actual_output,"
                        "deterministic_scores,latency_ms) "
                        "VALUES ($1,$2,'case-1','{}'::jsonb,'{}'::jsonb,10)",
                        uuid.uuid4(),
                        run_id,
                    )
                with pytest.raises(asyncpg.CheckViolationError):
                    await connection.execute(
                        "INSERT INTO evaluation_runs "
                        "(id,dataset_version,dataset_sha256,status,model,embedding_model,"
                        "reranker_model,top_k,prompt_version,random_parameters,configuration) "
                        "VALUES ($1,'v1',$2,'PENDING','m','e','r',0,'p',"
                        "'{}'::jsonb,'{}'::jsonb)",
                        uuid.uuid4(),
                        "b" * 64,
                    )
                with pytest.raises(asyncpg.CheckViolationError):
                    await connection.execute(
                        "INSERT INTO evaluation_runs "
                        "(id,dataset_version,dataset_sha256,status,model,embedding_model,"
                        "reranker_model,top_k,prompt_version,random_parameters,configuration) "
                        "VALUES ($1,'v1',$2,'RUNNING','m','e','r',5,'p',"
                        "'{}'::jsonb,'{}'::jsonb)",
                        uuid.uuid4(),
                        "c" * 64,
                    )
                with pytest.raises(asyncpg.CheckViolationError):
                    await connection.execute(
                        "INSERT INTO evaluation_cases "
                        "(id,evaluation_run_id,dataset_case_id,actual_output,"
                        "deterministic_scores,latency_ms) "
                        "VALUES ($1,$2,'case-negative','{}'::jsonb,'{}'::jsonb,-1)",
                        uuid.uuid4(),
                        run_id,
                    )
            finally:
                await connection.close()

        asyncio.run(verify_constraints())
        command.downgrade(_config(), "0020_run_job_lifecycle")
        assert "evaluation_runs" not in asyncio.run(_table_names(database_url))
        command.upgrade(_config(), "head")
        assert {"evaluation_runs", "evaluation_cases"} <= asyncio.run(_table_names(database_url))


@pytest.mark.integration
def test_0023_backfills_real_0022_rows_without_data_loss() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0022_evaluation_execution_audit")
        _, _, execution_id = asyncio.run(_seed_0022_execution(database_url))
        command.upgrade(_config(), "head")

        async def verify() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                row = await connection.fetchrow(
                    "SELECT dataset_version,dataset_sha,configuration_sha,dataset_identity "
                    "FROM evaluation_test_executions WHERE id=$1",
                    execution_id,
                )
                assert row is not None
                assert row["dataset_version"] == "legacy"
                assert row["dataset_sha"] == "b" * 64
                assert row["configuration_sha"] == "c" * 64
                assert json.loads(row["dataset_identity"]) == {
                    "legacy_unverified": True,
                    "schema_version": "legacy-unverified",
                    "test_sha256": "b" * 64,
                }
            finally:
                await connection.close()

        asyncio.run(verify())


@pytest.mark.integration
def test_0023_backfill_failure_is_atomic() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "0022_evaluation_execution_audit")
        _, _, execution_id = asyncio.run(_seed_0022_execution(database_url))

        async def install_failure() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                await connection.execute(
                    "CREATE FUNCTION reject_identity_backfill() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'injected backfill failure'; END $$"
                )
                await connection.execute(
                    "CREATE TRIGGER reject_identity_backfill BEFORE UPDATE "
                    "ON evaluation_test_executions FOR EACH ROW "
                    "EXECUTE FUNCTION reject_identity_backfill()"
                )
            finally:
                await connection.close()

        asyncio.run(install_failure())
        with pytest.raises(Exception, match="injected backfill failure"):
            command.upgrade(_config(), "head")
        assert "dataset_identity" not in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )

        async def verify_and_remove_failure() -> None:
            connection = await asyncpg.connect(database_url)
            try:
                assert (
                    await connection.fetchval(
                        "SELECT dataset_sha FROM evaluation_test_executions WHERE id=$1",
                        execution_id,
                    )
                    == "b" * 64
                )
                await connection.execute(
                    "DROP TRIGGER reject_identity_backfill ON evaluation_test_executions"
                )
                await connection.execute("DROP FUNCTION reject_identity_backfill()")
            finally:
                await connection.close()

        asyncio.run(verify_and_remove_failure())
        command.upgrade(_config(), "head")
        assert "dataset_identity" in asyncio.run(
            _column_names(database_url, "evaluation_test_executions")
        )
