from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Query

from ..db import acquire
from ..models.company import Company


router = APIRouter()


def _split(csv: Optional[str]) -> Optional[List[str]]:
    if not csv:
        return None
    parts = [p.strip() for p in csv.split(",") if p.strip()]
    return parts or None


@router.get("", response_model=List[Company])
async def list_companies(
    jurisdiction: Literal["US", "JP"] = Query(...),
    exchange: Optional[str] = Query(None, description="Comma-separated exchange list"),
    sector: Optional[str] = Query(None, description="Comma-separated GICS sector codes"),
    industry: Optional[str] = Query(None, description="Comma-separated GICS industry-group codes"),
    q: Optional[str] = Query(None, description="Ticker / name search substring"),
    limit: int = Query(200, ge=1, le=1000),
) -> List[Company]:
    exchanges = _split(exchange)
    sectors = _split(sector)
    industries = _split(industry)
    q_like = f"%{q.strip()}%" if q and q.strip() else None

    if jurisdiction == "US":
        sql = """
            SELECT primary_ticker            AS ticker,
                   name,
                   cik::text                 AS cik,
                   exchange,
                   gics_sector_code,
                   gics_industry_group_code
            FROM   dim_company_us
            WHERE  primary_ticker IS NOT NULL
              AND  COALESCE(include_in_pipeline, true)
              AND  ($1::text[] IS NULL OR exchange = ANY($1))
              AND  ($2::text[] IS NULL OR gics_sector_code = ANY($2))
              AND  ($3::text[] IS NULL OR gics_industry_group_code = ANY($3))
              AND  ($4::text IS NULL OR primary_ticker ILIKE $4 OR name ILIKE $4)
            ORDER  BY primary_ticker
            LIMIT  $5
        """
        params = (exchanges, sectors, industries, q_like, limit)
    else:
        sql = """
            SELECT primary_ticker                                AS ticker,
                   COALESCE(name_en, name, primary_ticker)       AS name,
                   edinet_code,
                   NULL::text                                    AS exchange,
                   gics_sector_code,
                   gics_industry_group_code
            FROM   dim_company_jp
            WHERE  primary_ticker IS NOT NULL
              AND  COALESCE(include_in_pipeline, true)
              AND  ($1::text[] IS NULL OR gics_sector_code = ANY($1))
              AND  ($2::text[] IS NULL OR gics_industry_group_code = ANY($2))
              AND  ($3::text IS NULL OR primary_ticker ILIKE $3
                    OR COALESCE(name_en, name) ILIKE $3)
            ORDER  BY primary_ticker
            LIMIT  $4
        """
        params = (sectors, industries, q_like, limit)

    async with acquire() as conn:
        rows = await conn.fetch(sql, *params)

    if jurisdiction == "US":
        return [
            Company(
                ticker=r["ticker"],
                name=r["name"] or r["ticker"],
                cik=r["cik"],
                exchange=r["exchange"],
                gics_sector_code=r["gics_sector_code"],
                gics_industry_group_code=r["gics_industry_group_code"],
            )
            for r in rows
        ]
    return [
        Company(
            ticker=r["ticker"],
            name=r["name"] or r["ticker"],
            edinet_code=r["edinet_code"],
            gics_sector_code=r["gics_sector_code"],
            gics_industry_group_code=r["gics_industry_group_code"],
        )
        for r in rows
    ]
