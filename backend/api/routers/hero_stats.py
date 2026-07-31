"""Hero card stats — live universe summary for the equities landing page.

Data sources (with graceful fallback chain):
  - dim_company_us / dim_company_jp                        → company + sector counts
  - fact_metrics_us / _jp           (canonical target)     → growth aggregates
       ↓ fallback when canonical table is empty
  - fact_fundamentals_std_us / _jp  (interim source)       → same growth fields
       ↓ fallback when no pre-calc growth row exists
  - DERIVED YoY from raw line items (revenue, EPS diluted)
  - sec.fact_cross_asset                                   → ^GSPC / ^N225 sparkline

Once the metrics pipeline populates fact_metrics_*, the std-table fallback
is bypassed automatically — no code change needed.

5-minute in-process cache; empty payloads (n_companies=0) are never cached.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.hero_stats")

# ── Index ticker candidates (sec.fact_cross_asset, yfinance-style symbols) ──
_US_INDEX_TICKERS = ["^GSPC", "GSPC", "SPY", "SPX", "^SPX"]
_JP_INDEX_TICKERS = ["^N225", "N225", "^NKY", "1321", "1306"]
_INDEX_TABLE = "sec.fact_cross_asset"

# ── Revenue-growth histogram bins (decimal: 0.10 → 10 %) ────────────────────
_HIST_BINS: list[tuple[float, float]] = [
    (-1.00, -0.30),
    (-0.30, -0.20),
    (-0.20, -0.10),
    (-0.10, -0.00),
    ( 0.00,  0.10),
    ( 0.10,  0.20),
    ( 0.20,  0.30),
    ( 0.30,  2.00),
]
_HIST_LABELS = ["<−30", "−30·−20", "−20·−10", "−10·0", "0·10", "10·20", "20·30", ">30"]

# ── In-process cache ─────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, "HeroStats"]] = {}
_TTL = 300


class HeroStats(BaseModel):
    jurisdiction: Literal["US", "JP"]
    n_companies: int
    n_sectors: Optional[int] = None
    median_rev_growth: Optional[float] = None
    median_eps_growth: Optional[float] = None
    rev_growth_histogram: Optional[list[int]] = None
    hist_labels: list[str] = _HIST_LABELS
    index_series: Optional[list[float]] = None
    index_pct: Optional[float] = None
    index_label: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(prices: list[float]) -> list[float]:
    base = prices[0]
    return [round(p / base * 100, 2) for p in prices]


async def _index_series(conn, candidates: list[str], n_pts: int = 52):
    since = date.today() - timedelta(days=375)
    for ticker in candidates:
        try:
            rows = await conn.fetch(
                f"SELECT COALESCE(adj_close, close) AS px "
                f"FROM {_INDEX_TABLE} "
                f"WHERE ticker=$1 AND date>=$2 "
                f"AND COALESCE(adj_close, close) IS NOT NULL "
                f"ORDER BY date",
                ticker, since,
            )
            if len(rows) < 10:
                continue
            all_px = [float(r["px"]) for r in rows]
            step = max(1, len(all_px) // n_pts)
            sampled = all_px[::step]
            if sampled[-1] != all_px[-1]:
                sampled.append(all_px[-1])
            pct = round((all_px[-1] - all_px[0]) / all_px[0] * 100, 1)
            logger.info("hero_stats: %s resolved (%d rows, %+.1f%%)",
                        ticker, len(all_px), pct)
            return _norm(sampled), pct
        except Exception as exc:
            logger.debug("Index ticker %s: %s", ticker, exc)
    logger.warning("hero_stats: no index ticker resolved from %s", candidates)
    return None, None


async def _median_value(
    conn, table: str, id_col: str, candidate_ids: list[str]
) -> Optional[float]:
    """Median of `value` across the universe for the most recent FY, across
    a table whose row identifier lives in `id_col` (metric_id OR line_item_id)."""
    for mid in candidate_ids:
        try:
            row = await conn.fetchrow(
                f"""
                SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS med
                FROM   {table}
                WHERE  {id_col} = $1
                  AND  fiscal_period IN ('FY','Annual')
                  AND  fiscal_year = (
                      SELECT MAX(fiscal_year) FROM {table}
                      WHERE  {id_col} = $1 AND fiscal_period IN ('FY','Annual')
                  )
                  AND  value IS NOT NULL
                  AND  value BETWEEN -5 AND 5
                """,
                mid,
            )
            if row and row["med"] is not None:
                logger.info("hero_stats: median ← %s.%s='%s'", table, id_col, mid)
                return round(float(row["med"]), 4)
        except Exception as exc:
            logger.debug("Median %s on %s.%s: %s", mid, table, id_col, exc)
    return None


async def _histogram_value(
    conn, table: str, id_col: str, candidate_ids: list[str]
) -> Optional[list[int]]:
    """Bucketed distribution of `value` for the most recent FY (same shape as
    `_median_value`)."""
    for mid in candidate_ids:
        try:
            rows = await conn.fetch(
                f"""
                SELECT value FROM {table}
                WHERE  {id_col} = $1
                  AND  fiscal_period IN ('FY','Annual')
                  AND  fiscal_year = (
                      SELECT MAX(fiscal_year) FROM {table}
                      WHERE  {id_col} = $1 AND fiscal_period IN ('FY','Annual')
                  )
                  AND  value IS NOT NULL
                  AND  value BETWEEN -5 AND 5
                """,
                mid,
            )
            if len(rows) < 5:
                continue
            counts = [0] * len(_HIST_BINS)
            for r in rows:
                v = float(r["value"])
                placed = False
                for i, (lo, hi) in enumerate(_HIST_BINS):
                    if lo <= v < hi:
                        counts[i] += 1
                        placed = True
                        break
                if not placed:
                    counts[-1] += 1
            logger.info("hero_stats: histogram ← %s.%s='%s'", table, id_col, mid)
            return counts
        except Exception as exc:
            logger.debug("Histogram %s on %s.%s: %s", mid, table, id_col, exc)
    return None


async def _derived_yoy_median(
    conn, std_table: str, eid_col: str, base_ids: list[str]
) -> Optional[float]:
    """Compute median YoY growth from raw line-item values (window-fn).
    Last-resort fallback when no pre-calc growth row exists."""
    for base in base_ids:
        try:
            row = await conn.fetchrow(
                f"""
                WITH src AS (
                    SELECT {eid_col} AS eid, fiscal_year, value
                    FROM   {std_table}
                    WHERE  line_item_id = $1
                      AND  fiscal_period IN ('FY','Annual')
                      AND  value IS NOT NULL
                      AND  {eid_col} IS NOT NULL
                ),
                yoy AS (
                    SELECT eid, fiscal_year,
                           value AS curr,
                           LAG(value) OVER (PARTITION BY eid ORDER BY fiscal_year) AS prev
                    FROM   src
                ),
                growth AS (
                    SELECT (curr - prev) / NULLIF(ABS(prev), 0) AS g
                    FROM   yoy
                    WHERE  prev IS NOT NULL AND prev > 0
                      AND  fiscal_year = (SELECT MAX(fiscal_year) FROM src)
                )
                SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY g) AS med
                FROM   growth
                WHERE  g BETWEEN -5 AND 5
                """,
                base,
            )
            if row and row["med"] is not None:
                logger.info("hero_stats: median ← DERIVED YoY from %s.line_item_id='%s'",
                            std_table, base)
                return round(float(row["med"]), 4)
        except Exception as exc:
            logger.debug("Derived YoY %s on %s: %s", base, std_table, exc)
    return None


# ── Diagnostic endpoints (declared BEFORE the catch-all main endpoint) ──────

@router.get("/ping")
async def hero_stats_ping() -> dict:
    """Liveness probe — confirms this version of the router is loaded."""
    return {"ok": True, "version": "2026-05-30-B-metrics-then-std", "msg": "hero_stats router loaded"}


@router.get("/debug")
async def hero_stats_debug() -> dict:
    """Diagnostics: row counts, available line_item_ids, cross-asset universe."""
    out: dict = {"version": "2026-05-30-B-metrics-then-std", "errors": []}

    try:
        async with acquire() as conn:

            # ── Canonical growth metric_ids from sec.ref_metric_definitions ─
            for ref_tbl in ("sec.ref_metric_definitions", "ref_metric_definitions"):
                try:
                    rows = await conn.fetch(
                        f"SELECT metric_id, name, category FROM {ref_tbl} "
                        f"WHERE metric_id ILIKE '%growth%' "
                        f"ORDER BY metric_id"
                    )
                    out[f"{ref_tbl}_growth_metrics"] = [
                        {"id": r["metric_id"], "name": r["name"], "cat": r["category"]}
                        for r in rows
                    ]
                    break  # stop after first one that worked
                except Exception as exc:
                    out[f"{ref_tbl}_growth_metrics"] = f"error: {exc}"

            # ── Growth IDs present in metrics tables (primary source) ────────
            for tbl in ("fact_metrics_us", "fact_metrics_jp"):
                try:
                    rows = await conn.fetch(
                        f"SELECT DISTINCT metric_id FROM {tbl} "
                        f"WHERE fiscal_period IN ('FY','Annual') "
                        f"AND metric_id ILIKE '%growth%' "
                        f"ORDER BY metric_id"
                    )
                    out[f"{tbl}_growth_ids_present"] = [r["metric_id"] for r in rows]
                except Exception as exc:
                    out[f"{tbl}_growth_ids_present"] = f"error: {exc}"
                    out["errors"].append(f"{tbl}: {exc}")

            # ── Growth IDs present in _std tables (fallback source) ──────────
            for tbl in ("fact_fundamentals_std_us", "fact_fundamentals_std_jp"):
                try:
                    rows = await conn.fetch(
                        f"SELECT DISTINCT line_item_id FROM {tbl} "
                        f"WHERE fiscal_period IN ('FY','Annual') "
                        f"AND line_item_id ILIKE '%growth%' "
                        f"ORDER BY line_item_id"
                    )
                    out[f"{tbl}_growth_ids_present"] = [r["line_item_id"] for r in rows]
                except Exception as exc:
                    out[f"{tbl}_growth_ids_present"] = f"error: {exc}"

            # ── Row counts ──────────────────────────────────────────────────
            for tbl in ("fact_metrics_us", "fact_metrics_jp",
                        "fact_fundamentals_std_us", "fact_fundamentals_std_jp"):
                try:
                    r = await conn.fetchrow(f"SELECT COUNT(*) AS n FROM {tbl}")
                    out[f"{tbl}_total_rows"] = int(r["n"]) if r else None
                except Exception as exc:
                    out[f"{tbl}_total_rows"] = f"error: {exc}"

            # ── Cross-asset universe (capped) ───────────────────────────────
            try:
                rows = await conn.fetch(
                    "SELECT ticker, name, asset_class FROM sec.dim_cross_asset "
                    "ORDER BY asset_class, ticker LIMIT 50"
                )
                out["cross_asset_universe_first50"] = [
                    f"{r['asset_class']} | {r['ticker']} | {r['name']}" for r in rows
                ]
            except Exception as exc:
                out["cross_asset_universe_first50"] = f"error: {exc}"

            # ── Index candidate row counts ──────────────────────────────────
            for label, candidates in [
                ("us_index_candidates_found", _US_INDEX_TICKERS),
                ("jp_index_candidates_found", _JP_INDEX_TICKERS),
            ]:
                found = []
                for tk in candidates:
                    try:
                        r = await conn.fetchrow(
                            f"SELECT COUNT(*) AS n FROM {_INDEX_TABLE} WHERE ticker=$1", tk
                        )
                        if r and r["n"] > 0:
                            found.append(f"{tk} ({r['n']} rows)")
                    except Exception as exc:
                        out["errors"].append(f"{tk}: {exc}")
                out[label] = found or "none found"

            # ── dim_company reachability ────────────────────────────────────
            for tbl in ("dim_company_us", "dim_company_jp"):
                try:
                    r = await conn.fetchrow(
                        f"SELECT COUNT(*) AS n FROM {tbl} WHERE primary_ticker IS NOT NULL"
                    )
                    out[f"{tbl}_count"] = int(r["n"]) if r else None
                except Exception as exc:
                    out[f"{tbl}_count"] = f"error: {exc}"

    except Exception as exc:
        out["fatal"] = f"{type(exc).__name__}: {exc}"

    return out


# ── Main endpoint ─────────────────────────────────────────────────────────────

@router.get("", response_model=HeroStats)
async def hero_stats(market: Literal["US", "JP"] = Query(...)) -> HeroStats:
    cached = _CACHE.get(market)
    if cached and cached[0] > time.time():
        return cached[1]

    is_us = market == "US"
    metrics_table = "fact_metrics_us"           if is_us else "fact_metrics_jp"
    std_table     = "fact_fundamentals_std_us"  if is_us else "fact_fundamentals_std_jp"
    eid_col       = "cik"                       if is_us else "edinet_code"
    index_candidates = _US_INDEX_TICKERS if is_us else _JP_INDEX_TICKERS
    index_label_default = "S&P 500" if is_us else "Nikkei 225"

    n_companies = 0
    n_sectors: Optional[int] = None

    async with acquire() as conn:
        # ── Company count ───────────────────────────────────────────────────
        try:
            if is_us:
                row = await conn.fetchrow(
                    "SELECT COUNT(DISTINCT primary_ticker) AS n "
                    "FROM dim_company_us WHERE primary_ticker IS NOT NULL"
                )
            else:
                row = await conn.fetchrow(
                    "SELECT COUNT(DISTINCT primary_ticker) AS n "
                    "FROM dim_company_jp WHERE primary_ticker IS NOT NULL"
                )
            n_companies = int(row["n"]) if row and row["n"] is not None else 0
            if n_companies == 0:
                logger.warning("hero_stats: dim_company_%s returned 0", market.lower())
        except Exception as exc:
            logger.warning("Company count (%s) failed: %s", market, exc)

        # ── Sector count ────────────────────────────────────────────────────
        try:
            if is_us:
                srow = await conn.fetchrow(
                    "SELECT COUNT(DISTINCT gics_sector_code) AS n FROM dim_company_us "
                    "WHERE primary_ticker IS NOT NULL AND gics_sector_code IS NOT NULL"
                )
            else:
                srow = await conn.fetchrow(
                    "SELECT COUNT(DISTINCT gics_sector_code) AS n FROM dim_company_jp "
                    "WHERE primary_ticker IS NOT NULL AND gics_sector_code IS NOT NULL"
                )
            n_sectors = int(srow["n"]) if srow and srow["n"] else None
        except Exception as exc:
            logger.debug("Sector count (%s): %s", market, exc)

        # Candidate metric_id / line_item_id lists — same names work on both tables.
        rev_growth_ids = [
            "revenue_growth_year_over_year",
            "revenue_growth_yoy",
            "revenue_yoy_growth",
            "revenue_growth",
        ]
        eps_growth_ids = [
            "earnings_per_share_diluted_growth_year_over_year",
            "earnings_per_share_basic_growth_year_over_year",
            "earnings_per_share_growth_year_over_year",
            "eps_growth_year_over_year",
            "eps_growth_yoy",
            "eps_diluted_growth_yoy",
            "net_income_growth_year_over_year",  # last-resort proxy
        ]
        # Raw line-item IDs used to derive EPS YoY if no pre-calc growth row exists
        eps_base_ids = ["earnings_per_share_diluted", "earnings_per_share_basic"]
        rev_base_ids = ["revenue"]

        # ── Median revenue growth: metrics → std → derived from raw revenue ─
        median_rev_growth = await _median_value(
            conn, metrics_table, "metric_id", rev_growth_ids
        )
        if median_rev_growth is None:
            median_rev_growth = await _median_value(
                conn, std_table, "line_item_id", rev_growth_ids
            )
        if median_rev_growth is None:
            median_rev_growth = await _derived_yoy_median(
                conn, std_table, eid_col, rev_base_ids
            )

        # ── Median EPS growth: metrics → std → derived from raw EPS ─────────
        median_eps_growth = await _median_value(
            conn, metrics_table, "metric_id", eps_growth_ids
        )
        if median_eps_growth is None:
            median_eps_growth = await _median_value(
                conn, std_table, "line_item_id", eps_growth_ids
            )
        if median_eps_growth is None:
            median_eps_growth = await _derived_yoy_median(
                conn, std_table, eid_col, eps_base_ids
            )

        # ── Revenue-growth histogram: metrics → std ─────────────────────────
        rev_growth_histogram = await _histogram_value(
            conn, metrics_table, "metric_id", rev_growth_ids
        )
        if rev_growth_histogram is None:
            rev_growth_histogram = await _histogram_value(
                conn, std_table, "line_item_id", rev_growth_ids
            )

        # ── Index sparkline ─────────────────────────────────────────────────
        index_series, index_pct = await _index_series(conn, index_candidates)

    result = HeroStats(
        jurisdiction=market,
        n_companies=n_companies,
        n_sectors=n_sectors,
        median_rev_growth=median_rev_growth,
        median_eps_growth=median_eps_growth,
        rev_growth_histogram=rev_growth_histogram,
        index_series=index_series,
        index_pct=index_pct,
        index_label=index_label_default,
    )

    if n_companies > 0:
        _CACHE[market] = (time.time() + _TTL, result)
    else:
        logger.warning("hero_stats(%s): n_companies=0 — NOT caching", market)
    return result
