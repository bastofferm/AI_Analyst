"""Macro regime quadrant computation (US, JP, EZ, CH).

Builds ``fact_macro_regime`` from raw growth + inflation series already in
``fact_macro``. One row per (jurisdiction, quarter) with:

  - growth_z      8Q rolling z-score of growth momentum
  - inflation_z   8Q rolling z-score of inflation momentum
  - quadrant      Goldilocks | Reflation | Stagflation | Deflation
  - is_current    TRUE for the latest quarter per jurisdiction

This is distinct from ``business_cycle.py``'s PCA factor (``fact_macro_factor``).
That returns a single PC1 value used for "where are we in the cycle"; this
returns the two-axis growth/inflation coordinates used for the regime
scatter chart.

Series mapping (per jurisdiction): see ``REGIME_INPUTS``. Treatment differs
slightly because some series are already YoY/QoQ rates while others are
levels.

Run:
    python -m xbrl_sec.sec.sources.macro_regime_compute --jurisdiction all
    python -m xbrl_sec.sec.sources.macro_regime_compute --jurisdiction EZ
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.macro_regime")

Jurisdiction = Literal["US", "JP", "EZ", "CH"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
#
# growth_kind:
#   'level_qoq'   -> series is a level (e.g. real GDP), compute QoQ % growth
#   'rate_yoy'    -> series is already a growth rate, use as-is
# inflation_kind:
#   'level_yoy'   -> level series (e.g. CPI index), compute YoY %
#   'rate_yoy'    -> series is already a YoY % rate, use as-is
# ---------------------------------------------------------------------------

REGIME_INPUTS: dict[str, dict[str, str]] = {
    "US": {
        "growth_series":    "FRED:GDPC1",
        "growth_kind":      "level_qoq",
        "inflation_series": "FRED:CPIAUCSL",
        "inflation_kind":   "level_yoy",
    },
    "JP": {
        "growth_series":    "CAO_JP:GDP_REAL",
        "growth_kind":      "level_qoq",
        "inflation_series": "STATJP:CPI_EX_FRESH",
        "inflation_kind":   "level_yoy",
    },
    "EZ": {
        "growth_series":    "ECB:MNA_REAL_GDP_YOY",
        "growth_kind":      "rate_yoy",
        "inflation_series": "ECB:ICP_HICP_YOY",
        "inflation_kind":   "rate_yoy",
    },
    # SNB's native data.snb.ch/api/cube CSV endpoint was decommissioned in
    # 2026 (now an SPA + WAF). Falling back to FRED mirrors (CHEGDPNQDSMEI =
    # nominal quarterly GDP for CH, CHECPIALLMINMEI = monthly CPI level).
    # Both are levels, so they use the same QoQ/YoY derivation as US.
    "CH": {
        "growth_series":    "SNB:GDP",
        "growth_kind":      "level_qoq",
        "inflation_series": "SNB:CPI",
        "inflation_kind":   "level_yoy",
    },
}

WINDOW_QUARTERS = 8


# Short display unit for each kind, persisted with the row so the frontend
# tooltip can label "0.45 %" correctly as YoY or QoQ-annualised.
GROWTH_UNIT_LABEL: dict[str, str] = {
    "level_qoq": "QoQ % (ann.)",
    "rate_yoy":  "YoY %",
}
INFLATION_UNIT_LABEL: dict[str, str] = {
    "level_yoy": "YoY %",
    "rate_yoy":  "YoY %",
}


# ---------------------------------------------------------------------------
# Loading & transformation
# ---------------------------------------------------------------------------

def _load_series(series_id: str) -> pd.DataFrame:
    """Return a date-indexed DataFrame with a 'value' column for a series."""
    with connect() as conn:
        df = pd.read_sql(
            "SELECT date, value FROM fact_macro WHERE series_id=%s ORDER BY date",
            conn,
            params=(series_id,),
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def _to_quarterly_growth(series: pd.Series, kind: str) -> pd.Series:
    """Reduce a growth series to one observation per quarter (quarter-end)."""
    if series.empty:
        return series
    if kind == "level_qoq":
        # Quarterly level → annualised QoQ % change, then quarter-end stamp.
        # ((1+q)^4 − 1) gives the annualised rate that's comparable to YoY series.
        q = series.resample("QE").last()
        return (((1.0 + q.pct_change()) ** 4) - 1.0) * 100.0
    if kind == "rate_yoy":
        # Already a rate. Some are reported quarterly; resample to quarter end
        # by taking the period's last observation.
        return series.resample("QE").last()
    raise ValueError(f"unknown growth_kind: {kind}")


def _to_quarterly_inflation(series: pd.Series, kind: str) -> pd.Series:
    """Reduce inflation series to one observation per quarter."""
    if series.empty:
        return series
    if kind == "level_yoy":
        # Monthly level → YoY % change, then quarter-end aggregation (last value).
        yoy = series.pct_change(periods=12) * 100.0
        return yoy.resample("QE").last()
    if kind == "rate_yoy":
        return series.resample("QE").last()
    raise ValueError(f"unknown inflation_kind: {kind}")


def _rolling_zscore(s: pd.Series, window: int) -> pd.Series:
    mu = s.rolling(window, min_periods=max(3, window // 2)).mean()
    sigma = s.rolling(window, min_periods=max(3, window // 2)).std(ddof=0)
    return (s - mu) / sigma.replace(0.0, np.nan)


def _quadrant(growth_z: float | None, inflation_z: float | None) -> str | None:
    if growth_z is None or inflation_z is None or pd.isna(growth_z) or pd.isna(inflation_z):
        return None
    if growth_z >= 0 and inflation_z < 0:
        return "Goldilocks"
    if growth_z >= 0 and inflation_z >= 0:
        return "Reflation"
    if growth_z < 0 and inflation_z >= 0:
        return "Stagflation"
    return "Deflation"


def _fiscal_quarter_label(d: date) -> str:
    """'Q1 24' style label from a quarter-end date."""
    q = (d.month - 1) // 3 + 1
    return f"Q{q} {d.year % 100:02d}"


# ---------------------------------------------------------------------------
# Compute one jurisdiction
# ---------------------------------------------------------------------------

def compute_jurisdiction(j: Jurisdiction) -> int:
    """Compute and upsert macro regime rows for one jurisdiction. Returns row count."""
    cfg = REGIME_INPUTS[j]
    g_raw = _load_series(cfg["growth_series"])
    i_raw = _load_series(cfg["inflation_series"])
    if g_raw.empty or i_raw.empty:
        logger.warning(
            "%s: missing source data (growth=%s rows, inflation=%s rows)",
            j, len(g_raw), len(i_raw),
        )
        return 0

    g_q = _to_quarterly_growth(g_raw["value"], cfg["growth_kind"])
    i_q = _to_quarterly_inflation(i_raw["value"], cfg["inflation_kind"])

    df = pd.concat({"growth": g_q, "inflation": i_q}, axis=1).dropna(how="all")
    if df.empty:
        logger.warning("%s: no overlapping quarters after resample", j)
        return 0

    df["growth_z"] = _rolling_zscore(df["growth"], WINDOW_QUARTERS)
    df["inflation_z"] = _rolling_zscore(df["inflation"], WINDOW_QUARTERS)

    df = df.dropna(subset=["growth_z", "inflation_z"], how="all")
    if df.empty:
        logger.warning("%s: no quarters with z-scores", j)
        return 0

    growth_unit = GROWTH_UNIT_LABEL.get(cfg["growth_kind"], "%")
    inflation_unit = INFLATION_UNIT_LABEL.get(cfg["inflation_kind"], "%")

    rows: list[tuple] = []
    last_full_idx: int | None = None
    for idx, (ts, r) in enumerate(df.iterrows()):
        period_end: date = ts.date() if hasattr(ts, "date") else ts
        g_z = float(r["growth_z"]) if pd.notna(r["growth_z"]) else None
        i_z = float(r["inflation_z"]) if pd.notna(r["inflation_z"]) else None
        g_v = float(r["growth"]) if pd.notna(r["growth"]) else None
        i_v = float(r["inflation"]) if pd.notna(r["inflation"]) else None
        if g_z is not None and i_z is not None:
            last_full_idx = idx
        rows.append(
            (
                j,
                period_end,
                _fiscal_quarter_label(period_end),
                g_z,
                i_z,
                _quadrant(g_z, i_z),
                False,                 # is_current set below
                g_v,
                i_v,
                growth_unit,
                inflation_unit,
            )
        )

    if not rows:
        return 0

    # Stamp the most-recent quarter with BOTH z-scores populated as current
    # (the latest quarter often has only one axis reported because growth
    # and inflation publish at different cadences).
    if last_full_idx is not None:
        prev = rows[last_full_idx]
        rows[last_full_idx] = prev[:6] + (True,) + prev[7:]

    with connect() as conn, conn.cursor() as cur:
        # Reset any prior is_current flag for this jurisdiction first.
        cur.execute(
            "UPDATE fact_macro_regime SET is_current = FALSE, updated_at = now() "
            "WHERE jurisdiction = %s AND is_current = TRUE",
            (j,),
        )
        cur.executemany(
            """
            INSERT INTO fact_macro_regime
                (jurisdiction, period_end, fiscal_quarter,
                 growth_z, inflation_z, quadrant, is_current,
                 growth_value, inflation_value, growth_unit, inflation_unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (jurisdiction, period_end) DO UPDATE SET
                fiscal_quarter   = EXCLUDED.fiscal_quarter,
                growth_z         = EXCLUDED.growth_z,
                inflation_z      = EXCLUDED.inflation_z,
                quadrant         = EXCLUDED.quadrant,
                is_current       = EXCLUDED.is_current,
                growth_value     = EXCLUDED.growth_value,
                inflation_value  = EXCLUDED.inflation_value,
                growth_unit      = EXCLUDED.growth_unit,
                inflation_unit   = EXCLUDED.inflation_unit,
                updated_at       = now()
            """,
            rows,
        )

    latest_period_end = rows[-1][1] if rows else None
    current_period_end = rows[last_full_idx][1] if last_full_idx is not None else None
    logger.info(
        "%s: wrote %d rows, latest=%s, current=%s",
        j, len(rows), latest_period_end, current_period_end,
    )
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Compute macro regime quadrant per jurisdiction")
    p.add_argument(
        "--jurisdiction",
        choices=["US", "JP", "EZ", "CH", "all"],
        default="all",
    )
    args = p.parse_args()

    targets: list[Jurisdiction]
    if args.jurisdiction == "all":
        targets = list(REGIME_INPUTS.keys())  # type: ignore[assignment]
    else:
        targets = [args.jurisdiction]  # type: ignore[list-item]

    out: dict[str, int] = {}
    for j in targets:
        try:
            out[j] = compute_jurisdiction(j)
        except Exception as exc:
            logger.exception("%s: failed: %s", j, exc)
            out[j] = -1
    print(json.dumps(out))


if __name__ == "__main__":
    main()
