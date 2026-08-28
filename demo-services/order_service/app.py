"""Demo Order Service: canned in-memory order state.

Task 8 synthetic service. Real deployments replace this with the enterprise
order system; the Tool Gateway contract (get_order) is unchanged.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Order Service")


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


@app.get("/orders/{order_number}", response_model=Order)
async def get_order(order_number: str) -> Order:
    try:
        return ORDERS[order_number]
    except KeyError as error:
        raise HTTPException(status_code=404, detail="order not found") from error
