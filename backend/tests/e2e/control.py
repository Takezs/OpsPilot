"""Cross-process lifetime guard used only by synthetic E2E harnesses."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import asyncpg
from sqlalchemy.engine import make_url

# Injected in memory by the Compose test launcher, never by application config.
COMPOSE_SECRET: bytes | None = None
LOCK_ID = 0x4F50534532454C4B


@asynccontextmanager
async def payment_control_lock(database_url: str, wait_seconds: float = 10) -> AsyncIterator[None]:
    # PostgreSQL advisory locks are database-scoped. Use the same management
    # database even when tests otherwise use different scratch databases.
    url = make_url(database_url).set(drivername="postgresql", database="postgres")
    if url.host == "localhost":
        url = url.set(host="127.0.0.1")
    connection = await asyncpg.connect(url.render_as_string(hide_password=False), timeout=5)
    acquired = False
    monitor = None
    owner = asyncio.current_task()
    deadline = time.monotonic() + wait_seconds

    async def watch_connection() -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
                await connection.fetchval("SELECT 1", timeout=2)
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            if owner is not None:
                owner.cancel()

    try:
        while not acquired:
            acquired = await connection.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID)
            if not acquired:
                if time.monotonic() >= deadline:
                    raise TimeoutError("demo control session is already active")
                await asyncio.sleep(0.05)
        monitor = asyncio.create_task(watch_connection())
        yield
    finally:
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        if acquired and not connection.is_closed():
            with suppress(asyncpg.PostgresError, OSError):
                await connection.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
        await connection.close(timeout=5)
