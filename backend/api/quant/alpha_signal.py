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


def latest_cross_section(jurisdiction: str = "US", *, ttl: float | None = None) -> pd.Series:
    """Expected forward returns (one row per instrument) for the latest month.

    Cached per jurisdiction for ``ttl`` seconds. Empty Series if no model exists or the
    warehouse yields no recent panel.
    """
    juris = jurisdiction.upper()
    ttl = _TTL_SECONDS if ttl is None else ttl
    hit = _CACHE.get(juris)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    with _LOCK:
        hit = _CACHE.get(juris)  # re-check: another thread may have just built it
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        art = qlib_alpha.get_model(juris)
        if art is None:
            series = pd.Series(dtype=float)
        else:
            try:
                series = qlib_alpha.predict_cross_section(art)
            except Exception:  # noqa: BLE001 - never let scoring/committee fail on this
                logger.warning("alpha cross-section failed for %s", juris, exc_info=True)
                series = pd.Series(dtype=float)
        _CACHE[juris] = (time.time(), series)
        return series


def prewarm(jurisdictions: Sequence[str] = ("US",)) -> None:
    """Best-effort warm of the model + latest cross-section (call in a startup thread)."""
    for j in jurisdictions:
        try:
            n = len(latest_cross_section(j))
            logger.info("alpha prewarm %s: %d names", j, n)
        except Exception:  # noqa: BLE001
            logger.warning("alpha prewarm failed for %s", j, exc_info=True)


def expected_returns(jurisdiction: str, tickers: Sequence[str]) -> dict[str, float | None]:
    """Monthly expected return per ticker (``None`` where unavailable)."""
    cross = latest_cross_section(jurisdiction)
    if cross.empty:
        return {t: None for t in tickers}
    idx = cross.index
    return {t: (float(cross[t]) if t in idx else None) for t in tickers}


def model_meta(jurisdiction: str = "US") -> dict | None:
    """Metadata for the persisted model (trained_at, horizon, IC), or ``None``."""
    art = qlib_alpha.get_model(jurisdiction)
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


def clear_cache() -> None:
    _CACHE.clear()
