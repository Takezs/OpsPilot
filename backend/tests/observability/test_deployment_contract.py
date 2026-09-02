from pathlib import Path

from opspilot.knowledge.schemas import AccessLevel
from opspilot.release import RELEASE_ADMIN_ACCESS_LEVEL

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


def test_release_seed_admin_uses_a_valid_production_access_level() -> None:
    assert RELEASE_ADMIN_ACCESS_LEVEL is AccessLevel.CONFIDENTIAL
