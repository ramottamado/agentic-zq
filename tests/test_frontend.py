import json

import httpx2
from fastapi.testclient import TestClient

from zero_query.frontend.server import create_frontend_app


def test_proxies_api_with_service_token(monkeypatch):
    monkeypatch.setenv("ZQ_SERVICE_TOKEN", "secret-token")
    seen = {}

    def backend(request: httpx2.Request) -> httpx2.Response:
        seen.update(path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
        return httpx2.Response(200, json={"answer": "42", "route": "sql"})

    app = create_frontend_app(backend_url="http://backend.test", transport=httpx2.MockTransport(backend))
    with TestClient(app) as client:
        res = client.post("/api/ask", json={"question": "q?"})
        assert res.status_code == 200 and res.json()["answer"] == "42"
        assert seen == {"path": "/api/ask", "auth": "Bearer secret-token", "body": {"question": "q?"}}
        assert "Zero Query" in client.get("/").text


def test_backend_down_returns_502():
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    app = create_frontend_app(backend_url="http://backend.test", transport=httpx2.MockTransport(down))
    with TestClient(app) as client:
        res = client.post("/api/ask", json={"question": "q?"})
        assert res.status_code == 502 and "unreachable" in res.json()["detail"]


def test_health_is_local_and_does_not_call_backend():
    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("health must not hit the backend")

    app = create_frontend_app(backend_url="http://backend.test", transport=httpx2.MockTransport(never))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
