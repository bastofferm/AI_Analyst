"""Macro + news/sentiment context for the committee's feedback loop.

- ``macro_context``: key US rates / USD / inflation / growth prints, the current
  growth-inflation regime quadrant, and the latest generated macro story.
- ``news_summary``: aggregated ticker sentiment + recent scored headlines from
  ``news.sentiment_scores`` (+ ``news.articles``); empty-safe.
- ``ensure_news``: best-effort pre-step that adds the ticker to ``news.watchlist``
  and ingests + DeepSeek-scores fresh articles (used by the runner, not the node).

The lead node uses ``macro_signal`` to nudge scenario probabilities dynamically.
"""
from __future__ import annotations

from typing import Any

from .._db import read_sql

_KEY_SERIES = {
    "FRED:DGS10": "US 10Y Treasury",
    "FRED:DGS2": "US 2Y Treasury",
    "FRED:DFF": "Fed Funds",
    "ATLFED:GDPNOW": "Atlanta Fed GDPNow",
    "FRED:CPILFESL": "US Core CPI (index)",
    "FRED:DEXUSEU": "USD/EUR",
}


def macro_context() -> dict[str, Any]:
    series = _macro_series()
    regime = _regime()
    story = _macro_story()
    signal = _macro_signal(series, regime)
    return {"available": bool(series or regime), "series": series, "regime": regime,
            "story": story, "signal": signal}


def _macro_series() -> list[dict[str, Any]]:
    df = read_sql(
        "SELECT series_id, name, category, units, latest_value, latest_date "
        "FROM v_macro_latest WHERE series_id = ANY(%(ids)s)",
        {"ids": list(_KEY_SERIES)},
    )
    rows = []
    for r in df.to_dict("records"):
        rows.append({
            "series_id": r["series_id"], "label": _KEY_SERIES.get(r["series_id"], r.get("name")),
            "value": _num(r.get("latest_value")), "units": r.get("units"),
            "date": r["latest_date"].isoformat() if hasattr(r.get("latest_date"), "isoformat") else str(r.get("latest_date")),
        })
    order = list(_KEY_SERIES)
    rows.sort(key=lambda x: order.index(x["series_id"]) if x["series_id"] in order else 99)
    return rows


def _regime() -> dict[str, Any] | None:
    # Prefer the flagged current quarter, but only if it actually has a quadrant —
    # the latest-dated row is often a provisional quarter with NULL quadrant. Fall
    # back to the most recent quarter that carries a classified quadrant.
    df = read_sql(
        "SELECT period_end, quadrant, growth_z, inflation_z, growth_value, inflation_value "
        "FROM fact_macro_regime WHERE jurisdiction='US' AND quadrant IS NOT NULL "
        "ORDER BY is_current DESC, period_end DESC LIMIT 1"
    )
    if df.empty:  # no classified quadrant anywhere → derive from the latest z-scored row
        df = read_sql(
            "SELECT period_end, quadrant, growth_z, inflation_z, growth_value, inflation_value "
            "FROM fact_macro_regime WHERE jurisdiction='US' AND growth_z IS NOT NULL "
            "ORDER BY period_end DESC LIMIT 1"
        )
    if df.empty:
        return None
    r = df.iloc[0].to_dict()
    quad = r.get("quadrant") or _derive_quadrant(_num(r.get("growth_z")), _num(r.get("inflation_z")))
    return {
        "quadrant": quad,
        "period_end": r["period_end"].isoformat() if hasattr(r.get("period_end"), "isoformat") else str(r.get("period_end")),
        "growth_value": _num(r.get("growth_value")), "inflation_value": _num(r.get("inflation_value")),
    }


def _derive_quadrant(g: float | None, i: float | None) -> str | None:
    if g is None or i is None:
        return None
    if g >= 0 and i < 0: return "Goldilocks"
    if g >= 0 and i >= 0: return "Reflation"
    if g < 0 and i >= 0: return "Stagflation"
    return "Deflation"


def _macro_story() -> str | None:
    df = read_sql(
        "SELECT text FROM fact_macro_story WHERE lang='en' ORDER BY generated_at DESC LIMIT 1"
    )
    if df.empty:
        return None
    return str(df.iloc[0]["text"] or "")[:2500] or None


def _macro_signal(series: list[dict[str, Any]], regime: dict[str, Any] | None) -> dict[str, Any]:
    """Coarse directional tilt for scenario weighting (rates + regime)."""
    by = {s["series_id"]: s.get("value") for s in series}
    ten, two, ff = by.get("FRED:DGS10"), by.get("FRED:DGS2"), by.get("FRED:DFF")
    curve = (ten - two) if (ten is not None and two is not None) else None
    easing = (ff is not None and ten is not None and ff > ten)  # inverted policy vs 10Y → easing bias
    quad = (regime or {}).get("quadrant")
    tilt = "neutral"
    if quad in ("Goldilocks", "Reflation") or easing:
        tilt = "supportive"       # supports higher multiples / up-weights the upside case
    elif quad in ("Stagflation",):
        tilt = "cautious"
    return {"tilt": tilt, "regime_quadrant": quad, "yield_curve_2s10s": round(curve, 2) if curve is not None else None,
            "policy_easing_bias": easing, "ten_year": ten}


# ------------------------------------------------------------------- news

def news_summary(ticker: str, limit: int = 12) -> dict[str, Any]:
    try:
        scores = read_sql(
            """
            SELECT s.label, s.score, s.rationale, s.scored_at, a.title, a.url, a.published_at
            FROM news.sentiment_scores s
            LEFT JOIN news.articles a ON a.id = s.article_id
            WHERE UPPER(s.ticker) = UPPER(%(t)s)
            ORDER BY COALESCE(a.published_at, s.scored_at) DESC
            LIMIT %(n)s
            """,
            {"t": ticker, "n": limit},
        )
    except Exception:  # noqa: BLE001 - schema drift / missing join column
        try:
            scores = read_sql(
                "SELECT label, score, rationale, scored_at FROM news.sentiment_scores "
                "WHERE UPPER(ticker)=UPPER(%(t)s) ORDER BY scored_at DESC LIMIT %(n)s",
                {"t": ticker, "n": limit},
            )
        except Exception:  # noqa: BLE001
            return {"available": False, "note": "news schema unavailable"}
    if scores.empty:
        return {"available": False, "note": "no scored news in warehouse for ticker (macro-led fallback)"}
    recs = scores.to_dict("records")
    vals = [_num(r.get("score")) for r in recs if _num(r.get("score")) is not None]
    avg = round(sum(vals) / len(vals), 2) if vals else None
    labels = [str(r.get("label") or "").lower() for r in recs]
    return {
        "available": True,
        "article_count": len(recs),
        "avg_sentiment": avg,
        "label_mix": {k: labels.count(k) for k in set(labels)},
        "headlines": [{"title": r.get("title"), "label": r.get("label"),
                       "published": _iso(r.get("published_at") or r.get("scored_at"))} for r in recs[:8]],
    }


def ensure_news(ticker: str, backend: str = "deepseek", limit: int = 40) -> dict[str, Any]:
    """Best-effort: watchlist the ticker, ingest feeds, DeepSeek-score. Runner pre-step."""
    out: dict[str, Any] = {"ticker": ticker.upper()}
    try:
        from xbrl_sec.sec.db.connection import connect
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news.watchlist (ticker, market, enabled) VALUES (%s,'US',TRUE) "
                "ON CONFLICT (ticker) DO UPDATE SET enabled=TRUE",
                (ticker.upper(),),
            )
            conn.commit()
        out["watchlisted"] = True
    except Exception as exc:  # noqa: BLE001
        out["watchlist_error"] = str(exc)[:200]
    try:
        from xbrl_sec.sec.news.ingest import ingest_feeds, score_pending_articles
        out["ingest"] = ingest_feeds(limit=limit)
        out["score"] = score_pending_articles(backend=backend, limit=limit)
    except Exception as exc:  # noqa: BLE001
        out["ingest_error"] = str(exc)[:200]
    return out


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)
