import pytest

from zero_query.agents.mcp_client import ToolCallError


async def test_list_and_describe(tools):
    tables = {t["table"] for t in await tools.list_tables()}
    assert tables == {"sales.customers", "sales.orders", "sales.products"}
    cols = {c["column"] for c in await tools.describe_table("sales.orders")}
    assert {"order_id", "revenue", "order_date"} <= cols


async def test_execute_query(tools):
    result = await tools.execute_query("SELECT COUNT(*) AS n FROM sales.customers")
    assert result["columns"] == ["n"] and result["rows"][0][0] > 0


async def test_rejected_sql_surfaces_message(tools):
    with pytest.raises(ToolCallError, match="read-only"):
        await tools.execute_query("DELETE FROM sales.orders")


def test_health_route_does_not_touch_warehouse():
    from starlette.testclient import TestClient

    from zero_query.mcp_impala.server import build_server

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("health must not touch the warehouse")

    app = build_server(warehouse=Exploding()).streamable_http_app()
    with TestClient(app) as client:
        res = client.get("/health")
    assert res.status_code == 200 and res.json() == {"status": "ok"}
