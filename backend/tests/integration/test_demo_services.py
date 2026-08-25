"""Demo service failure modes, server-side idempotency and HTTP tool adapters.

The payment service distinguishes ``timeout_before_effect`` (no refund was
applied) from ``timeout_after_effect`` and ``unknown_5xx_after_effect`` (the
refund WAS applied but the caller could not confirm it) so the task 9/10
executor can retry only when safe. Repeated refunds of the same order share one
refund id and provider reference (server-side business idempotency key).
"""

import importlib.util
from pathlib import Path

import httpx

from opspilot.tools.adapters.python import (
    get_order_adapter,
    get_refund_status_adapter,
    refund_order_adapter,
    send_email_adapter,
)
from opspilot.tools.schemas import (
    GetOrderArgs,
    GetRefundStatusArgs,
    RefundOrderArgs,
    SendEmailArgs,
)

DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo-services"
# The test client timeout stays above the failure delay so the delayed response
# completes and we can assert what the provider actually did. The real caller
# uses a shorter timeout and observes a TimeoutException instead.
FAILURE_DELAY = 0.3
CLIENT_TIMEOUT = 2.0


def _load_app(service: str):
    """Load a demo service app from its source file as a fresh module."""
    path = DEMO_ROOT / service / "app.py"
    spec = importlib.util.spec_from_file_location(f"demo_{service}_app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _client_for(app, timeout: float = CLIENT_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://demo",
        timeout=timeout,
    )


async def test_order_service_returns_order() -> None:
    app = _load_app("order_service")
    async with _client_for(app) as client:
        response = await client.get("/orders/A100")

    assert response.status_code == 200
    body = response.json()
    assert body["order_number"] == "A100"
    assert body["status"] == "OPEN"


async def test_order_service_unknown_order_404() -> None:
    app = _load_app("order_service")
    async with _client_for(app) as client:
        response = await client.get("/orders/UNKNOWN")

    assert response.status_code == 404


async def test_payment_success_is_idempotent() -> None:
    app = _load_app("payment_service")
    async with _client_for(app) as client:
        first = await client.post("/refunds", json={"order_number": "A100"})
        second = await client.post("/refunds", json={"order_number": "A100"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["refund_id"] == second.json()["refund_id"]
    assert first.json()["provider_reference"] == second.json()["provider_reference"]


async def test_payment_timeout_before_effect_creates_no_refund() -> None:
    app = _load_app("payment_service")
    async with _client_for(app) as client:
        headers = {
            "x-failure-mode": "timeout_before_effect",
            "x-failure-delay": str(FAILURE_DELAY),
        }
        response = await client.post("/refunds", json={"order_number": "A101"}, headers=headers)
        status = await client.get("/refunds/A101")

    # The delay outlives the caller's read timeout, but no refund is applied, so
    # a retry is safe (a status query finds no refund).
    assert response.json()["applied"] is False
    assert status.status_code == 404


async def test_payment_timeout_after_effect_creates_refund() -> None:
    app = _load_app("payment_service")
    async with _client_for(app) as client:
        headers = {
            "x-failure-mode": "timeout_after_effect",
            "x-failure-delay": str(FAILURE_DELAY),
        }
        response = await client.post("/refunds", json={"order_number": "A102"}, headers=headers)
        status = await client.get("/refunds/A102")

    # The refund WAS applied before the delay; reconciliation must treat the
    # operation as possibly-succeeded and query status instead of retrying.
    assert response.status_code == 201
    assert status.status_code == 200
    assert status.json()["status"] == "REFUNDED"


async def test_payment_unknown_5xx_after_effect_creates_refund() -> None:
    app = _load_app("payment_service")
    async with _client_for(app) as client:
        headers = {"x-failure-mode": "unknown_5xx_after_effect"}
        response = await client.post("/refunds", json={"order_number": "A103"}, headers=headers)
        status = await client.get("/refunds/A103")

    assert response.status_code == 500
    assert status.status_code == 200
    assert status.json()["status"] == "REFUNDED"


async def test_email_service_accepts_message() -> None:
    app = _load_app("email_service")
    async with _client_for(app) as client:
        response = await client.post(
            "/emails", json={"to": "user@example.com", "subject": "Refund", "body": "done"}
        )

    assert response.status_code == 202
    assert response.json()["message_id"]


async def test_http_adapters_map_results() -> None:
    order_app = _load_app("order_service")
    payment_app = _load_app("payment_service")
    email_app = _load_app("email_service")
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=order_app),
            base_url="http://order",
            timeout=CLIENT_TIMEOUT,
        ) as order_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=payment_app),
            base_url="http://payment",
            timeout=CLIENT_TIMEOUT,
        ) as payment_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=email_app),
            base_url="http://email",
            timeout=CLIENT_TIMEOUT,
        ) as email_client,
    ):
        order_result = await get_order_adapter(order_client)(GetOrderArgs(order_number="A100"))
        assert order_result.ok is True
        assert order_result.data["order_number"] == "A100"

        refund_result = await refund_order_adapter(payment_client)(
            RefundOrderArgs(order_number="A100", amount=250.0)
        )
        assert refund_result.ok is True
        assert refund_result.data["refund_id"]

        status_result = await get_refund_status_adapter(payment_client)(
            GetRefundStatusArgs(order_number="A100")
        )
        assert status_result.ok is True
        assert status_result.data["status"] == "REFUNDED"

        email_result = await send_email_adapter(email_client)(
            SendEmailArgs(to="user@example.com", subject="hi", body="body")
        )
        assert email_result.ok is True


async def test_refund_adapter_maps_transport_timeout_to_retryable_error() -> None:
    def _timed_out_handler(request):
        raise httpx.ReadTimeout("read timed out")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_timed_out_handler),
        base_url="http://payment",
        timeout=CLIENT_TIMEOUT,
    ) as client:
        result = await refund_order_adapter(client)(
            RefundOrderArgs(order_number="A100", amount=250.0)
        )

    assert result.ok is False
    assert "timeout" in (result.error or "")
