r"""Top-level LangGraph: router -> one specialist agent.

    START -> router --general--> general_agent -> END
                    \--sql-----> sql_agent (subgraph) -> END

Planned specialists (e.g. a semantic-layer agent backed by a Cube/dbt MCP server)
plug in as additional routes here.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from zero_query.agents.general import make_general_agent
from zero_query.agents.mcp_client import ImpalaTools
from zero_query.agents.router import make_router
from zero_query.agents.sql_agent import build_sql_agent
from zero_query.agents.state import ZeroQueryState


def build_graph(tools: ImpalaTools, llm: BaseChatModel | None, max_sql_attempts: int = 3):
    g = StateGraph(ZeroQueryState)
    g.add_node("router", make_router(llm))
    g.add_node("general_agent", make_general_agent(llm))
    g.add_node("sql_agent", build_sql_agent(tools, llm, max_attempts=max_sql_attempts))
    g.add_edge(START, "router")
    g.add_conditional_edges("router", lambda s: s["route"], {"general": "general_agent", "sql": "sql_agent"})
    g.add_edge("general_agent", END)
    g.add_edge("sql_agent", END)
    return g.compile()
