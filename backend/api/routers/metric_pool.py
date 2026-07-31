from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..db import acquire


router = APIRouter()


class MetricPoolItem(BaseModel):
    metric_id: str
    name: str
    category: str
    unit_type: Optional[str] = None
    value: Optional[float] = None
    formatted: str = "\u2014"
    delta_direction: Literal["up", "down", "neu"] = "neu"
    delta_label: str = ""
    series: list[float] = Field(default_factory=list)


class MetricPoolResponse(BaseModel):
    ticker: str
    jurisdiction: Literal["US", "JP"]
    metrics: list[MetricPoolItem]


def _fmt(unit_type: Optional[str], category: str, v: Optional[float], jurisdiction: str = "US") -> str:
    if v is None:
        return "\u2014"
    ut = (unit_type or "").upper()
    cat = (category or "").lower()

    # Growth metrics are stored as decimal ratios but read naturally as percentages.
    if cat == "growth" or ut in ("PERCENT", "PCT", "RATIO_PCT"):
        pct = v * 100
        sign = "+" if pct > 0 else ""
        return f"{sign}{pct:.1f}%"
    if ut == "RATIO":
        return f"{v:.1f}\u00d7"
    if ut in ("CCY", "CURRENCY", "USD", "JPY"):
        prefix = "\u00a5" if jurisdiction == "JP" else "$"
        a = abs(v)
        if a >= 1e12:
            return f"{prefix}{v / 1e12:.2f}T"
        if a >= 1e9:
            return f"{prefix}{v / 1e9:.1f}B"
        if a >= 1e6:
            return f"{prefix}{v / 1e6:.1f}M"
        return f"{prefix}{v:,.0f}"
    if ut in ("PER_SHARE", "PERSHARE"):
        prefix = "\u00a5" if jurisdiction == "JP" else "$"
        return f"{prefix}{v:.2f}"
    if ut == "DAYS":
        return f"{v:.0f}d"
    if ut == "COUNT":
        return f"{int(v):,}"
    return f"{v:.2f}"


def _direction(v: Optional[float]) -> Literal["up", "down", "neu"]:
    if v is None or abs(v) < 1e-9:
        return "neu"
    return "up" if v > 0 else "down"


def _period_order(alias: str = "") -> str:
    p = f"{alias}fiscal_period" if alias else "fiscal_period"
    return f"""
        CASE {p}
            WHEN 'FY' THEN 8
            WHEN 'Annual' THEN 8
            WHEN 'Q4' THEN 7
            WHEN 'Q3' THEN 6
            WHEN 'Q2' THEN 5
            WHEN 'H1' THEN 4
            WHEN 'SemiAnnual' THEN 4
            WHEN 'Q1' THEN 3
            WHEN 'Q' THEN 2
            ELSE 1
        END
    """


async def _entity_id(conn, ticker: str, jurisdiction: str) -> Optional[str]:
    if jurisdiction == "US":
        row = await conn.fetchrow(
            "SELECT cik::text AS entity_id FROM sec.dim_company_us WHERE primary_ticker = $1 LIMIT 1",
            ticker,
        )
    else:
        row = await conn.fetchrow(
            "SELECT edinet_code AS entity_id FROM sec.dim_company_jp WHERE primary_ticker = $1 LIMIT 1",
            ticker,
        )
    return row["entity_id"] if row and row["entity_id"] else None


@router.get("", response_model=MetricPoolResponse)
async def metric_pool(
    ticker: str = Query(..., description="Ticker symbol (US format e.g. AAPL, JP format e.g. 7203.T)"),
    jurisdiction: Literal["US", "JP"] = Query(..., description="Jurisdiction"),
    categories: str = Query("growth,valuation", description="Comma-separated metric categories"),
    periods: int = Query(8, ge=2, le=40, description="Max series points per metric"),
) -> MetricPoolResponse:
    """Return metrics from the requested categories that have ticker data."""
    cats = [c.strip().lower() for c in categories.split(",") if c.strip()]
    fact_table = "sec.fact_metrics_us" if jurisdiction == "US" else "sec.fact_metrics_jp"
    std_table = "sec.fact_fundamentals_std_us" if jurisdiction == "US" else "sec.fact_fundamentals_std_jp"
    std_entity_col = "cik" if jurisdiction == "US" else "edinet_code"
    metric_period_desc = _period_order("m.")
    std_period_desc = _period_order("s.")
    period_asc = _period_order("")

    async with acquire() as conn:
        defs = await conn.fetch(
            """
            SELECT metric_id, name, category, unit_type
            FROM   sec.ref_metric_definitions
            WHERE  LOWER(category) = ANY($1)
            ORDER  BY metric_id
            """,
            cats,
        )
        if not defs:
            return MetricPoolResponse(ticker=ticker, jurisdiction=jurisdiction, metrics=[])

        metric_ids = [d["metric_id"] for d in defs]

        rows = await conn.fetch(
            f"""
            WITH ranked AS (
                SELECT m.metric_id, m.fiscal_year, m.fiscal_period, m.period_end, m.value,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.metric_id
                           ORDER BY m.fiscal_year DESC,
                                    {metric_period_desc} DESC,
                                    m.period_end DESC NULLS LAST
                       ) AS rn
                FROM   {fact_table} m
                WHERE  m.ticker = $1
                  AND  m.metric_id = ANY($2)
                  AND  m.value IS NOT NULL
            )
            SELECT metric_id, fiscal_year, fiscal_period, period_end, value
            FROM   ranked
            WHERE  rn <= $3
            ORDER  BY metric_id,
                      fiscal_year ASC,
                      {period_asc} ASC,
                      period_end ASC NULLS FIRST
            """,
            ticker, metric_ids, periods,
        )

        if not rows:
            entity_id = await _entity_id(conn, ticker, jurisdiction)
            if entity_id:
                rows = await conn.fetch(
                    f"""
                    WITH ranked AS (
                        SELECT s.line_item_id AS metric_id,
                               s.fiscal_year,
                               s.fiscal_period,
                               s.period_end,
                               s.value,
                               ROW_NUMBER() OVER (
                                   PARTITION BY s.line_item_id
                                   ORDER BY s.fiscal_year DESC,
                                            {std_period_desc} DESC,
                                            s.period_end DESC NULLS LAST
                               ) AS rn
                        FROM   {std_table} s
                        WHERE  s.{std_entity_col} = $1
                          AND  s.line_item_id = ANY($2)
                          AND  s.value IS NOT NULL
                    )
                    SELECT metric_id, fiscal_year, fiscal_period, period_end, value
                    FROM   ranked
                    WHERE  rn <= $3
                    ORDER  BY metric_id,
                              fiscal_year ASC,
                              {period_asc} ASC,
                              period_end ASC NULLS FIRST
                    """,
                    entity_id, metric_ids, periods,
                )

    by_metric: dict[str, list[float]] = {}
    for r in rows:
        by_metric.setdefault(r["metric_id"], []).append(float(r["value"]))

    items: list[MetricPoolItem] = []
    for d in defs:
        mid = d["metric_id"]
        series = by_metric.get(mid)
        if not series:
            continue
        latest = series[-1]
        items.append(
            MetricPoolItem(
                metric_id=mid,
                name=d["name"] or mid,
                category=d["category"] or "",
                unit_type=d["unit_type"],
                value=latest,
                formatted=_fmt(d["unit_type"], d["category"], latest, jurisdiction),
                delta_direction=_direction(latest),
                delta_label="latest",
                series=series,
            )
        )

    return MetricPoolResponse(ticker=ticker, jurisdiction=jurisdiction, metrics=items[:30])
