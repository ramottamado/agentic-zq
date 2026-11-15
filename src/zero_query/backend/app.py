"""FastAPI backend: serves the UI and exposes the agent graph over HTTP.

Run standalone:  uv run zq-backend   (expects the MCP server to be running; see scripts/start_app.py)

The UI only talks to `POST /api/ask`; it never knows which agent answered beyond the
`route` field, so agents can be added or swapped without frontend changes.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from zero_query.agents.graph import build_graph
from zero_query.agents.llm import get_chat_model
from zero_query.agents.mcp_client import ImpalaTools
from zero_query.config import get_settings

log = logging.getLogger("zero_query.backend")

FRONTEND_DIR = Path(os.environ.get("ZQ_FRONTEND_DIR", Path(__file__).resolve().parents[3] / "frontend"))


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    route: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated: bool = False
    error: str | None = None


def create_app(graph=None) -> FastAPI:
    """`graph` can be injected (tests); otherwise it is built from settings at startup."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.graph = graph or build_graph(
            ImpalaTools(settings.mcp_url, headers=settings.auth_headers),
            get_chat_model(settings),
            settings.max_sql_attempts,
        )
        yield

    app = FastAPI(title="Zero Query", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm": "mock" if settings.use_mock_llm else "openrouter",
            "model": settings.openrouter_model if not settings.use_mock_llm else None,
            "mcp_url": settings.mcp_url,
        }

    @app.post("/api/ask", response_model=AskResponse)
    async def ask(req: AskRequest) -> AskResponse:
        try:
            state = await app.state.graph.ainvoke({"question": req.question.strip()})
        except Exception as exc:  # surface infra failures (MCP down, LLM errors) to the UI
            log.exception("graph failed")
            raise HTTPException(status_code=502, detail=f"Agent pipeline failed: {exc}") from exc
        return AskResponse(
            answer=state.get("answer", ""),
            route=state.get("route", "unknown"),
            sql=state.get("sql"),
            columns=state.get("columns", []),
            rows=state.get("rows", []),
            truncated=state.get("truncated", False),
            error=state.get("error"),
        )

    # Off when the UI is deployed as its own CAI Application (scripts/cai/app_frontend.py).
    if settings.serve_ui and FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    uvicorn.run(create_app(), host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    main()
