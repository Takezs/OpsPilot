import json

import httpx
import pytest

from opspilot.config import Settings


def test_runtime_credentials_load_from_file_before_production_validation(tmp_path):
    path = tmp_path / "runtime"
    path.write_text(
        json.dumps(
            {
                "jwt_secret": "fixture-jwt-" + "x" * 40,
                "database_url": "postgresql+asyncpg://fixture:fixture@postgres/fixture",
                "deepseek_api_key": "fixture-provider",
                "bge_api_key": "fixture-bge",
            }
        )
    )
    settings = Settings(
        _env_file=None, environment="production", jwt_secret="", runtime_secrets_file=str(path)
    )
    assert settings.jwt_secret == "fixture-jwt-" + "x" * 40
    assert settings.deepseek_api_key == "fixture-provider"


@pytest.mark.parametrize("actual_role", ["ADMIN", "REVIEWER"])
async def test_user_credentials_cannot_silently_use_privileged_principal(tmp_path, actual_role):
    from opspilot.evaluation.credentials import authenticate_file

    path = tmp_path / "identity"
    path.write_text(json.dumps({"username": "fixture", "password": "fixture-secret"}))

    def response(request):
        if request.url.path.endswith("login"):
            return httpx.Response(200, json={"access_token": "fixture-token"})
        return httpx.Response(200, json={"role": actual_role, "user_id": "fixture"})

    async with httpx.AsyncClient(
        base_url="http://api:8000/api/v1", transport=httpx.MockTransport(response)
    ) as client:
        with pytest.raises(ValueError, match="principal"):
            await authenticate_file(client, path, expected_role="USER")


async def test_authenticated_file_never_renders_password_in_error(tmp_path):
    from opspilot.evaluation.credentials import authenticate_file

    path = tmp_path / "identity"
    path.write_text(json.dumps({"username": "fixture", "password": "credential-canary"}))
    async with httpx.AsyncClient(
        base_url="http://api:8000/api/v1",
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="credential-canary")),
    ) as client:
        with pytest.raises(ValueError) as error:
            await authenticate_file(client, path, expected_role="USER")
        assert "credential-canary" not in str(error.value)
