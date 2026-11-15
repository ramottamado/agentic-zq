"""LLM factory. OpenRouter is OpenAI-compatible, so we use ChatOpenAI with its base URL.

Returns None when OPENROUTER_API_KEY is unset: every agent then falls back to its
rule-based mock behaviour, so the whole stack runs without network or credentials.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from zero_query.config import OPENROUTER_BASE_URL, Settings


def get_chat_model(settings: Settings) -> BaseChatModel | None:
    if settings.use_mock_llm:
        return None
    if not settings.openrouter_model:
        raise RuntimeError("OPENROUTER_API_KEY is set but OPENROUTER_MODEL is not (e.g. 'vendor/model-name').")

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=OPENROUTER_BASE_URL,
        temperature=0,
        default_headers=settings.openrouter_headers,
    )


def message_text(message) -> str:
    """Plain text from a chat model response (content may be a str or a list of blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
