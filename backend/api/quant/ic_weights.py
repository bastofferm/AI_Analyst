"""Regime-conditioned factor-IC weights for the value+sentiment composite score.

Turns the research table ``fact_equity_factor_ic_regime`` (per-metric Spearman IC vs
forward returns, conditioned on the macro regime — produced by
``xbrl_sec.sec.cycle.ic.compute_regime_factor_ic``) into a *relative emphasis* per
metric family (value / growth / quality). The scanner uses this to split its
fundamental score budget by how predictive each family has actually been in the
current regime, instead of the historical hard-coded constants.

Emphasis = normalized mean |IC| across the family's metrics at the latest date
(magnitude, because the composite already encodes each metric's economic direction).
Returns ``None`` on any missing data so callers fall back to the fixed weights.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Sequence

logger = logging.getLogger("mzqa.quant.ic_weights")

DEFAULT_FAMILIES: tuple[str, ...] = ("value", "growth", "quality")

_TTL_SECONDS = float(os.environ.get("QLIB_IC_WEIGHTS_CACHE_TTL", "3600"))
_CACHE: dict[str, tuple[float, dict[str, float] | None]] = {}


def family_weights(
    jurisdiction: str = "US",
    *,
    families: Sequence[str] = DEFAULT_FAMILIES,
    window: str = "1m",
    ttl: float | None = None,
) -> dict[str, float] | None:
    """Normalized emphasis per family in the current regime, or ``None``.

    Cached per (jurisdiction, window). The result sums to 1 over ``families`` present
    with a positive signal; families with no data are omitted.
    """
    key = f"{jurisdiction.upper()}:{window}:{','.join(families)}"
    ttl = _TTL_SECONDS if ttl is None else ttl
    hit = _CACHE.get(key)
    now = time.time()
    if hit and (now - hit[0]) < ttl:
        return hit[1]

    weights = _compute(jurisdiction, families, window)
    _CACHE[key] = (now, weights)
    return weights


def _compute(jurisdiction: str, families: Sequence[str], window: str) -> dict[str, float] | None:
    try:
        import pandas as pd
        from xbrl_sec.sec.cycle.ic import _metric_matches_family  # reuse family patterns
        from xbrl_sec.sec.db.connection import connect

        with connect() as conn:
            df = pd.read_sql(
                """
                SELECT metric_id, spearman_ic
                FROM   fact_equity_factor_ic_regime
                WHERE  jurisdiction = %s
                  AND  forward_return_window = %s
                  AND  spearman_ic IS NOT NULL
                  AND  date = (
                        SELECT MAX(date) FROM fact_equity_factor_ic_regime
                        WHERE jurisdiction = %s AND forward_return_window = %s
                  )
                """,
                conn,
                params=(jurisdiction.upper(), window, jurisdiction.upper(), window),
            )
    except Exception:  # noqa: BLE001 - table missing / DB down -> fixed weights
        logger.warning("IC-weights query failed for %s", jurisdiction, exc_info=True)
        return None

    if df.empty:
        return None

    df["abs_ic"] = df["spearman_ic"].abs()
    per_metric = df.groupby("metric_id")["abs_ic"].mean()

    raw: dict[str, float] = {}
    for fam in families:
        vals = [ic for mid, ic in per_metric.items() if _metric_matches_family(str(mid), fam)]
        if vals:
            raw[fam] = float(sum(vals) / len(vals))
    total = sum(raw.values())
    if total <= 0:
        return None
    return {fam: v / total for fam, v in raw.items()}


def clear_cache() -> None:
    _CACHE.clear()
