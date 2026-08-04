"""Central bridge to the qlib alpha model's expected returns.

One place for the scanner (``routers.screener_agent``), the committee evidence node
(``ai_analyst.committee.nodes.qlib_signals_node``), and the quant router
(``routers.quant``) to obtain per-ticker expected returns without each re-loading the
model or rebuilding the panel. The latest cross-section is cached per jurisdiction with
a short TTL because building it hits the warehouse.

All calls degrade gracefully: if no model has been trained for a jurisdiction, every
ticker maps to ``None`` and callers fall back to their prior behavior.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Sequence

import threading

import pandas as pd

from . import qlib_alpha

logger = logging.getLogger("mzqa.quant.alpha_signal")

_TTL_SECONDS = float(os.environ.get("QLIB_ALPHA_CACHE_TTL", "3600"))
_CACHE: dict[str, tuple[float, pd.Series]] = {}
# Serialize builds so concurrent requests (page mount fires several at once) wait for
# one build instead of each kicking off its own expensive cross-section.
_LOCK = threading.Lock()


def latest_cross_section(jurisdiction: str = "US", *, label: str = "forward_1m", ttl: float | None = None) -> pd.Series:
    """Expected forward returns (one row per instrument) for the latest month.

    Cached per (jurisdiction, horizon label) for ``ttl`` seconds. Empty Series if no model
    exists for that horizon or the warehouse yields no recent panel.
    """
    juris = jurisdiction.upper()
    ttl = _TTL_SECONDS if ttl is None else ttl
    ckey = f"{juris}|{label}"
    hit = _CACHE.get(ckey)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    with _LOCK:
        hit = _CACHE.get(ckey)  # re-check: another thread may have just built it
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        art = qlib_alpha.get_model(juris, label)
        if art is None:
            series = pd.Series(dtype=float)
        else:
            try:
                series = qlib_alpha.predict_cross_section(art)
            except Exception:  # noqa: BLE001 - never let scoring/committee fail on this
                logger.warning("alpha cross-section failed for %s %s", juris, label, exc_info=True)
                series = pd.Series(dtype=float)
        _CACHE[ckey] = (time.time(), series)
        return series


def prewarm(jurisdictions: Sequence[str] = ("US",)) -> None:
    """Best-effort warm of the model + latest cross-section (call in a startup thread)."""
    for j in jurisdictions:
        try:
            n = len(latest_cross_section(j))
            logger.info("alpha prewarm %s: %d names", j, n)
        except Exception:  # noqa: BLE001
            logger.warning("alpha prewarm failed for %s", j, exc_info=True)


_COUNTRY_CACHE: tuple[float, dict[str, str]] | None = None


def _ticker_countries() -> dict[str, str]:
    """{primary_ticker: ISO-2 country} for the INTL universe (cached), so INTL predictions
    route each name to its country's model."""
    global _COUNTRY_CACHE
    now = time.time()
    if _COUNTRY_CACHE and (now - _COUNTRY_CACHE[0]) < _TTL_SECONDS:
        return _COUNTRY_CACHE[1]
    try:
        from xbrl_sec.sec.db.connection import connect
        with connect() as conn:
            df = pd.read_sql(
                "SELECT primary_ticker, country_code FROM dim_company_intl "
                "WHERE primary_ticker IS NOT NULL AND country_code IS NOT NULL AND country_code <> ''",
                conn,
            )
        mapping = {str(t): str(c).upper() for t, c in zip(df["primary_ticker"], df["country_code"])}
    except Exception:  # noqa: BLE001 - never let routing sink a request
        logger.warning("INTL country map load failed", exc_info=True)
        mapping = {}
    _COUNTRY_CACHE = (now, mapping)
    return mapping


def _intl_expected_returns(tickers: Sequence[str], *, label: str = "forward_1m") -> dict[str, float | None]:
    """Route each INTL ticker to its country model's latest cross-section (for this horizon)."""
    countries = _ticker_countries()
    out: dict[str, float | None] = {t: None for t in tickers}
    by_country: dict[str, list[str]] = {}
    for t in tickers:
        cc = countries.get(t)
        if cc:
            by_country.setdefault(cc, []).append(t)
    for cc, ts in by_country.items():
        cross = latest_cross_section(f"INTL:{cc}", label=label)
        if cross.empty:
            continue
        idx = cross.index
        for t in ts:
            if t in idx:
                out[t] = float(cross[t])
    return out


def expected_returns(jurisdiction: str, tickers: Sequence[str], *, label: str = "forward_1m") -> dict[str, float | None]:
    """Per-period expected return per ticker for the given horizon (``None`` where unavailable).

    INTL routes each name to its country model ("INTL:<cc>"); US/JP use the single market model.
    The value is over the horizon's period (monthly for forward_1m); annualize via `annualization`.
    """
    if jurisdiction.upper() == "INTL":
        return _intl_expected_returns(tickers, label=label)
    cross = latest_cross_section(jurisdiction, label=label)
    if cross.empty:
        return {t: None for t in tickers}
    idx = cross.index
    return {t: (float(cross[t]) if t in idx else None) for t in tickers}


def expected_returns_with_fallback(
    jurisdiction: str, tickers: Sequence[str], hist_annual: dict[str, float], *, label: str = "forward_1m",
) -> tuple[list[float], list[str]]:
    """Annualized expected return per ticker: the trained model (for this horizon) where it has a
    prediction, else the caller-supplied on-the-spot historical annualized mean.

    Returns ``(mu, sources)`` aligned to ``tickers`` — ``sources[i]`` is ``"model"`` or
    ``"historical"``. This keeps a partly-covered universe from being silently zeroed (which
    collapses a mean-variance optimizer onto the single covered name). Shared by the quant
    optimizer, the committee node, and the scanner.
    """
    er = expected_returns(jurisdiction, tickers, label=label)
    ann = (model_meta(jurisdiction, label) or {}).get("annualization", 12.0)
    mu: list[float] = []
    sources: list[str] = []
    for t in tickers:
        v = er.get(t)
        if v is not None:
            mu.append(float(v) * ann)
            sources.append("model")
        else:
            mu.append(float(hist_annual.get(t, 0.0)))
            sources.append("historical")
    return mu, sources


def model_meta(jurisdiction: str = "US", label: str = "forward_1m") -> dict | None:
    """Metadata for the persisted model (trained_at, horizon, IC), or ``None``."""
    art = qlib_alpha.get_model(jurisdiction, label)
    if art is None:
        return None
    return {
        "jurisdiction": art.jurisdiction,
        "label": art.label,
        "horizon_months": art.horizon_months,
        "annualization": art.annualization,
        "trained_at": art.trained_at,
        "train_range": art.train_range,
        "metrics": art.metrics,
        "n_features": len(art.feature_cols),
    }


def list_trained_models() -> list[dict]:
    """Rows from the quant_alpha_model ledger (US, JP, and per-country INTL). Empty when the
    ledger is missing or nothing has been trained. Used by /backends and the startup prewarm."""
    try:
        from xbrl_sec.sec.db.connection import connect
        with connect() as conn:
            df = pd.read_sql(
                "SELECT model_key, jurisdiction, country_code, label, rank_ic, coverage_count, "
                "trained_at, next_due FROM quant_alpha_model ORDER BY jurisdiction, country_code NULLS FIRST",
                conn,
            )
        return [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in df.to_dict("records")]
    except Exception:  # noqa: BLE001 - ledger optional; never break discovery on it
        return []


def clear_cache() -> None:
    _CACHE.clear()
