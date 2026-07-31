"""Sector × macro-factor rolling betas.

Defines four canonical macro factors via 1-month changes of:
  - growth:    business-cycle PC1 (fact_macro_factor)
  - inflation: CPI (US) / Statistics Japan CPI index (JP)
  - policy:    2Y yield (US) / call rate (JP)
  - usd:       DXY (US side) / USDJPY (JP side)

Computes OLS beta of monthly sector returns vs each factor, with a 24-month
rolling window. Writes to ``sec.mv_sector_macro_beta`` (regular table, not a
true matview — refreshed via this script).

Run:
    python -m xbrl_sec.sec.sources.sector_macro_beta
"""
from __future__ import annotations

import argparse
import logging
import warnings

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.connection import connect

warnings.filterwarnings("ignore", category=UserWarning, message=".*pandas only supports SQLAlchemy.*")

logger = logging.getLogger("mzqa.sector_macro_beta")


def _ensure_table() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mv_sector_macro_beta (
                date         DATE NOT NULL,
                jurisdiction CHAR(2) NOT NULL,
                sector       TEXT NOT NULL,
                factor       TEXT NOT NULL,
                beta         DOUBLE PRECISION,
                t_stat       DOUBLE PRECISION,
                r2           DOUBLE PRECISION,
                window_n     INTEGER,
                PRIMARY KEY (date, jurisdiction, sector, factor)
            );
            CREATE INDEX IF NOT EXISTS idx_mv_sector_macro_latest
              ON mv_sector_macro_beta (jurisdiction, date DESC);
            """
        )


def _monthly_sector_returns(jurisdiction: str) -> pd.DataFrame:
    sql = """
        SELECT date, gics_name AS sector, cap_weighted_return AS ret
        FROM   fact_sector_returns
        WHERE  jurisdiction=%s AND grouping_level='sector'
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn, params=(jurisdiction,))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    # Compound daily → monthly
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        df.groupby(["month", "sector"])["ret"]
          .apply(lambda x: (1.0 + x.fillna(0)).prod() - 1.0)
          .reset_index()
    )
    return monthly.pivot(index="month", columns="sector", values="ret").sort_index()


def _series_monthly(series_id: str, op: str = "last") -> pd.Series:
    with connect() as conn:
        df = pd.read_sql(
            "SELECT DATE_TRUNC('month', date)::date AS m, value FROM fact_macro WHERE series_id=%s ORDER BY date",
            conn,
            params=(series_id,),
        )
    if df.empty:
        return pd.Series(dtype=float)
    df["m"] = pd.to_datetime(df["m"])
    if op == "last":
        s = df.groupby("m")["value"].last()
    else:
        s = df.groupby("m")["value"].mean()
    return s.sort_index()


def _cycle_factor_monthly(factor_id: str) -> pd.Series:
    with connect() as conn:
        df = pd.read_sql(
            "SELECT date, value FROM fact_macro_factor WHERE factor_id=%s ORDER BY date",
            conn,
            params=(factor_id,),
        )
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df["value"].values, index=pd.to_datetime(df["date"]))
    return s.groupby(s.index.to_period("M").to_timestamp()).last().sort_index()


def _build_factor_panel(juris: str) -> pd.DataFrame:
    if juris == "US":
        cycle  = _cycle_factor_monthly("us_cycle")
        cpi    = _series_monthly("FRED:CPIAUCSL")
        rate2y = _series_monthly("FRED:DGS2")
        usd    = _series_monthly("FRED:DTWEXBGS")
    else:
        cycle  = _cycle_factor_monthly("jp_cycle")
        cpi    = _series_monthly("STATJP:CPI_EX_FRESH")
        rate2y = _series_monthly("BOJ:IR01_OCRT")
        usd    = _series_monthly("BOJ:USDJPY")
    # Monthly changes
    F = pd.DataFrame({
        "growth":    cycle.diff(),
        "inflation": cpi.pct_change(12),
        "policy":    rate2y.diff(),
        "usd":       usd.pct_change(),
    }).dropna(how="all")
    return F


def _rolling_beta(y: pd.Series, x: pd.Series, window: int = 24) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Plain rolling OLS, beta + t-stat + r².  Both inputs share an index."""
    df = pd.concat([y.rename("y"), x.rename("x")], axis=1).dropna()
    if df.shape[0] < window + 4:
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float)
    betas, ts, r2s = [], [], []
    idx = df.index[window - 1:]
    arr_x = df["x"].values
    arr_y = df["y"].values
    for i in range(window - 1, df.shape[0]):
        xs = arr_x[i - window + 1: i + 1]
        ys = arr_y[i - window + 1: i + 1]
        xm = xs - xs.mean()
        ym = ys - ys.mean()
        denom = float((xm * xm).sum())
        if denom <= 0:
            betas.append(np.nan); ts.append(np.nan); r2s.append(np.nan); continue
        b = float((xm * ym).sum() / denom)
        resid = ys - (b * xs + (ys.mean() - b * xs.mean()))
        sigma2 = float((resid * resid).sum() / max(window - 2, 1))
        se = math.sqrt(sigma2 / denom) if denom > 0 else float("nan")
        t = b / se if se > 0 else float("nan")
        sst = float((ym * ym).sum())
        r2 = 1.0 - float((resid * resid).sum()) / sst if sst > 0 else float("nan")
        betas.append(b); ts.append(t); r2s.append(r2)
    return (pd.Series(betas, index=idx),
            pd.Series(ts,    index=idx),
            pd.Series(r2s,   index=idx))


def compute_sector_betas(jurisdiction: str = "US", window: int = 24) -> int:
    sectors = _monthly_sector_returns(jurisdiction)
    if sectors.empty:
        logger.warning("%s: no sector returns", jurisdiction)
        return 0
    factors = _build_factor_panel(jurisdiction)
    if factors.empty:
        logger.warning("%s: no factor panel", jurisdiction)
        return 0
    factors = factors.reindex(sectors.index, method="ffill")

    rows: list[tuple] = []
    for sector in sectors.columns:
        for factor in factors.columns:
            b, t, r2 = _rolling_beta(sectors[sector], factors[factor], window=window)
            for dt, beta in b.items():
                if pd.isna(beta):
                    continue
                rows.append((
                    dt.date(), jurisdiction, sector, factor,
                    float(beta),
                    float(t.loc[dt]) if not pd.isna(t.loc[dt]) else None,
                    float(r2.loc[dt]) if not pd.isna(r2.loc[dt]) else None,
                    window,
                ))
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO mv_sector_macro_beta (date, jurisdiction, sector, factor, beta, t_stat, r2, window_n)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (date, jurisdiction, sector, factor) DO UPDATE SET
                beta = EXCLUDED.beta,
                t_stat = EXCLUDED.t_stat,
                r2 = EXCLUDED.r2
            """,
            rows,
        )
    return len(rows)


import math


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--region", choices=["us", "jp", "all"], default="all")
    p.add_argument("--window", type=int, default=24)
    args = p.parse_args()
    _ensure_table()
    out = {}
    if args.region in ("us", "all"):
        out["us"] = compute_sector_betas("US", window=args.window)
    if args.region in ("jp", "all"):
        out["jp"] = compute_sector_betas("JP", window=args.window)
    import json as _json
    print(_json.dumps(out))


if __name__ == "__main__":
    main()
