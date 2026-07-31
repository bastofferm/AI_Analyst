"""Lazy FinBERT scorer so ingestion works without the ML runtime installed."""
from __future__ import annotations

from functools import lru_cache

from xbrl_sec.sec.local_deps import add_project_deps
from xbrl_sec.sec.settings import load_settings


@lru_cache(maxsize=1)
def _pipeline():
    add_project_deps()
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError(
            "FinBERT scoring requires transformers. Install transformers>=4.40 "
            "into the MZQA Python environment or run news ingest --no-score."
        ) from exc
    settings = load_settings()
    return pipeline(
        "text-classification",
        model=settings.news_finbert_model,
        tokenizer=settings.news_finbert_model,
        truncation=True,
    )


def score(text: str) -> dict[str, object]:
    result = _pipeline()(text[:6000])[0]
    label = str(result["label"]).casefold()
    if label not in {"positive", "neutral", "negative"}:
        raise RuntimeError(f"Unexpected FinBERT label: {label}")
    return {"label": label, "score": float(result["score"]), "rationale": None}
