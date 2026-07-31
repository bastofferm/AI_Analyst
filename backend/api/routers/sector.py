"""Sector / industry-group returns endpoint.

Reads from `sec.fact_sector_returns` and returns latest 1D/1W/1M cumulative
returns plus a 22-trading-day level series for the sparkline column.

Also exposes `/constituents` which lists the top-10 tickers in a sector by
market cap (plus an "Other" rollup and a sector total) with weight, 1d/1w/1m
performance, and trailing P/E — used by the home sector panel's hover popout.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.sector")


class SectorReturn(BaseModel):
    jurisdiction: Literal["US", "JP"]
    grouping_level: Literal["sector", "industry_group"]
    gics_code: str
    gics_name: str
    total_market_cap: float | None
    market_cap_currency: str | None
    pe_ratio: float | None
    ret_1d: float | None      # most-recent daily return
    ret_1w: float | None      # trailing 5-trading-day cumulative
    ret_1m: float | None      # trailing 21-trading-day cumulative
    ret_ytd: float | None     # cumulative since Jan 1 of current year (when ytd=true)
    level_series: list[float] # last ~22 trading-day index levels (oldest → newest)
    as_of: str                # ISO date of the latest observation


# Number of trading days pulled per group; covers the longest window (1M ≈ 21) + 1
_WINDOW = 22


@router.get("/returns", response_model=list[SectorReturn])
async def sector_returns(
    jurisdiction: Literal["US", "JP"] = Query("US"),
    level: Literal["sector", "industry_group"] = Query("sector"),
    ytd: bool = Query(False, description="Include cumulative YTD return for dispersion view"),
) -> list[SectorReturn]:
    """Return one row per GICS code with windowed cumulative returns + sparkline series.

    When ytd=true the response also pulls all rows since Jan 1 of the latest
    available year so ret_ytd can be computed; the level_series remains the
    trailing ~22 days for sparkline display.
    """
    # When YTD is requested we need a wider history window (since Jan 1).
    if ytd:
        history_sql = """
            SELECT  gics_code, gics_name, date, cap_weighted_return, level
            FROM    sec.fact_sector_returns
            WHERE   jurisdiction   = $1
              AND   grouping_level = $2
              AND   date >= (
                      SELECT date_trunc('year', MAX(date))::date
                      FROM   sec.fact_sector_returns
                      WHERE  jurisdiction = $1 AND grouping_level = $2
                    )
            ORDER BY gics_code, date
        """
    else:
        history_sql = """
            SELECT  gics_code, gics_name, date, cap_weighted_return, level
            FROM    sec.fact_sector_returns
            WHERE   jurisdiction   = $1
              AND   grouping_level = $2
              AND   date >= (
                      SELECT MAX(date) - INTERVAL '60 days'
                      FROM   sec.fact_sector_returns
                      WHERE  jurisdiction = $1 AND grouping_level = $2
                    )
            ORDER BY gics_code, date
        """
    if jurisdiction == "US":
        summary_sql = """
            WITH dim AS (
                SELECT primary_ticker AS ticker, gics_sector_code AS gics_code
                FROM   dim_company_us
                WHERE  primary_ticker IS NOT NULL
                  AND  gics_sector_code IS NOT NULL
            ),
            mcap AS (
                SELECT DISTINCT ON (ticker)
                       ticker,
                       value::float8 AS market_cap,
                       COALESCE(currency, 'USD') AS currency
                FROM   fact_market_metrics
                WHERE  jurisdiction = 'US'
                  AND  metric_id = 'market_capitalization'
                  AND  value IS NOT NULL
                ORDER  BY ticker, market_date DESC
            ),
            pe AS (
                SELECT DISTINCT ON (ticker)
                       ticker,
                       value::float8 AS pe_ratio
                FROM   fact_metrics_us
                WHERE  metric_id = 'price_to_earnings_trailing'
                  AND  value IS NOT NULL
                  -- fact_metrics_* also holds Q1..Q4/H1 rows whose P/E divides price by a
                  -- SINGLE quarter's EPS (~4x too high: AAPL Q2 reads 123x against an FY 34x).
                  -- Restrict to full-year/TTM rows and prefer TTM, exactly as the screener does.
                  AND  fiscal_period IN ('FY', 'TTM')
                ORDER  BY ticker, (fiscal_period = 'TTM') DESC, fiscal_year DESC,
                          period_end DESC NULLS LAST
            )
            SELECT  d.gics_code,
                    SUM(m.market_cap) AS total_market_cap,
                    MAX(m.currency) AS market_cap_currency,
                    SUM(pe.pe_ratio * m.market_cap)
                      / NULLIF(SUM(CASE WHEN pe.pe_ratio IS NOT NULL THEN m.market_cap ELSE 0 END), 0) AS pe_ratio
            FROM    dim d
            JOIN    mcap m ON m.ticker = d.ticker
            LEFT JOIN pe ON pe.ticker = d.ticker
            GROUP BY d.gics_code
        """
    else:
        summary_sql = """
            WITH dim AS (
                SELECT primary_ticker AS ticker,
                       regexp_replace(primary_ticker, '\\.T$', '') AS bare_ticker,
                       gics_sector_code AS gics_code,
                       shares_outstanding
                FROM   dim_company_jp
                WHERE  primary_ticker IS NOT NULL
                  AND  gics_sector_code IS NOT NULL
                  AND  is_active = TRUE
            ),
            mcap AS (
                SELECT DISTINCT ON (ticker)
                       ticker,
                       value::float8 AS market_cap,
                       COALESCE(currency, 'JPY') AS currency
                FROM   fact_market_metrics
                WHERE  jurisdiction = 'JP'
                  AND  metric_id = 'market_capitalization'
                  AND  value IS NOT NULL
                ORDER  BY ticker, market_date DESC
            ),
            latest_price AS (
                SELECT d.bare_ticker AS ticker,
                       lp.close,
                       lp.shares_outstanding
                FROM   dim d
                LEFT JOIN LATERAL (
                    SELECT COALESCE(p.adj_close, p.close)::float8 AS close,
                           p.shares_outstanding
                    FROM   fact_prices_jp p
                    WHERE  p.ticker = d.bare_ticker
                      AND  COALESCE(p.adj_close, p.close) IS NOT NULL
                    ORDER  BY p.date DESC
                    LIMIT  1
                ) lp ON TRUE
            ),
            pe AS (
                SELECT DISTINCT ON (ticker)
                       ticker,
                       value::float8 AS pe_ratio
                FROM   fact_metrics_jp
                WHERE  metric_id = 'price_to_earnings_trailing'
                  AND  value IS NOT NULL
                  -- fact_metrics_* also holds Q1..Q4/H1 rows whose P/E divides price by a
                  -- SINGLE quarter's EPS (~4x too high: AAPL Q2 reads 123x against an FY 34x).
                  -- Restrict to full-year/TTM rows and prefer TTM, exactly as the screener does.
                  AND  fiscal_period IN ('FY', 'TTM')
                ORDER  BY ticker, (fiscal_period = 'TTM') DESC, fiscal_year DESC,
                          period_end DESC NULLS LAST
            ),
            resolved AS (
                SELECT  d.gics_code,
                        COALESCE(
                            m.market_cap,
                            lp.close * NULLIF(lp.shares_outstanding, 0)::float8,
                            lp.close * NULLIF(d.shares_outstanding, 0)::float8
                        ) AS market_cap,
                        COALESCE(m.currency, 'JPY') AS currency,
                        pe.pe_ratio
                FROM    dim d
                LEFT JOIN mcap m ON m.ticker = d.ticker
                LEFT JOIN latest_price lp ON lp.ticker = d.bare_ticker
                LEFT JOIN pe ON pe.ticker = d.bare_ticker
            )
            SELECT  gics_code,
                    SUM(market_cap) AS total_market_cap,
                    MAX(currency) AS market_cap_currency,
                    SUM(pe_ratio * market_cap)
                      / NULLIF(SUM(CASE WHEN pe_ratio IS NOT NULL THEN market_cap ELSE 0 END), 0) AS pe_ratio
            FROM    resolved
            WHERE   market_cap IS NOT NULL
              AND   market_cap > 0
            GROUP BY gics_code
        """

    try:
        async with acquire() as conn:
            rows = await conn.fetch(history_sql, jurisdiction, level)
            summary_rows = await conn.fetch(summary_sql) if level == "sector" else []
    except Exception as exc:
        logger.warning("sector_returns query failed: %s", exc)
        return []

    summaries = {
        str(r["gics_code"]): {
            "total_market_cap": r["total_market_cap"],
            "market_cap_currency": r["market_cap_currency"],
            "pe_ratio": r["pe_ratio"],
        }
        for r in summary_rows
    }

    # Group rows by (gics_code, gics_name) — preserving chronological order
    by_code: dict[str, dict] = {}
    for r in rows:
        code = r["gics_code"]
        entry = by_code.setdefault(
            code,
            {"gics_name": r["gics_name"], "dates": [], "rets": [], "levels": []},
        )
        entry["dates"].append(r["date"])
        entry["rets"].append(r["cap_weighted_return"])
        entry["levels"].append(r["level"])

    def _cum_return(rets: list[float | None], n: int) -> float | None:
        """Compound the last `n` returns. Returns None if any are missing."""
        tail = rets[-n:]
        if len(tail) < n:
            return None
        result = 1.0
        for r in tail:
            if r is None:
                return None
            result *= (1.0 + float(r))
        return result - 1.0

    out: list[SectorReturn] = []
    for code, entry in by_code.items():
        rets   = entry["rets"]
        levels = entry["levels"]
        dates  = entry["dates"]
        if not rets:
            continue

        ret_1d = float(rets[-1]) if rets[-1] is not None else None
        ret_1w = _cum_return(rets, 5)
        ret_1m = _cum_return(rets, 21)
        ret_ytd: float | None = None
        if ytd and rets:
            # Compound everything since Jan 1 of the latest available year.
            result = 1.0
            valid = True
            for r in rets:
                if r is None:
                    valid = False
                    break
                result *= (1.0 + float(r))
            ret_ytd = (result - 1.0) if valid else None
        level_series = [float(v) for v in levels[-_WINDOW:] if v is not None]
        as_of = dates[-1].isoformat() if dates and dates[-1] else ""
        summary = summaries.get(str(code), {})

        out.append(SectorReturn(
            jurisdiction=jurisdiction,
            grouping_level=level,
            gics_code=str(code),
            gics_name=entry["gics_name"] or str(code),
            total_market_cap=(
                float(summary["total_market_cap"])
                if summary.get("total_market_cap") is not None
                else None
            ),
            market_cap_currency=summary.get("market_cap_currency"),
            pe_ratio=(
                float(summary["pe_ratio"])
                if summary.get("pe_ratio") is not None
                else None
            ),
            ret_1d=ret_1d,
            ret_1w=ret_1w,
            ret_1m=ret_1m,
            ret_ytd=ret_ytd,
            level_series=level_series,
            as_of=as_of,
        ))

    # When ytd=True sort by YTD descending (for the dispersion chart);
    # otherwise sort by 1M return as before.
    if ytd:
        out.sort(key=lambda r: r.ret_ytd if r.ret_ytd is not None else 0.0, reverse=True)
    else:
        out.sort(key=lambda r: r.ret_1m if r.ret_1m is not None else 0.0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# /sector/constituents — hover popout payload
# ---------------------------------------------------------------------------

class SectorConstituentRow(BaseModel):
    ticker: str | None              # None for "Other" / "Total" rollups
    name: str
    market_cap: float | None        # USD
    weight_pct: float | None        # of total sector market cap
    ret_1d: float | None
    ret_1w: float | None
    ret_1m: float | None
    pe_ratio: float | None


class SectorConstituentsResponse(BaseModel):
    jurisdiction: str
    gics_code: str
    gics_name: str
    total_market_cap: float
    n_tickers: int                  # total tickers in sector with mcap
    top: list[SectorConstituentRow] # top 10 by market cap
    other: SectorConstituentRow | None    # aggregated remainder, None if <=10 tickers
    total: SectorConstituentRow     # sector-wide total row
    prices_as_of: str | None = None # newest close underpinning the 1D/1W/1M columns


@router.get("/constituents", response_model=SectorConstituentsResponse)
async def sector_constituents(
    gics_code: str = Query(..., description="GICS sector code (e.g. '10' for Energy)"),
    jurisdiction: Literal["US", "JP"] = Query("US"),
    top_n: int = Query(10, ge=1, le=50),
) -> SectorConstituentsResponse:
    """Top-N tickers in a GICS sector by market cap + Other + Total rollups."""
    # Per-jurisdiction SQL. For JP we first try fact_market_metrics (same as US),
    # then fall back to latest close × shares_outstanding from dim_company_jp.
    if jurisdiction == "US":
        tickers_sql = """
            SELECT primary_ticker AS ticker, name, gics_sector_name,
                   NULL::bigint    AS shares_outstanding
            FROM   dim_company_us
            WHERE  gics_sector_code = $1
              AND  primary_ticker IS NOT NULL
        """
        mcap_sql = """
            SELECT DISTINCT ON (ticker) ticker, value::float8 AS market_cap
            FROM   fact_market_metrics
            WHERE  jurisdiction = 'US'
              AND  metric_id    = 'market_capitalization'
              AND  ticker       = ANY($1::text[])
            ORDER  BY ticker, market_date DESC
        """
        pe_sql = """
            SELECT DISTINCT ON (ticker) ticker, value::float8 AS pe_ratio
            FROM   fact_metrics_us
            WHERE  metric_id = 'price_to_earnings_trailing'
              AND  ticker    = ANY($1::text[])
              AND  value IS NOT NULL
              -- Full-year/TTM only. The quarterly rows divide price by one quarter's
              -- EPS, which reads ~4x too high (AAPL Q2 = 123x vs FY 34x).
              AND  fiscal_period IN ('FY', 'TTM')
            ORDER  BY ticker, (fiscal_period = 'TTM') DESC, fiscal_year DESC,
                      period_end DESC NULLS LAST
        """
        prices_sql = """
            -- Anchor the lookback to the newest close we actually hold, not to
            -- CURRENT_DATE: the price feed can lag by weeks, and a calendar-dated
            -- window then yields fewer than the 22 sessions the 1M return needs,
            -- silently blanking it for every constituent.
            SELECT ticker, date, close::float8
            FROM   fact_prices_us
            WHERE  ticker = ANY($1::text[])
              AND  close IS NOT NULL
              AND  date >= (SELECT MAX(date) FROM fact_prices_us) - INTERVAL '75 days'
            ORDER  BY ticker, date
        """
    else:
        # JP: dim_company_jp.primary_ticker carries the '.T' suffix
        # (e.g. '7203.T'); fact_prices_jp / fact_metrics_jp use the bare
        # ticker. Strip the suffix for joins.
        # Market cap: first try fact_market_metrics (jurisdiction='JP'),
        # then fall back to latest close × shares_outstanding.
        tickers_sql = """
            SELECT primary_ticker                                AS ticker,
                   COALESCE(name_en, name, primary_ticker)       AS name,
                   gics_sector_name,
                   shares_outstanding
            FROM   dim_company_jp
            WHERE  gics_sector_code = $1
              AND  primary_ticker IS NOT NULL
              AND  is_active = TRUE
        """
        mcap_sql = """
            SELECT DISTINCT ON (ticker) ticker, value::float8 AS market_cap
            FROM   fact_market_metrics
            WHERE  jurisdiction = 'JP'
              AND  metric_id    = 'market_capitalization'
              AND  ticker       = ANY($1::text[])
            ORDER  BY ticker, market_date DESC
        """
        pe_sql = """
            SELECT DISTINCT ON (ticker) ticker, value::float8 AS pe_ratio
            FROM   fact_metrics_jp
            WHERE  metric_id = 'price_to_earnings_trailing'
              AND  ticker    = ANY($1::text[])
              AND  value IS NOT NULL
              -- Full-year/TTM only. The quarterly rows divide price by one quarter's
              -- EPS, which reads ~4x too high (AAPL Q2 = 123x vs FY 34x).
              AND  fiscal_period IN ('FY', 'TTM')
            ORDER  BY ticker, (fiscal_period = 'TTM') DESC, fiscal_year DESC,
                      period_end DESC NULLS LAST
        """
        prices_sql = """
            -- Anchor the lookback to the newest close we actually hold, not to
            -- CURRENT_DATE: the price feed can lag by weeks, and a calendar-dated
            -- window then yields fewer than the 22 sessions the 1M return needs,
            -- silently blanking it for every constituent.
            SELECT ticker, date, close::float8
            FROM   fact_prices_jp
            WHERE  ticker = ANY($1::text[])
              AND  close IS NOT NULL
              AND  date >= (SELECT MAX(date) FROM fact_prices_jp) - INTERVAL '75 days'
            ORDER  BY ticker, date
        """

    # JP tickers in dim_company_jp carry the '.T' suffix (e.g. '7203.T') but
    # fact_prices_jp and fact_metrics_jp store the bare code. Use _join_key
    # to bridge both worlds.
    def _join_key(tkr: str) -> str:
        return tkr[:-2] if jurisdiction == "JP" and tkr.endswith(".T") else tkr

    try:
        async with acquire() as conn:
            ticker_rows = await conn.fetch(tickers_sql, gics_code)
            if not ticker_rows:
                return SectorConstituentsResponse(
                    jurisdiction=jurisdiction, gics_code=gics_code, gics_name="",
                    total_market_cap=0.0, n_tickers=0, top=[], other=None,
                    total=SectorConstituentRow(
                        ticker=None, name="Total",
                        market_cap=0.0, weight_pct=1.0,
                        ret_1d=None, ret_1w=None, ret_1m=None, pe_ratio=None,
                    ),
                )

            ticker_list = [r["ticker"] for r in ticker_rows]
            # Bare-ticker list for fact_prices/fact_metrics joins.
            join_keys = [_join_key(t) for t in ticker_list]
            gics_name = ticker_rows[0]["gics_sector_name"] or ""

            # For JP, fact_market_metrics stores the '.T'-suffixed ticker
            # (same format as dim_company_jp.primary_ticker), so pass
            # ticker_list (with suffix) for the mcap query, but join_keys
            # (bare) for fact_prices_jp / fact_metrics_jp.
            mcap_keys = ticker_list if jurisdiction == "JP" else join_keys
            mcap_rows = await conn.fetch(mcap_sql, mcap_keys)
            pe_rows = await conn.fetch(pe_sql, join_keys)
            price_rows = await conn.fetch(prices_sql, join_keys)
    except Exception as exc:
        logger.warning("sector_constituents query failed: %s", exc)
        return SectorConstituentsResponse(
            jurisdiction=jurisdiction, gics_code=gics_code, gics_name="",
            total_market_cap=0.0, n_tickers=0, top=[], other=None,
            total=SectorConstituentRow(
                ticker=None, name="Total",
                market_cap=0.0, weight_pct=1.0,
                ret_1d=None, ret_1w=None, ret_1m=None, pe_ratio=None,
            ),
        )

    mcap_by_ticker = {r["ticker"]: r["market_cap"] for r in mcap_rows}
    pe_by_ticker = {r["ticker"]: r["pe_ratio"] for r in pe_rows}

    # Build per-ticker close-price arrays oldest→newest (keyed by bare ticker).
    closes: dict[str, list[float]] = {}
    for r in price_rows:
        closes.setdefault(r["ticker"], []).append(float(r["close"]))

    # The newest close in the pull. Surfaced so the UI can say how fresh the
    # move columns are — the feed is not always current.
    prices_as_of = max((r["date"] for r in price_rows), default=None)

    def _ret(arr: list[float], n_back: int) -> float | None:
        if len(arr) <= n_back:
            return None
        prev = arr[-1 - n_back]
        if prev <= 0:
            return None
        return arr[-1] / prev - 1.0

    # Shares-outstanding for JP mcap fallback (close_t × shares).
    shares_by_display_ticker: dict[str, int] = {}
    name_by_ticker: dict[str, str] = {}
    for r in ticker_rows:
        name_by_ticker[r["ticker"]] = r["name"]
        s = r["shares_outstanding"]
        if s is not None and s > 0:
            shares_by_display_ticker[r["ticker"]] = int(s)

    # Assemble per-ticker rows.
    # JP mcap resolution order:
    #   1. fact_market_metrics (jurisdiction='JP') — same as US path
    #   2. Fall back to latest close × shares_outstanding
    #   3. If still unavailable, include the ticker with mc=None so the
    #      popout is never empty (popout formats None as "—").
    # US: always use fact_market_metrics; drop tickers without mcap.
    constituents: list[dict] = []
    for tkr in ticker_list:
        jk = _join_key(tkr)
        arr = closes.get(jk, [])
        last_close = arr[-1] if arr else None

        if jurisdiction == "US":
            mc = mcap_by_ticker.get(jk)
            if mc is None or mc <= 0:
                continue   # US: still drop tickers without mcap
        else:
            # JP stage 1: fact_market_metrics — keyed by the '.T' ticker (tkr)
            mc = mcap_by_ticker.get(tkr)
            if mc is None or mc <= 0:
                # JP stage 2: close × shares
                shares = shares_by_display_ticker.get(tkr)
                mc = float(shares) * float(last_close) if shares and last_close else None
            if mc is not None and mc <= 0:
                mc = None
            # JP stage 3: include even without mcap (mc stays None)

        constituents.append({
            "ticker": tkr,
            "name": name_by_ticker.get(tkr) or tkr,
            "market_cap": float(mc) if mc is not None else None,
            "last_close": last_close,
            "ret_1d": _ret(arr, 1),
            "ret_1w": _ret(arr, 5),
            "ret_1m": _ret(arr, 21),
            "pe_ratio": pe_by_ticker.get(jk),
        })

    # Sort: tickers with valid mcap first (descending), then null-mcap tickers
    # sorted by latest close price (descending) as a size proxy.
    constituents.sort(
        key=lambda c: (
            c["market_cap"] is None,
            -(c["market_cap"] or 0) if c["market_cap"] is not None else -(c["last_close"] or 0),
        )
    )

    # For rollup calculations, only tickers with known mcap contribute weight.
    known_mcap = [c for c in constituents if c["market_cap"] is not None]
    total_mcap = sum(c["market_cap"] for c in known_mcap) or 1.0   # avoid div-by-zero

    top = constituents[:top_n]
    rest = constituents[top_n:]

    def _weighted_avg(rows: list[dict], field: str) -> float | None:
        weighted = 0.0
        weight_sum = 0.0
        for r in rows:
            v = r.get(field)
            mc = r.get("market_cap")
            if v is None or mc is None:
                continue
            weighted += v * mc
            weight_sum += mc
        return weighted / weight_sum if weight_sum > 0 else None

    def _row(rows: list[dict], label: str, ticker: str | None = None) -> SectorConstituentRow:
        mc = sum(r["market_cap"] for r in rows if r["market_cap"] is not None)
        return SectorConstituentRow(
            ticker=ticker,
            name=label,
            market_cap=mc if mc > 0 else None,
            weight_pct=(mc / total_mcap) if total_mcap > 0 and mc > 0 else None,
            ret_1d=_weighted_avg(rows, "ret_1d"),
            ret_1w=_weighted_avg(rows, "ret_1w"),
            ret_1m=_weighted_avg(rows, "ret_1m"),
            pe_ratio=_weighted_avg(rows, "pe_ratio"),
        )

    top_rows = [
        SectorConstituentRow(
            ticker=c["ticker"],
            name=c["name"],
            market_cap=c["market_cap"],
            weight_pct=(c["market_cap"] / total_mcap) if c["market_cap"] is not None and total_mcap > 0 else None,
            ret_1d=c["ret_1d"],
            ret_1w=c["ret_1w"],
            ret_1m=c["ret_1m"],
            pe_ratio=c["pe_ratio"],
        )
        for c in top
    ]
    other_row = _row(rest, f"Other ({len(rest)} tickers)") if rest else None
    total_row = _row(constituents, "Total")

    return SectorConstituentsResponse(
        jurisdiction=jurisdiction,
        gics_code=gics_code,
        gics_name=gics_name,
        total_market_cap=total_mcap,
        n_tickers=len(constituents),
        top=top_rows,
        other=other_row,
        total=total_row,
        prices_as_of=prices_as_of.isoformat() if prices_as_of else None,
    )
