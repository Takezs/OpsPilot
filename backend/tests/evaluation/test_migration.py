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
from opspilot.evaluation.models import EvaluationCaseRecord, EvaluationRun


def _config() -> Config:
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


@contextmanager
def _migration_database() -> Iterator[str]:
    database_name = f"opspilot_0021_{uuid.uuid4().hex}"
    sqlalchemy_url = Settings().database_url
    asyncpg_url = sqlalchemy_url.replace("+asyncpg", "")
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
    os.environ["DATABASE_URL"] = sqlalchemy_url.rsplit("/", 1)[0] + f"/{database_name}"
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


def test_evaluation_models_use_reserved_tables() -> None:
    assert EvaluationRun.__tablename__ == "evaluation_runs"
    assert EvaluationCaseRecord.__tablename__ == "evaluation_cases"


@pytest.mark.integration
def test_0021_full_chain_constraints_and_round_trip() -> None:
    with _migration_database() as database_url:
        command.upgrade(_config(), "head")
        assert {"evaluation_runs", "evaluation_cases"} <= asyncio.run(_table_names(database_url))

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
