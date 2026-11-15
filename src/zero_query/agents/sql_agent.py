"""Text-to-SQL agent, built as its own LangGraph subgraph.

    load_schema -> generate_sql -> execute_sql --ok--> summarize -> END
                        ^              |
                        +--error-------+   (retry until max_sql_attempts, then give up)
                        +--empty-------+   (a result with no data is retried the same way)

All data access goes through the Impala MCP server (`ImpalaTools`); this agent never
connects to the warehouse directly. Guardrails (read-only, row cap) are enforced by
the MCP server, so a bad generated query fails there and is fed back here for repair.
A warehouse outage is different: no SQL can fix it, so the agent gives up straight away.
"""

from __future__ import annotations

import json
import re
import time
from numbers import Number
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from zero_query.agents.llm import message_text
from zero_query.agents.mcp_client import ImpalaTools, ToolCallError
from zero_query.agents.state import ZeroQueryState
from zero_query.config import DEFAULT_MAX_SQL_ATTEMPTS

SQL_PROMPT = """You write a single read-only SQL query that answers the user's question.

Rules:
- Use only the tables and columns in the schema below; reference tables as db.table.
- Return ONLY the SQL, no explanation, no code fences.
- One SELECT (or WITH ... SELECT) statement. Aggregate where sensible; add ORDER BY and LIMIT for "top" questions.
- Use readable column aliases (e.g. total_revenue).
- Where a column lists [values: ...], those are its actual values: filter with them exactly (spelling and case).

Schema:
{schema}"""

REPAIR_SUFFIX = """

Your previous query failed. Fix it.
Previous query:
{sql}
Error:
{error}"""

EMPTY_RESULT_SUFFIX = """

Your previous query ran but returned no usable data (no rows, or only NULL values).
Previous query:
{sql}
If a filter value may be wrong (spelling, case, or a code used instead of a word), correct it.
Otherwise return the same query."""

SUMMARY_PROMPT = """You explain query results to a business user. Answer the question directly in \
1-3 sentences using the numbers in the result. Do not mention SQL. If the result was truncated, say \
the answer is based on the first rows returned."""

GIVE_UP_ANSWER = "Sorry, I couldn't build a working query for that question. Try rephrasing it."
UNAVAILABLE_ANSWER = "The data warehouse is temporarily unavailable. Please try again in a few minutes."

SUMMARY_ROW_LIMIT = 50

# Sampling column values for the prompt (opt-in, see Settings.schema_sample_values).
SAMPLE_SCAN_ROWS = 100_000  # look at this many rows only, so a huge table is never fully scanned
MAX_SAMPLED_COLUMNS_PER_TABLE = 10
MAX_SAMPLE_VALUE_LENGTH = 40  # longer values look like free text, not codes
SAMPLE_TTL_SECONDS = 3600
_TEXT_TYPES = ("string", "varchar", "char", "text")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip().rstrip(";").strip()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def has_no_data(rows: list[list[Any]]) -> bool:
    """True for no rows, or rows made only of NULLs (e.g. SUM over a filter that matched nothing)."""
    return all(value is None for row in rows for value in row)


# --- mock behaviour (no LLM) ----------------------------------------------------------

_MOCK_QUERIES: list[tuple[tuple[str, ...], str]] = [
    (
        ("region",),
        "SELECT c.region, ROUND(SUM(o.revenue), 2) AS total_revenue FROM sales.orders o "
        "JOIN sales.customers c ON c.customer_id = o.customer_id GROUP BY c.region ORDER BY total_revenue DESC",
    ),
    (
        ("segment",),
        "SELECT c.segment, COUNT(DISTINCT c.customer_id) AS customers, ROUND(SUM(o.revenue), 2) AS total_revenue "
        "FROM sales.customers c LEFT JOIN sales.orders o ON o.customer_id = c.customer_id "
        "GROUP BY c.segment ORDER BY total_revenue DESC",
    ),
    (
        ("categor",),
        "SELECT p.category, ROUND(SUM(o.revenue), 2) AS total_revenue FROM sales.orders o "
        "JOIN sales.products p ON p.product_id = o.product_id GROUP BY p.category ORDER BY total_revenue DESC",
    ),
    (
        ("month", "trend"),
        "SELECT substr(o.order_date, 1, 7) AS month, ROUND(SUM(o.revenue), 2) AS total_revenue "
        "FROM sales.orders o GROUP BY month ORDER BY month",
    ),
    (
        ("top", "best"),
        "SELECT p.product_name, ROUND(SUM(o.revenue), 2) AS total_revenue FROM sales.orders o "
        "JOIN sales.products p ON p.product_id = o.product_id GROUP BY p.product_name "
        "ORDER BY total_revenue DESC LIMIT 5",
    ),
    (("customer",), "SELECT COUNT(*) AS customer_count FROM sales.customers"),
    (("order",), "SELECT COUNT(*) AS order_count FROM sales.orders"),
    (("revenue", "sales"), "SELECT ROUND(SUM(revenue), 2) AS total_revenue FROM sales.orders"),
]
_MOCK_FALLBACK = "SELECT * FROM sales.orders ORDER BY order_date DESC LIMIT 10"


def mock_generate_sql(question: str) -> str:
    lowered = question.lower()
    for keywords, sql in _MOCK_QUERIES:
        if any(keyword in lowered for keyword in keywords):
            return sql
    return _MOCK_FALLBACK


def _format_value(value: Any) -> str:
    """Thousands separators for numbers; anything else (text, NULL, dates) as plain text."""
    if isinstance(value, Number) and not isinstance(value, bool):
        return f"{value:,}"
    return "no value" if value is None else str(value)


def mock_summarize(columns: list[str], rows: list[list[Any]], truncated: bool) -> str:
    if not rows:
        return "The query returned no rows."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0].replace('_', ' ').capitalize()}: {_format_value(rows[0][0])}."
    first = ", ".join(f"{column}={value}" for column, value in zip(columns, rows[0], strict=False))
    more = " (results truncated)" if truncated else ""
    return (
        f"Found {len(rows)} rows{more}. First row: {first}. (Mock mode: set OPENROUTER_API_KEY for a written answer.)"
    )


# --- graph ---------------------------------------------------------------------------


class SqlAgent:
    """The nodes of the SQL subgraph. `llm=None` selects the rule-based mock behaviour."""

    def __init__(
        self,
        tools: ImpalaTools,
        llm: BaseChatModel | None,
        max_attempts: int,
        sample_values: int = 0,
        sample_ttl_seconds: float = SAMPLE_TTL_SECONDS,
    ) -> None:
        self.tools = tools
        self.llm = llm
        self.max_attempts = max_attempts
        self.sample_values = sample_values
        self.sample_ttl_seconds = sample_ttl_seconds
        self._samples: dict[str, tuple[float, dict[str, list[str]]]] = {}  # table -> (when, column -> values)

    async def _distinct_values(self, table: str, column: str) -> list[str]:
        """The column's distinct values if it has few short ones (a code or category), else []."""
        if not (_QUALIFIED_TABLE.match(table) and _IDENTIFIER.match(column)):
            return []
        limit = self.sample_values + 1  # one extra tells us the column has too many values to list
        sql = (
            f"SELECT DISTINCT `{column}` FROM (SELECT `{column}` FROM {table} LIMIT {SAMPLE_SCAN_ROWS}) sampled "
            f"LIMIT {limit}"
        )
        try:
            result = await self.tools.execute_query(sql)
        except ToolCallError:
            return []  # sampling is a hint; never let it break the question
        values = [row[0] for row in result["rows"] if row and row[0] is not None]
        if len(values) > self.sample_values or any(len(str(v)) > MAX_SAMPLE_VALUE_LENGTH for v in values):
            return []
        return sorted(str(v) for v in values)

    async def _sample_columns(self, table: str, columns: list[dict[str, str]]) -> dict[str, list[str]]:
        """Distinct values per low-cardinality text column, cached for `sample_ttl_seconds`."""
        if self.llm is None or self.sample_values <= 0:
            return {}
        cached = self._samples.get(table)
        if cached and time.monotonic() - cached[0] < self.sample_ttl_seconds:
            return cached[1]
        text_columns = [c["column"] for c in columns if c["type"].lower().startswith(_TEXT_TYPES)]
        found: dict[str, list[str]] = {}
        for column in text_columns[:MAX_SAMPLED_COLUMNS_PER_TABLE]:
            if values := await self._distinct_values(table, column):
                found[column] = values
        self._samples[table] = (time.monotonic(), found)
        return found

    async def load_schema(self, state: ZeroQueryState) -> ZeroQueryState:
        parts = []
        for table in await self.tools.list_tables():
            columns = await self.tools.describe_table(table["table"])
            samples = await self._sample_columns(table["table"], columns)
            column_list = ", ".join(self._describe_column(column, samples) for column in columns)
            parts.append(f"{table['table']} ({column_list})  -- {table['comment']}")
        return {"schema": "\n".join(parts), "attempts": 0, "error": None, "unavailable": False, "empty_result": False}

    @staticmethod
    def _describe_column(column: dict[str, str], samples: dict[str, list[str]]) -> str:
        text = f"{column['column']} {column['type']}"
        if values := samples.get(column["column"]):
            text += f" [values: {', '.join(_sql_literal(v) for v in values)}]"
        return text

    async def generate_sql(self, state: ZeroQueryState) -> ZeroQueryState:
        if self.llm is None:
            return {"sql": mock_generate_sql(state["question"])}
        system = SQL_PROMPT.format(schema=state["schema"])
        if state.get("error"):
            system += REPAIR_SUFFIX.format(sql=state.get("sql", ""), error=state["error"])
        elif state.get("empty_result"):
            system += EMPTY_RESULT_SUFFIX.format(sql=state.get("sql", ""))
        reply = await self.llm.ainvoke([SystemMessage(system), HumanMessage(state["question"])])
        return {"sql": extract_sql(message_text(reply))}

    async def execute_sql(self, state: ZeroQueryState) -> ZeroQueryState:
        attempts = state.get("attempts", 0) + 1
        try:
            result = await self.tools.execute_query(state["sql"])
        except ToolCallError as exc:
            return {"error": str(exc), "unavailable": exc.unavailable, "attempts": attempts}
        return {
            "columns": result["columns"],
            "rows": result["rows"],
            "truncated": result["truncated"],
            "error": None,
            "unavailable": False,
            "empty_result": has_no_data(result["rows"]),
            "attempts": attempts,
        }

    def _can_retry(self, state: ZeroQueryState) -> bool:
        return self.llm is not None and state["attempts"] < self.max_attempts

    def after_execute(self, state: ZeroQueryState) -> str:
        if state.get("unavailable"):
            return "give_up"  # an outage cannot be fixed by rewriting SQL
        if state.get("error"):
            return "generate_sql" if self._can_retry(state) else "give_up"
        if state.get("empty_result") and self._can_retry(state):
            return "generate_sql"
        return "summarize"

    async def summarize(self, state: ZeroQueryState) -> ZeroQueryState:
        columns, rows, truncated = state["columns"], state["rows"], state.get("truncated", False)
        if self.llm is None:
            return {"answer": mock_summarize(columns, rows, truncated)}
        payload = json.dumps(
            {
                "columns": columns,
                "rows": rows[:SUMMARY_ROW_LIMIT],
                "truncated": truncated or len(rows) > SUMMARY_ROW_LIMIT,
            },
            default=str,
        )
        reply = await self.llm.ainvoke(
            [SystemMessage(SUMMARY_PROMPT), HumanMessage(f"Question: {state['question']}\nResult: {payload}")]
        )
        return {"answer": message_text(reply).strip()}

    async def give_up(self, state: ZeroQueryState) -> ZeroQueryState:
        return {"answer": UNAVAILABLE_ANSWER if state.get("unavailable") else GIVE_UP_ANSWER}

    def build(self) -> CompiledStateGraph:
        graph = StateGraph(ZeroQueryState)
        graph.add_node("load_schema", self.load_schema)
        graph.add_node("generate_sql", self.generate_sql)
        graph.add_node("execute_sql", self.execute_sql)
        graph.add_node("summarize", self.summarize)
        graph.add_node("give_up", self.give_up)
        graph.add_edge(START, "load_schema")
        graph.add_edge("load_schema", "generate_sql")
        graph.add_edge("generate_sql", "execute_sql")
        graph.add_conditional_edges("execute_sql", self.after_execute, ["summarize", "generate_sql", "give_up"])
        graph.add_edge("summarize", END)
        graph.add_edge("give_up", END)
        return graph.compile()


def build_sql_agent(
    tools: ImpalaTools,
    llm: BaseChatModel | None,
    max_attempts: int = DEFAULT_MAX_SQL_ATTEMPTS,
    sample_values: int = 0,
) -> CompiledStateGraph:
    return SqlAgent(tools, llm, max_attempts, sample_values).build()
