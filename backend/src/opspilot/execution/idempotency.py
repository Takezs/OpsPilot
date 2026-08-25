"""Server-side idempotency derivation and stable argument hashing.

Idempotency keys are derived from normalized business parameters — never from a
prompt or agent — with no round/timestamp/operation-id suffix: refunds always
use ``refund:{order_id}`` so the payment provider and the operation layer agree
on the same business key. The arguments hash is a stable SHA-256 over the
canonical JSON of the Pydantic-normalized arguments.
"""

import hashlib
import json
from typing import Any

from opspilot.tools.schemas import RefundOrderArgs

REFUND_IDEMPOTENCY_PREFIX = "refund:"


def derive_refund_idempotency_key(order_number: str) -> str:
    """Return the stable server-side business idempotency key for a refund."""
    return f"{REFUND_IDEMPOTENCY_PREFIX}{order_number}"


def normalize_refund_arguments(order_number: str, amount: float) -> dict[str, Any]:
    """Validate and normalize refund parameters through the Pydantic schema."""
    model = RefundOrderArgs(order_number=order_number, amount=amount)
    return model.model_dump(mode="json")


def stable_arguments_hash(arguments: dict[str, Any]) -> str:
    """Return a stable SHA-256 hash over canonicalized normalized arguments."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
