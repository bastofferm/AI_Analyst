"""Reasoning backend contract."""
from __future__ import annotations

from typing import Protocol


class ReasoningBackend(Protocol):
    model_key: str

    def reason(self, *, ticker: str, title: str, text: str) -> dict[str, object]:
        ...
