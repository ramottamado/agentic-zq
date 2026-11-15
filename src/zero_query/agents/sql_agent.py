"""Text-to-SQL agent, built as its own LangGraph subgraph.

    load_schema -> generate_sql -> execute_sql --ok--> summarize -> END
                        ^              |
                        +--error-------+   (retry until max_sql_attempts, then give up)

All data access goes through the Impala MCP server (`ImpalaTools`); this agent never
connects to the warehouse directly. Guardrails (read-only, row cap) are enforced by
the MCP server, so a bad generated query fails there and is fed back here for repair.
"""

from __future__ import annotations

import json
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from zero_query.agents.llm import message_text
from zero_query.agents.mcp_client import ImpalaTools, ToolCallError
from zero_query.agents.state import ZeroQueryState

SQL_PROMPT = """You write a single read-only SQL query that answers the user's question.

Rules:
- Use only the tables and columns in the schema below; reference tables as db.table.
- Return ONLY the SQL, no explanation, no code fences.
- One SELECT (or WITH ... SELECT) statement. Aggregate where sensible; add ORDER BY and LIMIT for "top" questions.
- Use readable column aliases (e.g. total_revenue).

Schema:
{schema}"""

REPAIR_SUFFIX = """

Your previous query failed. Fix it.
Previous query:
{sql}
Error:
{error}"""

SUMMARY_PROMPT = """You explain query results to a business user. Answer the question directly in \
1-3 sentences using the numbers in the result. Do not mention SQL. If the result was truncated, say \
the answer is based on the first rows returned."""

SUMMARY_ROW_LIMIT = 50

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip().rstrip(";").strip()


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
    q = question.lower()
    for keywords, sql in _MOCK_QUERIES:
        if any(k in q for k in keywords):
            return sql
    return _MOCK_FALLBACK


def mock_summarize(columns: list[str], rows: list[list], truncated: bool) -> str:
    if not rows:
        return "The query returned no rows."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0].replace('_', ' ').capitalize()}: {rows[0][0]:,}."
    first = ", ".join(f"{c}={v}" for c, v in zip(columns, rows[0], strict=False))
    more = " (results truncated)" if truncated else ""
    return (
        f"Found {len(rows)} rows{more}. First row: {first}. (Mock mode: set OPENROUTER_API_KEY for a written answer.)"
    )


# --- graph ---------------------------------------------------------------------------


def build_sql_agent(tools: ImpalaTools, llm: BaseChatModel | None, max_attempts: int = 3):
    async def load_schema(state: ZeroQueryState) -> ZeroQueryState:
        parts = []
        for t in await tools.list_tables():
            cols = await tools.describe_table(t["table"])
            col_list = ", ".join(f"{c['column']} {c['type']}" for c in cols)
            parts.append(f"{t['table']} ({col_list})  -- {t['comment']}")
        return {"schema": "\n".join(parts), "attempts": 0, "error": None}

    async def generate_sql(state: ZeroQueryState) -> ZeroQueryState:
        if llm is None:
            return {"sql": mock_generate_sql(state["question"])}
        system = SQL_PROMPT.format(schema=state["schema"])
        if state.get("error"):
            system += REPAIR_SUFFIX.format(sql=state.get("sql", ""), error=state["error"])
        reply = await llm.ainvoke([SystemMessage(system), HumanMessage(state["question"])])
        return {"sql": extract_sql(message_text(reply))}

    async def execute_sql(state: ZeroQueryState) -> ZeroQueryState:
        attempts = state.get("attempts", 0) + 1
        try:
            result = await tools.execute_query(state["sql"])
        except ToolCallError as exc:
            return {"error": str(exc), "attempts": attempts}
        return {
            "columns": result["columns"],
            "rows": result["rows"],
            "truncated": result["truncated"],
            "error": None,
            "attempts": attempts,
        }

    def after_execute(state: ZeroQueryState) -> str:
        if not state.get("error"):
            return "summarize"
        if llm is not None and state["attempts"] < max_attempts:
            return "generate_sql"
        return "give_up"

    async def summarize(state: ZeroQueryState) -> ZeroQueryState:
        columns, rows, truncated = state["columns"], state["rows"], state.get("truncated", False)
        if llm is None:
            return {"answer": mock_summarize(columns, rows, truncated)}
        payload = json.dumps(
            {
                "columns": columns,
                "rows": rows[:SUMMARY_ROW_LIMIT],
                "truncated": truncated or len(rows) > SUMMARY_ROW_LIMIT,
            },
            default=str,
        )
        reply = await llm.ainvoke(
            [SystemMessage(SUMMARY_PROMPT), HumanMessage(f"Question: {state['question']}\nResult: {payload}")]
        )
        return {"answer": message_text(reply).strip()}

    async def give_up(state: ZeroQueryState) -> ZeroQueryState:
        return {"answer": "Sorry, I couldn't build a working query for that question. Try rephrasing it."}

    g = StateGraph(ZeroQueryState)
    g.add_node("load_schema", load_schema)
    g.add_node("generate_sql", generate_sql)
    g.add_node("execute_sql", execute_sql)
    g.add_node("summarize", summarize)
    g.add_node("give_up", give_up)
    g.add_edge(START, "load_schema")
    g.add_edge("load_schema", "generate_sql")
    g.add_edge("generate_sql", "execute_sql")
    g.add_conditional_edges("execute_sql", after_execute, ["summarize", "generate_sql", "give_up"])
    g.add_edge("summarize", END)
    g.add_edge("give_up", END)
    return g.compile()
