"""Article-level FinBERT and optional reasoning scoring."""
from __future__ import annotations

from typing import Any

from . import finbert
from .backends import get_backend


def _validated(result: dict[str, Any]) -> dict[str, object]:
    label = str(result.get("label") or "").casefold()
    if label not in {"positive", "neutral", "negative"}:
        raise RuntimeError(f"Invalid sentiment label: {label!r}")
    score = min(1.0, max(0.0, float(result.get("score", 0.0))))
    return {
        "label": label,
        "score": score,
        "rationale": str(result.get("rationale") or "").strip() or None,
    }


def score_article(
    *,
    ticker: str,
    title: str,
    text: str,
    fast_lane: bool,
    backend_name: str,
) -> list[tuple[str, dict[str, object]]]:
    results = [("finbert", _validated(finbert.score(f"{title}\n{text}")))]
    if fast_lane:
        backend = get_backend(backend_name)
        results.append((
            backend.model_key,
            _validated(backend.reason(ticker=ticker, title=title, text=text)),
        ))
    return results
