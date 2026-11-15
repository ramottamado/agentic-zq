from langchain_core.language_models.fake_chat_models import FakeListChatModel

from zero_query.agents.graph import build_graph


async def test_mock_general_route(tools):
    state = await build_graph(tools, llm=None).ainvoke({"question": "hello, what can you do?"})
    assert state["route"] == "general"
    assert "Zero Query" in state["answer"]


async def test_mock_sql_route(tools):
    state = await build_graph(tools, llm=None).ainvoke({"question": "What was total revenue by region?"})
    assert state["route"] == "sql"
    assert state["columns"] == ["region", "total_revenue"]
    assert len(state["rows"]) == 4
    assert state["error"] is None


async def test_sql_agent_repairs_failed_query(tools):
    # router -> bad SQL -> repaired SQL -> summary
    llm = FakeListChatModel(
        responses=[
            "sql",
            "SELECT nope FROM sales.orders",
            "```sql\nSELECT COUNT(*) AS order_count FROM sales.orders\n```",
            "There are 1,500 orders.",
        ]
    )
    state = await build_graph(tools, llm=llm).ainvoke({"question": "How many orders?"})
    assert state["attempts"] == 2
    assert state["sql"] == "SELECT COUNT(*) AS order_count FROM sales.orders"
    assert state["answer"] == "There are 1,500 orders."


async def test_sql_agent_gives_up_after_max_attempts(tools):
    llm = FakeListChatModel(responses=["sql", "SELECT bad", "SELECT bad", "SELECT bad"])
    state = await build_graph(tools, llm=llm, max_sql_attempts=3).ainvoke({"question": "How many orders?"})
    assert state["attempts"] == 3
    assert state["error"]
    assert state["answer"].startswith("Sorry")
