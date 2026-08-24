"""MCP transport adapter contract for external tools.

The design spec (section 3.3) allows external tools to be reached through a
selected transport's MCP adapter; none of the six registered tools use MCP in
this milestone. This module defines the contract a remote MCP tool must satisfy
so a future ``mcp_server`` can be wired into the registry without bypassing the
Tool Gateway.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opspilot.tools.types import ToolResult


@dataclass(frozen=True)
class McpToolEndpoint:
    """A tool exposed by a remote MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]


def mcp_invoke(
    remote: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
    """Wrap a remote MCP tool call, translating its result into a ToolResult."""

    async def invoke(arguments: dict[str, Any]) -> ToolResult:
        result = await remote(arguments)
        return ToolResult(ok=True, data=result)

    return invoke
