"""Router: decides which specialist agent handles a question.

Add a new specialist by extending `Route` in state.py, teaching the router about it
here, and adding the node + edge in graph.py.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from zero_query.agents.llm import message_text
from zero_query.agents.state import Route, ZeroQueryState

ROUTER_PROMPT = """You route questions for Zero Query, an assistant that answers business questions \
from a data warehouse.

Reply with exactly one word:
- sql: the question needs data from the warehouse (numbers, lists, trends, comparisons, counts).
- general: greetings, help, questions about what Zero Query can do, or anything not needing data."""

_DATA_WORDS = re.compile(
    r"\b(revenue|sales|sold|customers?|products?|orders?|how many|how much|top|total|average|avg|"
    r"trend|monthly|by month|by region|segment|category|categories|count|sum|compare|highest|lowest)\b",
    re.IGNORECASE,
)


def mock_route(question: str) -> Route:
    return "sql" if _DATA_WORDS.search(question) else "general"


def make_router(llm: BaseChatModel | None) -> Callable[[ZeroQueryState], Awaitable[ZeroQueryState]]:
    async def router(state: ZeroQueryState) -> ZeroQueryState:
        question = state["question"]
        if llm is None:
            return {"route": mock_route(question)}
        reply = await llm.ainvoke([SystemMessage(ROUTER_PROMPT), HumanMessage(question)])
        word = message_text(reply).strip().lower()
        return {"route": "sql" if "sql" in word else "general"}

    return router
