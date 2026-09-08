"""Exchange external credential files for short-lived, role-checked API tokens."""

import json
from pathlib import Path

import httpx


async def authenticate_file(
    client: httpx.AsyncClient, path: Path, *, expected_role: str
) -> dict[str, str]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("credential file too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"username", "password"}:
            raise ValueError("invalid credential file")
        if not all(isinstance(item, str) and item for item in value.values()):
            raise ValueError("invalid credential file")
        response = await client.post("/auth/login", json=value)
        if response.status_code != 200:
            raise ValueError("authentication rejected")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("authentication rejected")
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.get("/auth/me", headers=headers)
        if response.status_code != 200 or response.json().get("role") != expected_role:
            raise ValueError("evaluation principal role mismatch")
        return headers
    except (OSError, ValueError, TypeError, AttributeError, httpx.HTTPError):
        # Never propagate response bodies, credential values or request objects.
        raise ValueError("evaluation principal authentication failed") from None
