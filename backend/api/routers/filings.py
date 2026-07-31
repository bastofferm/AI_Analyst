from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from ..db import acquire


router = APIRouter()


class FilingItem(BaseModel):
    fiscal_year: int
    fiscal_period: str
    filed_date: str | None = None
    filing_form: str | None = None
    filing_id: str | None = None


@router.get("/{ticker}", response_model=list[FilingItem])
async def list_filings(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    limit: int = Query(50, ge=1, le=500),
) -> list[FilingItem]:
    if jurisdiction == "US":
        resolve_sql = "SELECT cik::text AS eid FROM dim_company_us WHERE primary_ticker=$1 LIMIT 1"
        sql = """
            SELECT DISTINCT ON (filing_id)
                   fiscal_year, fiscal_period, filed_date,
                   filing_form, filing_id
            FROM   fact_fundamentals_std_us
            WHERE  cik = $1 AND filing_id IS NOT NULL
            ORDER  BY filing_id, fiscal_year DESC
        """
    else:
        resolve_sql = "SELECT edinet_code AS eid FROM dim_company_jp WHERE primary_ticker=$1 LIMIT 1"
        sql = """
            SELECT DISTINCT ON (filing_id)
                   fiscal_year, fiscal_period, filed_date,
                   filing_form, filing_id
            FROM   fact_fundamentals_std_jp
            WHERE  edinet_code = $1 AND filing_id IS NOT NULL
            ORDER  BY filing_id, fiscal_year DESC
        """

    async with acquire() as conn:
        row = await conn.fetchrow(resolve_sql, ticker)
        if row is None or not row["eid"]:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
        rows = await conn.fetch(sql, row["eid"])

    items = [
        FilingItem(
            fiscal_year=int(r["fiscal_year"]),
            fiscal_period=r["fiscal_period"] or "FY",
            filed_date=r["filed_date"].isoformat() if r["filed_date"] else None,
            filing_form=r["filing_form"],
            filing_id=r["filing_id"],
        )
        for r in rows
    ]
    items.sort(key=lambda x: (x.filed_date or "", x.fiscal_year), reverse=True)
    return items[:limit]
