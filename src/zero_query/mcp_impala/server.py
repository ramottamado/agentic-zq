"""MCP server exposing the (mock) Impala warehouse as tools.

Run standalone:  uv run zq-mcp      (streamable HTTP on MCP_IMPALA_HOST:MCP_IMPALA_PORT/mcp)

Guardrails live here, not in the agents: read-only single statements and a row cap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from zero_query.config import Settings, get_settings
from zero_query.errors import WAREHOUSE_UNAVAILABLE_MARKER
from zero_query.mcp_impala.warehouse import (
    ImpalaWarehouse,
    MockWarehouse,
    QueryRejected,
    Warehouse,
    WarehouseUnavailable,
)

logger = logging.getLogger(__name__)


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Turn warehouse errors into tool errors the agent can act on.

    A rejected query is reported verbatim so the agent can fix its SQL. An outage is logged in
    full here but reported generically (no hostnames or Kerberos details) with a marker the agent
    recognises, so it stops retrying instead of rewriting a query that was never wrong.
    """
    try:
        yield
    except QueryRejected as exc:
        raise ToolError(str(exc)) from exc
    except WarehouseUnavailable as exc:
        logger.error("warehouse unavailable: %s", exc)
        raise ToolError(f"{WAREHOUSE_UNAVAILABLE_MARKER}: the data warehouse is temporarily unreachable.") from exc


def build_warehouse(settings: Settings) -> Warehouse:
    if settings.warehouse == "mock":
        return MockWarehouse()
    if settings.warehouse != "impala":
        raise ValueError(f"ZQ_WAREHOUSE must be 'mock' or 'impala', got {settings.warehouse!r}")
    if not settings.impala_host:
        raise ValueError("IMPALA_HOST is required when ZQ_WAREHOUSE=impala")
    return ImpalaWarehouse(
        host=settings.impala_host,
        port=settings.impala_port,
        http_path=settings.impala_http_path,
        krb_service_name=settings.impala_krb_service_name,
        krb_host=settings.impala_krb_host,
        ssl_verify=settings.impala_ssl_verify,
        ca_cert=settings.impala_ca_cert,
        databases=settings.impala_databases,
    )


def build_server(warehouse: Warehouse | None = None, max_rows: int | None = None) -> MCPServer:
    wh = warehouse or build_warehouse(get_settings())
    row_cap = max_rows or get_settings().max_rows
    server = MCPServer(
        "impala",
        instructions="Read-only access to the Impala virtual warehouse used by Zero Query.",
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        # Liveness only (CDSW_APP_POLLING_ENDPOINT); deliberately does not touch the warehouse.
        return JSONResponse({"status": "ok"})

    @server.tool()
    def list_tables() -> dict[str, Any]:
        """List queryable tables (as db.table) with a short description of each."""
        with _tool_errors():
            return {"tables": wh.list_tables()}

    @server.tool()
    def describe_table(table: str) -> dict[str, Any]:
        """Return the columns and types of a table, given as db.table."""
        with _tool_errors():
            return {"table": table, "columns": wh.describe_table(table)}

    @server.tool()
    def execute_query(sql: str) -> dict[str, Any]:
        """Run one read-only SELECT/WITH statement and return columns and rows.

        Results are capped; `truncated` is true when more rows exist than were returned.
        """
        with _tool_errors():
            return wh.execute(sql, max_rows=row_cap).to_dict()

    return server


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host/Origin checks. The SDK's default for a 127.0.0.1 bind only accepts localhost Host
    headers, which rejects requests arriving through the CAI application proxy."""
    if not settings.mcp_host_check:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.mcp_allowed_hosts]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    origins += [f"https://{h}" for h in settings.mcp_allowed_hosts]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def main() -> None:
    settings = get_settings()
    # As its own CAI Application, the launcher sets MCP_IMPALA_PORT=$CDSW_APP_PORT.
    build_server().run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        transport_security=transport_security(settings),
    )


if __name__ == "__main__":
    main()
