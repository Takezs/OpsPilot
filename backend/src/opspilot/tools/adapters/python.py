"""HTTP adapters for the order, payment and email demo services.

Each factory returns a callable matching one ``ToolDependencies`` slot. It maps
transport timeouts and non-success HTTP statuses into a journalable
``ToolResult``; the durable retry/reconciliation policy that reads these
outcomes belongs to the task 9/10 executor, not the adapter.
"""

from collections.abc import Awaitable, Callable

import httpx

from opspilot.execution.errors import ProviderFailureKind
from opspilot.tools.schemas import (
    CheckRefundEligibilityArgs,
    GetOrderArgs,
    GetRefundStatusArgs,
    RefundOrderArgs,
    SendEmailArgs,
)
from opspilot.tools.types import ToolResult


def _timeout_result(error: httpx.TimeoutException) -> ToolResult:
    if isinstance(error, httpx.ConnectTimeout):
        return ToolResult(
            ok=False,
            error=f"timeout: {error}",
            provider_not_called=True,
            failure_kind=ProviderFailureKind.CONNECTION_ERROR,
        )
    return ToolResult(
        ok=False,
        error=f"timeout: {error}",
        failure_kind=ProviderFailureKind.READ_TIMEOUT,
    )


def _connection_result(error: httpx.ConnectError) -> ToolResult:
    return ToolResult(
        ok=False,
        error=f"connection error: {error}",
        provider_not_called=True,
        failure_kind=ProviderFailureKind.CONNECTION_ERROR,
    )


def _disconnect_result(error: httpx.RemoteProtocolError, *, side_effect: bool) -> ToolResult:
    return ToolResult(
        ok=False,
        error=f"connection closed while reading response: {error}",
        failure_kind=(
            ProviderFailureKind.DISCONNECTED_AFTER_SEND
            if side_effect
            else ProviderFailureKind.CONNECTION_ERROR
        ),
    )


def _status_failure(service: str, status_code: int, *, side_effect: bool) -> ToolResult:
    if status_code == 429:
        kind = ProviderFailureKind.RATE_LIMITED
    elif status_code >= 500:
        kind = ProviderFailureKind.UNKNOWN_5XX if side_effect else ProviderFailureKind.RETRYABLE_5XX
    else:
        kind = ProviderFailureKind.PERMANENT
    return ToolResult(ok=False, error=f"{service} returned {status_code}", failure_kind=kind)


def get_order_adapter(
    client: httpx.AsyncClient,
) -> Callable[[GetOrderArgs], Awaitable[ToolResult]]:
    async def invoke(args: GetOrderArgs) -> ToolResult:
        try:
            response = await client.get(f"/orders/{args.order_number}")
        except httpx.TimeoutException as error:
            return _timeout_result(error)
        except httpx.RemoteProtocolError as error:
            return _disconnect_result(error, side_effect=False)
        except httpx.ConnectError as error:
            return _connection_result(error)
        if response.status_code != 200:
            return _status_failure("order service", response.status_code, side_effect=False)
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
        except httpx.RemoteProtocolError as error:
            return _disconnect_result(error, side_effect=False)
        except httpx.ConnectError as error:
            return _connection_result(error)
        if response.status_code != 200:
            return _status_failure("payment service", response.status_code, side_effect=False)
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
        except httpx.RemoteProtocolError as error:
            return _disconnect_result(error, side_effect=True)
        except httpx.ConnectError as error:
            return _connection_result(error)
        if response.status_code >= 400:
            return _status_failure("payment service", response.status_code, side_effect=True)
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
        except httpx.RemoteProtocolError as error:
            return _disconnect_result(error, side_effect=False)
        except httpx.ConnectError as error:
            return _connection_result(error)
        if response.status_code == 404:
            return ToolResult(
                ok=True, data={"order_number": args.order_number, "status": "NOT_REFUNDED"}
            )
        if response.status_code != 200:
            return _status_failure("payment service", response.status_code, side_effect=False)
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
        except httpx.RemoteProtocolError as error:
            return _disconnect_result(error, side_effect=True)
        except httpx.ConnectError as error:
            return _connection_result(error)
        if response.status_code != 202:
            return _status_failure("email service", response.status_code, side_effect=True)
        return ToolResult(ok=True, data=response.json())

    return invoke
