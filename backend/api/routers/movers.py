"""Top movers across the equity universe.

`GET /api/movers?jurisdiction=US|JP&window=1d|1w|1m&top_n=8`

Per-ticker cumulative return over the chosen window, ranked descending.
Returns top N winners + bottom N losers. Joined to the company dimension for
name and CIK / EDINET so the frontend can render the corporate logo.

The query is a single round-trip with window functions — `fact_prices_us` is
indexed on (ticker, date) so this stays cheap. Last-day cache lives in-process
for 5 minutes; the underlying tables update once a day.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.movers")


WINDOW_DAYS = {"1d": 1, "1w": 5, "1m": 21}


class MoverRow(BaseModel):
    ticker: str
    name: str
    cik_padded: str | None = None
    edinet_code: str | None = None
    sector_code: str | None = None
    sector_name: str | None = None
    close: float
    market_cap: float | None = None
    market_cap_currency: str | None = None
    pe_ratio: float | None = None
    ret_1d: float | None = None
    ret_window: float
    trend_series: list[float] = Field(default_factory=list)


class MoversResponse(BaseModel):
    jurisdiction: Literal["US", "JP"]
    window: str
    as_of: str | None
    winners: list[MoverRow]
    losers: list[MoverRow]


# Tiny in-process cache: (jurisdiction, window, top_n) -> (expires_at, payload)
_CACHE: dict[tuple, tuple[float, MoversResponse]] = {}
_TTL_SECONDS = 300


@router.get("", response_model=MoversResponse)
async def get_movers(
    jurisdiction: Literal["US", "JP"] = Query("US"),
    window: Literal["1d", "1w", "1m"] = Query("1w"),
    market_cap_bucket: Literal["all", "large", "mid_small"] = Query("all"),
    top_n: int = Query(8, ge=1, le=50),
) -> MoversResponse:
    cache_key = (jurisdiction, window, market_cap_bucket, top_n)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    win_days = WINDOW_DAYS[window]
    prices_table = "fact_prices_us" if jurisdiction == "US" else "fact_prices_jp"
    dim_table = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    name_expr = "d.name" if jurisdiction == "US" else "COALESCE(d.name_en, d.name, d.primary_ticker)"
    id_expr = "LPAD(d.cik::text, 10, '0')" if jurisdiction == "US" else "NULL::text"
    edinet_expr = "d.edinet_code" if jurisdiction == "JP" else "NULL::text"
    metrics_table = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    # Price floor suppresses sub-threshold pump names. JPY prices are denser; use
    # a higher absolute floor so we don't filter out legitimate JP small caps.
    price_floor = 5.0 if jurisdiction == "US" else 100.0
    # Look back further in JP since the price-table update cadence is slower.
    lookback_days = 60 if jurisdiction == "US" else 90
    # JP price ticker is bare (e.g. "7203"); JP dim primary_ticker has ".T".
    dim_join_expr = "pt.ticker" if jurisdiction == "US" else "(pt.ticker || '.T')"
    mcap_join_expr = "pt.ticker" if jurisdiction == "US" else "d.primary_ticker"
    market_cap_expr = (
        "COALESCE(m.market_cap, pt.close_latest * NULLIF(pt.shares_outstanding_latest, 0)::float8, pt.close_latest * NULLIF(d.shares_outstanding, 0)::float8)"
        if jurisdiction == "US"
        else "COALESCE(m.market_cap, pt.close_latest * NULLIF(pt.shares_outstanding_latest, 0)::float8, pt.close_latest * NULLIF(d.shares_outstanding, 0)::float8)"
    )
    market_cap_currency_expr = (
        "COALESCE(m.market_cap_currency, 'USD')"
        if jurisdiction == "US"
        else "COALESCE(m.market_cap_currency, 'JPY')"
    )

    # Strategy: for each ticker, find the latest two closes and the close
    # `win_days` trading-days earlier. Compute ret_window and ret_1d. Filter to
    # tickers covered by the dimension table (so we have a name + logo). Rank
    # by ret_window, pull top N + bottom N.
    sql = f"""
        WITH ranked AS (
            SELECT  p.ticker,
                    p.date,
                    COALESCE(p.adj_close, p.close) AS close,
                    p.shares_outstanding,
                    ROW_NUMBER() OVER (PARTITION BY p.ticker ORDER BY p.date DESC) AS rn
            FROM    {prices_table} p
            WHERE   p.date >= (
                        SELECT MAX(date) - INTERVAL '{lookback_days} days'
                        FROM   {prices_table}
                    )
              AND   COALESCE(p.adj_close, p.close) IS NOT NULL
        ),
        per_ticker AS (
            SELECT  ticker,
                    MAX(CASE WHEN rn = 1 THEN date END)            AS latest_date,
                    MAX(CASE WHEN rn = 1 THEN close END)           AS close_latest,
                    MAX(CASE WHEN rn = 1 THEN shares_outstanding END) AS shares_outstanding_latest,
                    MAX(CASE WHEN rn = 2 THEN close END)           AS close_prev,
                    MAX(CASE WHEN rn = {win_days} + 1 THEN close END) AS close_window_ago
            FROM    ranked
            WHERE   rn <= {win_days} + 1
            GROUP   BY ticker
        ),
        trend AS (
            SELECT  ticker,
                    ARRAY_AGG(close::float8 ORDER BY date) AS trend_series
            FROM    ranked
            WHERE   rn <= {win_days} + 1
            GROUP   BY ticker
        ),
        mcap AS (
            SELECT DISTINCT ON (ticker)
                    ticker,
                    value::float8 AS market_cap,
                    currency      AS market_cap_currency
            FROM    fact_market_metrics
            WHERE   jurisdiction = '{jurisdiction}'
              AND   metric_id = 'market_capitalization'
              AND   value IS NOT NULL
            ORDER   BY ticker, market_date DESC
        ),
        pe AS (
            SELECT DISTINCT ON (ticker)
                    ticker,
                    value::float8 AS pe_ratio
            FROM    {metrics_table}
            WHERE   metric_id = 'price_to_earnings_trailing'
              AND   value IS NOT NULL
            ORDER   BY ticker, period_end DESC
        ),
        joined AS (
            SELECT  pt.ticker,
                    pt.latest_date,
                    pt.close_latest                                                AS close,
                    CASE WHEN pt.close_prev IS NOT NULL AND pt.close_prev > 0
                         THEN (pt.close_latest / pt.close_prev) - 1.0 END           AS ret_1d,
                    CASE WHEN pt.close_window_ago IS NOT NULL AND pt.close_window_ago > 0
                         THEN (pt.close_latest / pt.close_window_ago) - 1.0 END     AS ret_window,
                    {name_expr}                                                     AS name,
                    {id_expr}                                                       AS cik_padded,
                    {edinet_expr}                                                   AS edinet_code,
                    d.gics_sector_code                                              AS sector_code,
                    d.gics_sector_name                                              AS sector_name,
                    {market_cap_expr}                                               AS market_cap,
                    {market_cap_currency_expr}                                      AS market_cap_currency,
                    pe.pe_ratio                                                     AS pe_ratio,
                    trend.trend_series                                              AS trend_series
            FROM    per_ticker pt
            JOIN    {dim_table} d ON d.primary_ticker = {dim_join_expr}
            LEFT JOIN trend ON trend.ticker = pt.ticker
            LEFT JOIN mcap m ON m.ticker = {mcap_join_expr}
            LEFT JOIN pe     ON pe.ticker = pt.ticker
            WHERE   pt.close_latest IS NOT NULL
              AND   pt.close_window_ago IS NOT NULL
              AND   pt.close_latest >= {price_floor}      -- suppress micro-priced pump names
              AND   pt.close_window_ago >= {price_floor}
              AND   COALESCE(d.include_in_pipeline, true)
        )
        SELECT  *
        FROM    joined
        WHERE   ret_window IS NOT NULL
        ORDER   BY ret_window DESC
    """

    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql)
    except Exception as exc:
        logger.exception("movers query failed")
        raise HTTPException(status_code=500, detail=f"Movers query failed: {exc}") from exc

    rows = [r for r in rows if r["market_cap"] is not None and r["market_cap"] > 0]
    if market_cap_bucket != "all":
        caps = sorted(float(r["market_cap"]) for r in rows if r["market_cap"] is not None and r["market_cap"] > 0)
        if caps:
            median_cap = caps[len(caps) // 2]
            if market_cap_bucket == "large":
                rows = [r for r in rows if r["market_cap"] is not None and float(r["market_cap"]) >= median_cap]
            else:
                rows = [r for r in rows if r["market_cap"] is not None and float(r["market_cap"]) < median_cap]
        else:
            rows = []

    def _row(r) -> MoverRow:
        # Display JP tickers with the .T suffix used everywhere else in the UI.
        t = r["ticker"]
        if jurisdiction == "JP" and t and not t.endswith(".T"):
            t = f"{t}.T"
        return MoverRow(
            ticker=t,
            name=r["name"] or t,
            cik_padded=r["cik_padded"],
            edinet_code=r["edinet_code"],
            sector_code=r["sector_code"],
            sector_name=r["sector_name"],
            close=float(r["close"]),
            market_cap=float(r["market_cap"]) if r["market_cap"] is not None else None,
            market_cap_currency=r["market_cap_currency"],
            pe_ratio=float(r["pe_ratio"]) if r["pe_ratio"] is not None else None,
            ret_1d=float(r["ret_1d"]) if r["ret_1d"] is not None else None,
            ret_window=float(r["ret_window"]),
            trend_series=[float(v) for v in (r["trend_series"] or []) if v is not None],
        )

    winners = [_row(r) for r in rows[:top_n]]
    losers = [_row(r) for r in rows[-top_n:][::-1]] if len(rows) > top_n else []
    as_of = str(rows[0]["latest_date"]) if rows else None

    resp = MoversResponse(
        jurisdiction=jurisdiction,
        window=window,
        as_of=as_of,
        winners=winners,
        losers=losers,
    )
    _CACHE[cache_key] = (now + _TTL_SECONDS, resp)
    return resp
