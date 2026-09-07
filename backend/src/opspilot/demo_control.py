"""Authenticated, order-scoped controls for synthetic demo services only."""

import hashlib
import hmac
import os
import re
from pathlib import Path

from fastapi import HTTPException, Request

CONTROL_HEADER = "X-OpsPilot-Control-Token"
E2E_ORDER = re.compile(r"E2E-[0-9a-f]{32}\Z")
FAULT_ORDER = re.compile(r"EVAL-[0-9a-f]{8}-[0-9]{3}\Z")


def enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true"}


def control_order_allowed(order: str) -> bool:
    return bool(
        (enabled("OPSPILOT_DEMO_E2E") and E2E_ORDER.fullmatch(order))
        or (enabled("OPSPILOT_EVAL_FAULT_MATRIX") and FAULT_ORDER.fullmatch(order))
    )


def control_secret() -> bytes | None:
    path = os.getenv("OPSPILOT_E2E_CONTROL_FILE", "")
    if not path:
        return None
    try:
        with Path(path).open("rb") as stream:
            value = stream.read(33)
    except OSError:
        return None
    return value if len(value) == 32 else None


def control_proof(secret: bytes, method: str, action: str, order: str) -> str:
    if len(secret) != 32:
        raise ValueError("invalid demo control credential")
    message = f"opspilot-demo-control:v1\n{method.upper()}\n{action}\n{order}"
    return hmac.new(secret, message.encode("ascii"), hashlib.sha256).hexdigest()


def control_headers(secret: bytes, method: str, action: str, order: str) -> dict[str, str]:
    return {CONTROL_HEADER: control_proof(secret, method, action, order)}


def authorize_control(request: Request, order: str, action: str) -> None:
    secret = control_secret()
    supplied = request.headers.get(CONTROL_HEADER, "")
    if (
        secret is None
        or not control_order_allowed(order)
        or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
        or not hmac.compare_digest(supplied, control_proof(secret, request.method, action, order))
    ):
        raise HTTPException(status_code=404, detail="not found")
