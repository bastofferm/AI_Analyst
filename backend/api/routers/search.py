"""Global search endpoint — equities (US + JP) and institutional managers."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.search")


class SearchResult(BaseModel):
    type: Literal["equity", "manager", "institutional_security"]
    id: str              # ticker for equity, manager_cik for manager
    label: str           # display name shown in dropdown
    sub: str             # secondary line (exchange / sector / type)
    href: str            # navigation target
    jurisdiction: Literal["US", "JP", ""] = ""


async def _search_us(conn, pattern: str, exact: str, limit: int) -> list[SearchResult]:
    try:
        rows = await conn.fetch(
            """
            SELECT primary_ticker, name, exchange, gics_sector_name
            FROM   dim_company_us
            WHERE  primary_ticker IS NOT NULL
              AND  (
                       primary_ticker ILIKE $1
                    OR cik::text      ILIKE $1
                    OR name           ILIKE $1
              )
            ORDER BY
                CASE WHEN UPPER(primary_ticker) = $2 THEN 0
                     WHEN UPPER(primary_ticker) LIKE $3 THEN 1
                     ELSE 2 END,
                primary_ticker
            LIMIT $4
            """,
            pattern, exact, f"{exact}%", limit,
        )
    except Exception as exc:
        logger.warning("US search failed: %s", exc)
        return []

    out: list[SearchResult] = []
    for r in rows:
        sub_bits = [r["name"] or ""]
        if r["exchange"]:         sub_bits.append(r["exchange"])
        if r["gics_sector_name"]: sub_bits.append(r["gics_sector_name"])
        out.append(SearchResult(
            type="equity",
            id=r["primary_ticker"],
            label=r["primary_ticker"],
            sub=" - ".join([s for s in sub_bits if s]),
            href=f"/equities?ticker={r['primary_ticker']}&j=US&period=FY&tab=fundamental&statement=BS",
            jurisdiction="US",
        ))
    return out


async def _search_jp(conn, pattern: str, exact: str, limit: int) -> list[SearchResult]:
    try:
        rows = await conn.fetch(
            """
            SELECT primary_ticker,
                   COALESCE(name_en, name, primary_ticker) AS name,
                   edinet_code,
                   gics_sector_name
            FROM   dim_company_jp
            WHERE  primary_ticker IS NOT NULL
              AND  (
                       primary_ticker ILIKE $1
                    OR edinet_code    ILIKE $1
                    OR name           ILIKE $1
                    OR name_en        ILIKE $1
              )
            ORDER BY
                CASE WHEN UPPER(primary_ticker) = $2 THEN 0
                     WHEN UPPER(primary_ticker) LIKE $3 THEN 1
                     ELSE 2 END,
                primary_ticker
            LIMIT $4
            """,
            pattern, exact, f"{exact}%", limit,
        )
    except Exception as exc:
        logger.warning("JP search failed: %s", exc)
        return []

    out: list[SearchResult] = []
    for r in rows:
        sub_bits = [r["name"] or ""]
        if r["edinet_code"]:      sub_bits.append(f"EDINET {r['edinet_code']}")
        if r["gics_sector_name"]: sub_bits.append(r["gics_sector_name"])
        out.append(SearchResult(
            type="equity",
            id=r["primary_ticker"],
            label=r["primary_ticker"],
            sub=" · ".join([s for s in sub_bits if s]),
            href=f"/equities?ticker={r['primary_ticker']}&j=JP&period=FY&tab=fundamental&statement=BS",
            jurisdiction="JP",
        ))
    return out


async def _search_managers(conn, pattern: str, exact: str, limit: int) -> list[SearchResult]:
    try:
        if not await conn.fetchval("SELECT to_regclass('sec.dim_13f_manager') IS NOT NULL"):
            return []
        rows = await conn.fetch(
            """
            SELECT manager_cik, manager_name, manager_type
            FROM   dim_13f_manager
            WHERE  manager_name ILIKE $1
               OR  manager_cik  ILIKE $1
            ORDER BY
                CASE WHEN manager_cik = $2 THEN 0 ELSE 1 END,
                manager_name
            LIMIT $3
            """,
            pattern, exact, limit,
        )
    except Exception as exc:
        logger.warning("Manager search failed: %s", exc)
        return []

    out: list[SearchResult] = []
    for r in rows:
        sub_bits = [f"Manager · CIK {r['manager_cik']}"]
        if r["manager_type"]: sub_bits.append(r["manager_type"])
        out.append(SearchResult(
            type="manager",
            id=r["manager_cik"],
            label=r["manager_name"] or r["manager_cik"],
            sub=" · ".join(sub_bits),
            href=f"/institutional/manager/{r['manager_cik']}",
            jurisdiction="",
        ))
    return out


async def _search_13f_securities(conn, pattern: str, exact: str, limit: int) -> list[SearchResult]:
    try:
        if not await conn.fetchval("SELECT to_regclass('sec.dim_13f_security_us') IS NOT NULL"):
            return []
        rows = await conn.fetch(
            """
            SELECT s.primary_ticker,
                   COALESCE(MAX(NULLIF(s.issuer_name, '')), MAX(NULLIF(s.security_title, '')), s.primary_ticker) AS name,
                   COALESCE(MAX(NULLIF(s.asset_bucket, '')), 'other') AS asset_bucket,
                   COUNT(DISTINCT s.cusip) AS cusips,
                   BOOL_OR(s.isin IS NOT NULL) AS has_isin
            FROM dim_13f_security_us s
            WHERE s.primary_ticker IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM dim_company_us c
                  WHERE upper(c.primary_ticker) = upper(s.primary_ticker)
              )
              AND (
                     s.primary_ticker ILIKE $1
                  OR s.cusip ILIKE $1
                  OR s.isin ILIKE $1
                  OR s.issuer_name ILIKE $1
                  OR s.security_title ILIKE $1
              )
            GROUP BY s.primary_ticker
            ORDER BY
                CASE WHEN UPPER(s.primary_ticker) = $2 THEN 0
                     WHEN UPPER(s.primary_ticker) LIKE $3 THEN 1
                     ELSE 2 END,
                s.primary_ticker
            LIMIT $4
            """,
            pattern, exact, f"{exact}%", limit,
        )
    except Exception as exc:
        logger.warning("13F security search failed: %s", exc)
        return []

    out: list[SearchResult] = []
    for r in rows:
        sub_bits = [r["name"] or ""]
        sub_bits.append(f"13F {r['asset_bucket']}")
        sub_bits.append(f"{r['cusips']} CUSIP{'s' if r['cusips'] != 1 else ''}")
        if r["has_isin"]:
            sub_bits.append("ISIN")
        out.append(SearchResult(
            type="institutional_security",
            id=r["primary_ticker"],
            label=r["primary_ticker"],
            sub=" · ".join([s for s in sub_bits if s]),
            href=f"/institutional/security/{r['primary_ticker']}",
            jurisdiction="US",
        ))
    return out


@router.get("", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(default=12, le=30),
) -> list[SearchResult]:
    """
    Search equities (US + JP) and institutional managers by ticker, name, or CIK.
    Each source is queried independently; failures degrade gracefully.
    """
    q = q.strip()
    if not q:
        return []

    pattern = f"%{q}%"
    exact   = q.upper()

    results: list[SearchResult] = []
    try:
        async with acquire() as conn:
            results.extend(await _search_us(conn, pattern, exact, limit))
            results.extend(await _search_jp(conn, pattern, exact, limit))
            results.extend(await _search_13f_securities(conn, pattern, exact, limit))
            results.extend(await _search_managers(conn, pattern, q, limit))
    except Exception as exc:
        logger.exception("search failed at acquire(): %s", exc)
        return []

    # Deduplicate by (type, id), keep first occurrence
    seen: set[tuple[str, str]] = set()
    deduped: list[SearchResult] = []
    for item in results:
        key = (item.type, item.id)
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:limit]
