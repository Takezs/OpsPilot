from pathlib import Path

from opspilot.auth.models import Role, User
from opspilot.knowledge.schemas import AccessLevel
from opspilot.outbox_publisher import OutboxPublisherSettings
from opspilot.release import RELEASE_ADMIN_ACCESS_LEVEL, validate_existing_admin
from opspilot.worker import WorkerSettings

ROOT = Path(__file__).parents[3]


def test_release_images_run_as_non_root_and_do_not_copy_env() -> None:
    backend = (ROOT / "deploy" / "Dockerfile.backend").read_text(encoding="utf-8")
    frontend = (ROOT / "deploy" / "Dockerfile.frontend").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "USER opspilot" in backend
    assert "nginx-unprivileged" in frontend
    assert ".env" in dockerignore
    assert "Docker Desktop Installer.exe" in dockerignore


def test_compose_contains_complete_health_checked_topology() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in (
        "api:",
        "web:",
        "worker:",
        "outbox-publisher:",
        "order:",
        "payment:",
        "email:",
        "postgres:",
        "redis:",
    ):
        assert service in compose
    assert "healthcheck:" in compose
    assert "read_only: true" in compose
    assert "opspilot_storage:/app/data/documents" in compose
    assert "/proc/1/cmdline" not in compose
    assert "arq, --check, opspilot.worker.WorkerSettings" in compose
    assert "arq, --check, opspilot.outbox_publisher.OutboxPublisherSettings" in compose


def test_worker_and_publisher_have_distinct_fresh_redis_heartbeats() -> None:
    assert WorkerSettings.health_check_key != OutboxPublisherSettings.health_check_key
    assert WorkerSettings.health_check_interval <= 10
    assert OutboxPublisherSettings.health_check_interval <= 10


def test_release_seed_admin_uses_a_valid_production_access_level() -> None:
    assert RELEASE_ADMIN_ACCESS_LEVEL is AccessLevel.CONFIDENTIAL


def test_release_seed_rejects_incompatible_existing_identity() -> None:
    base = dict(
        username="opspilot-admin",
        password_hash="unchanged",
        allowed_departments=["*"],
        max_access_level=int(RELEASE_ADMIN_ACCESS_LEVEL),
        is_active=True,
        role=Role.ADMIN,
    )
    validate_existing_admin(User(**base))
    for changes in (
        {"is_active": False},
        {"role": Role.USER},
        {"max_access_level": int(AccessLevel.INTERNAL)},
        {"allowed_departments": []},
    ):
        user = User(**(base | changes))
        try:
            validate_existing_admin(user)
        except RuntimeError:
            pass
        else:
            raise AssertionError("incompatible release identity must fail closed")
