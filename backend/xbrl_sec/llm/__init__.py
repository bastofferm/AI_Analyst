"""Unified LangChain-backed LLM layer for MZQA pipelines.

Pipeline-LLM-Aufrufe gehen über diese Schicht (Chat-Modell + Pydantic-Schemas +
Prompt-Registry + SQLite-Cache). Provider-Auswahl läuft über
``make_chat_model``/``make_reasoning_model`` aus ``factory`` — ``ChatDeepSeek``
bleibt als DeepSeek-Alias erhalten. Die Chatbot-UI-Runtimes in ai_analyst/ und
api/ai/ bleiben unangetastet, weil ihr DSML-Markup ein UI-Vertrag ist.
"""
from __future__ import annotations

import llm_providers

from xbrl_sec.llm.callbacks import is_langsmith_enabled, setup_llm_cache

DEFAULT_BASE_URL = llm_providers.PROVIDERS["deepseek"].base_url
DEFAULT_CHAT_MODEL = llm_providers.PROVIDERS["deepseek"].chat_model

__all__ = [
    "ChatDeepSeek",
    "ChatOpenAICompat",
    "DEFAULT_BASE_URL",
    "DEFAULT_CHAT_MODEL",
    "is_langsmith_enabled",
    "make_chat_model",
    "make_reasoning_model",
    "setup_llm_cache",
]


def __getattr__(name: str):
    if name in ("ChatDeepSeek", "ChatOpenAICompat"):
        from xbrl_sec.llm import chat_deepseek

        return getattr(chat_deepseek, name)
    if name in ("make_chat_model", "make_reasoning_model"):
        from xbrl_sec.llm import factory

        return getattr(factory, name)
    raise AttributeError(name)
