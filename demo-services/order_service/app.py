"""Demo Order Service: canned in-memory order state.

Task 8 synthetic service. Real deployments replace this with the enterprise
order system; the Tool Gateway contract (get_order) is unchanged.
"""

import os
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Order Service")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


class Order(BaseModel):
    order_number: str
    status: str
    amount: float
    currency: str = "USD"


ORDERS: dict[str, Order] = {
    "A100": Order(order_number="A100", status="OPEN", amount=250.0),
    "A101": Order(order_number="A101", status="OPEN", amount=50.0),
    "A102": Order(order_number="A102", status="OPEN", amount=350.0),
    "A103": Order(order_number="A103", status="OPEN", amount=1200.0),
    "ORD-002": Order(order_number="ORD-002", status="OPEN", amount=350.0),
}
if os.getenv("OPSPILOT_EVAL_FAULT_MATRIX", "").lower() in {"1", "true"}:
    ORDERS.update(
        {
            f"EVAL-{index:03d}": Order(
                order_number=f"EVAL-{index:03d}", status="OPEN", amount=350.0
            )
            for index in range(1, 61)
        }
    )

EVALUATION_ORDER_PATTERN = re.compile(r"EVAL-[0-9a-f]{8}-[0-9]{3}")


def _evaluation_order(order_number: str) -> Order | None:
    if os.getenv("OPSPILOT_EVAL_FAULT_MATRIX", "").lower() not in {"1", "true"}:
        return None
    if EVALUATION_ORDER_PATTERN.fullmatch(order_number) is None:
        return None
    return Order(order_number=order_number, status="OPEN", amount=350.0)


@app.get("/orders/{order_number}", response_model=Order)
async def get_order(order_number: str) -> Order:
    evaluation_order = _evaluation_order(order_number)
    if evaluation_order is not None:
        return evaluation_order
    try:
        return ORDERS[order_number]
    except KeyError as error:
        raise HTTPException(status_code=404, detail="order not found") from error
