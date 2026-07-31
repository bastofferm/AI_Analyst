from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from ..db import acquire


router = APIRouter()


class MdaSection(BaseModel):
    fiscal_year: Optional[int] = None
    filed_date: Optional[str] = None
    form_type: Optional[str] = None
    section_id: str
    char_count: Optional[int] = None
    excerpt: str


class MdaResponse(BaseModel):
    ticker: str
    sections: list[MdaSection]


_EXCERPT_CHARS = 600


@router.get("/{ticker}", response_model=MdaResponse)
async def get_mda(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    limit: int = Query(8, ge=1, le=40),
) -> MdaResponse:
    if jurisdiction == "US":
        sql_resolve = "SELECT cik::text AS eid FROM dim_company_us WHERE primary_ticker=$1 LIMIT 1"
        sql = """
            SELECT  m.section_id, m.form_type, m.filed_date, m.period_end,
                    m.char_count, m.section_text
            FROM    fact_mda_sections_us m
            WHERE   m.cik = $1
              AND   m.section_text IS NOT NULL
            ORDER   BY m.filed_date DESC NULLS LAST, m.section_id
            LIMIT   $2
        """
    else:
        sql_resolve = "SELECT edinet_code AS eid FROM dim_company_jp WHERE primary_ticker=$1 LIMIT 1"
        sql = """
            SELECT  m.section_id,
                    NULL::text AS form_type,
                    m.filed_date,
                    m.period_end,
                    m.char_count,
                    m.section_text
            FROM    fact_mda_sections_jp m
            WHERE   m.edinet_code = $1
              AND   m.section_text IS NOT NULL
            ORDER   BY m.filed_date DESC NULLS LAST, m.section_id
            LIMIT   $2
        """

    async with acquire() as conn:
        row = await conn.fetchrow(sql_resolve, ticker)
        if row is None or not row["eid"]:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
        rows = await conn.fetch(sql, row["eid"], limit)

    sections: list[MdaSection] = []
    for r in rows:
        text = (r["section_text"] or "").strip()
        excerpt = text[:_EXCERPT_CHARS] + ("…" if len(text) > _EXCERPT_CHARS else "")
        sections.append(MdaSection(
            fiscal_year=r["period_end"].year if r["period_end"] else None,
            filed_date=r["filed_date"].isoformat() if r["filed_date"] else None,
            form_type=r["form_type"],
            section_id=r["section_id"],
            char_count=r["char_count"],
            excerpt=excerpt,
        ))
    return MdaResponse(ticker=ticker, sections=sections)
