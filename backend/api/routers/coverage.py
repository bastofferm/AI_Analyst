from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from ..db import acquire
from ..models.financials import (
    CoverageMatrix,
    CoverageResponse,
    FilingCoverageState,
)


router = APIRouter()


_FULL_THRESHOLD_FY = 10
_FULL_THRESHOLD_Q  = 6


@router.get("/{ticker}", response_model=CoverageResponse)
async def get_filing_coverage(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    max_columns: int = Query(17, ge=5, le=40),
) -> CoverageResponse:
    if jurisdiction == "US":
        resolve_sql = "SELECT cik::text AS eid FROM dim_company_us WHERE primary_ticker=$1 LIMIT 1"
        cov_sql = """
            SELECT fiscal_year, fiscal_period, COUNT(*) AS n
            FROM   fact_fundamentals_std_us
            WHERE  cik = $1
            GROUP  BY fiscal_year, fiscal_period
        """
    else:
        resolve_sql = "SELECT edinet_code AS eid FROM dim_company_jp WHERE primary_ticker=$1 LIMIT 1"
        cov_sql = """
            SELECT fiscal_year, fiscal_period, COUNT(*) AS n
            FROM   fact_fundamentals_std_jp
            WHERE  edinet_code = $1
            GROUP  BY fiscal_year, fiscal_period
        """

    async with acquire() as conn:
        row = await conn.fetchrow(resolve_sql, ticker)
        if row is None or not row["eid"]:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
        entity_id = row["eid"]
        cov_rows = await conn.fetch(cov_sql, entity_id)

    by_period: dict[str, dict[int, int]] = {
        "FY": {},
        "H1": {},
        "Q1": {},
        "Q2": {},
        "Q3": {},
        "Q4": {},
    }
    for r in cov_rows:
        fp = (r["fiscal_period"] or "").upper()
        if fp in ("ANNUAL",):
            fp = "FY"
        elif jurisdiction == "JP" and fp in ("SEMIANNUAL", "Q"):
            fp = "H1"
        if fp not in by_period:
            continue
        yr = int(r["fiscal_year"])
        by_period[fp][yr] = int(r["n"])

    all_years = sorted({y for periods in by_period.values() for y in periods})
    if not all_years:
        return CoverageResponse(ticker=ticker, years=[], matrix=CoverageMatrix())

    if len(all_years) > max_columns:
        all_years = all_years[-max_columns:]
    year_min, year_max = all_years[0], all_years[-1]
    years = list(range(year_min, year_max + 1))

    def classify(n: int, threshold: int) -> FilingCoverageState:
        if n >= threshold:
            return "filled"
        if n >= 1:
            return "partial"
        return "miss"

    matrix = CoverageMatrix()

    for y in years:
        fy_n = by_period["FY"].get(y, 0)
        matrix.FY[y] = classify(fy_n, _FULL_THRESHOLD_FY) if fy_n else "miss"
        h1_n = by_period["H1"].get(y, 0)
        matrix.H1[y] = classify(h1_n, _FULL_THRESHOLD_Q) if h1_n else "miss"
        for qp in ("Q1", "Q2", "Q3", "Q4"):
            q_n = by_period[qp].get(y, 0)
            if q_n == 0:
                has_any_q = any(by_period[qp].get(yy, 0) > 0 for yy in years)
                matrix.__dict__[qp][y] = "miss" if has_any_q else "empty"
            else:
                matrix.__dict__[qp][y] = classify(q_n, _FULL_THRESHOLD_Q)

    return CoverageResponse(ticker=ticker, years=years, matrix=matrix)
