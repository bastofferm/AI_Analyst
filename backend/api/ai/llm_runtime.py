"""Multi-provider HTTP runtime — async port of ai_analyst/llm_runtime.py.

Two wire dialects behind one public surface (registry: ``backend/llm_providers.py``):

* ``openai``    — DeepSeek / OpenAI / Moonshot / Gemini over ``/chat/completions``
  with ``httpx.AsyncClient``.
* ``anthropic`` — Claude via ``api/ai/anthropic_adapter.py`` (official SDK).

Every entry point takes ``provider``; omitting it reproduces the historical
DeepSeek behaviour exactly. Keys stay per-request so the dashboard can use the
user's session-scoped key without it ever touching disk or the database.
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

import llm_providers
from . import anthropic_adapter


logger = logging.getLogger("mzqa.ai.llm_runtime")

# Deprecated DeepSeek-shaped aliases, kept so untouched callers keep working.
DEFAULT_BASE_URL = llm_providers.PROVIDERS["deepseek"].base_url
DEFAULT_CHAT_MODEL = llm_providers.PROVIDERS["deepseek"].chat_model
DEFAULT_REASONER = llm_providers.PROVIDERS["deepseek"].reasoner_model
ALLOWED_DEEPSEEK_HOSTS = set(llm_providers.PROVIDERS["deepseek"].hosts)

ENV_KEY_NAMES = ("DEEPSEEK_API_KEY",)
_DSML_TAG_RE = re.compile(
    r"<\s*(?P<closing>/?)\s*\|\s*DSML\s*\|\s*\|\s*"
    r"(?P<tag>[A-Za-z_][\w-]*)\b(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*\"([^\"]*)\"")


def resolve_env_key(provider: str | None = None) -> str:
    """API key from the environment / Windows registry for ``provider``."""
    return llm_providers.resolve_env_key(provider)


class LLMError(RuntimeError):
    pass


def _missing_key_message(prov: llm_providers.Provider) -> str:
    return (
        f"No {prov.label} API key configured. Add one under Setup in the app, "
        f"or set {' or '.join(prov.env)} in your Windows user environment variables."
    )


async def _post_chat(
    api_key: str,
    base_url: str,
    payload: dict,
    timeout: float = 120.0,
    provider: str | None = None,
) -> dict:
    """OpenAI-dialect ``/chat/completions`` POST (DeepSeek, OpenAI, Moonshot, Gemini)."""
    prov = llm_providers.get(provider)
    if not api_key:
        raise LLMError(_missing_key_message(prov))
    try:
        url = f"{llm_providers.resolve_base_url(prov.id, base_url)}/chat/completions"
    except llm_providers.LLMProviderError as exc:
        raise LLMError(str(exc)) from exc
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise LLMError(f"Network error talking to {prov.label}: {e}") from e
    if r.status_code != 200:
        snippet = r.text[:500].replace(api_key, "***")
        raise LLMError(f"{prov.label} returned {r.status_code}: {snippet}")
    return r.json()


def _openai_payload(
    prov: llm_providers.Provider,
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int,
    response_format: dict | None,
) -> dict[str, Any]:
    """Shape the request for one OpenAI-dialect provider's quirks."""
    payload: dict[str, Any] = {"model": model, "messages": messages}
    payload[prov.caps.max_tokens_field] = llm_providers.clamp_max_tokens(prov.id, max_tokens)
    if prov.caps.temperature:
        payload["temperature"] = temperature
    if tools and prov.caps.tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        if prov.caps.json_object:
            payload["response_format"] = response_format
        else:
            payload["messages"] = _json_mode_via_prompt(messages)
    if prov.caps.disable_thinking:
        # Turn off Kimi's hidden reasoning: otherwise it eats the token budget and the
        # answer comes back empty / times out. Verified: reasoning_tokens ~1 with this set.
        payload["thinking"] = {"type": "disabled"}
    return payload


def _json_mode_via_prompt(messages: list[dict]) -> list[dict]:
    """Fallback JSON mode for providers without ``response_format`` support."""
    import anthropic_wire  # stdlib-only helper, shares the instruction wording

    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            msg["content"] = f"{msg.get('content') or ''}\n\n{anthropic_wire.JSON_ONLY_INSTRUCTION}".strip()
            return out
    return [{"role": "system", "content": anthropic_wire.JSON_ONLY_INSTRUCTION}, *out]


async def chat_once(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    response_format: dict | None = None,
    provider: str | None = None,
    thinking: bool = False,
    effort: str | None = None,
) -> dict:
    """Single completion call. Returns the assistant message dict."""
    try:
        prov = llm_providers.get(provider)
    except llm_providers.LLMProviderError as exc:
        raise LLMError(str(exc)) from exc
    model = llm_providers.chat_model(prov.id, model)

    if prov.dialect == "anthropic":
        if not api_key:
            raise LLMError(_missing_key_message(prov))
        try:
            return await anthropic_adapter.chat_once(
                api_key=api_key,
                base_url=llm_providers.resolve_base_url(prov.id, base_url),
                model=model, messages=messages, tools=tools, max_tokens=max_tokens,
                response_format=response_format, thinking=thinking, effort=effort,
            )
        except (anthropic_adapter.AnthropicError, llm_providers.LLMProviderError) as exc:
            raise LLMError(str(exc)) from exc

    payload = _openai_payload(
        prov, model=model, messages=messages, tools=tools,
        temperature=temperature, max_tokens=max_tokens, response_format=response_format,
    )
    data = await _post_chat(api_key, base_url, payload, provider=prov.id)
    if not data.get("choices"):
        raise LLMError(f"{prov.label} returned no choices: {data}")
    return data["choices"][0]["message"]


async def chat_with_tools(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Awaitable[Any]],
    history: list[dict] | None = None,
    max_hops: int = 8,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    provider: str | None = None,
) -> tuple[str, list[dict]]:
    """Multi-hop tool-calling loop.

    `tool_executor` is awaited per tool call. Returns (assistant_text, trace) where
    trace is a list of {name, arguments, result_preview} dicts for UI display.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    trace: list[dict] = []
    for _ in range(max_hops):
        msg = await chat_once(
            api_key=api_key, base_url=base_url, model=model, provider=provider,
            messages=messages, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
        )
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            tool_calls = _parse_dsml_tool_calls(content)
        if not tool_calls:
            return _sanitize_final_content(content, trace), trace

        assistant_msg = {
            "role": "assistant",
            "content": _strip_dsml_tool_markup(content),
            "tool_calls": tool_calls,
        }
        if msg.get("reasoning_content"):
            assistant_msg["reasoning_content"] = msg["reasoning_content"]
        messages.append(assistant_msg)

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await tool_executor(name, args)
            except Exception as exc:
                result = {"error": str(exc)}
            preview = result if isinstance(result, (str, dict, list)) else str(result)
            trace.append({
                "name": name,
                "arguments": args,
                "result_preview": _shorten_preview(preview),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": json.dumps(result, default=str)[:30000],
            })

    final = await chat_once(
        api_key=api_key, base_url=base_url, model=model, provider=provider,
        messages=messages + [{"role": "user", "content":
            "Hop budget exhausted. Answer now using what you have. "
            "Do not call tools, do not write DSML, and do not describe tool markup. "
            "Return only the final Markdown answer."}],
        temperature=temperature, max_tokens=max_tokens,
    )
    final_content = final.get("content") or ""
    return _sanitize_final_content(final_content, trace), trace


async def chat_json(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 2000,
    attempts: int = 3,
    provider: str | None = None,
) -> dict:
    """Completion forced to JSON output, retried when the model breaks the format.

    Even with response_format=json_object, DeepSeek intermittently emits a malformed
    object — typically closing a nested object early and then continuing without a
    comma. It is nondeterministic: the identical prompt usually parses on the next
    try. Retrying here (rather than surfacing the parse error) keeps one unlucky roll
    from becoming a 502 for the caller. On each retry the model is shown its own
    broken output, which is a far stronger correction signal than re-asking blind.
    """
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    messages = list(base_messages)
    last_error: LLMError | None = None
    for attempt in range(max(1, attempts)):
        msg = await chat_once(
            api_key=api_key, base_url=base_url, model=model, provider=provider,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=temperature, max_tokens=max_tokens,
        )
        text = msg.get("content") or "{}"
        try:
            return parse_json_response(text)
        except LLMError as exc:
            last_error = exc
            logger.warning(
                "chat_json: malformed JSON on attempt %d/%d (%s)", attempt + 1, attempts, exc,
            )
            messages = base_messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "That response was not parseable JSON. Reply with ONLY the corrected "
                    "JSON object — no prose, no code fence, every brace and comma balanced."
                )},
            ]
    assert last_error is not None
    raise last_error


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse a model JSON-object response with small repairs for provider drift."""
    candidate = _strip_json_fence(text)
    attempts = [candidate]
    balanced = _extract_balanced_json_object(candidate)
    if balanced and balanced != candidate:
        attempts.append(balanced)

    last_error: json.JSONDecodeError | None = None
    for item in attempts:
        for repaired in (item, _escape_json_string_controls(item)):
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if not isinstance(parsed, dict):
                raise LLMError("Model returned JSON, but not a JSON object.")
            return parsed

    detail = str(last_error) if last_error else "no JSON object found"
    preview = candidate[:1200]
    raise LLMError(f"Model did not return valid JSON: {detail}\n---\n{preview}")


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced_json_object(text: str) -> str | None:
    start = (text or "").find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _escape_json_string_controls(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                changed = True
                continue
            if ch == "\r":
                out.append("\\r")
                changed = True
                continue
            if ch == "\t":
                out.append("\\t")
                changed = True
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out) if changed else text


def _parse_dsml_tool_calls(content: str) -> list[dict]:
    """Parse textual DSML tool-call markup into OpenAI-style calls."""
    if "DSML" not in (content or "") or "invoke" not in content:
        return []
    tags = list(_DSML_TAG_RE.finditer(content))
    calls: list[dict] = []
    i = 0
    while i < len(tags):
        tag = tags[i]
        if tag.group("closing") or tag.group("tag").lower() != "invoke":
            i += 1
            continue
        attrs = _parse_attrs(tag.group("attrs") or "")
        name = attrs.get("name")
        end_index = _find_matching_tag(tags, i + 1, "invoke")
        if not name or end_index is None:
            i += 1
            continue
        args = _parse_dsml_parameters(content[tag.end():tags[end_index].start()])
        calls.append({
            "id": f"dsml_call_{len(calls)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
        i = end_index + 1
    return calls


def _strip_dsml_tool_markup(content: str) -> str:
    match = _DSML_TAG_RE.search(content or "")
    return (content[:match.start()] if match else content).strip()


def _sanitize_final_content(content: str, trace: list[dict] | None = None) -> str:
    text = content or ""
    if "DSML" not in text:
        return text
    text = _strip_dsml_tool_markup(text)
    if "DSML" in text:
        text = _DSML_TAG_RE.sub("", text)
        text = re.sub(r"</?\s*\|\s*DSML\s*\|\s*\|[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
    if text:
        return text
    if trace:
        return (
            "I ran the available tools, but the model tried to issue more tool calls "
            "instead of writing the final answer. Please retry the question with a "
            "slightly narrower screen."
        )
    return ""


def _parse_dsml_parameters(body: str) -> dict[str, Any]:
    tags = list(_DSML_TAG_RE.finditer(body))
    args: dict[str, Any] = {}
    i = 0
    while i < len(tags):
        tag = tags[i]
        if tag.group("closing") or tag.group("tag").lower() != "parameter":
            i += 1
            continue
        attrs = _parse_attrs(tag.group("attrs") or "")
        name = attrs.get("name")
        end_index = _find_matching_tag(tags, i + 1, "parameter")
        if not name or end_index is None:
            i += 1
            continue
        raw_value = html.unescape(body[tag.end():tags[end_index].start()].strip())
        args[name] = _coerce_dsml_value(raw_value, attrs)
        i = end_index + 1
    return args


def _find_matching_tag(tags: list[re.Match], start: int, tag_name: str) -> int | None:
    for idx in range(start, len(tags)):
        tag = tags[idx]
        if tag.group("closing") and tag.group("tag").lower() == tag_name:
            return idx
    return None


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
    return {k: html.unescape(v) for k, v in _ATTR_RE.findall(raw_attrs or "")}


def _coerce_dsml_value(raw_value: str, attrs: dict[str, str]) -> Any:
    if attrs.get("json", "").lower() == "true" or raw_value[:1] in ("[", "{"):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            pass
    if attrs.get("integer", "").lower() == "true":
        try:
            return int(raw_value)
        except ValueError:
            return raw_value
    if attrs.get("number", "").lower() == "true":
        try:
            return float(raw_value)
        except ValueError:
            return raw_value
    if attrs.get("boolean", "").lower() == "true":
        return raw_value.strip().lower() in {"1", "true", "yes"}
    return raw_value


def _shorten_preview(obj: Any, limit: int = 280) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"
