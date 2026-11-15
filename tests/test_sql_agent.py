import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from zero_query.agents.graph import build_graph
from zero_query.agents.mcp_client import ImpalaTools
from zero_query.agents.sql_agent import UNAVAILABLE_ANSWER, has_no_data, mock_summarize
from zero_query.mcp_impala.server import build_server
from zero_query.mcp_impala.warehouse import MockWarehouse, WarehouseUnavailable

NO_DATA_SQL = "SELECT SUM(revenue) AS total FROM sales.orders WHERE order_id < 0"  # SUM of nothing is NULL
ORDER_COUNT_SQL = "SELECT COUNT(*) AS order_count FROM sales.orders"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([], True), ([[None]], True), ([[None, None], [None, None]], True), ([[0]], False), ([[None, 3]], False)],
)
def test_has_no_data(rows, expected):
    assert has_no_data(rows) is expected


def test_mock_summary_formats_numbers_and_does_not_crash_on_text_or_null():
    assert mock_summarize(["order_count"], [[1500]], False) == "Order count: 1,500."
    assert mock_summarize(["region"], [["EMEA"]], False) == "Region: EMEA."
    assert mock_summarize(["total"], [[None]], False) == "Total: no value."


async def test_empty_result_is_retried_with_a_corrected_query(tools):
    llm = FakeListChatModel(responses=["sql", NO_DATA_SQL, ORDER_COUNT_SQL, "There are 1,500 orders."])
    state = await build_graph(tools, llm=llm).ainvoke({"question": "How many orders?"})
    assert state["attempts"] == 2
    assert state["sql"] == ORDER_COUNT_SQL
    assert state["answer"] == "There are 1,500 orders."


async def test_still_empty_on_the_last_attempt_is_answered_not_given_up(tools):
    llm = FakeListChatModel(responses=["sql", NO_DATA_SQL, NO_DATA_SQL, NO_DATA_SQL, "Nothing matched."])
    state = await build_graph(tools, llm=llm, max_sql_attempts=3).ainvoke({"question": "Orders below zero?"})
    assert state["attempts"] == 3
    assert state["error"] is None
    assert state["answer"] == "Nothing matched."


async def test_warehouse_outage_is_not_retried_and_is_reported_plainly():
    class DownWarehouse(MockWarehouse):
        def execute(self, sql, max_rows):
            raise WarehouseUnavailable("kerberos ticket for host secret-host.internal expired")

    tools = ImpalaTools(build_server(warehouse=DownWarehouse(), max_rows=100))
    llm = FakeListChatModel(responses=["sql", ORDER_COUNT_SQL, ORDER_COUNT_SQL, ORDER_COUNT_SQL])
    state = await build_graph(tools, llm=llm, max_sql_attempts=3).ainvoke({"question": "How many orders?"})
    assert state["attempts"] == 1
    assert llm.i == 2  # the router and one SQL generation: no repair attempts for an outage
    assert state["answer"] == UNAVAILABLE_ANSWER
    assert "secret-host" not in state["error"]
