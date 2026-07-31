"""Reasoning backend factory."""
from __future__ import annotations

from .base import ReasoningBackend
from .deepseek import DeepSeekBackend
from .qwen_ollama import QwenOllamaBackend


def get_backend(name: str) -> ReasoningBackend:
    normalized = name.strip().casefold()
    if normalized in {"qwen", "qwen_ollama", "ollama"}:
        return QwenOllamaBackend()
    if normalized == "deepseek":
        return DeepSeekBackend()
    raise ValueError(f"Unknown news reasoning backend: {name}")


__all__ = ["ReasoningBackend", "get_backend"]
