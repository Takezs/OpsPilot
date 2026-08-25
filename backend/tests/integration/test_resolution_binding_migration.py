"""0013 resolution-binding immutability migration roundtrip.

Task 9 review (P2). Verifies that the new migration is reversible and that after
a downgrade/re-upgrade cycle the replacement binding is preserved, the foreign
key is ``ON DELETE RESTRICT`` (not ``SET NULL``), and the immutability trigger is
back in place and enforced.
"""

import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config

from alembic import command
from opspilot.config import Settings
from opspilot.db import engine


def alembic_config() -> Config:
    return Config(str(Path(__file__).parents[2] / "alembic.ini"))


async def seed_bound_resolution() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert an original + replacement operation and a bound resolution."""
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        run_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO agent_runs (id, status) VALUES ($1, 'QUEUED')", run_id
        )
        original_id, replacement_id = uuid.uuid4(), uuid.uuid4()
        await connection.execute(
            "INSERT INTO tool_operations "
            "(id, run_id, tool_name, normalized_arguments, arguments_hash, "
            " idempotency_key, status, version, policy_decision, created_at, updated_at) "
            "VALUES "
            "($1, $2, 'refund_order', '{}'::jsonb, $3, 'refund:A100', 'MANUAL_REVIEW', "
            " 1, 'ALLOW', now(), now()), "
            "($4, $2, 'refund_order', '{}'::jsonb, $3, 'refund:A100', 'READY', "
            " 1, 'ALLOW', now(), now())",
            original_id,
            run_id,
            "0" * 64,
            replacement_id,
        )
        resolution_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO manual_review_resolutions "
            "(id, operation_id, outcome, resolved_by, replacement_operation_id, created_at) "
            "VALUES ($1, $2, 'RETRY_NEW_OPERATION', 'admin', $3, now())",
            resolution_id,
            original_id,
            replacement_id,
        )
        return original_id, replacement_id, resolution_id
    finally:
        await connection.close()


async def cleanup(original_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        await connection.execute(
            "DELETE FROM manual_review_resolutions "
            "WHERE operation_id = $1 OR replacement_operation_id = $1",
            original_id,
        )
        await connection.execute(
            "DELETE FROM agent_runs WHERE id = (SELECT run_id FROM tool_operations WHERE id = $1)",
            original_id,
        )
    finally:
        await connection.close()


async def assert_binding_preserved_and_enforced(
    original_id: uuid.UUID, replacement_id: uuid.UUID, resolution_id: uuid.UUID
) -> None:
    connection = await asyncpg.connect(Settings().database_url.replace("+asyncpg", ""))
    try:
        # the foreign key is RESTRICT now, not SET NULL
        ondelete = await connection.fetchval(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conname = 'manual_review_resolutions_replacement_operation_id_fkey'"
        )
        # confdeltype is a "char" column; asyncpg returns it as bytes
        assert ondelete.decode() == "r"
        # the immutability trigger is back after the roundtrip
        has_trigger = await connection.fetchval(
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgname = 'guard_resolution_binding' AND NOT tgisinternal"
        )
        assert has_trigger == 1
        # the binding survived the downgrade/re-upgrade cycle
        bound = await connection.fetchval(
            "SELECT replacement_operation_id FROM manual_review_resolutions WHERE id = $1",
            resolution_id,
        )
        assert bound == replacement_id
        # and it is enforced after the roundtrip
        try:
            await connection.execute(
                "UPDATE manual_review_resolutions SET replacement_operation_id = NULL "
                "WHERE id = $1",
                resolution_id,
            )
            raise AssertionError("binding was cleared after the roundtrip")
        except Exception as error:
            assert "immutable" in str(error)
        # deleting the bound replacement is refused by the RESTRICT foreign key
        try:
            await connection.execute("DELETE FROM tool_operations WHERE id = $1", replacement_id)
            raise AssertionError("bound replacement was deleted after the roundtrip")
        except Exception as error:
            assert "foreign key" in str(error)
    finally:
        await connection.close()


@pytest.mark.integration
def test_0013_resolution_binding_roundtrip() -> None:
    config = alembic_config()
    original_id, replacement_id, resolution_id = asyncio.run(seed_bound_resolution())
    try:
        # at head (0013); downgrade only the new migration and re-upgrade it
        command.downgrade(config, "0012_resolution_consumption")
        command.upgrade(config, "0013_resolution_binding")
        asyncio.run(
            assert_binding_preserved_and_enforced(original_id, replacement_id, resolution_id)
        )
    finally:
        command.upgrade(config, "head")
        # fresh connections in case the pool cached metadata from the cycle
        asyncio.run(engine.dispose())
        asyncio.run(cleanup(original_id))
