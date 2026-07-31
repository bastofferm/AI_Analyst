"""Identifier normalization helpers for Japanese listed companies."""
from __future__ import annotations

import re
from typing import Any


_JP_CODE_RE = re.compile(r"[0-9A-Z]{1,5}")


def normalize_jp_ticker_code(value: Any) -> str | None:
    """Return a JPX issue code without exchange suffix.

    EDINET and JPX often expose listed equity codes as five-character values
    with a trailing zero, e.g. ``72030`` or ``297A0``.  The trading ticker is
    the four-character issue code: ``7203.T`` or ``297A.T``.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.lower() in {"", "nan", "-"}:
        return None
    if re.fullmatch(r"\d+(\.0+)?", text):
        text = str(int(float(text)))
    text = text.replace(".T", "")
    if len(text) == 5 and text.endswith("0"):
        text = text[:-1]
    if not _JP_CODE_RE.fullmatch(text):
        return None
    return text.zfill(4)


def normalize_jp_primary_ticker(value: Any) -> str | None:
    code = normalize_jp_ticker_code(value)
    return f"{code}.T" if code else None
