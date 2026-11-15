import sqlite3

import pytest

from zero_query.mcp_impala.warehouse import MockWarehouse, QueryRejected, check_read_only


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM sales.orders", "DROP TABLE sales.orders", "SELECT 1; DROP TABLE x", "", "-- only a comment"],
)
def test_rejects_non_read_only(sql):
    with pytest.raises(QueryRejected):
        check_read_only(sql)


def test_allows_select_and_with():
    assert check_read_only("  select 1; ") == "select 1"
    assert check_read_only("WITH t AS (SELECT 1) SELECT * FROM t").startswith("WITH")


def test_row_cap_sets_truncated():
    wh = MockWarehouse()
    result = wh.execute("SELECT * FROM sales.orders", max_rows=5)
    assert len(result.rows) == 5 and result.truncated


def test_engine_is_read_only_even_if_guard_bypassed():
    wh = MockWarehouse()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        wh._conn.execute("DELETE FROM sales.orders")
