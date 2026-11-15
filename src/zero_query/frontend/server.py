"""Frontend server for when the UI runs as its own CAI Application.

Serves the static UI (frontend/) and forwards `/api/*` to the backend Application
server-side, adding the service token. The browser therefore only ever talks to this
app's own origin: no CORS, and the token never reaches the browser.

Run standalone:  uv run zq-frontend   (needs ZQ_BACKEND_URL or ZQ_BACKEND_SUBDOMAIN)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from zero_query.config import get_settings

log = logging.getLogger("zero_query.frontend")

FRONTEND_DIR = Path(os.environ.get("ZQ_FRONTEND_DIR", Path(__file__).resolve().parents[3] / "frontend"))
# Agent runs (several LLM calls + queries) can take a while.
BACKEND_TIMEOUT = httpx2.Timeout(30.0, read=float(os.environ.get("ZQ_BACKEND_TIMEOUT", "180")))
_FORWARD_REQUEST_HEADERS = ("content-type", "accept")


def create_frontend_app(backend_url: str | None = None, transport: httpx2.AsyncBaseTransport | None = None) -> FastAPI:
    """`transport` lets tests route to an in-process backend instead of the network."""
    settings = get_settings()
    target = backend_url or settings.backend_url
    if not target:
        raise RuntimeError("Set ZQ_BACKEND_URL (or ZQ_BACKEND_SUBDOMAIN) to the backend Application URL.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx2.AsyncClient(
            base_url=target, headers=settings.auth_headers, timeout=BACKEND_TIMEOUT, transport=transport
        ) as client:
            app.state.backend = client
            yield

    app = FastAPI(title="Zero Query UI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # Liveness of this app only (CDSW_APP_POLLING_ENDPOINT); /api/health reports the backend.
        return {"status": "ok"}

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST_HEADERS}
        try:
            upstream = await app.state.backend.request(
                request.method,
                f"/api/{path}",
                content=await request.body(),
                headers=headers,
                params=request.query_params,
            )
        except httpx2.HTTPError as exc:
            log.warning("backend unreachable: %s", exc)
            return Response(
                content=b'{"detail": "Backend is unreachable."}', status_code=502, media_type="application/json"
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    uvicorn.run(create_frontend_app(), host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    main()
