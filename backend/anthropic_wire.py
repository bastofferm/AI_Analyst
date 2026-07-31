"""OpenAI-dialect <-> Anthropic Messages translation (pure, stdlib only).

The whole codebase speaks the OpenAI chat-completions shape (``messages`` with a
``system`` role, ``tools[].function``, assistant ``tool_calls``, ``role="tool"``
results) because that is what DeepSeek serves. Claude's Messages API differs in
four ways that all have to be handled or the request 400s:

1. the system prompt is a top-level ``system=`` argument, not a message;
2. tools are ``{name, description, input_schema}``, not ``{type, function}``;
3. tool calls are ``tool_use`` content blocks and results are ``tool_result``
   blocks inside a **user** message — and every result for one assistant turn
   must arrive in a *single* user message;
4. ``temperature``/``top_p``/``top_k`` are rejected outright on Opus 4.6+ and
   Sonnet 5, and ``budget_tokens`` thinking was replaced by adaptive thinking.

This module owns that translation so the sync and async adapters stay thin. It
imports nothing but the standard library, so it is safe to import from any
package regardless of whether the ``anthropic`` SDK is installed.
"""
from __future__ import annotations

import json
from typing import Any

# Non-streaming ceiling recommended by the Claude API docs — above this the SDK
# risks an HTTP timeout. Thinking shares the same budget as the visible answer,
# so a thinking request needs the headroom.
THINKING_MAX_TOKENS = 16000

JSON_ONLY_INSTRUCTION = (
    "Reply with a single valid JSON object and nothing else — no prose, no "
    "explanation, no markdown code fence."
)


class AnthropicRefusal(RuntimeError):
    """Raised when Claude declines the request (``stop_reason == 'refusal'``)."""


def build_request(
    *,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    thinking: bool = False,
    effort: str | None = None,
) -> dict[str, Any]:
    """Turn an OpenAI-shaped call into ``client.messages.create(**kwargs)``.

    Sampling parameters are intentionally *not* accepted: every current Claude
    model returns 400 for them, and every caller in this repo passes a
    temperature it does not actually need.
    """
    system, converted = split_messages(messages)
    if response_format and (response_format or {}).get("type") == "json_object":
        system = f"{system}\n\n{JSON_ONLY_INSTRUCTION}".strip()

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max(int(max_tokens), 1),
        "messages": converted,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = to_anthropic_tools(tools)
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["max_tokens"] = max(kwargs["max_tokens"], THINKING_MAX_TOKENS)
        if effort:
            kwargs["output_config"] = {"effort": effort}
    return kwargs


def split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split OpenAI messages into (system prompt, Anthropic messages)."""
    system_parts: list[str] = []
    out: list[dict] = []

    for msg in messages or []:
        role = msg.get("role")
        if role == "system":
            text = _as_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                "content": _as_text(msg.get("content")) or "(empty result)",
            }
            # Anthropic requires every tool_result for one assistant turn to sit
            # in the same user message — merge into the previous one if it is
            # already a tool-result carrier.
            if out and out[-1]["role"] == "user" and _is_tool_result_message(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            text = _as_text(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_{len(blocks)}",
                    "name": fn.get("name") or "",
                    "input": args if isinstance(args, dict) else {"_raw": args},
                })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        # Everything else is a user turn.
        text = _as_text(msg.get("content"))
        if text:
            out.append({"role": "user", "content": text})

    # The Messages API requires the conversation to open with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    if not out:
        out = [{"role": "user", "content": "(no input)"}]
    return "\n\n".join(system_parts).strip(), out


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for tool in tools or []:
        if tool.get("type") == "function" or "function" in tool:
            fn = tool.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            converted.append({
                "name": name,
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif tool.get("name") and tool.get("input_schema"):
            converted.append(tool)  # already Anthropic-shaped
    return converted


def to_openai_message(response: Any) -> dict[str, Any]:
    """Turn a Claude ``Message`` into the assistant-message dict callers expect.

    ``stop_reason`` is checked before any content access — a refusal comes back
    as a successful HTTP 200 with empty or partial content.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise AnthropicRefusal(
            "Claude declined this request"
            + (f" (category: {category})" if category else "")
            + ". Try rephrasing, or switch provider in Setup."
        )

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in getattr(response, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            reasoning_parts.append(getattr(block, "thinking", "") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": getattr(block, "id", "") or f"toolu_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": getattr(block, "name", "") or "",
                    "arguments": json.dumps(getattr(block, "input", None) or {}, default=str),
                },
            })

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    reasoning = "".join(reasoning_parts).strip()
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and bool(content) and content[0].get("type") == "tool_result"


def _as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts).strip()
    return json.dumps(content, default=str)
