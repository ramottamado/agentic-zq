import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from zero_query.agents.graph import build_graph
from zero_query.agents.mcp_client import ImpalaTools, ToolCallError
from zero_query.agents.sql_agent import (
    SAMPLE_SCAN_ROWS,
    UNAVAILABLE_ANSWER,
    SqlAgent,
    has_no_data,
    mock_summarize,
)
from zero_query.mcp_impala.server import build_server
from zero_query.mcp_impala.warehouse import MockWarehouse, QueryResult, WarehouseUnavailable

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


# --- opt-in sampling of column values for the schema prompt ------------------------------------


class CountingTools(ImpalaTools):
    """Counts the queries the agent sends, and can make them fail."""

    def __init__(self, server, fail=False):
        super().__init__(server)
        self.queries: list[str] = []
        self.fail = fail

    async def execute_query(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise ToolCallError("boom")
        return await super().execute_query(sql)


def make_agent(tools, sample_values, llm=True, **kwargs):
    model = FakeListChatModel(responses=["x"]) if llm else None
    return SqlAgent(tools, model, max_attempts=3, sample_values=sample_values, **kwargs)


def customers_line(schema: str) -> str:
    return next(line for line in schema.splitlines() if line.startswith("sales.customers"))


async def test_sampling_is_off_by_default(tools):
    schema = (await make_agent(tools, sample_values=0).load_schema({}))["schema"]
    assert "[values:" not in schema


async def test_sampling_lists_only_low_cardinality_text_columns():
    tools = CountingTools(build_server(max_rows=100))
    schema = (await make_agent(tools, sample_values=5).load_schema({}))["schema"]
    line = customers_line(schema)
    assert "region TEXT [values: 'APAC', 'EMEA', 'LATAM', 'North America']" in line
    assert "segment TEXT [values: 'Consumer', 'Enterprise', 'SMB']" in line
    # 60 distinct names/dates exceed the cap, so they are left out entirely: no PII-like columns reach the LLM
    assert "customer_name TEXT," in line and "signup_date TEXT" in line
    assert "customer_name TEXT [" not in line and "signup_date TEXT [" not in line
    assert "customer_id INTEGER," in line  # numeric columns are never sampled


async def test_sampling_queries_a_bounded_window_not_the_whole_table():
    tools = CountingTools(build_server(max_rows=100))
    await make_agent(tools, sample_values=5).load_schema({})
    assert tools.queries and all("FROM (SELECT" in q and f"LIMIT {SAMPLE_SCAN_ROWS}" in q for q in tools.queries)
    assert all(q.endswith("LIMIT 6") for q in tools.queries)  # cap + 1, to detect "too many values"


async def test_sampling_is_skipped_for_the_mock_llm():
    tools = CountingTools(build_server(max_rows=100))
    schema = (await make_agent(tools, sample_values=5, llm=False).load_schema({}))["schema"]
    assert tools.queries == [] and "[values:" not in schema


async def test_samples_are_cached_until_they_expire():
    tools = CountingTools(build_server(max_rows=100))
    agent = make_agent(tools, sample_values=5)
    await agent.load_schema({})
    first = len(tools.queries)
    await agent.load_schema({})
    assert len(tools.queries) == first  # second question: no new queries

    expired = make_agent(tools, sample_values=5, sample_ttl_seconds=0)
    await expired.load_schema({})
    await expired.load_schema({})
    assert len(tools.queries) == first + 2 * first  # sampled again each time


async def test_a_failing_sample_never_breaks_the_schema():
    tools = CountingTools(build_server(max_rows=100), fail=True)
    schema = (await make_agent(tools, sample_values=5).load_schema({}))["schema"]
    assert "sales.customers" in schema and "[values:" not in schema


async def test_sampled_values_are_quoted_like_sql_literals():
    class Quirky(MockWarehouse):
        def execute(self, sql, max_rows):
            return QueryResult(columns=["code"], rows=[["O'Neil"], ["plain"]], truncated=False)

    tools = ImpalaTools(build_server(warehouse=Quirky(), max_rows=100))
    schema = (await make_agent(tools, sample_values=5).load_schema({}))["schema"]
    assert "'O''Neil'" in schema and "'plain'" in schema


def test_sample_setting_defaults_to_off_and_reads_the_environment(monkeypatch):
    from zero_query.config import get_settings

    monkeypatch.delenv("ZQ_SCHEMA_SAMPLE_VALUES", raising=False)
    assert get_settings().schema_sample_values == 0
    monkeypatch.setenv("ZQ_SCHEMA_SAMPLE_VALUES", "20")
    assert get_settings().schema_sample_values == 20
