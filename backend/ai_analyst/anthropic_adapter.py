"""Sync Claude adapter — the ``anthropic`` dialect behind ``ai_analyst.llm_runtime``.

Sync twin of ``api/ai/anthropic_adapter.py``; the duplication mirrors the
existing sync/async ``llm_runtime`` pair (the committee engine runs on a worker
thread with psycopg2 and blocking httpx). Translation is shared via the
stdlib-only ``anthropic_wire`` module.
"""
from __future__ import annotations

import site
from typing import Any

site.addsitedir(site.getusersitepackages())

import anthropic_wire


class AnthropicError(RuntimeError):
    """Provider-level failure, already redacted and safe to surface."""


def _client(api_key: str, base_url: str, timeout: float):
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise AnthropicError(
            "The 'anthropic' package is required for Claude. "
            "Install it with: pip install -r backend/requirements.txt"
        ) from exc
    return Anthropic(api_key=api_key, base_url=base_url, timeout=timeout)


def chat_once(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 2000,
    response_format: dict | None = None,
    thinking: bool = False,
    effort: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """One Messages call, returned as an OpenAI-style assistant message dict."""
    kwargs = anthropic_wire.build_request(
        model=model, messages=messages, max_tokens=max_tokens, tools=tools,
        response_format=response_format, thinking=thinking, effort=effort,
    )
    client = _client(api_key, base_url, timeout)
    try:
        response = client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise AnthropicError(_describe(exc, api_key)) from exc
    finally:
        client.close()

    try:
        return anthropic_wire.to_openai_message(response)
    except anthropic_wire.AnthropicRefusal as exc:
        raise AnthropicError(str(exc)) from exc


def _describe(exc: Exception, api_key: str) -> str:
    status = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "message", "") or exc)
    if api_key:
        detail = detail.replace(api_key, "***")
    prefix = f"Claude returned {status}" if status else "Network error talking to Claude"
    return f"{prefix}: {detail[:500]}"
