"""LLM-Layer-Infrastruktur: SQLite-Cache + LangSmith-Toggle.

Wird einmalig pro Prozess vor dem ersten LLM-Call aufgerufen.
"""
from __future__ import annotations

import os
from pathlib import Path

_CACHE_INSTALLED = False


def cache_path() -> Path:
    root = os.environ.get("MZQA_ROOT") or os.getcwd()
    return Path(root) / ".cache" / "llm" / "deepseek_cache.sqlite"


def setup_llm_cache() -> None:
    """Aktiviert den globalen SQLite-Cache für LangChain-LLM-Calls (idempotent)."""
    global _CACHE_INSTALLED
    if _CACHE_INSTALLED:
        return
    try:
        from langchain_core.caches import BaseCache
        from langchain_core.globals import set_llm_cache
    except ImportError:
        return

    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    cache = _build_sqlite_cache(str(path))
    if cache is not None:
        set_llm_cache(cache)
        _CACHE_INSTALLED = True


def _build_sqlite_cache(database_path: str):
    """SQLiteCache lebt im langchain_community-Paket, das wir absichtlich nicht
    pinnen. Versuche zunächst den optionalen Import, sonst fällt der Cache
    stillschweigend aus (kein Funktionsbruch, nur kein Speedup)."""
    try:
        from langchain_community.cache import SQLiteCache  # type: ignore[import-not-found]

        return SQLiteCache(database_path=database_path)
    except ImportError:
        return None


def is_langsmith_enabled() -> bool:
    flag = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    return str(flag).lower() in {"1", "true", "yes"}
