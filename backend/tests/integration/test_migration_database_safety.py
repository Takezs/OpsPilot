import pytest

from tests.migration_database import assert_safe_migration_database


def test_destructive_migration_target_rejects_main_database_before_ddl() -> None:
    with pytest.raises(RuntimeError, match="unique scratch database"):
        assert_safe_migration_database(
            "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"
        )
    with pytest.raises(RuntimeError, match="unique scratch database"):
        assert_safe_migration_database(
            "postgresql+asyncpg://opspilot:opspilot@localhost:5432/random_database"
        )
