"""Home hero stats — payload for the /home landing page.

Returns n_companies (US + JP combined) plus metric-catalog counts. Designed to
be SSR-prefetched by the Next.js home page and client-side recovered if the
SSR call fails.

5-minute in-process cache; empty payloads (n_companies=0) are never cached so
transient DB failures self-heal next request.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.home_hero_stats")

_CACHE: dict[str, tuple[float, "HomeHeroStats"]] = {}
_TTL = 300


class HomeHeroStats(BaseModel):
    n_companies: int
    n_metrics_defined: Optional[int] = None
    n_metrics_computed: Optional[int] = None


async def _company_count(conn) -> int:
    n = 0
    for tbl in ("dim_company_us", "dim_company_jp"):
        try:
            row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT primary_ticker) AS n "
                f"FROM {tbl} WHERE primary_ticker IS NOT NULL"
            )
            n += int(row["n"]) if row and row["n"] is not None else 0
        except Exception as exc:
            logger.warning("home_hero_stats: %s count failed: %r", tbl, exc)
    return n


async def _metrics_count(conn) -> tuple[Optional[int], Optional[int]]:
    """Returns (n_defined, n_computed):
      - n_defined: row count in sec.ref_metric_definitions (catalog size)
      - n_computed: distinct metric_id populated in fact_metrics_us ∪ fact_metrics_jp
    Each is independently optional — if either query fails, that field is None.
    """
    defined: Optional[int] = None
    computed: Optional[int] = None
    try:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM sec.ref_metric_definitions")
        if row and row["n"] is not None:
            defined = int(row["n"])
    except Exception as exc:
        logger.warning("home_hero_stats: ref_metric_definitions count failed: %r", exc)

    try:
        row = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT metric_id) AS n FROM (
                SELECT metric_id FROM fact_metrics_us
                UNION
                SELECT metric_id FROM fact_metrics_jp
            ) m
            """
        )
        if row and row["n"] is not None:
            computed = int(row["n"])
    except Exception as exc:
        logger.warning("home_hero_stats: computed metrics count failed: %r", exc)

    return defined, computed


@router.get("/ping")
async def home_hero_ping() -> dict:
    return {"ok": True, "version": "2026-06-02-A", "msg": "home_hero_stats router loaded"}


@router.get("", response_model=HomeHeroStats)
async def home_hero_stats() -> HomeHeroStats:
    cached = _CACHE.get("default")
    if cached and cached[0] > time.time():
        return cached[1]

    async with acquire() as conn:
        n_companies = await _company_count(conn)
        n_defined, n_computed = await _metrics_count(conn)

    result = HomeHeroStats(
        n_companies=n_companies,
        n_metrics_defined=n_defined,
        n_metrics_computed=n_computed,
    )

    if n_companies > 0:
        _CACHE["default"] = (time.time() + _TTL, result)
    else:
        logger.warning("home_hero_stats: n_companies=0 — NOT caching")
    return result
