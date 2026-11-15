"""Shared LangGraph state. Every agent (sub)graph reads and writes these keys."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Route = Literal["general", "sql"]


class ZeroQueryState(TypedDict, total=False):
    question: str
    route: Route
    # SQL agent working state
    schema: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    error: str | None
    attempts: int
    # Final output
    answer: str
