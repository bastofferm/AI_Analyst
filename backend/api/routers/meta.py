from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query

from ..db import acquire
from ..models.company import (
    ExchangeOption,
    FilterOptions,
    IndustryOption,
    MetaResponse,
    SectorOption,
)


router = APIRouter()


_EXCHANGE_FLAG = {"Nasdaq": "🇺🇸", "NYSE": "🇺🇸", "CBOE": "🇺🇸", "OTC": "🇺🇸"}


@router.get("/filters", response_model=MetaResponse)
async def get_filters(
    jurisdiction: Literal["US", "JP", "INTL"] = Query(...),
    country_code: str | None = Query(default=None, description="ISO-2, only meaningful when jurisdiction=INTL"),
) -> MetaResponse:
    table = {"US": "dim_company_us", "JP": "dim_company_jp", "INTL": "dim_company_intl"}[jurisdiction]
    exchange_sql = None
    where_extra = ""
    args: list[Any] = []
    if jurisdiction == "US":
        exchange_sql = f"""
            SELECT COALESCE(exchange, '__none__') AS exchange, COUNT(*) AS n
            FROM   {table}
            WHERE  primary_ticker IS NOT NULL
              AND  COALESCE(include_in_pipeline, true)
            GROUP  BY 1
            ORDER  BY n DESC
        """
    if jurisdiction == "INTL" and country_code:
        args.append(country_code.strip().upper())
        where_extra = f" AND country_code = ${len(args)}"

    sector_sql = f"""
        SELECT DISTINCT gics_sector_code AS code, gics_sector_name AS name
        FROM   {table}
        WHERE  primary_ticker IS NOT NULL
          AND  COALESCE(include_in_pipeline, true)
          AND  gics_sector_code IS NOT NULL
          {where_extra}
        ORDER  BY 1
    """
    industry_sql = f"""
        SELECT DISTINCT
               gics_industry_group_code AS code,
               gics_industry_group_name AS name,
               gics_sector_code         AS sector_code
        FROM   {table}
        WHERE  primary_ticker IS NOT NULL
          AND  COALESCE(include_in_pipeline, true)
          AND  gics_industry_group_code IS NOT NULL
          {where_extra}
        ORDER  BY 1
    """

    exchanges: list[ExchangeOption] = []
    async with acquire() as conn:
        if exchange_sql:
            for r in await conn.fetch(exchange_sql):
                ex = r["exchange"]
                label = "Unknown" if ex == "__none__" else ex
                flag = _EXCHANGE_FLAG.get(ex, "")
                exchanges.append(
                    ExchangeOption(
                        value=ex,
                        label=f"{flag} {label}".strip(),
                        count=int(r["n"]),
                    )
                )
        sectors = [SectorOption(code=r["code"], name=r["name"] or "Unknown")
                   for r in await conn.fetch(sector_sql, *args)]
        industries = [
            IndustryOption(
                code=r["code"],
                name=r["name"] or "Unknown",
                sector_code=r["sector_code"],
            )
            for r in await conn.fetch(industry_sql, *args)
        ]

    return MetaResponse(
        jurisdiction=jurisdiction,
        filters=FilterOptions(
            exchanges=exchanges,
            sectors=sectors,
            industries=industries,
        ),
    )


@router.get("/default-ticker")
async def default_ticker(
    jurisdiction: Literal["US", "JP"] = Query(...),
) -> dict:
    """Return the default ticker for a jurisdiction. Prefers AAPL for US,
    alphabetically-first for JP (matches mzqa_terminal_v2.py JP_DEFAULT_TICKER)."""
    tbl = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    async with acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT primary_ticker
            FROM   {tbl}
            WHERE  primary_ticker IS NOT NULL
              AND  COALESCE(include_in_pipeline, true)
            ORDER  BY (CASE WHEN primary_ticker = 'AAPL' THEN 0 ELSE 1 END),
                     primary_ticker
            LIMIT  1
        """)
    return {
        "jurisdiction": jurisdiction,
        "ticker": row["primary_ticker"] if row else None,
    }
