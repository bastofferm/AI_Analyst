"""Provider registry for the multi-provider LLM layer.

Lives at the backend sys.path root (``.claude/launch.json`` puts ``backend`` on
PYTHONPATH and there is no ``backend/__init__.py``) so all three packages can
``import llm_providers``:

* ``api.ai.llm_runtime``      — async HTTP runtime used by the FastAPI routers
* ``ai_analyst.llm_runtime``  — sync twin used by the committee engine
* ``xbrl_sec.llm``            — LangChain chat models used by the tribunal

Deliberately dependency-free (stdlib only) so importing it can never drag a
provider SDK into a process that does not need one.

Two wire dialects:

* ``openai``    — DeepSeek, OpenAI, Moonshot and Gemini all serve
  ``POST {base_url}/chat/completions`` with the same message/tool shape. Gemini
  goes through Google's OpenAI-compatibility endpoint rather than the native
  ``generateContent`` API so there is a single code path. ``Caps`` captures the
  places the dialect is *not* uniform (sampling params, JSON mode, the
  max-tokens field name).
* ``anthropic`` — Claude has no OpenAI-compatible endpoint and is served by a
  dedicated adapter built on the official ``anthropic`` SDK.

``model_hints`` are UI conveniences only. The host allowlist is the security
boundary (it is what stops a user-supplied ``base_url`` from walking off with
the API key); model IDs are never prefix-checked, so any model the provider
serves can be typed in by hand. The frontend can also pull a provider's live
catalogue via ``GET /api/llm/models``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

DEFAULT_PROVIDER = "deepseek"
DEFAULT_PROVIDER_ENV = "AI_ANALYST_LLM_PROVIDER"


class LLMProviderError(ValueError):
    """Raised for an unknown provider or a request that escapes its allowlist."""


@dataclass(frozen=True)
class Caps:
    """Where the OpenAI dialect is not actually uniform across providers."""

    temperature: bool = True          # False → drop temperature/top_p/top_k (Claude 4.6+ 400s on them)
    json_object: bool = True          # False → ask for JSON in the prompt instead of response_format
    max_tokens_field: str = "max_tokens"
    tools: bool = True
    # Reasoning models (Kimi) spend the max_tokens budget on hidden `reasoning_content`
    # BEFORE emitting the answer; a normal budget (e.g. 900/2400) is consumed entirely by
    # reasoning (finish_reason="length") and `content` comes back empty → "{}". Two guards:
    #  * disable_thinking → send `{"thinking": {"type": "disabled"}}` so the model answers
    #    directly (fast, no empty content, no timeout). This is the primary fix for Kimi.
    #  * min_max_tokens → floor the budget so that IF a variant still reasons, reasoning +
    #    the answer both fit rather than truncating to empty. Safety net. 0 = no floor.
    disable_thinking: bool = False
    min_max_tokens: int = 0


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    dialect: str                      # "openai" | "anthropic"
    base_url: str
    hosts: frozenset[str]
    env: tuple[str, ...]
    chat_model: str
    reasoner_model: str
    key_placeholder: str = "sk-…"
    docs_url: str = ""
    caps: Caps = field(default_factory=Caps)
    model_hints: tuple[str, ...] = ()

    def models(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in (self.chat_model, self.reasoner_model, *self.model_hints):
            if m and m not in seen:
                seen.append(m)
        return tuple(seen)


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        id="deepseek",
        label="DeepSeek",
        dialect="openai",
        base_url="https://api.deepseek.com/v1",
        hosts=frozenset({"api.deepseek.com"}),
        env=("DEEPSEEK_API_KEY",),
        chat_model="deepseek-v4-flash",
        reasoner_model="deepseek-v4-pro",
        key_placeholder="sk-…",
        docs_url="https://platform.deepseek.com/api_keys",
        caps=Caps(),
        model_hints=("deepseek-chat", "deepseek-reasoner"),
    ),
    "openai": Provider(
        id="openai",
        label="ChatGPT (OpenAI)",
        dialect="openai",
        base_url="https://api.openai.com/v1",
        hosts=frozenset({"api.openai.com"}),
        env=("OPENAI_API_KEY", "CHATGPT_API_KEY"),
        chat_model="gpt-5",
        reasoner_model="gpt-5",
        key_placeholder="sk-proj-…",
        docs_url="https://platform.openai.com/api-keys",
        # Reasoning-tier models reject `temperature` and want max_completion_tokens.
        caps=Caps(temperature=False, max_tokens_field="max_completion_tokens"),
        model_hints=("gpt-5-mini", "gpt-4.1"),
    ),
    "anthropic": Provider(
        id="anthropic",
        label="Claude (Anthropic)",
        dialect="anthropic",
        base_url="https://api.anthropic.com",
        hosts=frozenset({"api.anthropic.com"}),
        env=("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
        chat_model="claude-opus-4-8",
        reasoner_model="claude-opus-4-8",
        key_placeholder="sk-ant-…",
        docs_url="https://platform.claude.com/settings/keys",
        # Opus 4.6+ / Sonnet 5 return 400 on temperature/top_p/top_k, and JSON
        # mode is a schema-bound output_config rather than a bare json_object.
        caps=Caps(temperature=False, json_object=False),
        model_hints=("claude-sonnet-5", "claude-haiku-4-5"),
    ),
    "moonshot": Provider(
        id="moonshot",
        label="Moonshot (Kimi)",
        dialect="openai",
        base_url="https://api.moonshot.ai/v1",
        hosts=frozenset({"api.moonshot.ai", "api.moonshot.cn"}),
        env=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        # The legacy moonshot-v1-* generation is retired — the live /v1/models
        # catalogue now serves only the Kimi K2.6/K3 line (a bare moonshot-v1-32k
        # returns 404 "Not found the model"). K3 is the newest flagship; K2.6 the
        # cheaper workhorse. The two -code variants are offered as hints.
        chat_model="kimi-k2.6",
        reasoner_model="kimi-k3",
        key_placeholder="sk-…",
        docs_url="https://platform.moonshot.ai/console/api-keys",
        # These models only accept temperature=1, and 400 on any other value
        # ("only 1 is allowed for this model"), so drop sampling params entirely
        # and let the server use its default. They are also reasoning models whose
        # hidden reasoning eats the completion budget (a bare 2400 truncates mid-reasoning
        # and returns empty content). We turn reasoning OFF for these calls
        # (disable_thinking → fast, direct answer); min_max_tokens is a safety floor for
        # any variant that ignores the flag.
        caps=Caps(temperature=False, disable_thinking=True, min_max_tokens=16000),
        model_hints=("kimi-k2.7-code", "kimi-k2.7-code-highspeed"),
    ),
    "gemini": Provider(
        id="gemini",
        label="Gemini (Google)",
        dialect="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        hosts=frozenset({"generativelanguage.googleapis.com"}),
        env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        chat_model="gemini-2.5-flash",
        reasoner_model="gemini-2.5-pro",
        key_placeholder="AIza…",
        docs_url="https://aistudio.google.com/apikey",
        caps=Caps(),
        model_hints=("gemini-2.0-flash",),
    ),
}


# --------------------------------------------------------------------- lookup

def normalize_id(provider: str | None) -> str:
    """Map a user/wire value to a known provider id (empty → server default)."""
    pid = (provider or "").strip().lower()
    if not pid:
        return default_provider_id()
    if pid in PROVIDERS:
        return pid
    # Friendly aliases so the wire contract tolerates the obvious spellings.
    alias = {
        "chatgpt": "openai",
        "gpt": "openai",
        "claude": "anthropic",
        "kimi": "moonshot",
        "google": "gemini",
        "google-gemini": "gemini",
    }.get(pid)
    if alias:
        return alias
    raise LLMProviderError(
        f"Unknown LLM provider {provider!r}. Known providers: {', '.join(sorted(PROVIDERS))}."
    )


def default_provider_id() -> str:
    """Server-side default provider (``AI_ANALYST_LLM_PROVIDER``, else DeepSeek)."""
    pid = (os.environ.get(DEFAULT_PROVIDER_ENV) or "").strip().lower()
    return pid if pid in PROVIDERS else DEFAULT_PROVIDER


def get(provider: str | None) -> Provider:
    return PROVIDERS[normalize_id(provider)]


# ----------------------------------------------------------------------- keys

def resolve_env_key_source(provider: str | None = None) -> tuple[str, str, str]:
    """Locate an API key for ``provider``, reporting where it came from.

    Returns ``(key, var_name, origin)`` — origin is "process" for a variable in
    this process's environment, "windows-user" for one read straight out of
    ``HKCU\\Environment``, or "" when nothing was found.

    The registry pass matters on Windows: a key added through the System
    Properties dialog does not reach an already-running process's environment,
    so without it the user would have to restart the backend (or the whole
    terminal) before a freshly-set variable was visible.

    Each provider accepts several names — the vendor-canonical one first, then
    the shorter aliases people actually tend to set (CLAUDE_API_KEY,
    CHATGPT_API_KEY, KIMI_API_KEY). First match wins.
    """
    names = get(provider).env
    for name in names:
        value = os.environ.get(name)
        if value:
            return value, name, "process"
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    if val:
                        return str(val), name, "windows-user"
                except FileNotFoundError:
                    continue
    except Exception:  # noqa: BLE001 - registry access is best-effort
        pass
    return "", "", ""


def resolve_env_key(provider: str | None = None) -> str:
    """API key for ``provider`` from the environment / Windows user variables."""
    return resolve_env_key_source(provider)[0]


def providers_with_env_key() -> list[str]:
    return [pid for pid in PROVIDERS if resolve_env_key(pid)]


# ----------------------------------------------------------------- validation

def resolve_base_url(provider: str | None, base_url: str | None = None) -> str:
    """Return the effective base URL, validating any caller-supplied override."""
    prov = get(provider)
    url = (base_url or "").strip() or prov.base_url
    _validate_host(prov, url)
    return url.rstrip("/")


def validate(provider: str | None, base_url: str | None, model: str | None = None) -> Provider:
    """Validate a request against the provider allowlist.

    Only the host is pinned — the model is free-form on purpose so any model the
    provider serves can be selected without a code change.
    """
    prov = get(provider)
    _validate_host(prov, (base_url or "").strip() or prov.base_url)
    if model is not None and not str(model).strip():
        raise LLMProviderError(f"{prov.label}: model must not be empty.")
    return prov


def _validate_host(prov: Provider, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in prov.hosts:
        allowed = " or ".join(sorted(prov.hosts))
        raise LLMProviderError(
            f"{prov.label} requests must go to https://{allowed} — refusing to send the "
            f"API key to {url!r}."
        )


# ---------------------------------------------------------------------- models

def clamp_max_tokens(provider: str | None, requested: int) -> int:
    """Raise a completion budget to the provider's floor for reasoning models.

    Kimi spends the budget on hidden `reasoning_content` before answering, so a small
    `max_tokens` is consumed by reasoning and the answer comes back empty. Floor it (per
    `Caps.min_max_tokens`) so both fit. Non-reasoning providers (floor 0) are unchanged.
    """
    floor = get(provider).caps.min_max_tokens
    return max(requested, floor) if floor else requested


def chat_model(provider: str | None, model: str | None = None) -> str:
    return (model or "").strip() or get(provider).chat_model


def reasoner_model(provider: str | None, model: str | None = None) -> str:
    return (model or "").strip() or get(provider).reasoner_model


def describe(provider_ids: Iterable[str] | None = None) -> list[dict]:
    """JSON-safe registry view for ``GET /api/llm/providers``."""
    ids = list(provider_ids) if provider_ids is not None else list(PROVIDERS)
    out: list[dict] = []
    for pid in ids:
        prov = PROVIDERS[pid]
        key, var_name, origin = resolve_env_key_source(pid)
        out.append({
            "id": prov.id,
            "label": prov.label,
            "dialect": prov.dialect,
            "base_url": prov.base_url,
            "chat_model": prov.chat_model,
            "reasoner_model": prov.reasoner_model,
            "models": list(prov.models()),
            "key_placeholder": prov.key_placeholder,
            "docs_url": prov.docs_url,
            "env_names": list(prov.env),
            "has_server_key": bool(key),
            # Which variable supplied it, so the UI can name the exact thing to
            # fix when the key turns out to be wrong. The key itself never leaves
            # the server through this endpoint.
            "server_key_env": var_name or None,
            "server_key_origin": origin or None,
        })
    return out
