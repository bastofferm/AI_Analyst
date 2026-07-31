"""Cap-weighted sector / industry-group return aggregation.

Reads:
  * fact_prices_{us,jp}                    — daily close + return
  * dim_company_{us,jp}                    — GICS classification + shares_outstanding
  * fact_fundamentals_std_us               — historical shares_outstanding_diluted (US only)

Writes:
  * fact_sector_returns(jurisdiction, grouping_level, gics_code, date, ...)
  * fact_sector_weights(jurisdiction, grouping_level, gics_code, snapshot_date, ticker, ...)

CLI:
  python -m xbrl_sec.sec.sources.sector_returns_compute --full|--incremental
                                                        [--jurisdiction US|JP|all]
                                                        [--start-date 2010-01-01]
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import Iterable, Literal

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


logger = logging.getLogger(__name__)
JurisdictionT = Literal["US", "JP"]
GroupingT     = Literal["sector", "industry_group"]


# ---------------------------------------------------------------------------
# Daily market-cap construction
# ---------------------------------------------------------------------------

_PRICE_SQL_US = """
    SELECT  p.date,
            p.ticker,
            COALESCE(p.adj_close, p.close)::float8 AS close,
            p.return::float8                       AS ret,
            c.gics_sector_code,
            c.gics_sector_name,
            c.gics_industry_group_code,
            c.gics_industry_group_name,
            p.shares_outstanding::float8           AS shares
    FROM    fact_prices_us p
    JOIN    dim_company_us c ON c.primary_ticker = p.ticker
    WHERE   p.date >= %s
      AND   c.gics_sector_code IS NOT NULL
      AND   p.shares_outstanding IS NOT NULL
      AND   p.shares_outstanding > 0
      AND   COALESCE(p.adj_close, p.close) > 0
    ORDER BY p.date, p.ticker
"""

_PRICE_SQL_JP = """
    SELECT  p.date,
            p.ticker,
            COALESCE(p.adj_close, p.close)::float8 AS close,
            p.return::float8                       AS ret,
            c.gics_sector_code,
            c.gics_sector_name,
            c.gics_industry_group_code,
            c.gics_industry_group_name,
            p.shares_outstanding::float8           AS shares
    FROM    fact_prices_jp p
    JOIN    dim_company_jp c ON c.primary_ticker = p.ticker || '.T'
    WHERE   p.date >= %s
      AND   c.gics_sector_code IS NOT NULL
      AND   p.shares_outstanding IS NOT NULL
      AND   p.shares_outstanding > 0
      AND   COALESCE(p.adj_close, p.close) > 0
    ORDER BY p.date, p.ticker
"""


def _load_panel(jurisdiction: JurisdictionT, start_date: date):
    """Return a pandas DataFrame keyed by (date, ticker) with mcap + return + GICS."""
    import pandas as pd

    sql = _PRICE_SQL_US if jurisdiction == "US" else _PRICE_SQL_JP
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (start_date,))
        rows = cur.fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=[
            "date", "ticker", "close", "ret",
            "sector_code", "sector_name",
            "industry_code", "industry_name",
            "shares",
        ],
    )
    df["date"]   = pd.to_datetime(df["date"])
    df["close"]  = df["close"].astype(float)
    df["shares"] = df["shares"].astype(float)
    df["mcap"]   = df["close"] * df["shares"]
    df["ret"]    = df["ret"].astype(float)
    return df


# ---------------------------------------------------------------------------
# Sector aggregation
# ---------------------------------------------------------------------------

def _aggregate(df, grouping_level: GroupingT):
    """Return DataFrame keyed by (date, gics_code) with cap_weighted_return,
    level (base 100), total_market_cap, constituent_count, gics_name."""
    import pandas as pd

    code_col = "sector_code" if grouping_level == "sector" else "industry_code"
    name_col = "sector_name" if grouping_level == "sector" else "industry_name"

    if df.empty:
        return pd.DataFrame()

    sub = df[[code_col, name_col, "date", "ticker", "mcap", "ret"]].rename(
        columns={code_col: "gics_code", name_col: "gics_name"}
    )
    sub = sub.dropna(subset=["gics_code"])

    # Yesterday's mcap is the weight basis; shift per-ticker.
    sub = sub.sort_values(["ticker", "date"])
    sub["mcap_prev"] = sub.groupby("ticker")["mcap"].shift(1)

    # Drop first observation per ticker (no prior weight) and any with bad ret/mcap
    sub = sub.dropna(subset=["mcap_prev", "ret"])
    sub = sub[(sub["mcap_prev"] > 0) & sub["ret"].between(-1.0, 5.0)]

    # Per (date, gics_code): sum(mcap_prev), sum(ret * mcap_prev), and constituent count
    grouped = sub.groupby(["date", "gics_code", "gics_name"], as_index=False).agg(
        weighted_num   = ("ret",       lambda s: float((s * sub.loc[s.index, "mcap_prev"]).sum())),
        total_mcap_prev= ("mcap_prev", "sum"),
        total_mcap     = ("mcap",      "sum"),
        constituent_count = ("ticker", "nunique"),
    )
    grouped["cap_weighted_return"] = grouped["weighted_num"] / grouped["total_mcap_prev"]

    # Level series, base 100 on first row per (gics_code) with >= 3 constituents
    grouped = grouped.sort_values(["gics_code", "date"]).reset_index(drop=True)

    levels = []
    last_level: dict[str, float] = {}
    for r in grouped.itertuples(index=False):
        code = r.gics_code
        if code not in last_level:
            # seed at 100 on first valid date (regardless of count — we want
            # continuity; sparse early days will just have noisier index)
            last_level[code] = 100.0
        else:
            last_level[code] = last_level[code] * (1.0 + float(r.cap_weighted_return))
        levels.append(last_level[code])
    grouped["level"] = levels

    out = grouped[[
        "date", "gics_code", "gics_name",
        "cap_weighted_return", "level", "total_mcap", "constituent_count",
    ]].rename(columns={"total_mcap": "total_market_cap"})
    return out


# ---------------------------------------------------------------------------
# DB writeback
# ---------------------------------------------------------------------------

def _upsert_returns(jurisdiction: JurisdictionT, grouping_level: GroupingT, df):
    if df.empty:
        return 0

    payload = [
        (
            jurisdiction,
            grouping_level,
            str(r.gics_code),
            str(r.gics_name),
            r.date.date() if hasattr(r.date, "date") else r.date,
            float(r.cap_weighted_return) if r.cap_weighted_return == r.cap_weighted_return else None,
            float(r.level) if r.level == r.level else None,
            float(r.total_market_cap) if r.total_market_cap == r.total_market_cap else None,
            int(r.constituent_count),
        )
        for r in df.itertuples(index=False)
    ]

    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_sector_returns
                (jurisdiction, grouping_level, gics_code, gics_name, date,
                 cap_weighted_return, level, total_market_cap, constituent_count)
            VALUES %s
            ON CONFLICT (jurisdiction, grouping_level, gics_code, date)
            DO UPDATE SET
                gics_name           = EXCLUDED.gics_name,
                cap_weighted_return = EXCLUDED.cap_weighted_return,
                level               = EXCLUDED.level,
                total_market_cap    = EXCLUDED.total_market_cap,
                constituent_count   = EXCLUDED.constituent_count,
                updated_at          = now()
            """,
            payload,
            page_size=5000,
        )
    return len(payload)


def _upsert_weights(
    jurisdiction: JurisdictionT,
    grouping_level: GroupingT,
    df_panel,
):
    """Take month-end snapshots from the constituent panel and upsert weights."""
    import pandas as pd

    if df_panel.empty:
        return 0

    code_col = "sector_code" if grouping_level == "sector" else "industry_code"

    sub = df_panel[["date", "ticker", "mcap", code_col]].dropna()
    sub = sub.rename(columns={code_col: "gics_code"})

    # Month-end (last available trading day per calendar month)
    sub = sub.sort_values("date")
    sub["ym"] = sub["date"].dt.to_period("M")
    last_dates = sub.groupby("ym")["date"].transform("max")
    monthly = sub[sub["date"] == last_dates].copy()

    # Compute per-(date,gics_code) total mcap, then per-ticker weight
    totals = monthly.groupby(["date", "gics_code"])["mcap"].sum().rename("total_mcap")
    monthly = monthly.join(totals, on=["date", "gics_code"])
    monthly = monthly[monthly["total_mcap"] > 0]
    monthly["weight"] = monthly["mcap"] / monthly["total_mcap"]

    payload = [
        (
            jurisdiction,
            grouping_level,
            str(r.gics_code),
            r.date.date() if hasattr(r.date, "date") else r.date,
            str(r.ticker),
            float(r.mcap),
            float(r.weight),
        )
        for r in monthly.itertuples(index=False)
    ]

    if not payload:
        return 0

    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_sector_weights
                (jurisdiction, grouping_level, gics_code, snapshot_date, ticker,
                 market_cap, weight)
            VALUES %s
            ON CONFLICT (jurisdiction, grouping_level, gics_code, snapshot_date, ticker)
            DO UPDATE SET
                market_cap = EXCLUDED.market_cap,
                weight     = EXCLUDED.weight,
                updated_at = now()
            """,
            payload,
            page_size=5000,
        )
    return len(payload)


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def _incremental_start(jurisdiction: JurisdictionT) -> date | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(date) FROM fact_sector_returns WHERE jurisdiction = %s",
            (jurisdiction,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def compute_jurisdiction(
    jurisdiction: JurisdictionT,
    start_date: date,
    levels: Iterable[GroupingT] = ("sector", "industry_group"),
) -> dict[str, int]:
    """Run the full pipeline for one jurisdiction. Returns a small stats dict."""
    logger.info("Loading panel for %s starting %s …", jurisdiction, start_date)
    df = _load_panel(jurisdiction, start_date)
    if df.empty:
        logger.warning("No data for %s — skipping", jurisdiction)
        return {"returns_rows": 0, "weights_rows": 0, "tickers": 0}

    n_tickers = df["ticker"].nunique()
    logger.info("  loaded %d rows across %d tickers", len(df), n_tickers)

    total_ret_rows = 0
    total_wt_rows  = 0
    for lvl in levels:
        agg = _aggregate(df, lvl)
        ret_rows = _upsert_returns(jurisdiction, lvl, agg)
        wt_rows  = _upsert_weights(jurisdiction, lvl, df)
        logger.info("  %s — %s: %d return rows, %d weight rows",
                    jurisdiction, lvl, ret_rows, wt_rows)
        total_ret_rows += ret_rows
        total_wt_rows  += wt_rows

    return {"returns_rows": total_ret_rows, "weights_rows": total_wt_rows, "tickers": n_tickers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Recompute from --start-date")
    mode.add_argument("--incremental", action="store_true", help="Only compute dates after MAX(date) in fact_sector_returns")
    parser.add_argument("--jurisdiction", choices=["US", "JP", "all"], default="all")
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.full and not args.incremental:
        args.incremental = True

    jurisdictions: list[JurisdictionT] = (
        ["US", "JP"] if args.jurisdiction == "all" else [args.jurisdiction]
    )

    base_start = date.fromisoformat(args.start_date)

    for j in jurisdictions:
        if args.incremental:
            last = _incremental_start(j)
            start = last if last else base_start
            logger.info("[%s] incremental from %s", j, start)
        else:
            start = base_start
            logger.info("[%s] full from %s", j, start)
        stats = compute_jurisdiction(j, start)
        logger.info("[%s] done — %s", j, stats)

    return 0


if __name__ == "__main__":
    sys.exit(main())
