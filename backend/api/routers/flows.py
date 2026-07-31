"""Institutional flow aggregates - common-equity 13F net dollar flows.

`GET /api/institutional/flow-aggregates?group_by=sector|ticker&top_n=8`

The current period is always the most recent eligible 13F report period. Flow
deltas compare that quarter with the immediately prior eligible quarter:

    inflow_usd = (latest_shares - prior_shares) * avg(implied_price)

Eligibility is intentionally strict. Every aggregate, detail, logo, member, and
rollup uses latest-amendment common-equity rows only, resolved by CUSIP through
the security dimensions.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.flows")


class FlowMemberRow(BaseModel):
    ticker: str | None = None
    name: str
    inflow_usd: float
    prior_value_usd: float
    latest_value_usd: float
    cik_padded: str | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    ret_1q: float | None = None
    row_type: Literal["member", "other", "total"] = "member"


class FlowSectorStats(BaseModel):
    name_count: int
    winsorized_pe_avg: float | None = None
    ret_1q: float | None = None


class FlowAggregateRow(BaseModel):
    group: str
    name: str
    inflow_usd: float
    prior_value_usd: float
    latest_value_usd: float
    total_flow_usd: float | None = None
    sector_code: str | None = None
    sector_name: str | None = None
    sector_stats: FlowSectorStats | None = None
    cik_padded: str | None = None
    sector_slug: str | None = None
    manager_count_change: int | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    ret_1q: float | None = None
    top_firms: list[FlowMemberRow] = Field(default_factory=list)
    top_members: list[FlowMemberRow] = Field(default_factory=list)


class FlowAggregatesResponse(BaseModel):
    group_by: Literal["sector", "ticker"]
    lookback: int
    prior_period: str | None
    latest_period: str | None
    top_inflows: list[FlowAggregateRow]
    top_outflows: list[FlowAggregateRow]


_CACHE: dict[tuple, tuple[float, FlowAggregatesResponse]] = {}
_TTL_SECONDS = 1800


def _eligible_13f_common_equity_cte(extra_where: str = "") -> str:
    return f"""
eligible_13f_common_equity AS (
    SELECT  h.report_period,
            h.manager_cik,
            upper(h.cusip) AS cusip,
            COALESCE(
                NULLIF(sec.primary_ticker, ''),
                NULLIF(id.issuer_ticker, ''),
                NULLIF(h.issuer_ticker, '')
            ) AS ticker,
            COALESCE(
                NULLIF(sec.issuer_cik, ''),
                NULLIF(id.issuer_cik, ''),
                NULLIF(h.issuer_cik, '')
            ) AS issuer_cik,
            COALESCE(
                NULLIF(sec.issuer_name, ''),
                NULLIF(id.issuer_name, ''),
                NULLIF(h.issuer_name, '')
            ) AS issuer_name,
            h.shares_or_principal::float8 AS shares,
            COALESCE(h.market_value_usd, h.value_reported, 0)::float8 AS value_usd
    FROM    core_13f_holding h
    JOIN    dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
    JOIN    dim_security_identifier_us id ON id.cusip = upper(h.cusip)
    WHERE   h.is_latest_amendment = TRUE
      AND   h.shares_or_principal IS NOT NULL
      AND   h.cusip IS NOT NULL
      AND   COALESCE(h.sh_prn_flag, 'SH') = 'SH'
      AND   COALESCE(h.put_call, '') = ''
      AND   lower(COALESCE(sec.asset_bucket, '')) = 'equity'
      AND   id.security_type = 'common_equity'
      {extra_where}
      AND   COALESCE(
                NULLIF(sec.primary_ticker, ''),
                NULLIF(id.issuer_ticker, ''),
                NULLIF(h.issuer_ticker, '')
            ) IS NOT NULL
)
"""


def _sector_slug(label: str) -> str:
    return (
        label.lower()
        .replace("&", "and")
        .replace(" / ", "-")
        .replace("/", "-")
        .replace(" ", "-")
    )


def _float(v) -> float | None:
    return float(v) if v is not None else None


async def _fetch_ticker_stats(conn, tickers: list[str]) -> dict[str, dict[str, float | None]]:
    if not tickers:
        return {}
    rows = await conn.fetch(
        """
        WITH wanted AS (
            SELECT DISTINCT unnest($1::text[]) AS ticker
        ),
        mcap AS (
            SELECT DISTINCT ON (ticker)
                   ticker,
                   value::float8 AS market_cap
            FROM   fact_market_metrics
            WHERE  jurisdiction = 'US'
              AND  metric_id = 'market_capitalization'
              AND  ticker = ANY($1::text[])
              AND  value IS NOT NULL
            ORDER  BY ticker, market_date DESC NULLS LAST, period_end DESC NULLS LAST
        ),
        pe AS (
            SELECT DISTINCT ON (ticker)
                   ticker,
                   value::float8 AS pe_ratio
            FROM   fact_metrics_us
            WHERE  metric_id = 'price_to_earnings_trailing'
              AND  ticker = ANY($1::text[])
              AND  value IS NOT NULL
            ORDER  BY ticker, period_end DESC NULLS LAST
        ),
        prices AS (
            SELECT ticker,
                   date,
                   COALESCE(adj_close, close)::float8 AS close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM   fact_prices_us
            WHERE  ticker = ANY($1::text[])
              AND  COALESCE(adj_close, close) IS NOT NULL
              AND  date >= CURRENT_DATE - INTERVAL '150 days'
        ),
        ret AS (
            SELECT ticker,
                   CASE
                     WHEN MAX(close) FILTER (WHERE rn = 64) > 0
                     THEN MAX(close) FILTER (WHERE rn = 1)
                          / MAX(close) FILTER (WHERE rn = 64) - 1.0
                   END::float8 AS ret_1q
            FROM   prices
            WHERE  rn IN (1, 64)
            GROUP  BY ticker
        )
        SELECT w.ticker,
               m.market_cap,
               p.pe_ratio,
               r.ret_1q
        FROM   wanted w
        LEFT JOIN mcap m ON m.ticker = w.ticker
        LEFT JOIN pe p ON p.ticker = w.ticker
        LEFT JOIN ret r ON r.ticker = w.ticker
        """,
        tickers,
    )
    return {
        r["ticker"]: {
            "market_cap": _float(r["market_cap"]),
            "pe_ratio": _float(r["pe_ratio"]),
            "ret_1q": _float(r["ret_1q"]),
        }
        for r in rows
    }


async def _fetch_sector_stats(conn, sector_codes: list[str]) -> dict[str, FlowSectorStats]:
    if not sector_codes:
        return {}
    rows = await conn.fetch(
        """
        WITH latest_pe AS (
            SELECT DISTINCT ON (ticker)
                   ticker,
                   value::float8 AS pe_ratio
            FROM   fact_metrics_us
            WHERE  metric_id = 'price_to_earnings_trailing'
              AND  value IS NOT NULL
              AND  value > 0
            ORDER  BY ticker, period_end DESC NULLS LAST
        ),
        base AS (
            SELECT d.gics_sector_code,
                   d.primary_ticker,
                   lp.pe_ratio
            FROM   dim_company_us d
            LEFT JOIN latest_pe lp ON lp.ticker = d.primary_ticker
            WHERE  d.gics_sector_code = ANY($1::text[])
              AND  d.primary_ticker IS NOT NULL
              AND  COALESCE(d.include_in_pipeline, true)
        ),
        bounds AS (
            SELECT gics_sector_code,
                   percentile_cont(0.05) WITHIN GROUP (ORDER BY pe_ratio) AS p05,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY pe_ratio) AS p95
            FROM   base
            WHERE  pe_ratio IS NOT NULL
            GROUP  BY gics_sector_code
        ),
        pe_stats AS (
            SELECT b.gics_sector_code,
                   COUNT(DISTINCT b.primary_ticker)::int AS name_count,
                   AVG(
                       CASE
                         WHEN b.pe_ratio IS NULL OR bo.p05 IS NULL OR bo.p95 IS NULL THEN NULL
                         ELSE LEAST(GREATEST(b.pe_ratio, bo.p05), bo.p95)
                       END
                   )::float8 AS winsorized_pe_avg
            FROM   base b
            LEFT JOIN bounds bo ON bo.gics_sector_code = b.gics_sector_code
            GROUP  BY b.gics_sector_code
        ),
        ret_rows AS (
            SELECT gics_code,
                   cap_weighted_return,
                   ROW_NUMBER() OVER (PARTITION BY gics_code ORDER BY date DESC) AS rn
            FROM   sec.fact_sector_returns
            WHERE  jurisdiction = 'US'
              AND  grouping_level = 'sector'
              AND  gics_code = ANY($1::text[])
        ),
        ret_stats AS (
            SELECT gics_code,
                   CASE
                     WHEN COUNT(*) FILTER (
                            WHERE rn <= 63
                              AND cap_weighted_return IS NOT NULL
                              AND cap_weighted_return > -1
                          ) = 0
                     THEN NULL
                     ELSE EXP(SUM(LN(1.0 + cap_weighted_return)) FILTER (
                            WHERE rn <= 63
                              AND cap_weighted_return IS NOT NULL
                              AND cap_weighted_return > -1
                          )) - 1.0
                   END::float8 AS ret_1q
            FROM   ret_rows
            WHERE  rn <= 63
            GROUP  BY gics_code
        )
        SELECT p.gics_sector_code,
               p.name_count,
               p.winsorized_pe_avg,
               r.ret_1q
        FROM   pe_stats p
        LEFT JOIN ret_stats r ON r.gics_code = p.gics_sector_code
        """,
        sector_codes,
    )
    return {
        r["gics_sector_code"]: FlowSectorStats(
            name_count=int(r["name_count"] or 0),
            winsorized_pe_avg=_float(r["winsorized_pe_avg"]),
            ret_1q=_float(r["ret_1q"]),
        )
        for r in rows
    }


def _rollup_member(rows: list[FlowMemberRow], label: str, row_type: Literal["other", "total"]) -> FlowMemberRow:
    prior = sum(r.prior_value_usd for r in rows)
    latest = sum(r.latest_value_usd for r in rows)
    flow = sum(r.inflow_usd for r in rows)
    mcap_values = [r.market_cap for r in rows if r.market_cap is not None]
    weights = [r.market_cap or 0 for r in rows]

    def weighted(field: str) -> float | None:
        total_w = 0.0
        acc = 0.0
        for r, w in zip(rows, weights):
            v = getattr(r, field)
            if v is None or w <= 0:
                continue
            acc += float(v) * w
            total_w += w
        return acc / total_w if total_w > 0 else None

    return FlowMemberRow(
        ticker=None,
        name=label,
        inflow_usd=flow,
        prior_value_usd=prior,
        latest_value_usd=latest,
        market_cap=sum(mcap_values) if mcap_values else None,
        pe_ratio=weighted("pe_ratio"),
        ret_1q=weighted("ret_1q"),
        row_type=row_type,
    )


async def _fetch_ticker_manager_overrides(
    latest_period,
    prior_period,
    selected_groups: list[str],
) -> dict[str, list[FlowMemberRow]]:
    if not selected_groups:
        return {}
    async with acquire() as conn:
        rows = await conn.fetch(
            f"""
            WITH {_eligible_13f_common_equity_cte("AND h.report_period IN ($1, $2)")},
            agg AS (
                SELECT  ticker,
                        manager_cik,
                        h.report_period,
                        SUM(shares) AS shares,
                        SUM(value_usd) AS value_usd
                FROM    eligible_13f_common_equity h
                WHERE   h.ticker = ANY($3::text[])
                GROUP   BY ticker, manager_cik, h.report_period
            ),
            paired AS (
                SELECT  ticker,
                        manager_cik,
                        SUM(shares) FILTER (WHERE report_period = $1) AS latest_shares,
                        SUM(shares) FILTER (WHERE report_period = $2) AS prior_shares,
                        SUM(value_usd) FILTER (WHERE report_period = $1) AS latest_value,
                        SUM(value_usd) FILTER (WHERE report_period = $2) AS prior_value
                FROM    agg
                GROUP   BY ticker, manager_cik
            )
            SELECT  p.ticker,
                    p.manager_cik,
                    COALESCE(NULLIF(m.manager_name, ''), p.manager_cik) AS manager_name,
                    COALESCE(p.prior_value, 0) AS prior_value,
                    COALESCE(p.latest_value, 0) AS latest_value,
                    CASE
                      WHEN COALESCE(p.latest_shares, 0) = COALESCE(p.prior_shares, 0) THEN 0
                      ELSE
                        (COALESCE(p.latest_shares, 0) - COALESCE(p.prior_shares, 0))
                        *
                        (
                          (
                            CASE WHEN COALESCE(p.latest_shares, 0) > 0 THEN COALESCE(p.latest_value, 0) / p.latest_shares END
                          + CASE WHEN COALESCE(p.prior_shares, 0) > 0 THEN COALESCE(p.prior_value, 0) / p.prior_shares END
                          )
                          / NULLIF(
                              (CASE WHEN COALESCE(p.latest_shares, 0) > 0 THEN 1 ELSE 0 END)
                            + (CASE WHEN COALESCE(p.prior_shares, 0) > 0 THEN 1 ELSE 0 END),
                            0
                          )
                        )
                    END AS inflow_usd
            FROM    paired p
            LEFT JOIN dim_13f_manager m ON m.manager_cik = p.manager_cik
            WHERE   COALESCE(p.latest_value, 0) <> 0
                 OR COALESCE(p.prior_value, 0) <> 0
            """,
            latest_period,
            prior_period,
            selected_groups,
        )

    grouped: dict[str, list[FlowMemberRow]] = {}
    for r in rows:
        grouped.setdefault(r["ticker"], []).append(
            FlowMemberRow(
                ticker=str(r["manager_cik"]).zfill(10) if r["manager_cik"] else None,
                name=r["manager_name"] or r["manager_cik"] or "",
                inflow_usd=float(r["inflow_usd"] or 0),
                prior_value_usd=float(r["prior_value"] or 0),
                latest_value_usd=float(r["latest_value"] or 0),
            )
        )
    return {
        k: sorted(v, key=lambda m: abs(m.inflow_usd), reverse=True)
        for k, v in grouped.items()
    }


@router.get("/flow-aggregates", response_model=FlowAggregatesResponse)
async def flow_aggregates(
    group_by: Literal["sector", "ticker"] = Query("sector"),
    lookback: int = Query(2, ge=2, le=4),
    top_n: int = Query(8, ge=1, le=50),
) -> FlowAggregatesResponse:
    # lookback is accepted for backward compatibility, but flow math is always
    # latest eligible common-equity quarter vs the immediately prior one.
    cache_key = (group_by, 2, top_n)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        async with acquire() as conn:
            candidate_periods = await conn.fetch(
                """
                SELECT DISTINCT report_period
                FROM   core_13f_holding
                WHERE  is_latest_amendment = TRUE
                ORDER  BY report_period DESC
                LIMIT  12
                """
            )
            periods_row = []
            for period_row in candidate_periods:
                is_eligible = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM   core_13f_holding h
                        JOIN   dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
                        JOIN   dim_security_identifier_us id ON id.cusip = upper(h.cusip)
                        WHERE  h.report_period = $1
                          AND  h.is_latest_amendment = TRUE
                          AND  h.shares_or_principal IS NOT NULL
                          AND  h.cusip IS NOT NULL
                          AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                          AND  COALESCE(h.put_call, '') = ''
                          AND  lower(COALESCE(sec.asset_bucket, '')) = 'equity'
                          AND  id.security_type = 'common_equity'
                          AND  COALESCE(
                                 NULLIF(sec.primary_ticker, ''),
                                 NULLIF(id.issuer_ticker, ''),
                                 NULLIF(h.issuer_ticker, '')
                               ) IS NOT NULL
                        LIMIT  1
                    )
                    """,
                    period_row["report_period"],
                )
                if is_eligible:
                    periods_row.append(period_row)
                if len(periods_row) == 2:
                    break
            if len(periods_row) < 2:
                raise HTTPException(
                    status_code=503,
                    detail="Not enough common-equity 13F reporting periods to compute flow aggregates yet.",
                )
            latest_period = periods_row[0]["report_period"]
            prior_period = periods_row[1]["report_period"]

            # Per-issuer rollup. `inflow_usd_summed` aggregates the *per-manager*
            # Δshares × manager-implied avg price, mirroring the popout's
            # Inflow column exactly (so a name with offsetting manager rotations
            # nets correctly instead of being inflated by an issuer-level avg
            # price applied to a near-zero net share delta).
            per_issuer = await conn.fetch(
                f"""
                WITH {_eligible_13f_common_equity_cte("AND h.report_period IN ($1, $2)")},
                manager_agg AS (
                    SELECT  ticker,
                            issuer_cik,
                            manager_cik,
                            MAX(issuer_name) AS issuer_name,
                            SUM(shares) FILTER (WHERE report_period = $1) AS latest_shares_mgr,
                            SUM(shares) FILTER (WHERE report_period = $2) AS prior_shares_mgr,
                            SUM(value_usd) FILTER (WHERE report_period = $1) AS latest_value_mgr,
                            SUM(value_usd) FILTER (WHERE report_period = $2) AS prior_value_mgr
                    FROM    eligible_13f_common_equity
                    WHERE   report_period IN ($1, $2)
                    GROUP BY ticker, issuer_cik, manager_cik
                ),
                manager_flow AS (
                    SELECT  ticker,
                            issuer_cik,
                            issuer_name,
                            CASE
                              WHEN COALESCE(latest_shares_mgr, 0) = COALESCE(prior_shares_mgr, 0) THEN 0
                              ELSE
                                (COALESCE(latest_shares_mgr, 0) - COALESCE(prior_shares_mgr, 0))
                                *
                                (
                                  (
                                    CASE WHEN COALESCE(latest_shares_mgr, 0) > 0
                                         THEN COALESCE(latest_value_mgr, 0) / latest_shares_mgr END
                                  + CASE WHEN COALESCE(prior_shares_mgr, 0) > 0
                                         THEN COALESCE(prior_value_mgr, 0) / prior_shares_mgr END
                                  )
                                  / NULLIF(
                                      (CASE WHEN COALESCE(latest_shares_mgr, 0) > 0 THEN 1 ELSE 0 END)
                                    + (CASE WHEN COALESCE(prior_shares_mgr, 0) > 0 THEN 1 ELSE 0 END),
                                      0
                                  )
                                )
                            END AS inflow_usd_mgr
                    FROM    manager_agg
                ),
                issuer_flow AS (
                    SELECT  ticker,
                            issuer_cik,
                            MAX(issuer_name) AS issuer_name,
                            SUM(inflow_usd_mgr) AS inflow_usd_summed
                    FROM    manager_flow
                    GROUP BY ticker, issuer_cik
                ),
                agg AS (
                    SELECT  report_period,
                            ticker,
                            issuer_cik,
                            MAX(issuer_name) AS issuer_name,
                            SUM(shares) AS shares,
                            SUM(value_usd) AS value_usd,
                            COUNT(DISTINCT manager_cik) AS manager_count
                    FROM    eligible_13f_common_equity
                    WHERE   report_period IN ($1, $2)
                    GROUP   BY report_period, ticker, issuer_cik
                )
                SELECT  a.ticker,
                        a.issuer_cik,
                        a.report_period,
                        a.shares,
                        a.value_usd,
                        CASE WHEN a.shares > 0 THEN a.value_usd / a.shares END AS price,
                        a.manager_count,
                        f.inflow_usd_summed,
                        COALESCE(d.name, dt.name, a.issuer_name, a.ticker) AS company_name,
                        COALESCE(d.cik::text, dt.cik::text, a.issuer_cik) AS cik,
                        COALESCE(d.gics_sector_code, dt.gics_sector_code) AS gics_sector_code,
                        COALESCE(d.gics_sector_name, dt.gics_sector_name) AS gics_sector_name
                FROM    agg a
                LEFT JOIN issuer_flow f ON f.ticker = a.ticker AND f.issuer_cik = a.issuer_cik
                LEFT JOIN dim_company_us d  ON d.cik::text = a.issuer_cik
                LEFT JOIN dim_company_us dt ON dt.primary_ticker = a.ticker
                """,
                latest_period,
                prior_period,
            )

            tickers = sorted({r["ticker"] for r in per_issuer if r["ticker"]})
            ticker_stats = await _fetch_ticker_stats(conn, tickers)

            sector_codes = sorted({r["gics_sector_code"] for r in per_issuer if r["gics_sector_code"]})
            sector_stats = await _fetch_sector_stats(conn, sector_codes)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("flow-aggregates failed")
        raise HTTPException(status_code=500, detail=f"Flow aggregates query failed: {exc}") from exc

    issuers: dict[str, dict] = {}
    for r in per_issuer:
        ticker = r["ticker"]
        if not ticker:
            continue
        bucket = issuers.setdefault(
            ticker,
            {
                "ticker": ticker,
                "company_name": r["company_name"] or ticker,
                "cik": r["cik"],
                "cik_padded": str(r["cik"]).zfill(10) if r["cik"] else None,
                "sector_code": r["gics_sector_code"],
                "sector_name": r["gics_sector_name"],
                "prior_shares": 0.0,
                "latest_shares": 0.0,
                "prior_value": 0.0,
                "latest_value": 0.0,
                "prior_price": None,
                "latest_price": None,
                "prior_managers": 0,
                "latest_managers": 0,
                # Issuer-level inflow, summed from per-(ticker, manager) flows
                # in SQL. Identical aggregation to the popout's Inflow column.
                "inflow_usd": 0.0,
                **ticker_stats.get(ticker, {}),
            },
        )
        is_latest = r["report_period"] == latest_period
        prefix = "latest" if is_latest else "prior"
        bucket[f"{prefix}_shares"] = float(r["shares"] or 0)
        bucket[f"{prefix}_value"] = float(r["value_usd"] or 0)
        bucket[f"{prefix}_price"] = _float(r["price"])
        bucket[f"{prefix}_managers"] = int(r["manager_count"] or 0)
        # `inflow_usd_summed` is identical across the two period rows for the
        # same (ticker, issuer_cik) — assigning either is fine.
        if r["inflow_usd_summed"] is not None:
            bucket["inflow_usd"] = float(r["inflow_usd_summed"])

    for it in issuers.values():
        it["manager_count_change"] = it["latest_managers"] - it["prior_managers"]

    rolled: dict[tuple[str, str], dict] = {}
    for it in issuers.values():
        member = FlowMemberRow(
            ticker=it["ticker"],
            name=it["company_name"],
            inflow_usd=it["inflow_usd"],
            prior_value_usd=it["prior_value"],
            latest_value_usd=it["latest_value"],
            cik_padded=it["cik_padded"],
            market_cap=it.get("market_cap"),
            pe_ratio=it.get("pe_ratio"),
            ret_1q=it.get("ret_1q"),
        )

        if group_by == "ticker":
            key_id = it["ticker"]
            display = it["company_name"]
            extras = {
                "sector_code": it["sector_code"],
                "sector_name": it["sector_name"],
                "sector_stats": None,
                "cik_padded": it["cik_padded"],
                "sector_slug": None,
                "market_cap": it.get("market_cap"),
                "pe_ratio": it.get("pe_ratio"),
                "ret_1q": it.get("ret_1q"),
            }
        else:
            if not it["sector_name"] or not it["sector_code"]:
                continue
            key_id = _sector_slug(it["sector_name"])
            display = it["sector_name"]
            extras = {
                "sector_code": it["sector_code"],
                "sector_name": it["sector_name"],
                "sector_stats": sector_stats.get(it["sector_code"]),
                "cik_padded": None,
                "sector_slug": key_id,
                "market_cap": None,
                "pe_ratio": None,
                "ret_1q": sector_stats.get(it["sector_code"]).ret_1q if sector_stats.get(it["sector_code"]) else None,
            }

        slot = rolled.setdefault(
            (group_by, key_id),
            {
                "group": key_id,
                "name": display,
                "inflow_usd": 0.0,
                "prior_value_usd": 0.0,
                "latest_value_usd": 0.0,
                "total_flow_usd": 0.0,
                "manager_count_change": 0,
                "_members": [],
                **extras,
            },
        )
        slot["inflow_usd"] += it["inflow_usd"]
        slot["total_flow_usd"] += it["inflow_usd"]
        slot["prior_value_usd"] += it["prior_value"]
        slot["latest_value_usd"] += it["latest_value"]
        slot["manager_count_change"] += it["manager_count_change"]
        slot["_members"].append(member)

    ranked = sorted(rolled.values(), key=lambda r: r["inflow_usd"], reverse=True)
    manager_override: dict[str, list[FlowMemberRow]] = {}
    if group_by == "ticker":
        selected_groups = [
            r["group"]
            for r in [
                *[r for r in ranked if r["inflow_usd"] > 0][:top_n],
                *[r for r in reversed(ranked) if r["inflow_usd"] < 0][:top_n],
            ]
            if r.get("group")
        ]
        manager_override = await _fetch_ticker_manager_overrides(
            latest_period,
            prior_period,
            selected_groups,
        )

    def row_payload(src: dict) -> FlowAggregateRow:
        payload = dict(src)
        members_all = sorted(
            payload.pop("_members", []),
            key=lambda m: abs(m.inflow_usd),
            reverse=True,
        )
        top_firms = members_all[:3]
        if group_by == "ticker" and payload.get("group") in manager_override:
            members_all = manager_override[payload["group"]]
        top_members = members_all[:10]
        rest = members_all[10:]
        if rest:
            top_members.append(_rollup_member(rest, f"Other ({len(rest)})", "other"))
        if members_all:
            top_members.append(_rollup_member(members_all, "Total", "total"))
        return FlowAggregateRow(**payload, top_firms=top_firms, top_members=top_members)

    top_inflows = [row_payload(r) for r in ranked if r["inflow_usd"] > 0][:top_n]
    top_outflows = [row_payload(r) for r in reversed(ranked) if r["inflow_usd"] < 0][:top_n]

    resp = FlowAggregatesResponse(
        group_by=group_by,
        lookback=2,
        prior_period=str(prior_period) if prior_period else None,
        latest_period=str(latest_period) if latest_period else None,
        top_inflows=top_inflows,
        top_outflows=top_outflows,
    )
    _CACHE[cache_key] = (now + _TTL_SECONDS, resp)
    return resp
