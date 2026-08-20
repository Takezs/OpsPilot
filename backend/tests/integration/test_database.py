import asyncpg
import pytest

from opspilot.config import Settings


@pytest.mark.integration
async def test_required_postgres_extensions_are_installed() -> None:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    try:
        rows = await connection.fetch("select extname from pg_extension")
    finally:
        await connection.close()

    extension_names = {row["extname"] for row in rows}
    assert {"vector", "pgcrypto"} <= extension_names
