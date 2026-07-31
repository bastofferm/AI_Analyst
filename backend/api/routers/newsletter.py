"""Newsletter page endpoints — chart-specific aggregations.

Backs the /newsletter Next.js page:
  - GET /factor-heatmap       — annual returns, 6 factors × N years
  - GET /factor-correlation   — trailing 36M Pearson correlation matrix
  - GET /macro-regime         — quarterly growth/inflation scatter per jurisdiction
  - GET /sector-drift         — institutional composite sector weights by quarter

Reads from existing tables: fact_fama_french (with AQR datasets),
fact_macro_regime, core_13f_holding.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.newsletter")


JurisdictionCode = Literal["US", "JP", "EZ", "CH"]


# ---------------------------------------------------------------------------
# Factor heatmap (Chart 1)
# ---------------------------------------------------------------------------

# Factor identifiers as stored in fact_fama_french.factor across the
# datasets we union. Ken French uses HML/Mom/RMW/SMB; AQR ingest writes
# 'Low Vol' (BAB) and 'Div. Yield' (HML-Devil). Display order matches the
# HTML mockup row order.
_FACTOR_DISPLAY: list[tuple[str, str]] = [
    # (display_name, fact_fama_french.factor key)
    ("Value",      "HML"),
    ("Momentum",   "Mom"),
    ("Quality",    "RMW"),
    ("Low Vol",    "Low Vol"),
    ("Size",       "SMB"),
    ("Div. Yield", "Div. Yield"),
]

_HEATMAP_DATASETS = (
    "Developed_5_Factors_Daily",
    "Developed_Mom_Factor_Daily",
    "AQR:BAB_Developed_Daily",
    "AQR:HML_Devil_Developed_Daily",
)


class FactorHeatmapRow(BaseModel):
    factor: str
    year: int
    return_pct: float | None


class FactorHeatmapResponse(BaseModel):
    factors: list[str]
    years: list[int]
    rows: list[FactorHeatmapRow]


@router.get("/factor-heatmap", response_model=FactorHeatmapResponse)
async def factor_heatmap(
    start_year: int = Query(2019, ge=2000, le=2100),
) -> FactorHeatmapResponse:
    """Compound daily factor returns into annual % returns per (factor, year)."""
    sql = """
        WITH src AS (
            SELECT  factor,
                    EXTRACT(year FROM date)::int AS year,
                    -- AQR + Ken French publish factor returns as percent. Compound
                    -- daily percent returns: exp(sum(ln(1 + r/100))) - 1, expressed in %
                    LN(1.0 + value / 100.0) AS ln_factor
            FROM    fact_fama_french
            WHERE   dataset = ANY($1)
              AND   factor  = ANY($2)
              AND   date >= make_date($3, 1, 1)
              AND   value IS NOT NULL
        )
        SELECT  factor, year, (EXP(SUM(ln_factor)) - 1.0) * 100.0 AS annual_return_pct
        FROM    src
        GROUP   BY factor, year
        ORDER   BY factor, year
    """
    wanted_keys = [k for _, k in _FACTOR_DISPLAY]
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                sql,
                list(_HEATMAP_DATASETS),
                wanted_keys,
                start_year,
            )
    except Exception as exc:
        logger.warning("factor_heatmap query failed: %s", exc)
        return FactorHeatmapResponse(factors=[d for d, _ in _FACTOR_DISPLAY], years=[], rows=[])

    # Map fact key -> display name (preserve mockup ordering).
    key_to_display = {k: d for d, k in _FACTOR_DISPLAY}
    out: list[FactorHeatmapRow] = []
    years_set: set[int] = set()
    for r in rows:
        d = key_to_display.get(r["factor"])
        if d is None:
            continue
        years_set.add(int(r["year"]))
        out.append(
            FactorHeatmapRow(
                factor=d,
                year=int(r["year"]),
                return_pct=float(r["annual_return_pct"]) if r["annual_return_pct"] is not None else None,
            )
        )
    years = sorted(years_set)
    return FactorHeatmapResponse(
        factors=[d for d, _ in _FACTOR_DISPLAY],
        years=years,
        rows=out,
    )


# ---------------------------------------------------------------------------
# Factor correlation matrix (Chart 6)
# ---------------------------------------------------------------------------

_CORR_DISPLAY: list[tuple[str, str]] = [
    ("Value",    "HML"),
    ("Momentum", "Mom"),
    ("Quality",  "RMW"),
    ("Low Vol",  "Low Vol"),
    ("Size",     "SMB"),
]


class FactorCorrelationMatrix(BaseModel):
    factors: list[str]
    matrix: list[list[float | None]]
    as_of: str
    window_months: int


@router.get("/factor-correlation", response_model=FactorCorrelationMatrix)
async def factor_correlation(
    window_months: int = Query(36, ge=12, le=120),
) -> FactorCorrelationMatrix:
    """Pearson correlation of daily factor returns over the trailing window."""
    sql = """
        SELECT  factor, date, value
        FROM    fact_fama_french
        WHERE   dataset = ANY($1)
          AND   factor  = ANY($2)
          AND   date >= (CURRENT_DATE - ($3::int || ' months')::interval)::date
          AND   value IS NOT NULL
        ORDER   BY date, factor
    """
    wanted_keys = [k for _, k in _CORR_DISPLAY]
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                sql,
                list(_HEATMAP_DATASETS),
                wanted_keys,
                window_months,
            )
    except Exception as exc:
        logger.warning("factor_correlation query failed: %s", exc)
        return FactorCorrelationMatrix(
            factors=[d for d, _ in _CORR_DISPLAY],
            matrix=[[None] * len(_CORR_DISPLAY) for _ in _CORR_DISPLAY],
            as_of="",
            window_months=window_months,
        )

    if not rows:
        return FactorCorrelationMatrix(
            factors=[d for d, _ in _CORR_DISPLAY],
            matrix=[[None] * len(_CORR_DISPLAY) for _ in _CORR_DISPLAY],
            as_of="",
            window_months=window_months,
        )

    # Build {date: {factor: value}} → align series.
    import math
    by_date: dict = {}
    max_date = None
    for r in rows:
        d = r["date"]
        max_date = d if (max_date is None or d > max_date) else max_date
        by_date.setdefault(d, {})[r["factor"]] = float(r["value"])

    factor_keys = wanted_keys
    aligned: dict[str, list[float]] = {k: [] for k in factor_keys}
    for d in sorted(by_date):
        row = by_date[d]
        if all(k in row for k in factor_keys):
            for k in factor_keys:
                aligned[k].append(row[k])

    n = len(next(iter(aligned.values())))
    matrix: list[list[float | None]] = [[None] * len(factor_keys) for _ in factor_keys]
    if n >= 30:
        # Compute means
        means = {k: sum(aligned[k]) / n for k in factor_keys}
        for i, ki in enumerate(factor_keys):
            xi = aligned[ki]
            mi = means[ki]
            for j, kj in enumerate(factor_keys):
                if i == j:
                    matrix[i][j] = None  # diagonal shown as em-dash
                    continue
                xj = aligned[kj]
                mj = means[kj]
                num = sum((xi[t] - mi) * (xj[t] - mj) for t in range(n))
                den_i = math.sqrt(sum((xi[t] - mi) ** 2 for t in range(n)))
                den_j = math.sqrt(sum((xj[t] - mj) ** 2 for t in range(n)))
                if den_i == 0 or den_j == 0:
                    matrix[i][j] = None
                else:
                    matrix[i][j] = num / (den_i * den_j)

    return FactorCorrelationMatrix(
        factors=[d for d, _ in _CORR_DISPLAY],
        matrix=matrix,
        as_of=max_date.isoformat() if max_date else "",
        window_months=window_months,
    )


# ---------------------------------------------------------------------------
# Macro regime quadrant (Chart 4) — multi-jurisdiction
# ---------------------------------------------------------------------------

class MacroRegimePoint(BaseModel):
    period_end: str
    fiscal_quarter: str
    growth_z: float | None
    inflation_z: float | None
    quadrant: str | None
    is_current: bool
    # Raw inputs to the z-scores (added in migration 064) so the chart tooltip
    # can show "1.99% QoQ ann." rather than just "-0.22 σ".
    growth_value: float | None = None
    inflation_value: float | None = None
    growth_unit: str | None = None
    inflation_unit: str | None = None


class MacroRegimeResponse(BaseModel):
    jurisdiction: JurisdictionCode
    points: list[MacroRegimePoint]


@router.get("/macro-regime", response_model=MacroRegimeResponse)
async def macro_regime(
    jurisdiction: JurisdictionCode = Query("US"),
    quarters: int = Query(16, ge=4, le=160),       # bumped from 80 → 160 (≈40 years)
) -> MacroRegimeResponse:
    sql = """
        SELECT period_end, fiscal_quarter, growth_z, inflation_z, quadrant, is_current,
               growth_value, inflation_value, growth_unit, inflation_unit
        FROM   fact_macro_regime
        WHERE  jurisdiction = $1
        ORDER  BY period_end DESC
        LIMIT  $2
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, jurisdiction, quarters)
    except Exception as exc:
        logger.warning("newsletter.macro_regime query failed: %s", exc)
        return MacroRegimeResponse(jurisdiction=jurisdiction, points=[])

    # Reverse to chronological order for the chart.
    rows = list(reversed(rows))
    points = [
        MacroRegimePoint(
            period_end=r["period_end"].isoformat(),
            fiscal_quarter=r["fiscal_quarter"],
            growth_z=float(r["growth_z"]) if r["growth_z"] is not None else None,
            inflation_z=float(r["inflation_z"]) if r["inflation_z"] is not None else None,
            quadrant=r["quadrant"],
            is_current=bool(r["is_current"]),
            growth_value=float(r["growth_value"]) if r["growth_value"] is not None else None,
            inflation_value=float(r["inflation_value"]) if r["inflation_value"] is not None else None,
            growth_unit=r["growth_unit"],
            inflation_unit=r["inflation_unit"],
        )
        for r in rows
    ]
    return MacroRegimeResponse(jurisdiction=jurisdiction, points=points)


# ---------------------------------------------------------------------------
# Sector allocation drift (Chart 9)
# ---------------------------------------------------------------------------

class SectorDriftRow(BaseModel):
    report_period: str
    sector: str
    weight_pct: float


class SectorDriftResponse(BaseModel):
    periods: list[str]
    sectors: list[str]
    rows: list[SectorDriftRow]


@router.get("/sector-drift", response_model=SectorDriftResponse)
async def sector_drift(
    quarters: int = Query(6, ge=2, le=20),
) -> SectorDriftResponse:
    """Institutional composite sector weights per quarter."""
    # Join issuer_ticker → dim_company_us.mapping_sector for GICS sector.
    # Falls back to 'Other' for unmapped issuers (ETFs, funds, bonds).
    sql = """
        WITH periods AS (
            SELECT DISTINCT report_period
            FROM   core_13f_holding
            WHERE  is_latest_amendment = TRUE
            ORDER  BY report_period DESC
            LIMIT  $1
        ),
        sector_lookup AS (
            SELECT primary_ticker AS ticker, mapping_sector AS sector
            FROM   dim_company_us
            WHERE  primary_ticker IS NOT NULL
              AND  mapping_sector IS NOT NULL
        ),
        agg AS (
            SELECT
                h.report_period,
                COALESCE(s.sector, 'Other') AS sector,
                SUM(h.market_value_usd)     AS sector_value
            FROM   core_13f_holding h
            JOIN   periods p USING (report_period)
            LEFT   JOIN sector_lookup s ON s.ticker = h.issuer_ticker
            WHERE  h.is_latest_amendment = TRUE
              AND  h.market_value_usd IS NOT NULL
            GROUP  BY h.report_period, sector
        ),
        totals AS (
            SELECT report_period, SUM(sector_value) AS total FROM agg GROUP BY report_period
        )
        SELECT  a.report_period, a.sector,
                (100.0 * a.sector_value / NULLIF(t.total, 0))::float AS weight_pct
        FROM    agg a
        JOIN    totals t USING (report_period)
        ORDER   BY a.report_period, weight_pct DESC NULLS LAST
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, quarters)
    except Exception as exc:
        logger.warning("newsletter.sector_drift query failed: %s", exc)
        return SectorDriftResponse(periods=[], sectors=[], rows=[])

    periods_set: list[str] = []
    sectors_set: list[str] = []
    seen_periods: set = set()
    seen_sectors: set = set()
    out: list[SectorDriftRow] = []
    for r in rows:
        period = r["report_period"].isoformat()
        sector = r["sector"]
        if period not in seen_periods:
            seen_periods.add(period)
            periods_set.append(period)
        if sector not in seen_sectors:
            seen_sectors.add(sector)
            sectors_set.append(sector)
        out.append(
            SectorDriftRow(
                report_period=period,
                sector=sector,
                weight_pct=float(r["weight_pct"]) if r["weight_pct"] is not None else 0.0,
            )
        )
    periods_set.sort()  # chronological order for the chart
    return SectorDriftResponse(periods=periods_set, sectors=sectors_set, rows=out)
