"""Business-cycle factor (PCA) — US and JP.

Computes a daily/monthly PC1 from a small set of activity / credit / curve
series and classifies the regime by 10Y percentile bucket. Writes to
``sec.fact_macro_factor``.

US inputs:  CFNAI, INDPRO, T10Y2Y, BAMLH0A0HYM2 (-), CPIAUCSL YoY
JP inputs:  CAO Coincident CI, CAO Leading CI, unemployment, and Statistics
            Japan CPI.  METI IIP is kept as a visible tile, but the current
            HTML repair is latest-snapshot only and should not anchor PCA
            history until the full workbook parser is enabled.

Run:
    python -m xbrl_sec.sec.sources.business_cycle
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import date

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.business_cycle")


# ---------------------------------------------------------------------------
# Series ingredients
# ---------------------------------------------------------------------------

US_SERIES: list[tuple[str, bool]] = [
    # (series_id, invert?  — higher = stronger growth)
    ("FRED:CFNAI",       False),
    ("FRED:INDPRO",      False),
    ("FRED:T10Y2Y",      False),
    ("FRED:BAMLH0A0HYM2", True),
    ("FRED:UNRATE",       True),
]

JP_SERIES: list[tuple[str, bool]] = [
    ("CAO_JP:CI_COIN",  False),
    ("CAO_JP:CI_LEAD",  False),
    ("BOJ:UNRATE",        True),
    ("STATJP:CPI_EX_FRESH", False),
]


def _load_monthly(series_ids: list[str]) -> pd.DataFrame:
    sql = """
        SELECT series_id, date_trunc('month', date)::date AS month,
               AVG(value)::float AS value
        FROM   fact_macro
        WHERE  series_id = ANY(%s)
        GROUP  BY series_id, date_trunc('month', date)
        ORDER  BY month
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn, params=(series_ids,))
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot(index="month", columns="series_id", values="value").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide


def _standardize(df: pd.DataFrame, inverts: dict[str, bool]) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if inverts.get(c):
            out[c] = -out[c]
    mu, sigma = out.mean(), out.std(ddof=0).replace(0.0, np.nan)
    z = (out - mu) / sigma
    return z


def _pca_factor(z: pd.DataFrame) -> tuple[pd.Series, dict[str, float]]:
    """PC1 via SVD on the centred matrix."""
    z = z.dropna(how="any")
    if z.shape[0] < 24 or z.shape[1] < 3:
        return pd.Series(dtype=float), {}
    X = z.values
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    # First component
    pc1 = X @ Vt[0]
    loadings = {col: float(Vt[0, i]) for i, col in enumerate(z.columns)}
    pc1_s = pd.Series(pc1, index=z.index, name="pc1")
    return pc1_s, loadings


def _regime(percentile: float) -> str:
    if percentile < 0.20:
        return "Contraction"
    if percentile < 0.45:
        return "Late-cycle"
    if percentile < 0.70:
        return "Mid-expansion"
    return "Early-expansion"


def _compute_factor(series: list[tuple[str, bool]], factor_id: str) -> int:
    ids = [s for s, _ in series]
    inverts = dict(series)
    wide = _load_monthly(ids)
    if wide.empty or wide.shape[1] < 3:
        logger.warning("%s: insufficient series in fact_macro (%s)", factor_id, list(wide.columns))
        return 0
    z = _standardize(wide, inverts)
    pc1, loadings = _pca_factor(z)
    if pc1.empty:
        logger.warning("%s: PCA produced empty result", factor_id)
        return 0

    # Percentile vs 10Y rolling sample
    look = min(120, len(pc1))
    pctile_series = pc1.rolling(look, min_periods=24).apply(
        lambda window: (window <= window.iloc[-1]).mean(), raw=False
    )

    top_loadings = sorted(loadings.items(), key=lambda kv: -abs(kv[1]))[:3]
    top_struct = [{"series": s.replace("FRED:", "").replace("CAO_JP:", "").replace("BOJ:", ""),
                   "loading": round(l, 3)} for s, l in top_loadings]

    rows = []
    for dt, val in pc1.items():
        if pd.isna(val):
            continue
        pct = pctile_series.loc[dt]
        regime = _regime(float(pct)) if not pd.isna(pct) else None
        rows.append((dt.date(), factor_id, float(val), float(pct) if not pd.isna(pct) else None, regime, json.dumps(top_struct)))

    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fact_macro_factor (date, factor_id, value, percentile, regime_label, top_loadings)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (date, factor_id) DO UPDATE SET
                value = EXCLUDED.value,
                percentile = EXCLUDED.percentile,
                regime_label = EXCLUDED.regime_label,
                top_loadings = EXCLUDED.top_loadings,
                updated_at = now()
            """,
            rows,
        )
    return len(rows)


def compute_us_cycle_factor() -> int:
    return _compute_factor(US_SERIES, "us_cycle")


def compute_jp_cycle_factor() -> int:
    return _compute_factor(JP_SERIES, "jp_cycle")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--region", choices=["us", "jp", "all"], default="all")
    args = p.parse_args()
    out = {}
    if args.region in ("us", "all"):
        out["us_cycle"] = compute_us_cycle_factor()
    if args.region in ("jp", "all"):
        out["jp_cycle"] = compute_jp_cycle_factor()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
