from fastapi.testclient import TestClient

from zero_query.agents.graph import build_graph
from zero_query.backend.app import create_app


def test_ask_endpoint(tools):
    with TestClient(create_app(graph=build_graph(tools, llm=None))) as client:
        res = client.post("/api/ask", json={"question": "Top 5 products by revenue"})
        assert res.status_code == 200
        body = res.json()
        assert body["route"] == "sql" and len(body["rows"]) == 5

        assert client.get("/api/health").json()["status"] == "ok"
        assert "Zero Query" in client.get("/").text


def test_rejects_empty_question(tools):
    with TestClient(create_app(graph=build_graph(tools, llm=None))) as client:
        assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_pipeline_failure_hides_internal_details_but_gives_a_reference():
    class Exploding:
        async def ainvoke(self, _state):
            raise RuntimeError("connect to secret-host.internal failed")

    with TestClient(create_app(graph=Exploding())) as client:
        res = client.post("/api/ask", json={"question": "anything"})
    assert res.status_code == 502
    assert "secret-host" not in res.text
    assert "ref " in res.json()["detail"]


class RateLimitError(Exception):
    """Stands in for openai.RateLimitError, which is matched by class name."""

    status_code = 429


def _ask_with_failing_graph(error: Exception):
    class Failing:
        async def ainvoke(self, _state):
            raise error

    with TestClient(create_app(graph=Failing())) as client:
        return client.post("/api/ask", json={"question": "anything"})


def test_llm_rate_limit_gets_a_clear_429_without_provider_details():
    res = _ask_with_failing_graph(RateLimitError("free-models-per-day: add 10 credits at openrouter.ai/settings"))
    assert res.status_code == 429
    assert "usage limit" in res.json()["detail"] and "ref " in res.json()["detail"]
    assert "openrouter" not in res.text and "credits" not in res.text


def test_rate_limit_is_found_even_when_wrapped():
    try:
        try:
            raise RateLimitError("429")
        except RateLimitError as inner:
            raise RuntimeError("graph failed") from inner
    except RuntimeError as wrapped:
        assert _ask_with_failing_graph(wrapped).status_code == 429


def test_other_failures_are_still_a_generic_502():
    assert _ask_with_failing_graph(RuntimeError("boom")).status_code == 502
