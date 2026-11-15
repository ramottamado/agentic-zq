"""General agent: answers questions that don't need warehouse data (greetings, help)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from zero_query.agents.llm import message_text
from zero_query.agents.state import ZeroQueryState

GENERAL_PROMPT = """You are Zero Query, an assistant that lets business users ask questions about \
their company data in plain language, without writing SQL. Answer briefly. If the user seems \
unsure what to ask, suggest example questions such as "What was total revenue by region?" or \
"What are the top 5 products by revenue?". Do not invent numbers: data questions are answered \
by querying the warehouse, not by you."""

MOCK_HELP = (
    "Hi! I'm Zero Query. Ask me about your sales data in plain language, for example:\n"
    "- What was total revenue by region?\n"
    "- What are the top 5 products by revenue?\n"
    "- Show monthly revenue trend\n"
    "- How many customers do we have per segment?"
)


def make_general_agent(llm: BaseChatModel | None) -> Callable[[ZeroQueryState], Awaitable[ZeroQueryState]]:
    async def general_agent(state: ZeroQueryState) -> ZeroQueryState:
        if llm is None:
            return {"answer": MOCK_HELP}
        reply = await llm.ainvoke([SystemMessage(GENERAL_PROMPT), HumanMessage(state["question"])])
        return {"answer": message_text(reply).strip()}

    return general_agent
