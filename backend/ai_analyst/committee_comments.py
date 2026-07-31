from __future__ import annotations

import re
from typing import Any


CORE_ROLES = {"advocate", "challenger", "auditor", "lead", "base"}


def build_specialist_comments(state: dict[str, Any], *, max_bullets: int = 4) -> list[dict[str, Any]]:
    metadata = _analyst_metadata(state)
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()

    for verdict in state.get("specialist_verdicts") or []:
        if not isinstance(verdict, dict):
            continue
        key = _clean_key(verdict.get("analyst_key"))
        if not key or key in CORE_ROLES or key in seen:
            continue
        bullets = _bullets_from_verdict(verdict, max_bullets)
        if not bullets:
            continue
        meta = metadata.get(key, {})
        comments.append({
            "analyst_key": key,
            "analyst": str(verdict.get("analyst") or meta.get("name") or key.replace("_", " ").title()),
            "origin": meta.get("origin") or "specialist",
            "focus": meta.get("focus") or None,
            "confidence": verdict.get("confidence"),
            "bullets": bullets,
        })
        seen.add(key)

    latest: dict[str, str] = {}
    order: list[str] = []
    for item in state.get("committee_chat_history") or []:
        if not isinstance(item, dict):
            continue
        key = _clean_key(item.get("role"))
        if not key or key in CORE_ROLES:
            continue
        if key not in latest:
            order.append(key)
        latest[key] = str(item.get("content") or "").strip()

    for key in order:
        if key in seen:
            continue
        bullets = _bullets_from_text(latest.get(key) or "", max_bullets)
        if not bullets:
            continue
        meta = metadata.get(key, {})
        comments.append({
            "analyst_key": key,
            "analyst": meta.get("name") or key.replace("_", " ").title(),
            "origin": meta.get("origin") or "specialist",
            "focus": meta.get("focus") or None,
            "confidence": None,
            "bullets": bullets,
        })
        seen.add(key)

    return comments


def _analyst_metadata(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    cfg = state.get("config") or {}
    for item in cfg.get("extra_analysts") or []:
        if not isinstance(item, dict):
            continue
        key = _clean_key(item.get("key") or item.get("name"))
        if not key:
            continue
        out[key] = {
            "name": str(item.get("name") or key.replace("_", " ").title()),
            "origin": str(item.get("origin") or "custom"),
            "focus": str(item.get("focus") or "").strip() or None,
        }
    return out


def _bullets_from_verdict(verdict: dict[str, Any], max_bullets: int) -> list[str]:
    bullets: list[str] = []
    thesis = _first_sentence(verdict.get("thesis"))
    if thesis:
        bullets.append(thesis)
    for item in verdict.get("sensitivity_adjustments") or []:
        text = _dict_summary(item, ("metric", "direction", "rationale", "stressed_value"))
        if text:
            bullets.append(text)
    for item in verdict.get("peer_comparison_metrics") or []:
        text = _dict_summary(item, ("metric", "comparison_group", "spread", "rationale"))
        if text:
            bullets.append(text)
    for flag in verdict.get("risk_flags") or []:
        text = _short(flag)
        if text:
            bullets.append(f"Risk: {text}")
    return _dedupe(bullets)[:max_bullets]


def _bullets_from_text(text: str, max_bullets: int) -> list[str]:
    clean = _strip_markdown(text)
    line_bullets = []
    for line in clean.splitlines():
        line = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if 24 <= len(line) <= 220 and not line.endswith(":"):
            line_bullets.append(line)
    if line_bullets:
        return _dedupe([_short(line) for line in line_bullets if _short(line)])[:max_bullets]
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", clean))
    return _dedupe([_short(s) for s in sentences if _short(s)])[:max_bullets]


def _dict_summary(item: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(item, dict):
        return _short(item)
    parts = []
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    return _short("; ".join(parts))


def _first_sentence(value: Any) -> str | None:
    text = _short(value, limit=360)
    if not text:
        return None
    return re.split(r"(?<=[.!?])\s+", text)[0]


def _short(value: Any, limit: int = 220) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    return text[:limit].rstrip()


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"#{1,6}\s*", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text.strip()


def _dedupe(items: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = (item or "").strip()
        key = text.lower()
        if text and key not in seen:
            out.append(text)
            seen.add(key)
    return out


def _clean_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
