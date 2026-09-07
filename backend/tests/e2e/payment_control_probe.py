"""Independent HTTP/PG process used to prove demo control session isolation."""

import asyncio
import json
import os
import sys
import time

import httpx

from opspilot.config import Settings
from opspilot.demo_control import control_headers
from tests.e2e.control import payment_control_lock


async def exercise(url: str, order: str, other: str, secret: bytes) -> dict:
    async with payment_control_lock(Settings().database_url):
        start = time.monotonic_ns()
        async with httpx.AsyncClient(base_url=url, timeout=2) as client:
            reset = await client.post(
                f"/__e2e/refunds/{order}/reset",
                headers=control_headers(secret, "POST", "reset", order),
            )
            reset.raise_for_status()
            # Give the other process time to contend for the external lock.
            await asyncio.sleep(0.3)
            wrong = await client.post(
                f"/__e2e/refunds/{other}/reset",
                headers=control_headers(secret, "POST", "reset", order),
            )
            assert wrong.status_code == 404
            timed_out = False
            try:
                await client.post("/refunds", json={"order_number": order}, timeout=0.1)
            except httpx.ReadTimeout:
                timed_out = True
            assert timed_out
            status = await client.get(f"/refunds/{order}")
            assert status.json()["status"] == "REFUNDED"
            repeat = await client.post("/refunds", json={"order_number": order})
            assert repeat.json()["refund_id"] == status.json()["refund_id"]
            count = await client.get(
                f"/__e2e/refunds/{order}/count",
                headers=control_headers(secret, "GET", "count", order),
            )
            assert count.json() == {"count": 1}
            wrong_count = await client.get(
                f"/__e2e/refunds/{other}/count",
                headers=control_headers(secret, "GET", "count", order),
            )
            assert wrong_count.status_code == 404
        return {
            "process_id": os.getpid(),
            "order_id": order,
            "start": start,
            "end": time.monotonic_ns(),
            "timeout_after_effect": timed_out,
            "refund_count": 1,
            "cross_order_rejected": True,
        }


if __name__ == "__main__":
    secret = sys.stdin.buffer.read(32)
    print(json.dumps(asyncio.run(exercise(*sys.argv[1:4], secret))))
