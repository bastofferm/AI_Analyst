"""Company data-basis endpoint.

`GET /api/company/{ticker}` returns the underlying warehouse data for one
company — the standardized filing line items (fact_fundamentals_std_us/jp),
the computed ratio history (fact_metrics_us/jp), the company profile and the
recent price statistics. This is the "show me the numbers behind the analysis"
surface: everything here is deterministic warehouse data, no LLM involved.

US / JP only, mirroring the prices / kpis routers (the INTL universe has no
standardized statement layer).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()

# How many trailing fiscal years of history to return.
HISTORY_YEARS = 10

# Standardized statement line items (fact_fundamentals_std_us/jp), in display
# order. Grouped so the frontend can render "Income & cash flow" vs "Balance
# sheet" blocks without re-deriving the split.
_STATEMENT_ITEMS: list[tuple[str, str, str]] = [
    # (line_item_id, label, group)
    ("revenue", "Revenue", "income"),
    ("gross_profit", "Gross profit", "income"),
    ("earnings_before_interest_taxes_depreciation_amortization", "EBITDA", "income"),
    ("earnings_before_interest_taxes", "Operating profit (EBIT)", "income"),
    ("net_income", "Net income", "income"),
    ("cash_flow_from_operations", "Operating cash flow", "cashflow"),
    ("capital_expenditures", "Capital expenditure", "cashflow"),
    ("free_cash_flow", "Free cash flow", "cashflow"),
    ("cash_and_cash_equivalents", "Cash & equivalents", "balance"),
    ("total_assets", "Total assets", "balance"),
    ("total_financial_debt", "Total debt", "balance"),
    ("net_debt", "Net debt", "balance"),
    ("total_equity", "Shareholders' equity", "balance"),
]

# Computed ratio metrics (fact_metrics_us/jp). `pct` values are FRACTIONS
# (0.157 = 15.7%) exactly as stored — the frontend formats them.
_RATIO_METRICS: list[tuple[str, str, str, str]] = [
    # (metric_id, label, unit, group)
    ("revenue_growth_year_over_year", "Revenue growth YoY", "pct", "growth"),
    ("revenue_compound_annual_growth_rate_3_year", "Revenue CAGR 3Y", "pct", "growth"),
    ("gross_margin", "Gross margin", "pct", "profitability"),
    ("operating_margin", "Operating margin", "pct", "profitability"),
    ("earnings_before_interest_taxes_depreciation_amortization_margin", "EBITDA margin", "pct", "profitability"),
    ("net_profit_margin", "Net margin", "pct", "profitability"),
    ("return_on_equity", "Return on equity", "pct", "returns"),
    ("return_on_invested_capital", "Return on invested capital", "pct", "returns"),
    ("free_cash_flow_yield", "FCF yield", "pct", "valuation"),
    ("dividend_yield", "Dividend yield", "pct", "valuation"),
    ("price_to_earnings_trailing", "P/E (trailing)", "ratio", "valuation"),
    ("price_to_book", "P/B", "ratio", "valuation"),
    ("enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization", "EV/EBITDA", "ratio", "valuation"),
]


class CompanyProfile(BaseModel):
    name: str
    # Native-script name when it differs from the display name (JP filers).
    name_local: str | None = None
    sector: str | None = None
    industry_group: str | None = None
    exchange: str | None = None
    currency: str
    fy_min: int | None = None
    fy_max: int | None = None
    shares_outstanding: float | None = None
    market_cap: float | None = None
    # Filing-authority id and the matching logo asset, for the identity header.
    entity_id: str | None = None     # CIK (US) / EDINET code (JP)
    entity_id_label: str | None = None
    logo_id: str | None = None


class CompanyPriceStats(BaseModel):
    last: float | None = None
    last_date: str | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    change_1y: float | None = None  # fraction, e.g. 0.23 = +23%


class CompanyDataRow(BaseModel):
    key: str
    label: str
    unit: Literal["currency", "pct", "ratio"]
    group: str
    values: list[float | None]  # aligned to `years`


class CompanyDataResponse(BaseModel):
    ticker: str
    jurisdiction: Literal["US", "JP"]
    profile: CompanyProfile
    price: CompanyPriceStats
    years: list[int]
    statement_rows: list[CompanyDataRow]
    ratio_rows: list[CompanyDataRow]
    source_note: str


def _price_ticker(ticker: str, jurisdiction: str) -> str:
    if jurisdiction == "JP" and ticker.upper().endswith(".T"):
        return ticker[:-2]
    return ticker


async def _price_stats(conn, ticker: str, jurisdiction: str) -> CompanyPriceStats:
    tbl = "fact_prices_us" if jurisdiction == "US" else "fact_prices_jp"
    stored = _price_ticker(ticker, jurisdiction)
    row = await conn.fetchrow(
        f"""
        WITH latest AS (
            SELECT date, COALESCE(adj_close, close) AS px
            FROM   {tbl}
            WHERE  ticker = $1 AND COALESCE(adj_close, close) IS NOT NULL
            ORDER  BY date DESC
            LIMIT  1
        ), window_52w AS (
            SELECT MAX(COALESCE(adj_close, close)) AS hi,
                   MIN(COALESCE(adj_close, close)) AS lo
            FROM   {tbl}
            WHERE  ticker = $1
              AND  COALESCE(adj_close, close) IS NOT NULL
              AND  date >= (SELECT date - INTERVAL '365 days' FROM latest)
        ), prior AS (
            SELECT COALESCE(adj_close, close) AS px
            FROM   {tbl}
            WHERE  ticker = $1
              AND  COALESCE(adj_close, close) IS NOT NULL
              AND  date <= (SELECT date - INTERVAL '365 days' FROM latest)
            ORDER  BY date DESC
            LIMIT  1
        )
        SELECT (SELECT px FROM latest)   AS last_px,
               (SELECT date FROM latest) AS last_date,
               (SELECT hi FROM window_52w) AS hi,
               (SELECT lo FROM window_52w) AS lo,
               (SELECT px FROM prior)    AS prior_px
        """,
        stored,
    )
    if not row or row["last_px"] is None:
        return CompanyPriceStats()
    last = float(row["last_px"])
    prior = float(row["prior_px"]) if row["prior_px"] is not None else None
    return CompanyPriceStats(
        last=last,
        last_date=str(row["last_date"]) if row["last_date"] else None,
        high_52w=float(row["hi"]) if row["hi"] is not None else None,
        low_52w=float(row["lo"]) if row["lo"] is not None else None,
        change_1y=((last - prior) / abs(prior)) if prior else None,
    )


async def _latest_market_cap(conn, entity_id: str | None, jurisdiction: str) -> Optional[float]:
    if not entity_id:
        return None
    row = await conn.fetchrow(
        """
        SELECT value
        FROM   fact_market_metrics
        WHERE  jurisdiction = $1 AND entity_id = $2 AND metric_id = 'market_capitalization'
        ORDER  BY market_date DESC NULLS LAST, period_end DESC NULLS LAST
        LIMIT  1
        """,
        jurisdiction, entity_id,
    )
    return float(row["value"]) if row and row["value"] is not None else None


class CompanySearchResult(BaseModel):
    ticker: str
    name: str
    jurisdiction: Literal["US", "JP", "INTL"]
    # ISO-2 listing country. "US"/"JP" for the primary markets; for INTL it is the
    # real country from dim_company_intl (FR, DE, NL, …) rather than the bucket
    # label, so the UI can show where a name actually trades.
    country_code: str | None = None
    country_name: str | None = None
    sector: str | None = None
    market_cap: float | None = None  # home currency (JPY for JP, USD otherwise)
    logo_id: str | None = None       # GET /logos/{logo_id} — None for INTL


class CompanySearchResponse(BaseModel):
    query: str
    results: list[CompanySearchResult]


def _logo_id(entity_id: str | None, jurisdiction: str) -> str | None:
    """Filename stem of the company logo, or None when we have no image for it.

    The shared MZQA logo library names US files by zero-padded CIK and JP files
    by EDINET code. INTL has no logo coverage at all, so those resolve to None
    and the UI falls back to an initials tile.
    """
    if not entity_id or jurisdiction == "INTL":
        return None
    eid = str(entity_id).strip()
    if not eid:
        return None
    return eid.zfill(10) if jurisdiction == "US" else eid


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _search_market(conn, jurisdiction: str, q: str, limit: int) -> list[dict]:
    """Rank-limited candidate search in one market's dimension table.

    match_rank: 0 = exact ticker, 1 = ticker prefix, 2 = name substring. The
    inner LIMIT bounds the market-cap lookup to a handful of rows.
    """
    esc = _escape_like(q.upper())
    ticker_prefix = f"{esc}%"
    name_sub = f"%{esc}%"

    if jurisdiction == "US":
        sql = r"""
            WITH cand AS (
                SELECT primary_ticker AS ticker, name, gics_sector_name AS sector,
                       'US'::text AS country_code, 'United States'::text AS country_name,
                       cik AS entity_id,
                       CASE WHEN UPPER(primary_ticker) = $1 THEN 0
                            WHEN UPPER(primary_ticker) LIKE $2 THEN 1
                            ELSE 2 END AS match_rank
                FROM   dim_company_us
                WHERE  primary_ticker IS NOT NULL AND COALESCE(include_in_pipeline, true)
                  AND  (UPPER(primary_ticker) LIKE $2 OR UPPER(name) LIKE $3)
                ORDER  BY match_rank, name
                LIMIT  40
            )
            SELECT c.ticker, c.name, c.sector, c.country_code, c.country_name, c.entity_id, c.match_rank,
                   (SELECT value FROM fact_market_metrics
                    WHERE jurisdiction = 'US' AND entity_id = c.entity_id
                      AND metric_id = 'market_capitalization'
                    ORDER BY market_date DESC NULLS LAST LIMIT 1) AS market_cap
            FROM   cand c
            ORDER  BY c.match_rank, market_cap DESC NULLS LAST
            LIMIT  $4
        """
    elif jurisdiction == "JP":
        # JP market caps mirror the screener: fact_market_metrics (keyed by
        # edinet_code OR bare/suffixed ticker) with a latest-close × filed-shares
        # fallback — coverage of the market-metric sidecar lags for JP.
        sql = r"""
            WITH cand AS (
                SELECT primary_ticker AS ticker,
                       COALESCE(NULLIF(name_en, ''), name, primary_ticker) AS name,
                       gics_sector_name AS sector,
                       'JP'::text AS country_code, 'Japan'::text AS country_name,
                       edinet_code AS entity_id,
                       primary_ticker AS raw_ticker,
                       shares_outstanding,
                       CASE WHEN UPPER(primary_ticker) = $1 THEN 0
                            WHEN UPPER(primary_ticker) LIKE $2 THEN 1
                            ELSE 2 END AS match_rank
                FROM   dim_company_jp
                WHERE  primary_ticker IS NOT NULL AND COALESCE(include_in_pipeline, true)
                  AND  (UPPER(primary_ticker) LIKE $2 OR UPPER(name) LIKE $3 OR UPPER(name_en) LIKE $3)
                ORDER  BY match_rank, name
                LIMIT  40
            )
            SELECT c.ticker, c.name, c.sector, c.country_code, c.country_name, c.entity_id, c.match_rank,
                   COALESCE(
                       (SELECT value FROM fact_market_metrics
                        WHERE jurisdiction = 'JP' AND metric_id = 'market_capitalization'
                          AND (entity_id = c.entity_id
                               OR ticker = c.raw_ticker
                               OR ticker = regexp_replace(c.raw_ticker, '\.T$', ''))
                        ORDER BY market_date DESC NULLS LAST LIMIT 1),
                       (SELECT p.close * NULLIF(c.shares_outstanding, 0)
                        FROM fact_prices_jp p
                        WHERE p.ticker = regexp_replace(c.raw_ticker, '\.T$', '')
                          AND p.close IS NOT NULL
                        ORDER BY p.date DESC LIMIT 1)
                   ) AS market_cap
            FROM   cand c
            ORDER  BY c.match_rank, market_cap DESC NULLS LAST
            LIMIT  $4
        """
    else:
        # dim_company_intl.market_cap is raw local-currency Yahoo data; the
        # USD-scaled figure lives in fact_market_metrics keyed by company id.
        sql = r"""
            WITH cand AS (
                SELECT primary_ticker AS ticker,
                       COALESCE(NULLIF(name_en, ''), name, primary_ticker) AS name,
                       gics_sector_name AS sector,
                       country_code, country_name,
                       intl_company_id::text AS entity_id,
                       CASE WHEN UPPER(primary_ticker) = $1 THEN 0
                            WHEN UPPER(primary_ticker) LIKE $2 THEN 1
                            ELSE 2 END AS match_rank
                FROM   dim_company_intl
                WHERE  primary_ticker IS NOT NULL AND COALESCE(include_in_pipeline, true)
                  AND  (UPPER(primary_ticker) LIKE $2 OR UPPER(name) LIKE $3 OR UPPER(name_en) LIKE $3)
                ORDER  BY match_rank, name
                LIMIT  40
            )
            SELECT c.ticker, c.name, c.sector, c.country_code, c.country_name, c.entity_id, c.match_rank,
                   (SELECT value FROM fact_market_metrics
                    WHERE jurisdiction = 'INTL' AND entity_id = c.entity_id
                      AND metric_id = 'market_capitalization'
                    ORDER BY market_date DESC NULLS LAST LIMIT 1) AS market_cap
            FROM   cand c
            ORDER  BY c.match_rank, market_cap DESC NULLS LAST
            LIMIT  $4
        """

    rows = await conn.fetch(sql, q.upper(), ticker_prefix, name_sub, limit)
    return [
        {
            "ticker": r["ticker"],
            "name": r["name"] or r["ticker"],
            "jurisdiction": jurisdiction,
            "country_code": (r["country_code"] or None),
            "country_name": (r["country_name"] or None),
            "logo_id": _logo_id(r["entity_id"], jurisdiction),
            "sector": r["sector"],
            "market_cap": float(r["market_cap"]) if r["market_cap"] is not None else None,
            "match_rank": int(r["match_rank"]),
        }
        for r in rows
    ]


@router.get("/search", response_model=CompanySearchResponse)
async def search_companies(
    q: str = Query(..., min_length=1, max_length=60),
    limit: int = Query(8, ge=1, le=20),
) -> CompanySearchResponse:
    """Type-ahead search over the whole coverage universe (US + JP + INTL) by
    ticker or company name. Results are merged across markets: best match rank
    first, then by market cap normalized to USD so ¥-scaled caps rank fairly."""
    query = q.strip()
    if not query:
        return CompanySearchResponse(query=q, results=[])

    async with acquire() as conn:
        jpy_per_usd = await conn.fetchval(
            """
            SELECT value::float8 FROM fact_macro
            WHERE  series_id = 'FRED:DEXJPUS' AND value IS NOT NULL AND value <> 0
            ORDER  BY date DESC LIMIT 1
            """
        )
        markets = []
        for jur in ("US", "JP", "INTL"):
            markets.extend(await _search_market(conn, jur, query, limit))

    def cap_usd(row: dict) -> float:
        cap = row["market_cap"] or 0.0
        if row["jurisdiction"] == "JP" and jpy_per_usd:
            return cap / float(jpy_per_usd)
        return cap

    markets.sort(key=lambda r: (r["match_rank"], -cap_usd(r)))
    return CompanySearchResponse(
        query=query,
        results=[CompanySearchResult(**{k: v for k, v in r.items() if k != "match_rank"}) for r in markets[:limit]],
    )


@router.get("/{ticker}", response_model=CompanyDataResponse)
async def get_company_data(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
) -> CompanyDataResponse:
    tk = ticker.strip().upper()
    dim_tbl = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    std_tbl = "fact_fundamentals_std_us" if jurisdiction == "US" else "fact_fundamentals_std_jp"
    met_tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    entity_col = "cik" if jurisdiction == "US" else "edinet_code"

    # dim_company_jp has no exchange column; every JP name trades on the TSE.
    # JP display names prefer the romanized name_en column, keeping the native
    # name alongside for the profile line.
    exchange_expr = "exchange" if jurisdiction == "US" else "'TSE'::text AS exchange"
    name_expr = (
        "name, NULL::text AS name_local"
        if jurisdiction == "US"
        else "COALESCE(NULLIF(name_en, ''), name) AS name, name AS name_local"
    )

    async with acquire() as conn:
        dim = await conn.fetchrow(
            f"""
            SELECT {entity_col} AS entity_id, {name_expr},
                   gics_sector_name, gics_industry_group_name,
                   {exchange_expr}, shares_outstanding
            FROM   {dim_tbl}
            WHERE  UPPER(primary_ticker) = $1
            LIMIT  1
            """,
            tk,
        )
        if not dim:
            raise HTTPException(status_code=404, detail=f"{tk} is not in the {jurisdiction} coverage universe.")
        entity_id = dim["entity_id"]

        # Trailing fiscal-year window, anchored on the newest FY with metrics.
        fy_row = await conn.fetchrow(
            f"""
            SELECT MIN(fiscal_year) AS fy_min, MAX(fiscal_year) AS fy_max
            FROM   {met_tbl}
            WHERE  ticker = $1 AND fiscal_period IN ('FY', 'Annual')
            """,
            tk,
        )
        fy_min = int(fy_row["fy_min"]) if fy_row and fy_row["fy_min"] is not None else None
        fy_max = int(fy_row["fy_max"]) if fy_row and fy_row["fy_max"] is not None else None
        if fy_max is None:
            # Fall back to the statement layer before giving up.
            fy_row = await conn.fetchrow(
                f"""
                SELECT MIN(fiscal_year) AS fy_min, MAX(fiscal_year) AS fy_max
                FROM   {std_tbl}
                WHERE  {entity_col} = $1 AND fiscal_period IN ('FY', 'Annual')
                """,
                entity_id,
            )
            fy_min = int(fy_row["fy_min"]) if fy_row and fy_row["fy_min"] is not None else None
            fy_max = int(fy_row["fy_max"]) if fy_row and fy_row["fy_max"] is not None else None

        years: list[int] = []
        if fy_max is not None:
            start = max(fy_min or fy_max, fy_max - HISTORY_YEARS + 1)
            years = list(range(start, fy_max + 1))

        stmt_values: dict[tuple[int, str], float] = {}
        ratio_values: dict[tuple[int, str], float] = {}
        currency: str | None = None

        if years:
            # Standardized statement facts, newest filing vintage per (fy, item).
            stmt_rows = await conn.fetch(
                f"""
                SELECT DISTINCT ON (fiscal_year, line_item_id)
                       fiscal_year, line_item_id, value::float8 AS value, currency
                FROM   {std_tbl}
                WHERE  {entity_col} = $1
                  AND  fiscal_period IN ('FY', 'Annual')
                  AND  fiscal_year = ANY($2::int[])
                  AND  line_item_id = ANY($3::text[])
                  AND  value IS NOT NULL
                ORDER  BY fiscal_year, line_item_id, filed_date DESC NULLS LAST
                """,
                entity_id, years, [i for i, _, _ in _STATEMENT_ITEMS],
            )
            for r in stmt_rows:
                stmt_values[(int(r["fiscal_year"]), r["line_item_id"])] = float(r["value"])
                currency = currency or r["currency"]

            ratio_rows_db = await conn.fetch(
                f"""
                SELECT DISTINCT ON (fiscal_year, metric_id)
                       fiscal_year, metric_id, value::float8 AS value
                FROM   {met_tbl}
                WHERE  ticker = $1
                  AND  fiscal_period IN ('FY', 'Annual')
                  AND  fiscal_year = ANY($2::int[])
                  AND  metric_id = ANY($3::text[])
                  AND  value IS NOT NULL
                ORDER  BY fiscal_year, metric_id, period_end DESC NULLS LAST
                """,
                tk, years, [m for m, _, _, _ in _RATIO_METRICS],
            )
            for r in ratio_rows_db:
                ratio_values[(int(r["fiscal_year"]), r["metric_id"])] = float(r["value"])

        price = await _price_stats(conn, tk, jurisdiction)
        market_cap = await _latest_market_cap(conn, entity_id, jurisdiction)
        if market_cap is None and price.last is not None and dim["shares_outstanding"]:
            market_cap = price.last * float(dim["shares_outstanding"])

    def _row(key: str, label: str, unit: str, group: str, source: dict[tuple[int, str], float]) -> CompanyDataRow:
        return CompanyDataRow(
            key=key, label=label, unit=unit, group=group,
            values=[source.get((y, key)) for y in years],
        )

    statement_rows = [
        r for r in (_row(k, lbl, "currency", grp, stmt_values) for k, lbl, grp in _STATEMENT_ITEMS)
        if any(v is not None for v in r.values)
    ]
    ratio_rows = [
        r for r in (_row(m, lbl, unit, grp, ratio_values) for m, lbl, unit, grp in _RATIO_METRICS)
        if any(v is not None for v in r.values)
    ]

    return CompanyDataResponse(
        ticker=tk,
        jurisdiction=jurisdiction,
        profile=CompanyProfile(
            name=dim["name"] or tk,
            name_local=(dim["name_local"] if dim["name_local"] and dim["name_local"] != dim["name"] else None),
            sector=dim["gics_sector_name"],
            industry_group=dim["gics_industry_group_name"],
            exchange=dim["exchange"],
            currency=currency or ("JPY" if jurisdiction == "JP" else "USD"),
            fy_min=fy_min,
            fy_max=fy_max,
            shares_outstanding=float(dim["shares_outstanding"]) if dim["shares_outstanding"] else None,
            market_cap=market_cap,
            entity_id=(str(dim["entity_id"]).strip() or None) if dim["entity_id"] else None,
            entity_id_label="CIK" if jurisdiction == "US" else "EDINET",
            logo_id=_logo_id(dim["entity_id"], jurisdiction),
        ),
        price=price,
        years=years,
        statement_rows=statement_rows,
        ratio_rows=ratio_rows,
        source_note=(
            "Standardized from SEC EDGAR XBRL filings"
            if jurisdiction == "US"
            else "Standardized from EDINET filings"
        ),
    )
