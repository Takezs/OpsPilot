"""Demo Payment Service with failure modes and server-side idempotency.

Failure modes (header ``x-failure-mode``, default ``success``):
- ``success``: refund applied and confirmed.
- ``timeout_before_effect``: delays, then responds without applying the refund,
  so a timed-out caller can safely retry (no refund exists).
- ``timeout_after_effect``: applies the refund, then delays, so the caller times
  out although the refund WAS applied; a later status query confirms it.
- ``unknown_5xx_after_effect``: applies the refund, then returns 500.

Refunds are keyed by the server-side business idempotency key ``refund:{order}``
and carry a stable ``refund_id`` plus ``provider_reference``, so repeated calls
never create a duplicate refund.
"""

import asyncio
import os
import re
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

app = FastAPI(title="Payment Service")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


DEFAULT_FAILURE_DELAY_SECONDS = 5.0

ORDER_AMOUNTS: dict[str, float] = {
    "A100": 250.0,
    "A101": 50.0,
    "A102": 350.0,
    "A103": 1200.0,
    "ORD-002": 350.0,
}
if os.getenv("OPSPILOT_EVAL_FAULT_MATRIX", "").lower() in {"1", "true"}:
    ORDER_AMOUNTS.update({f"EVAL-{index:03d}": 350.0 for index in range(1, 61)})

EVALUATION_ORDER_PATTERN = re.compile(r"EVAL-[0-9a-f]{8}-[0-9]{3}")


def _ensure_evaluation_order(order_number: str) -> bool:
    if os.getenv("OPSPILOT_EVAL_FAULT_MATRIX", "").lower() not in {"1", "true"}:
        return False
    if EVALUATION_ORDER_PATTERN.fullmatch(order_number) is None:
        return False
    ORDER_AMOUNTS.setdefault(order_number, 350.0)
    return True


# Server-side business idempotency key -> refund.
REFUNDS: dict[str, dict] = {}
_FAULTED_ORDERS: set[str] = set()


def _configured_mode(order_number: str) -> str:
    if os.getenv("OPSPILOT_DEMO_E2E", "").lower() not in {"1", "true"}:
        return "success"
    configured_order = os.getenv("OPSPILOT_DEMO_E2E_TIMEOUT_ORDER", "")
    if configured_order == order_number and order_number not in _FAULTED_ORDERS:
        _FAULTED_ORDERS.add(order_number)
        return "timeout_after_effect"
    return "success"


class RefundRequest(BaseModel):
    order_number: str


def _refund_key(order_number: str) -> str:
    return f"refund:{order_number}"


def _create_refund(order_number: str) -> tuple[dict, bool]:
    key = _refund_key(order_number)
    existing = REFUNDS.get(key)
    if existing is not None:
        return existing, False
    refund = {
        "order_number": order_number,
        "refund_id": str(uuid.uuid4()),
        "provider_reference": str(uuid.uuid4()),
        "amount": ORDER_AMOUNTS[order_number],
        "status": "REFUNDED",
    }
    REFUNDS[key] = refund
    return refund, True


@app.get("/refunds/{order_number}")
async def get_refund(order_number: str) -> dict:
    refund = REFUNDS.get(_refund_key(order_number))
    if refund is None:
        raise HTTPException(status_code=404, detail="no refund for order")
    return refund


@app.get("/__e2e/refunds/{order_number}/count")
async def e2e_refund_count(order_number: str) -> dict[str, int]:
    if os.getenv("OPSPILOT_DEMO_E2E", "").lower() not in {"1", "true"} and os.getenv(
        "OPSPILOT_EVAL_FAULT_MATRIX", ""
    ).lower() not in {"1", "true"}:
        raise HTTPException(status_code=404, detail="not found")
    return {"count": int(_refund_key(order_number) in REFUNDS)}


@app.post("/__e2e/refunds/{order_number}/reset")
async def reset_evaluation_refund(order_number: str) -> dict[str, bool]:
    if os.getenv("OPSPILOT_DEMO_E2E", "").lower() not in {"1", "true"} and os.getenv(
        "OPSPILOT_EVAL_FAULT_MATRIX", ""
    ).lower() not in {"1", "true"}:
        raise HTTPException(status_code=404, detail="not found")
    if order_number not in ORDER_AMOUNTS and not _ensure_evaluation_order(order_number):
        raise HTTPException(status_code=404, detail="not found")
    REFUNDS.pop(_refund_key(order_number), None)
    _FAULTED_ORDERS.discard(order_number)
    return {"reset": True}


@app.get("/refunds/{order_number}/eligibility")
async def check_eligibility(order_number: str) -> dict:
    if order_number not in ORDER_AMOUNTS and not _ensure_evaluation_order(order_number):
        raise HTTPException(status_code=404, detail="order not found")
    refunded = _refund_key(order_number) in REFUNDS
    return {
        "order_number": order_number,
        "amount": ORDER_AMOUNTS[order_number],
        "eligible": not refunded,
        "already_refunded": refunded,
    }


@app.post("/refunds")
async def create_refund(request: RefundRequest, raw: Request, response: Response) -> dict:
    if request.order_number not in ORDER_AMOUNTS and not _ensure_evaluation_order(
        request.order_number
    ):
        raise HTTPException(status_code=404, detail="order not found")
    mode = raw.headers.get("x-failure-mode", _configured_mode(request.order_number))
    delay = float(raw.headers.get("x-failure-delay", str(DEFAULT_FAILURE_DELAY_SECONDS)))

    if mode == "timeout_before_effect":
        # Delay past the caller's read timeout, then confirm without applying
        # the refund so a retry is always safe.
        await asyncio.sleep(delay)
        response.status_code = 200
        return {
            "order_number": request.order_number,
            "status": "PENDING",
            "applied": False,
        }

    if mode == "timeout_after_effect":
        refund, _ = _create_refund(request.order_number)
        await asyncio.sleep(delay)
        response.status_code = 201
        return refund

    if mode == "unknown_5xx_after_effect":
        refund, _ = _create_refund(request.order_number)
        response.status_code = 500
        return {"error": "provider unavailable", "refund_id": refund["refund_id"]}

    refund, created = _create_refund(request.order_number)
    response.status_code = 201 if created else 200
    return refund
