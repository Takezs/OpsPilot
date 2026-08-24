"""HTTP adapters for the order, payment and email demo services.

Each factory returns a callable matching one ``ToolDependencies`` slot. It maps
transport timeouts and non-success HTTP statuses into a journalable
``ToolResult``; the durable retry/reconciliation policy that reads these
outcomes belongs to the task 9/10 executor, not the adapter.
"""

from collections.abc import Awaitable, Callable

import httpx

from opspilot.tools.schemas import (
    CheckRefundEligibilityArgs,
    GetOrderArgs,
    GetRefundStatusArgs,
    RefundOrderArgs,
    SendEmailArgs,
)
from opspilot.tools.types import ToolResult


def _timeout_result(error: httpx.TimeoutException) -> ToolResult:
    return ToolResult(ok=False, error=f"timeout: {error}")


def get_order_adapter(
    client: httpx.AsyncClient,
) -> Callable[[GetOrderArgs], Awaitable[ToolResult]]:
    async def invoke(args: GetOrderArgs) -> ToolResult:
        try:
            response = await client.get(f"/orders/{args.order_number}")
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        if response.status_code != 200:
            return ToolResult(ok=False, error=f"order service returned {response.status_code}")
        return ToolResult(ok=True, data=response.json())

    return invoke


def check_refund_eligibility_adapter(
    client: httpx.AsyncClient,
) -> Callable[[CheckRefundEligibilityArgs], Awaitable[ToolResult]]:
    async def invoke(args: CheckRefundEligibilityArgs) -> ToolResult:
        try:
            response = await client.get(f"/refunds/{args.order_number}/eligibility")
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        if response.status_code != 200:
            return ToolResult(ok=False, error=f"payment service returned {response.status_code}")
        return ToolResult(ok=True, data=response.json())

    return invoke


def refund_order_adapter(
    client: httpx.AsyncClient,
) -> Callable[[RefundOrderArgs], Awaitable[ToolResult]]:
    async def invoke(args: RefundOrderArgs) -> ToolResult:
        try:
            response = await client.post("/refunds", json={"order_number": args.order_number})
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        if response.status_code >= 400:
            return ToolResult(ok=False, error=f"payment service returned {response.status_code}")
        return ToolResult(ok=True, data=response.json())

    return invoke


def get_refund_status_adapter(
    client: httpx.AsyncClient,
) -> Callable[[GetRefundStatusArgs], Awaitable[ToolResult]]:
    async def invoke(args: GetRefundStatusArgs) -> ToolResult:
        try:
            response = await client.get(f"/refunds/{args.order_number}")
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        if response.status_code == 404:
            return ToolResult(
                ok=True, data={"order_number": args.order_number, "status": "NOT_REFUNDED"}
            )
        if response.status_code != 200:
            return ToolResult(ok=False, error=f"payment service returned {response.status_code}")
        return ToolResult(ok=True, data=response.json())

    return invoke


def send_email_adapter(
    client: httpx.AsyncClient,
) -> Callable[[SendEmailArgs], Awaitable[ToolResult]]:
    async def invoke(args: SendEmailArgs) -> ToolResult:
        try:
            response = await client.post(
                "/emails",
                json={"to": args.to, "subject": args.subject, "body": args.body},
            )
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        if response.status_code != 202:
            return ToolResult(ok=False, error=f"email service returned {response.status_code}")
        return ToolResult(ok=True, data=response.json())

    return invoke
