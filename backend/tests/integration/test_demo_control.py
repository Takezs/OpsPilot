import asyncio
import hashlib
import hmac
import logging
import secrets

import pytest

from opspilot.observability.redaction import SafeLogFilter, safe_attributes
from tests.integration.test_demo_services import _client_for, _load_app

ORDER = "E2E-" + "a" * 32
OTHER = "E2E-" + "b" * 32
HEADER = "X-OpsPilot-Control-Token"


@pytest.mark.parametrize("mode", ["true", "false"])
async def test_fault_control_denial_has_uniform_body(monkeypatch, mode):
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", mode)
    monkeypatch.delenv("OPSPILOT_E2E_CONTROL_FILE", raising=False)
    async with _client_for(_load_app("payment_service")) as client:
        response = await client.post(
            "/refunds", json={"order_number": ORDER}, headers={"x-failure-mode": "success"}
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}


def proof(secret: bytes, order: str, action: str, method: str = "POST") -> str:
    message = f"opspilot-demo-control:v1\n{method}\n{action}\n{order}"
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def secret_file(monkeypatch, tmp_path):
    secret = secrets.token_bytes(32)
    path = tmp_path / "control-secret"
    path.write_bytes(secret)
    path.chmod(0o600)
    monkeypatch.setenv("OPSPILOT_E2E_CONTROL_FILE", str(path))
    return secret


@pytest.mark.parametrize(
    ("mode", "configured", "credential", "expected"),
    [
        (False, True, "correct", 404),
        (True, False, "correct", 404),
        (True, True, "missing", 404),
        (True, True, "wrong", 404),
        (True, True, "correct", 200),
    ],
)
async def test_control_requires_mode_secret_and_order_proof(
    monkeypatch, secret_file, mode, configured, credential, expected
) -> None:
    secret = secret_file
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true" if mode else "false")
    monkeypatch.delenv("OPSPILOT_EVAL_FAULT_MATRIX", raising=False)
    if not configured:
        monkeypatch.delenv("OPSPILOT_E2E_CONTROL_FILE")
    async with _client_for(_load_app("payment_service")) as client:
        for method, suffix in [("POST", "reset"), ("GET", "count")]:
            headers = (
                {}
                if credential == "missing"
                else {
                    HEADER: proof(secret, ORDER, suffix, method)
                    if credential == "correct"
                    else "wrong"
                }
            )
            response = await client.request(
                method, f"/__e2e/refunds/{ORDER}/{suffix}", headers=headers
            )
            assert response.status_code == expected
            if expected == 404:
                assert response.json() == {"detail": "not found"}


async def test_order_bound_control_and_case_insensitive_header(monkeypatch, secret_file) -> None:
    secret = secret_file
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true")
    monkeypatch.delenv("OPSPILOT_EVAL_FAULT_MATRIX", raising=False)
    async with _client_for(_load_app("payment_service")) as client:
        headers = {HEADER.lower(): proof(secret, ORDER, "reset")}
        assert (
            await client.post(f"/__e2e/refunds/{ORDER}/reset", headers=headers)
        ).status_code == 200
        for order in [OTHER, "ORD-002", "UNKNOWN", "EVAL-001"]:
            assert (
                await client.post(f"/__e2e/refunds/{order}/reset", headers=headers)
            ).status_code == 404
        assert (
            await client.post(
                "/__e2e/refunds/ORD-002/reset", headers={HEADER: proof(secret, "ORD-002", "reset")}
            )
        ).status_code == 404
        assert (
            await client.get(f"/__e2e/refunds/{OTHER}/count", headers=headers)
        ).status_code == 404
        assert (
            await client.post(
                f"/__e2e/refunds/{ORDER}/reset",
                headers={HEADER: proof(secret, ORDER, "count", "GET")},
            )
        ).status_code == 404
        assert (
            await client.post(
                "/refunds",
                json={"order_number": ORDER},
                headers=headers | {"x-failure-mode": "timeout_after_effect"},
            )
        ).status_code == 404


async def test_unauthed_fault_headers_are_not_a_control_bypass(monkeypatch) -> None:
    monkeypatch.delenv("OPSPILOT_DEMO_E2E", raising=False)
    monkeypatch.delenv("OPSPILOT_EVAL_FAULT_MATRIX", raising=False)
    async with _client_for(_load_app("payment_service")) as client:
        response = await client.post(
            "/refunds",
            json={"order_number": "A100"},
            headers={"x-failure-mode": "unknown_5xx_after_effect"},
        )
        assert response.status_code == 404
        assert (await client.get("/refunds/A100")).status_code == 404


async def test_concurrent_orders_have_independent_fault_and_refund_state(
    monkeypatch, secret_file
) -> None:
    secret = secret_file
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true")
    app = _load_app("payment_service")
    async with _client_for(app) as client:

        async def refund(order):
            headers = {HEADER: proof(secret, order, "reset")}
            assert (
                await client.post(f"/__e2e/refunds/{order}/reset", headers=headers)
            ).status_code == 200
            response = await client.post(
                "/refunds",
                json={"order_number": order},
                headers={
                    HEADER: proof(secret, order, "fault"),
                    "x-failure-mode": "timeout_after_effect",
                    "x-failure-delay": "0.01",
                },
            )
            assert response.status_code == 201
            assert (
                await client.get(
                    f"/__e2e/refunds/{order}/count",
                    headers={HEADER: proof(secret, order, "count", "GET")},
                )
            ).json() == {"count": 1}

        await asyncio.gather(refund(ORDER), refund(OTHER))
        await client.post(
            f"/__e2e/refunds/{ORDER}/reset", headers={HEADER: proof(secret, ORDER, "reset")}
        )
        assert (
            await client.get(
                f"/__e2e/refunds/{OTHER}/count",
                headers={HEADER: proof(secret, OTHER, "count", "GET")},
            )
        ).json() == {"count": 1}


@pytest.mark.parametrize("enabled", [False, True])
async def test_order_service_only_accepts_e2e_namespace_in_explicit_mode(monkeypatch, enabled):
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true" if enabled else "false")
    async with _client_for(_load_app("order_service")) as client:
        assert (await client.get(f"/orders/{ORDER}")).status_code == (200 if enabled else 404)


def test_secret_and_hmac_canaries_are_absent_from_logs_and_trace(secret_file):
    token = proof(secret_file, ORDER, "reset")
    fields = {"control_secret": secret_file.hex(), HEADER: token}
    record = logging.LogRecord(
        "demo.control", logging.INFO, __file__, 1, "headers=%s", (fields,), None
    )
    assert SafeLogFilter().filter(record)
    output = record.getMessage() + repr(safe_attributes(fields))
    raw_header = logging.LogRecord(
        "demo.control", logging.INFO, __file__, 1, HEADER + "=" + token, (), None
    )
    assert SafeLogFilter().filter(raw_header)
    output += raw_header.getMessage()
    assert not any(value in output for value in (secret_file.hex(), token))


@pytest.mark.parametrize("candidate", ["", "a" * 63, "b" * 65, "Z" * 64, "a" * 10000])
async def test_malformed_control_proof_is_rejected(monkeypatch, secret_file, candidate):
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true")
    async with _client_for(_load_app("payment_service")) as client:
        response = await client.post(f"/__e2e/refunds/{ORDER}/reset", headers={HEADER: candidate})
        assert response.status_code == 404


async def test_registered_e2e_orders_are_unavailable_when_mode_is_disabled(
    monkeypatch, secret_file
):
    monkeypatch.setenv("OPSPILOT_DEMO_E2E", "true")
    async with _client_for(_load_app("payment_service")) as client:
        await client.post(
            f"/__e2e/refunds/{ORDER}/reset", headers={HEADER: proof(secret_file, ORDER, "reset")}
        )
        monkeypatch.setenv("OPSPILOT_DEMO_E2E", "false")
        assert (await client.get(f"/refunds/{ORDER}/eligibility")).status_code == 404
        assert (await client.post("/refunds", json={"order_number": ORDER})).status_code == 404
