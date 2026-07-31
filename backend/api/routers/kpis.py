from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Path, Query

from ..db import acquire
from ..models.financials import Direction, KpiChip, KpiPoint, KpiResponse, Period


router = APIRouter()


def _direction(delta: Optional[float]) -> Direction:
    if delta is None or abs(delta) < 1e-9:
        return "neu"
    return "up" if delta > 0 else "down"


def _fmt_currency(v: Optional[float], jurisdiction: str = "US") -> str:
    if v is None:
        return "—"
    prefix = "¥" if jurisdiction == "JP" else "$"
    a = abs(v)
    if a >= 1e12:
        return f"{prefix}{v / 1e12:.2f}T"
    if a >= 1e9:
        return f"{prefix}{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{prefix}{v / 1e6:.1f}M"
    return f"{prefix}{v:,.0f}"


def _fmt_pct(v: Optional[float], signed: bool = False, decimals: int = 1, already_pct: bool = False) -> str:
    if v is None:
        return "—"
    pct = v if already_pct else v * 100
    sign = "+" if signed and pct > 0 else ""
    return f"{sign}{pct:.{decimals}f}%"


def _fmt_ratio(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}×"


async def _latest_market(conn, entity_id: str, jurisdiction: str, metric_id: str) -> Optional[float]:
    row = await conn.fetchrow(
        """
        SELECT value
        FROM   fact_market_metrics
        WHERE  jurisdiction = $1
          AND  entity_id    = $2
          AND  metric_id    = $3
        ORDER  BY market_date DESC NULLS LAST, period_end DESC NULLS LAST
        LIMIT  1
        """,
        jurisdiction, entity_id, metric_id,
    )
    return float(row["value"]) if row and row["value"] is not None else None


async def _yoy_market_delta(conn, entity_id: str, jurisdiction: str, metric_id: str) -> Optional[float]:
    row = await conn.fetchrow(
        """
        WITH latest AS (
            SELECT market_date AS d, value AS v
            FROM   fact_market_metrics
            WHERE  jurisdiction=$1 AND entity_id=$2 AND metric_id=$3
            ORDER  BY market_date DESC NULLS LAST
            LIMIT  1
        ), prior AS (
            SELECT value AS v
            FROM   fact_market_metrics
            WHERE  jurisdiction=$1 AND entity_id=$2 AND metric_id=$3
              AND  market_date <= (SELECT d - INTERVAL '365 days' FROM latest)
            ORDER  BY market_date DESC NULLS LAST
            LIMIT  1
        )
        SELECT (SELECT v FROM latest) AS latest_v,
               (SELECT v FROM prior)  AS prior_v
        """,
        jurisdiction, entity_id, metric_id,
    )
    if not row or row["latest_v"] is None or row["prior_v"] is None:
        return None
    prior = float(row["prior_v"])
    if prior == 0:
        return None
    return (float(row["latest_v"]) - prior) / abs(prior)


async def _latest_fy_metric(conn, ticker: str, jurisdiction: str, metric_id: str) -> Optional[float]:
    tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    row = await conn.fetchrow(
        f"""
        SELECT value
        FROM   {tbl}
        WHERE  ticker = $1
          AND  metric_id = $2
          AND  fiscal_period IN ('FY','Annual')
        ORDER  BY fiscal_year DESC
        LIMIT  1
        """,
        ticker, metric_id,
    )
    return float(row["value"]) if row and row["value"] is not None else None


def _price_ticker(ticker: str, jurisdiction: str) -> str:
    if jurisdiction == "JP" and ticker.upper().endswith(".T"):
        return ticker[:-2]
    return ticker


def _price_join(jurisdiction: str) -> tuple[str, str, str]:
    if jurisdiction == "JP":
        return (
            "fact_prices_jp",
            "dim_company_jp",
            "d.primary_ticker = p.ticker || '.T'",
        )
    return (
        "fact_prices_us",
        "dim_company_us",
        "d.primary_ticker = p.ticker",
    )


async def _latest_price_market_cap(conn, ticker: str, jurisdiction: str) -> Optional[float]:
    price_tbl, dim_tbl, join_clause = _price_join(jurisdiction)
    stored_ticker = _price_ticker(ticker, jurisdiction)
    row = await conn.fetchrow(
        f"""
        SELECT COALESCE(p.adj_close, p.close) AS px,
               COALESCE(p.shares_outstanding, d.shares_outstanding) AS shares
        FROM   {price_tbl} p
        LEFT   JOIN {dim_tbl} d ON {join_clause}
        WHERE  p.ticker = $1
          AND  COALESCE(p.adj_close, p.close) IS NOT NULL
        ORDER  BY p.date DESC
        LIMIT  1
        """,
        stored_ticker,
    )
    if not row or row["px"] is None or row["shares"] is None:
        return None
    shares = float(row["shares"])
    if shares <= 0:
        return None
    return float(row["px"]) * shares


async def _price_yoy_delta(conn, ticker: str, jurisdiction: str) -> Optional[float]:
    price_tbl, _, _ = _price_join(jurisdiction)
    stored_ticker = _price_ticker(ticker, jurisdiction)
    row = await conn.fetchrow(
        f"""
        WITH latest AS (
            SELECT date AS d, COALESCE(adj_close, close) AS px
            FROM   {price_tbl}
            WHERE  ticker = $1
              AND  COALESCE(adj_close, close) IS NOT NULL
            ORDER  BY date DESC
            LIMIT  1
        ), prior AS (
            SELECT COALESCE(adj_close, close) AS px
            FROM   {price_tbl}
            WHERE  ticker = $1
              AND  COALESCE(adj_close, close) IS NOT NULL
              AND  date <= (SELECT d - INTERVAL '365 days' FROM latest)
            ORDER  BY date DESC
            LIMIT  1
        )
        SELECT (SELECT px FROM latest) AS latest_px,
               (SELECT px FROM prior)  AS prior_px
        """,
        stored_ticker,
    )
    if not row or row["latest_px"] is None or row["prior_px"] is None:
        return None
    prior = float(row["prior_px"])
    if prior == 0:
        return None
    return (float(row["latest_px"]) - prior) / abs(prior)


async def _price_market_cap_yoy_delta(conn, ticker: str, jurisdiction: str) -> Optional[float]:
    price_tbl, dim_tbl, join_clause = _price_join(jurisdiction)
    stored_ticker = _price_ticker(ticker, jurisdiction)
    row = await conn.fetchrow(
        f"""
        WITH latest AS (
            SELECT p.date AS d,
                   COALESCE(p.adj_close, p.close) AS px,
                   COALESCE(p.shares_outstanding, d.shares_outstanding) AS shares
            FROM   {price_tbl} p
            LEFT   JOIN {dim_tbl} d ON {join_clause}
            WHERE  p.ticker = $1
              AND  COALESCE(p.adj_close, p.close) IS NOT NULL
            ORDER  BY p.date DESC
            LIMIT  1
        ), prior AS (
            SELECT COALESCE(p.adj_close, p.close) AS px,
                   COALESCE(p.shares_outstanding, d.shares_outstanding) AS shares
            FROM   {price_tbl} p
            LEFT   JOIN {dim_tbl} d ON {join_clause}
            WHERE  p.ticker = $1
              AND  COALESCE(p.adj_close, p.close) IS NOT NULL
              AND  p.date <= (SELECT d - INTERVAL '365 days' FROM latest)
            ORDER  BY p.date DESC
            LIMIT  1
        )
        SELECT (SELECT px FROM latest) AS latest_px,
               (SELECT shares FROM latest) AS latest_shares,
               (SELECT px FROM prior) AS prior_px,
               (SELECT shares FROM prior) AS prior_shares
        """,
        stored_ticker,
    )
    if (
        not row
        or row["latest_px"] is None
        or row["latest_shares"] is None
        or row["prior_px"] is None
        or row["prior_shares"] is None
    ):
        return None
    latest_cap = float(row["latest_px"]) * float(row["latest_shares"])
    prior_cap = float(row["prior_px"]) * float(row["prior_shares"])
    if prior_cap == 0:
        return None
    return (latest_cap - prior_cap) / abs(prior_cap)


async def _resolve_entity_id(conn, ticker: str, jurisdiction: str) -> Optional[str]:
    if jurisdiction == "US":
        row = await conn.fetchrow(
            "SELECT cik::text AS entity_id FROM dim_company_us WHERE primary_ticker=$1 LIMIT 1",
            ticker,
        )
    else:
        row = await conn.fetchrow(
            "SELECT edinet_code AS entity_id FROM dim_company_jp WHERE primary_ticker=$1 LIMIT 1",
            ticker,
        )
    return row["entity_id"] if row and row["entity_id"] else None


async def _series_from_metrics(
    conn,
    ticker: str,
    jurisdiction: str,
    metric_id: str,
    year_min: int,
    year_max: int,
) -> list[KpiPoint]:
    """Fetch per-fiscal-year values from fact_metrics_us / fact_metrics_jp."""
    tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    rows = await conn.fetch(
        f"""
        SELECT fiscal_year, value
        FROM   {tbl}
        WHERE  ticker         = $1
          AND  metric_id      = $2
          AND  fiscal_period  IN ('FY', 'Annual')
          AND  fiscal_year   BETWEEN $3 AND $4
          AND  value IS NOT NULL
        ORDER  BY fiscal_year
        """,
        ticker, metric_id, year_min, year_max,
    )
    return [KpiPoint(period=f"FY{r['fiscal_year']}", value=float(r["value"])) for r in rows]


async def _mcap_annual_series(
    conn,
    entity_id: str,
    jurisdiction: str,
    year_min: int,
    year_max: int,
) -> list[KpiPoint]:
    """Year-end market cap from fact_market_metrics, one value per calendar year."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (DATE_PART('year', market_date)::int)
               DATE_PART('year', market_date)::int AS yr,
               value::float8 AS market_cap
        FROM   fact_market_metrics
        WHERE  jurisdiction = $1
          AND  entity_id    = $2
          AND  metric_id    = 'market_capitalization'
          AND  DATE_PART('year', market_date) BETWEEN $3 AND $4
        ORDER  BY DATE_PART('year', market_date)::int, market_date DESC
        """,
        jurisdiction, entity_id, year_min, year_max,
    )
    return [KpiPoint(period=str(r["yr"]), value=float(r["market_cap"])) for r in rows]


async def _return_annual_series(
    conn,
    ticker: str,
    jurisdiction: str,
    year_min: int,
    year_max: int,
) -> list[KpiPoint]:
    """Approximate annual price return: year-end close vs prior year-end close."""
    price_tbl, _, _ = _price_join(jurisdiction)
    stored_ticker = _price_ticker(ticker, jurisdiction)
    # Fetch last trading-day close for each year in [year_min-1 .. year_max]
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (DATE_PART('year', date)::int)
               DATE_PART('year', date)::int AS yr,
               COALESCE(adj_close, close)   AS px
        FROM   {price_tbl}
        WHERE  ticker = $1
          AND  COALESCE(adj_close, close) IS NOT NULL
          AND  DATE_PART('year', date) BETWEEN $2 AND $3
        ORDER  BY DATE_PART('year', date)::int, date DESC
        """,
        stored_ticker, year_min - 1, year_max,
    )
    px_by_year: dict[int, float] = {int(r["yr"]): float(r["px"]) for r in rows}
    points: list[KpiPoint] = []
    for yr in range(year_min, year_max + 1):
        if yr in px_by_year and (yr - 1) in px_by_year:
            prior = px_by_year[yr - 1]
            if prior > 0:
                ret = (px_by_year[yr] - prior) / prior
                points.append(KpiPoint(period=str(yr), value=ret))
            else:
                points.append(KpiPoint(period=str(yr), value=None))
        else:
            points.append(KpiPoint(period=str(yr), value=None))
    return points


@router.get("/{ticker}", response_model=KpiResponse)
async def get_kpis(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    period: Period = Query("FY"),
    year_min: Optional[int] = Query(None, description="Start year for KPI historical series"),
    year_max: Optional[int] = Query(None, description="End year for KPI historical series"),
) -> KpiResponse:
    want_series = year_min is not None and year_max is not None

    async with acquire() as conn:
        entity_id = await _resolve_entity_id(conn, ticker, jurisdiction)

        mcap = mcap_delta = price_delta = None
        if entity_id:
            mcap = await _latest_market(conn, entity_id, jurisdiction, "market_capitalization")
            mcap_delta = await _yoy_market_delta(conn, entity_id, jurisdiction, "market_capitalization")
            price_delta = await _yoy_market_delta(conn, entity_id, jurisdiction, "stock_price")
        if mcap is None:
            mcap = await _latest_price_market_cap(conn, ticker, jurisdiction)
        if mcap_delta is None:
            mcap_delta = await _price_market_cap_yoy_delta(conn, ticker, jurisdiction)
        price_delta_from_prices = await _price_yoy_delta(conn, ticker, jurisdiction)
        if price_delta_from_prices is not None:
            price_delta = price_delta_from_prices

        rev_cagr_5y = await _latest_fy_metric(conn, ticker, jurisdiction, "revenue_compound_annual_growth_rate_5_year")
        rev_cagr_3y = await _latest_fy_metric(conn, ticker, jurisdiction, "revenue_compound_annual_growth_rate_3_year")
        rev_yoy     = await _latest_fy_metric(conn, ticker, jurisdiction, "revenue_growth_year_over_year")
        ev_ebitda   = await _latest_fy_metric(
            conn, ticker, jurisdiction,
            "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
        )
        div_yield   = await _latest_fy_metric(conn, ticker, jurisdiction, "dividend_yield")
        div_growth  = await _latest_fy_metric(conn, ticker, jurisdiction, "dividend_growth_1_year")

        # Historical series — only when year_min / year_max supplied.
        series_mcap: list[KpiPoint] | None = None
        series_rev_cagr: list[KpiPoint] | None = None
        series_rev_yoy: list[KpiPoint] | None = None
        series_ev_ebitda: list[KpiPoint] | None = None
        series_return: list[KpiPoint] | None = None
        series_div_yield: list[KpiPoint] | None = None

        if want_series:
            assert year_min is not None and year_max is not None
            # fact_metrics_us / fact_metrics_jp store the same ticker format
            # as the URL param (JP keeps the '.T' suffix, US is bare). The
            # existing _latest_fy_metric calls pass `ticker` directly, so we
            # do the same here.
            if entity_id:
                series_mcap = await _mcap_annual_series(conn, entity_id, jurisdiction, year_min, year_max)
            series_rev_cagr = await _series_from_metrics(
                conn, ticker, jurisdiction,
                "revenue_compound_annual_growth_rate_5_year", year_min, year_max,
            )
            series_rev_yoy = await _series_from_metrics(
                conn, ticker, jurisdiction,
                "revenue_growth_year_over_year", year_min, year_max,
            )
            series_ev_ebitda = await _series_from_metrics(
                conn, ticker, jurisdiction,
                "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
                year_min, year_max,
            )
            series_return = await _return_annual_series(conn, ticker, jurisdiction, year_min, year_max)
            series_div_yield = await _series_from_metrics(
                conn, ticker, jurisdiction,
                "dividend_yield", year_min, year_max,
            )

    rev_cagr_delta = None
    if rev_cagr_5y is not None and rev_cagr_3y is not None:
        rev_cagr_delta = rev_cagr_5y - rev_cagr_3y

    chips = {
        "market_cap": KpiChip(
            label="Market cap",
            value=mcap,
            formatted=_fmt_currency(mcap, jurisdiction),
            delta_pct=mcap_delta,
            delta_label=(f"{'▲' if (mcap_delta or 0) >= 0 else '▼'} "
                         f"{_fmt_pct(mcap_delta)} YoY") if mcap_delta is not None else "",
            delta_direction=_direction(mcap_delta),
            series=series_mcap or None,
        ),
        "revenue_cagr_5y": KpiChip(
            label="Revenue CAGR 5Y",
            value=rev_cagr_5y,
            formatted=_fmt_pct(rev_cagr_5y),
            delta_pct=rev_cagr_delta,
            delta_label=(f"▲ vs {_fmt_pct(rev_cagr_3y)} 3Y"
                         if rev_cagr_delta is not None and rev_cagr_delta >= 0 and rev_cagr_3y is not None
                         else (f"▼ vs {_fmt_pct(rev_cagr_3y)} 3Y"
                               if rev_cagr_delta is not None and rev_cagr_3y is not None else "")),
            delta_direction=_direction(rev_cagr_delta),
            series=series_rev_cagr or None,
        ),
        "eps_growth": KpiChip(
            label="Revenue YoY",
            value=rev_yoy,
            formatted=_fmt_pct(rev_yoy, signed=True),
            delta_direction=_direction(rev_yoy),
            delta_label=("▲ accelerating" if (rev_yoy or 0) > 0
                         else ("▼ contracting" if (rev_yoy or 0) < 0 else "")),
            series=series_rev_yoy or None,
        ),
        "ev_ebitda": KpiChip(
            label="EV / EBITDA",
            value=ev_ebitda,
            formatted=_fmt_ratio(ev_ebitda),
            delta_direction="neu",
            delta_label="latest filing",
            series=series_ev_ebitda or None,
        ),
        "return_1y": KpiChip(
            label="1Y return",
            value=price_delta,
            formatted=_fmt_pct(price_delta, signed=True),
            delta_direction=_direction(price_delta),
            delta_label="vs prior year",
            series=series_return or None,
        ),
        "dividend_yield": KpiChip(
            label="Div. yield",
            value=div_yield,
            formatted=_fmt_pct(div_yield, decimals=2),
            delta_pct=div_growth,
            delta_direction=_direction(div_growth),
            delta_label=(f"▲ {_fmt_pct(div_growth)} growth"
                         if (div_growth or 0) > 0
                         else (f"▼ {_fmt_pct(div_growth)} growth"
                               if (div_growth or 0) < 0 else "")),
            series=series_div_yield or None,
        ),
    }

    return KpiResponse(ticker=ticker, period=period, chips=chips)
