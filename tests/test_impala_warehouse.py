from decimal import Decimal

import pytest

from zero_query.agents.mcp_client import ImpalaTools, ToolCallError
from zero_query.mcp_impala.server import build_server
from zero_query.mcp_impala.warehouse import ImpalaWarehouse, QueryRejected, WarehouseUnavailable


class GSSError(Exception):
    """Stands in for kerberos.GSSError, which is matched by class name."""


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql):
        self.conn.log.append(sql)
        if "boom" in sql:
            raise RuntimeError("AnalysisException: boom")
        if "reset" in sql:
            raise ConnectionResetError("peer reset the connection")
        if "gss" in sql:
            raise GSSError("ticket expired")
        if sql.startswith("SHOW TABLES"):
            self.description, self._rows = [("name",)], [("orders",)]
        elif sql.startswith("DESCRIBE"):
            self.description, self._rows = [("name",), ("type",)], [("amount", "decimal(38,10)")]
        else:
            self.description, self._rows = [("amount",)], [(Decimal("1.50"),)] * 5

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def close(self):
        pass


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


@pytest.fixture
def calls():
    return []


@pytest.fixture
def wh(calls):
    def connect(**kwargs):
        calls.append(kwargs)
        return FakeConn(calls)

    return ImpalaWarehouse(host="h", databases=["salesdb"], krb_host="k", connect=connect)


def test_connect_kwargs(wh, calls):
    wh.list_tables()
    kw = calls[0]
    assert kw["auth_mechanism"] == "GSSAPI" and kw["use_http_transport"] and kw["use_ssl"]
    assert kw["verify_cert"] is False and kw["krb_host"] == "k" and kw["http_path"] == "cliservice"


def test_list_and_describe(wh):
    assert wh.list_tables() == [{"table": "salesdb.orders", "comment": ""}]
    assert wh.describe_table("orders") == [{"column": "amount", "type": "decimal(38,10)"}]


def test_database_scope(wh):
    with pytest.raises(QueryRejected, match="not allowed"):
        wh.describe_table("other.t")
    with pytest.raises(QueryRejected, match="not allowed"):
        wh.execute("SELECT * FROM other.t", 10)
    with pytest.raises(QueryRejected, match="Invalid"):
        wh.describe_table("t; drop table x")
    wh.execute("SELECT * FROM salesdb.orders WHERE note = 'a.b'", 10)


def test_read_only_and_truncation(wh):
    with pytest.raises(QueryRejected, match="read-only"):
        wh.execute("DROP TABLE salesdb.orders", 10)
    result = wh.execute("SELECT amount FROM salesdb.orders", 3)
    assert len(result.rows) == 3 and result.truncated


def test_engine_error_becomes_query_rejected(wh):
    with pytest.raises(QueryRejected, match="boom"):
        wh.execute("SELECT boom FROM salesdb.orders", 10)


async def test_decimal_rows_survive_mcp(wh):
    tools = ImpalaTools(build_server(warehouse=wh, max_rows=100))
    result = await tools.execute_query("SELECT amount FROM salesdb.orders")
    assert float(result["rows"][0][0]) == 1.5
    with pytest.raises(ToolCallError, match="not allowed"):
        await tools.execute_query("SELECT * FROM other.t")


def test_connection_failure_is_an_outage_not_a_bad_query():
    def refuse(**kwargs):
        raise OSError("connection refused")

    down = ImpalaWarehouse(host="h", databases=["salesdb"], connect=refuse)
    with pytest.raises(WarehouseUnavailable, match="Cannot connect"):
        down.list_tables()
    with pytest.raises(WarehouseUnavailable):
        down.execute("SELECT 1 FROM salesdb.orders", 10)


@pytest.mark.parametrize("marker", ["reset", "gss"])
def test_transport_and_kerberos_errors_while_running_are_outages(wh, marker):
    with pytest.raises(WarehouseUnavailable):
        wh.execute(f"SELECT {marker} FROM salesdb.orders", 10)


def test_engine_rejection_stays_a_query_error(wh):
    with pytest.raises(QueryRejected):
        wh.execute("SELECT boom FROM salesdb.orders", 10)


async def test_outage_reaches_the_agent_generically_and_flagged():
    def refuse(**kwargs):
        raise OSError("connect to secret-host.internal:443 refused")

    tools = ImpalaTools(build_server(warehouse=ImpalaWarehouse(host="h", databases=["salesdb"], connect=refuse)))
    with pytest.raises(ToolCallError) as caught:
        await tools.execute_query("SELECT 1 FROM salesdb.orders")
    assert caught.value.unavailable
    assert "secret-host" not in str(caught.value)  # details stay in the server log
    with pytest.raises(ToolCallError) as bad_query:
        await tools.execute_query("DELETE FROM salesdb.orders")
    assert not bad_query.value.unavailable
