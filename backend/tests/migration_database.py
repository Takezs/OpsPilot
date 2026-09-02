from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import asyncpg

from opspilot.config import Settings

MIGRATION_DATABASE_PREFIX = "opspilot_migration_test_"


def assert_safe_migration_database(database_url: str) -> str:
    database_name = urlparse(database_url.replace("+asyncpg", "")).path.lstrip("/")
    if database_name == "opspilot" or not database_name.startswith(MIGRATION_DATABASE_PREFIX):
        raise RuntimeError("destructive migration tests require a unique scratch database")
    if len(database_name) <= len(MIGRATION_DATABASE_PREFIX):
        raise RuntimeError("migration scratch database requires a random suffix")
    return database_name


@contextmanager
def migration_scratch_database() -> Iterator[str]:
    application_url = Settings().database_url
    base_url = application_url.rsplit("/", 1)[0]
    database_name = f"{MIGRATION_DATABASE_PREFIX}{uuid.uuid4().hex}"
    scratch_url = f"{base_url}/{database_name}"
    assert_safe_migration_database(scratch_url)
    admin_url = application_url.replace("+asyncpg", "").rsplit("/", 1)[0] + "/postgres"

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
        yield scratch_url.replace("+asyncpg", "")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(drop())
