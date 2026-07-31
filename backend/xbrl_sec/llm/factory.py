"""Provider-aware LangChain chat-model factory for the committee tribunal.

The tribunal leans on ``with_structured_output`` (agent theses, specialist
verdicts, scenario extraction, group ranking), so each provider needs a real
LangChain chat model rather than a raw HTTP call:

* OpenAI dialect (DeepSeek / OpenAI / Moonshot / Gemini) → ``ChatOpenAICompat``
  from ``chat_deepseek.py``.
* Claude → ``ChatAnthropic`` from ``langchain-anthropic``, which speaks the
  Messages API and maps tool calls correctly.

Sampling parameters are dropped for Claude on purpose: Opus 4.6+ and Sonnet 5
return a 400 for ``temperature``/``top_p``/``top_k``.
"""
from __future__ import annotations

from typing import Any

import llm_providers

# Non-streaming ceiling recommended by the Claude API docs; thinking shares the
# same budget as the visible answer, so a thinking request needs the headroom.
_THINKING_MAX_TOKENS = 16000


def make_chat_model(
    provider: str | None = None,
    model: str | None = None,
    *,
    api_key: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    timeout: float = 120.0,
    thinking: bool = False,
) -> Any:
    """Build a LangChain chat model for ``provider``.

    ``thinking`` enables adaptive thinking where the provider supports it
    (Claude today) and raises ``max_tokens`` so the answer is not squeezed out
    by the reasoning budget. Effort is left at the API default (``high``);
    ``langchain-anthropic`` has no first-class ``output_config`` field and
    shunting it through ``model_kwargs`` only emits a warning for a no-op.
    """
    prov = llm_providers.get(provider)
    resolved_model = llm_providers.chat_model(prov.id, model)

    if prov.dialect == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max(max_tokens, _THINKING_MAX_TOKENS) if thinking else max_tokens,
            "timeout": timeout,
            "base_url": llm_providers.resolve_base_url(prov.id),
            # Never pass temperature/top_p/top_k — current Claude models 400 on them.
        }
        key = api_key or llm_providers.resolve_env_key(prov.id)
        if key:
            kwargs["api_key"] = key
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        return ChatAnthropic(**kwargs)

    from .chat_deepseek import ChatOpenAICompat

    return ChatOpenAICompat(
        provider=prov.id,
        model=resolved_model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        api_key=api_key or None,
    )


def make_reasoning_model(
    provider: str | None = None,
    model: str | None = None,
    *,
    api_key: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 3200,
    timeout: float = 120.0,
) -> Any:
    """Deep-reasoning tier (committee narrative, memo, lead synthesis)."""
    prov = llm_providers.get(provider)
    return make_chat_model(
        prov.id,
        llm_providers.reasoner_model(prov.id, model),
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        thinking=prov.dialect == "anthropic",
    )
