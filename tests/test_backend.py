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
