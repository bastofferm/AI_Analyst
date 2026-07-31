"""Async Claude adapter — the ``anthropic`` dialect behind ``api.ai.llm_runtime``.

Thin on purpose: all OpenAI<->Messages translation lives in the stdlib-only
``anthropic_wire`` module; this file owns nothing but the SDK client and error
mapping. The official ``anthropic`` SDK is imported lazily so the runtime still
imports cleanly on a deployment that only uses the OpenAI-dialect providers.
"""
from __future__ import annotations

from typing import Any

import anthropic_wire


class AnthropicError(RuntimeError):
    """Provider-level failure, already redacted and safe to surface."""


def _client(api_key: str, base_url: str, timeout: float):
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise AnthropicError(
            "The 'anthropic' package is required for Claude. "
            "Install it with: pip install -r backend/requirements.txt"
        ) from exc
    return AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout)


async def chat_once(
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
        response = await client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise AnthropicError(_describe(exc, api_key)) from exc
    finally:
        await client.close()

    try:
        return anthropic_wire.to_openai_message(response)
    except anthropic_wire.AnthropicRefusal as exc:
        raise AnthropicError(str(exc)) from exc


async def list_models(*, api_key: str, base_url: str, timeout: float = 30.0) -> list[str]:
    client = _client(api_key, base_url, timeout)
    try:
        page = await client.models.list(limit=50)
        return [m.id for m in page.data]
    except Exception as exc:  # noqa: BLE001
        raise AnthropicError(_describe(exc, api_key)) from exc
    finally:
        await client.close()


def _describe(exc: Exception, api_key: str) -> str:
    status = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "message", "") or exc)
    if api_key:
        detail = detail.replace(api_key, "***")
    prefix = f"Claude returned {status}" if status else "Network error talking to Claude"
    return f"{prefix}: {detail[:500]}"
