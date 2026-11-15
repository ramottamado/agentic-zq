"""Thin async client for the Impala MCP server, used by agent nodes.

`server` is a URL (streamable HTTP, the normal deployment) or an `MCPServer`
instance (connected in-process; used by tests).
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from zero_query.errors import WAREHOUSE_UNAVAILABLE_MARKER


class ToolCallError(RuntimeError):
    """The MCP tool reported an error (e.g. rejected or failing SQL).

    `unavailable` is true when the warehouse itself could not be reached, in which case rewriting
    the SQL is pointless.
    """

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


class ImpalaTools:
    def __init__(self, server: str | MCPServer, headers: dict[str, str] | None = None) -> None:
        """`headers` are sent on every HTTP request (e.g. the bearer token the CAI proxy requires)."""
        self._server = server
        self._headers = headers or {}

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        # One connection per call keeps the mock simple; pool/reuse sessions if latency matters.
        if isinstance(self._server, str):
            # Same timeouts as the SDK default client; long read for slow queries.
            timeout = httpx2.Timeout(30.0, read=300.0)
            async with httpx2.AsyncClient(headers=self._headers, timeout=timeout) as http:
                async with Client(streamable_http_client(self._server, http_client=http)) as client:
                    result = await client.call_tool(name, arguments or {})
        else:
            async with Client(self._server) as client:
                result = await client.call_tool(name, arguments or {})
        text = "\n".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            raise ToolCallError(text or f"{name} failed", unavailable=WAREHOUSE_UNAVAILABLE_MARKER in text)
        if isinstance(result.structured_content, dict):
            return result.structured_content
        return json.loads(text)

    async def list_tables(self) -> list[dict[str, str]]:
        return (await self.call("list_tables"))["tables"]

    async def describe_table(self, table: str) -> list[dict[str, str]]:
        return (await self.call("describe_table", {"table": table}))["columns"]

    async def execute_query(self, sql: str) -> dict[str, Any]:
        return await self.call("execute_query", {"sql": sql})
