from __future__ import annotations

import hashlib
import json
import math
import os
import random
from datetime import date, timedelta
from statistics import mean
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query

import llm_providers

from ..ai import llm_runtime
from ..db import acquire

router = APIRouter()


FACTOR_COLUMNS = {
    "mkt": "beta_mkt",
    "smb": "beta_smb",
    "hml": "beta_hml",
    "mom": "beta_mom",
    "rmw": "beta_rmw",
    "cma": "beta_cma",
}
T_STAT_COLUMNS = {
    "mkt": "t_mkt",
    "smb": "t_smb",
    "hml": "t_hml",
    "mom": "t_mom",
    "rmw": "t_rmw",
    "cma": "t_cma",
}
T_STAT_SIGNIFICANCE_THRESHOLD = 1.96  # two-sided 95% confidence

FACTOR_NAMES = {
    "mkt": "Mkt-RF",
    "smb": "SMB",
    "hml": "HML",
    "mom": "Mom",
    "rmw": "RMW",
    "cma": "CMA",
}

CLASSIFICATION_BY_SLUG = {
    "alternative": "Asset Management: Alternative (Speculative/Trading)",
    "traditional": "Asset Management: Traditional (Long-Term Capital)",
    "wealth_trust": "Banking: Wealth & Trust (Investment)",
    "capital_markets": "Banking: Capital Markets & Trading (Speculative)",
    "insurance": "Insurance: General Account (Long-Term Capital)",
}


def _usd_from_x1000(value: int | float | None) -> int:
    # SEC structured 13F TSVs in this local corpus carry VALUE in dollars
    # despite older 13F conventions describing values in thousands.
    return int(value or 0)


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        if math.isnan(float(value)):
            return None
    except Exception:
        return None
    return float(value)


def _safe_div(num: float, den: float) -> float | None:
    return num / den if den else None


# --- 5-bucket classifier for the Holdings Heatmap + Latest Holdings table ---
# Layered fallback: put_call/derivatives flag → FIGI securityType → dim.asset_bucket.
# The FIGI layer rescues misclassified rows where dim.asset_bucket = 'other' but the
# instrument is actually a Common Stock / ETF / Mutual Fund (per OpenFIGI evidence).
_HEATMAP_BUCKET_EQUITY_FIGI = {
    "Common Stock", "ADR", "REIT", "Preferred Stock", "Tracking Stk", "Right",
}
_HEATMAP_BUCKET_FUND_FIGI = {
    "ETP", "Open-End Fund", "Closed-End Fund", "Mutual Fund", "Fund of Funds", "Unit",
}
_HEATMAP_BUCKET_DERIV_FIGI = {"Equity WRT"}


def _heatmap_bucket(put_call: str | None, asset_bucket: str | None, openfigi_security_type: str | None) -> str:
    """Return one of: 'equity', 'fund_etf', 'fixed_income', 'derivatives', 'others'."""
    if (put_call or "").strip() or asset_bucket == "derivatives":
        return "derivatives"
    figi = (openfigi_security_type or "").strip()
    if figi in _HEATMAP_BUCKET_EQUITY_FIGI:
        return "equity"
    if figi in _HEATMAP_BUCKET_FUND_FIGI:
        return "fund_etf"
    if figi in _HEATMAP_BUCKET_DERIV_FIGI:
        return "derivatives"
    if asset_bucket == "equity":
        return "equity"
    if asset_bucket == "fund_etf":
        return "fund_etf"
    if asset_bucket == "fixed_income":
        return "fixed_income"
    return "others"


def _friendly_sector(gics_sector_name: str | None, asset_bucket: str | None) -> str:
    """Sector label for the heatmap/table — never expose raw asset_bucket values
    like 'equity'/'fund_etf' as a 'sector', which is technically nonsense.

    Falls back from real GICS → friendly bucket label → 'Unresolved'.
    """
    if gics_sector_name and gics_sector_name not in {"equity", "fund_etf", "fixed_income"}:
        # Already a real GICS sector OR 'derivatives' (which is meaningful for the user).
        return gics_sector_name
    if asset_bucket == "derivatives":
        return "Derivatives"
    if asset_bucket == "fund_etf":
        return "Fund / ETF"
    if asset_bucket == "fixed_income":
        return "Fixed Income"
    return "Unresolved"


async def _has_relation(conn, name: str) -> bool:
    row = await conn.fetchrow("SELECT to_regclass($1) AS rel", name)
    return bool(row and row["rel"])


async def _relation_row_estimate(conn, name: str) -> int | None:
    row = await conn.fetchrow(
        """
        SELECT GREATEST(c.reltuples, 0)::bigint AS n
        FROM pg_class c
        WHERE c.oid = to_regclass($1)
        """,
        name,
    )
    return row["n"] if row else None


async def _institutional_security_scope(conn, ticker: str) -> dict:
    ticker_norm = ticker.upper()
    ent = await conn.fetchrow(
        """
        SELECT cik::text AS cik
        FROM dim_company_us
        WHERE upper(primary_ticker) = $1
        LIMIT 1
        """,
        ticker_norm,
    )
    cik = ent["cik"].zfill(10) if ent and ent["cik"] else None
    cusip_rows = await conn.fetch(
        """
        SELECT DISTINCT upper(cusip) AS cusip
        FROM dim_13f_security_us
        WHERE cusip IS NOT NULL
          AND (
              upper(primary_ticker) = $1
              OR ($2::text IS NOT NULL AND (issuer_cik = $2 OR issuer_cik = lpad($2, 10, '0')))
          )
        """,
        ticker_norm,
        cik,
    )
    return {
        "ticker": ticker_norm,
        "cik": cik,
        "cusips": [r["cusip"] for r in cusip_rows if r["cusip"]],
    }


async def _latest_issuer_holding_period(
    conn,
    cusips: list[str],
    cik: str | None,
    *,
    year_max_cap: date | None = None,
    shares_only: bool = False,
) -> date | None:
    params: list = [cusips]
    cik_param = None
    if cik:
        params.append(cik)
        cik_param = len(params)
    cap_param = None
    if year_max_cap:
        params.append(year_max_cap)
        cap_param = len(params)

    cap_sql = f"AND h.report_period <= ${cap_param}::date" if cap_param else ""
    shares_sql = """
      AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
      AND COALESCE(h.put_call, '') = ''
    """ if shares_only else ""
    cik_branch = f"""
        UNION ALL
        SELECT MAX(h.report_period) AS rp
        FROM   core_13f_holding h
        WHERE  h.issuer_cik = ${cik_param}
          AND  h.is_latest_amendment = TRUE
          {cap_sql}
          {shares_sql}
    """ if cik_param else ""

    row = await conn.fetchrow(
        f"""
        SELECT MAX(rp) AS rp
        FROM (
            SELECT MAX(h.report_period) AS rp
            FROM   core_13f_holding h
            WHERE  upper(h.cusip) = ANY($1::text[])
              AND  h.is_latest_amendment = TRUE
              {cap_sql}
              {shares_sql}
            GROUP  BY upper(h.cusip)
            {cik_branch}
        ) periods
        """,
        *params,
    )
    return row["rp"] if row else None


async def _security_13f_coverage(conn) -> dict | None:
    has_identifier = await _has_relation(conn, "dim_security_identifier_us")
    has_evidence = await _has_relation(conn, "fact_security_identifier_evidence_us")
    has_dim = await _has_relation(conn, "dim_13f_security_us")
    if not (has_identifier or has_dim):
        return None

    coverage: dict = {}
    if has_identifier:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'resolved') AS legacy_resolved_cusips,
                   COUNT(*) FILTER (WHERE issuer_cik IS NOT NULL) AS legacy_cik_cusips,
                   COUNT(*) FILTER (WHERE issuer_ticker IS NOT NULL) AS legacy_ticker_cusips,
                   COUNT(*) FILTER (WHERE security_type = 'etf_or_fund') AS fund_etf_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'unresolved') AS unresolved_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'ambiguous') AS ambiguous_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'non_company_security') AS non_company_cusips
            FROM dim_security_identifier_us
            """
        )
        coverage.update(dict(row))

    if has_evidence:
        evidence = await conn.fetchrow(
            """
            WITH observed AS (
                SELECT cusip,
                       MAX(row_count)::numeric AS row_count,
                       MAX(value_observed)::numeric AS value_observed
                FROM fact_security_identifier_evidence_us
                GROUP BY cusip
            ),
            non_spec AS (
                SELECT cusip,
                       MAX(row_count)::numeric AS row_count,
                       MAX(value_observed)::numeric AS value_observed
                FROM fact_security_identifier_evidence_us
                WHERE candidate_cik IS NOT NULL
                  AND source_name <> 'spec.cik-cusip-maps.csv'
                GROUP BY cusip
            ),
            spec AS (
                SELECT DISTINCT cusip
                FROM fact_security_identifier_evidence_us
                WHERE candidate_cik IS NOT NULL
                  AND source_name = 'spec.cik-cusip-maps.csv'
            ),
            isin AS (
                SELECT DISTINCT cusip
                FROM fact_security_identifier_evidence_us
                WHERE candidate_cik IS NOT NULL
                  AND source_name = 'dim_company_us.isin'
            )
            SELECT
                (SELECT COALESCE(SUM(row_count), 0) FROM observed) AS observed_holding_rows,
                (SELECT COALESCE(SUM(value_observed), 0) FROM observed) AS observed_value,
                (SELECT COUNT(*) FROM non_spec) AS resolved_company_cusips,
                (SELECT COALESCE(SUM(row_count), 0) FROM non_spec) AS resolved_holding_rows,
                (SELECT COALESCE(SUM(value_observed), 0) FROM non_spec) AS resolved_value,
                (SELECT COUNT(*) FROM isin) AS isin_resolved_cusips,
                (SELECT COUNT(*) FROM spec) AS spec_csv_cusips,
                (SELECT COUNT(*) FROM spec s LEFT JOIN non_spec n ON n.cusip = s.cusip WHERE n.cusip IS NULL) AS spec_only_cusips
            """
        )
        coverage.update(dict(evidence))
        observed_rows = float(evidence["observed_holding_rows"] or 0)
        observed_value = float(evidence["observed_value"] or 0)
        coverage["resolved_row_coverage"] = _safe_div(float(evidence["resolved_holding_rows"] or 0), observed_rows)
        coverage["resolved_value_coverage"] = _safe_div(float(evidence["resolved_value"] or 0), observed_value)

    if has_dim:
        dim = await conn.fetchrow(
            """
            SELECT COUNT(*) AS dimension_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'resolved') AS dimension_resolved_cusips,
                   COUNT(*) FILTER (WHERE resolution_status = 'price_resolved') AS dimension_price_resolved_cusips,
                   COUNT(*) FILTER (WHERE issuer_cik IS NOT NULL) AS dimension_cik_cusips,
                   COUNT(*) FILTER (WHERE primary_ticker IS NOT NULL) AS dimension_ticker_cusips,
                   COUNT(*) FILTER (WHERE isin IS NOT NULL) AS dimension_isin_cusips,
                   COUNT(*) FILTER (WHERE asset_bucket = 'fund_etf') AS dimension_fund_etf_cusips,
                   COALESCE(SUM(row_count), 0) AS dimension_holding_rows,
                   COALESCE(SUM(value_observed), 0) AS dimension_value
            FROM dim_13f_security_us
            """
        )
        coverage.update(dict(dim))

    if await _has_relation(conn, "fact_13f_prices_yahoo"):
        price = await conn.fetchrow(
            """
            WITH priced_dim AS (
                SELECT d.cusip, d.primary_ticker
                FROM dim_13f_security_us d
                WHERE d.cusip IS NOT NULL
                  AND d.primary_ticker IS NOT NULL
                  AND d.source_name = 'yahoo_finance.price_coverage'
                  AND EXISTS (
                      SELECT 1
                      FROM fact_13f_prices_yahoo p
                      WHERE p.cusip = d.cusip
                        AND p.ticker = d.primary_ticker
                  )
            )
            SELECT
                (SELECT COUNT(DISTINCT cusip) FROM priced_dim) AS price_13f_cusips,
                (SELECT COUNT(DISTINCT primary_ticker) FROM priced_dim) AS price_13f_tickers,
                (SELECT MIN(date) FROM fact_13f_prices_yahoo) AS price_13f_min_date,
                (SELECT MAX(date) FROM fact_13f_prices_yahoo) AS price_13f_max_date
            """
        )
        coverage.update({
            k: (str(price[k]) if isinstance(price[k], date) else price[k])
            for k in price.keys()
        })
    else:
        coverage.update({
            "price_13f_cusips": 0,
            "price_13f_tickers": 0,
            "price_13f_min_date": None,
            "price_13f_max_date": None,
        })

    coverage.setdefault("total_cusips", coverage.get("dimension_cusips", 0))
    coverage.setdefault("resolved_company_cusips", coverage.get("dimension_resolved_cusips", 0))
    coverage["fund_etf_cusips"] = coverage.get("dimension_fund_etf_cusips", coverage.get("fund_etf_cusips", 0))
    coverage.setdefault("unresolved_cusips", 0)
    coverage.setdefault("ambiguous_cusips", 0)
    coverage["basis"] = "full identifier/evidence universe; dimension_* fields show current dim_13f_security_us population"
    return coverage


async def _latest_manager_period(conn, manager_cik: str, report_period: str | None = None) -> date | None:
    if report_period:
        return date.fromisoformat(report_period)
    if await _has_relation(conn, "core_13f_manager_period"):
        row = await conn.fetchrow(
            """
            SELECT MAX(report_period) AS report_period
            FROM core_13f_manager_period
            WHERE manager_cik = $1
            """,
            manager_cik.zfill(10),
        )
        if row and row["report_period"]:
            return row["report_period"]
    row = await conn.fetchrow(
        """
        SELECT MAX(report_period) AS report_period
        FROM fact_13f_holdings
        WHERE manager_cik = $1 AND is_latest_amendment
        """,
        manager_cik.zfill(10),
    )
    return row["report_period"] if row else None


async def _manager_packet(conn, manager_cik: str, report_period: str | None = None) -> dict:
    manager_cik = manager_cik.zfill(10)
    if await _has_relation(conn, "core_13f_manager_period"):
        try:
            core_packet = await _core_manager_packet(conn, manager_cik, report_period)
            if core_packet.get("available"):
                return core_packet
        except Exception:
            pass  # core tables partially initialised; fall through to legacy packet
    if not await _has_relation(conn, "fact_13f_holdings"):
        return {"manager_cik": manager_cik, "available": False, "reason": "Institutional tables are not available."}

    period = await _latest_manager_period(conn, manager_cik, report_period)
    if not period:
        return {"manager_cik": manager_cik, "available": False, "reason": "No 13F holdings found for this manager."}

    manager = await conn.fetchrow(
        """
        SELECT manager_cik, manager_name, manager_type, is_public_company, public_entity_cik,
               name_source, crd_number, sec_file_number, form_13f_file_number, report_type,
               street1, street2, city, state, zip_code,
               filing_count_primary, filing_count_other, filing_count_total,
               first_quarter_filed, last_quarter_filed
        FROM dim_13f_manager
        WHERE manager_cik = $1
        """,
        manager_cik,
    )

    quarters_raw = await conn.fetch(
        """
        SELECT report_period, COUNT(*) AS rows, SUM(value_x1000) AS value_x1000
        FROM fact_13f_holdings
        WHERE manager_cik = $1 AND is_latest_amendment
        GROUP BY report_period
        ORDER BY report_period DESC
        LIMIT 16
        """,
        manager_cik,
    )
    quarters = [{"report_period": str(r["report_period"]), "rows": r["rows"], "value_usd": _usd_from_x1000(r["value_x1000"])} for r in quarters_raw]

    holdings_raw = await conn.fetch(
        """
        WITH base AS (
            SELECT h.*, d.name AS company_name, d.gics_sector_name, d.gics_industry_group_name
            FROM fact_13f_holdings h
            LEFT JOIN dim_company_us d ON d.cik = h.issuer_cik
            WHERE h.manager_cik = $1
              AND h.report_period = $2
              AND h.is_latest_amendment
        ),
        prev AS (
            SELECT cusip, SUM(shares_or_principal) AS prev_shares
            FROM fact_13f_holdings
            WHERE manager_cik = $1
              AND report_period = (
                  SELECT MAX(report_period)
                  FROM fact_13f_holdings
                  WHERE manager_cik = $1 AND report_period < $2 AND is_latest_amendment
              )
              AND is_latest_amendment
            GROUP BY cusip
        ),
        totals AS (
            SELECT SUM(value_x1000) FILTER (
                WHERE COALESCE(put_call, '') = '' AND COALESCE(sh_prn_flag, 'SH') = 'SH'
            ) AS long_value_x1000
            FROM base
        )
        SELECT base.issuer_ticker, COALESCE(base.company_name, base.issuer_name) AS company_name,
               base.issuer_cik, base.issuer_name, base.title_of_class, base.cusip,
               base.value_x1000, base.shares_or_principal, base.put_call, base.sh_prn_flag,
               base.gics_sector_name, base.gics_industry_group_name, prev.prev_shares,
               CASE WHEN totals.long_value_x1000 > 0
                    THEN base.value_x1000::float / totals.long_value_x1000::float
                    ELSE NULL END AS weight
        FROM base
        CROSS JOIN totals
        LEFT JOIN prev ON prev.cusip = base.cusip
        ORDER BY base.value_x1000 DESC NULLS LAST
        LIMIT 250
        """,
        manager_cik,
        period,
    )

    holdings = []
    long_holdings = []
    derivative_holdings = []
    unresolved_value = 0
    for r in holdings_raw:
        value_usd = _usd_from_x1000(r["value_x1000"])
        item = {
            "ticker": r["issuer_ticker"],
            "company_name": r["company_name"] or r["issuer_name"] or r["cusip"],
            "issuer_cik": r["issuer_cik"],
            "cusip": r["cusip"],
            "title_of_class": r["title_of_class"],
            "value_usd": value_usd,
            "shares": _float(r["shares_or_principal"]),
            "weight": _float(r["weight"]),
            "put_call": r["put_call"],
            "sh_prn_flag": r["sh_prn_flag"],
            "sector": r["gics_sector_name"] or "Unresolved",
            "industry_group": r["gics_industry_group_name"],
            "shares_change_pct": _safe_div(float(r["shares_or_principal"] or 0) - float(r["prev_shares"] or 0), float(r["prev_shares"] or 0)),
            "is_new": r["prev_shares"] is None,
        }
        holdings.append(item)
        if not item["ticker"]:
            unresolved_value += value_usd
        if (not item["put_call"]) and (item["sh_prn_flag"] in (None, "SH")):
            long_holdings.append(item)
        else:
            derivative_holdings.append(item)

    total_value = sum(h["value_usd"] for h in long_holdings)
    weights = [h["value_usd"] / total_value for h in long_holdings if total_value and h["value_usd"] > 0]
    sorted_weights = sorted(weights, reverse=True)
    hhi = sum(w * w for w in weights)
    summary = {
        "report_period": str(period),
        "total_value_usd": total_value,
        "holding_count": len(long_holdings),
        "derivative_count": len(derivative_holdings),
        "top5_weight": sum(sorted_weights[:5]) if sorted_weights else None,
        "top10_weight": sum(sorted_weights[:10]) if sorted_weights else None,
        "max_position_weight": sorted_weights[0] if sorted_weights else None,
        "hhi": hhi if weights else None,
        "effective_holdings": (1.0 / hhi) if hhi else None,
        "unresolved_value_usd": unresolved_value,
        "unresolved_weight": _safe_div(unresolved_value, total_value),
    }

    sector_map: dict[str, float] = {}
    for h in long_holdings:
        sector_map[h["sector"]] = sector_map.get(h["sector"], 0.0) + h["value_usd"]
    sectors = [
        {"sector": sector, "value_usd": int(value), "weight": _safe_div(value, total_value)}
        for sector, value in sorted(sector_map.items(), key=lambda kv: kv[1], reverse=True)
    ]

    factor = await _factor_exposure(conn, long_holdings, total_value)
    risk = await _risk_summary(conn, factor, summary, manager_cik, period)
    history = await _history(conn, manager_cik)
    metrics = await _weighted_metrics(conn, long_holdings, total_value)
    ownership_13dg = await _ownership_13dg(conn, long_holdings)
    gaps = []
    if summary["unresolved_weight"] and summary["unresolved_weight"] > 0.15:
        gaps.append("CUSIP-to-CIK resolution is weak for this portfolio; sector/factor analytics understate unresolved holdings.")
    if factor["coverage_weight"] < 0.75:
        gaps.append("Factor exposure coverage is incomplete; VaR is based on resolved holdings only.")
    if not ownership_13dg:
        gaps.append("No parsed 13D/G data is available yet; activist/passive beneficial ownership context is missing.")

    return {
        "available": True,
        "manager": dict(manager) if manager else {"manager_cik": manager_cik, "manager_name": manager_cik},
        "quarters": quarters,
        "summary": summary,
        "holdings": holdings[:250],
        "derivatives": derivative_holdings[:50],
        "sectors": sectors,
        "factor_exposure": factor,
        "risk": risk,
        "history": history,
        "weighted_metrics": metrics,
        "ownership_13dg": ownership_13dg,
        "data_gaps": gaps,
    }


async def _core_manager_packet(conn, manager_cik: str, report_period: str | None = None) -> dict:
    period = await _latest_manager_period(conn, manager_cik, report_period)
    if not period:
        return {"manager_cik": manager_cik, "available": False, "reason": "No standardized 13F manager metrics found."}
    manager = await conn.fetchrow(
        """
        SELECT manager_cik, legal_name AS manager_name, metadata_source AS name_source,
               crd_number, sec_file_number, form_13f_file_number, report_type,
               street1, street2, city, state, zip_code,
               filing_count_primary, filing_count_other, filing_count_total,
               first_report_period AS first_quarter_filed,
               last_report_period AS last_quarter_filed
        FROM core_13f_manager
        WHERE manager_cik = $1
        """,
        manager_cik,
    )
    metrics = await conn.fetchrow(
        """
        SELECT *
        FROM core_13f_manager_period
        WHERE manager_cik = $1 AND report_period = $2
        """,
        manager_cik,
        period,
    )
    if not metrics:
        return {"manager_cik": manager_cik, "available": False, "reason": "No standardized 13F manager metrics found."}

    quarters_raw = await conn.fetch(
        """
        SELECT report_period, position_count AS rows,
               COALESCE(long_market_value, portfolio_value_market) AS value_usd
        FROM core_13f_manager_period
        WHERE manager_cik = $1
        ORDER BY report_period DESC
        LIMIT 16
        """,
        manager_cik,
    )
    quarters = [
        {"report_period": str(r["report_period"]), "rows": r["rows"], "value_usd": _usd_from_x1000(r["value_usd"])}
        for r in quarters_raw
    ]
    previous_period_row = await conn.fetchrow(
        """
        SELECT MAX(report_period) AS report_period
        FROM core_13f_holding
        WHERE manager_cik = $1
          AND report_period < $2
          AND is_latest_amendment
        """,
        manager_cik,
        period,
    )
    previous_period = previous_period_row["report_period"] if previous_period_row else None

    totals = await conn.fetchrow(
        """
        WITH base AS (
            SELECT CASE
                       WHEN bucket.asset_bucket = 'derivatives'
                           THEN 'derivative:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                          upper(h.cusip), h.issuer_name, '') || ':'
                                               || COALESCE(h.put_call, '') || ':' || COALESCE(upper(h.cusip), '')
                       ELSE 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                   NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                   upper(h.cusip), h.issuer_name, '')
                   END AS display_key,
                   bucket.asset_bucket,
                   COALESCE(market_value_usd, value_reported, 0)::numeric AS value_usd,
                   COALESCE(sec.resolution_status, h.issuer_resolution_status) AS issuer_resolution_status
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND h.report_period = $2
              AND h.is_latest_amendment
        ),
        long_rows AS (
            SELECT display_key,
                   SUM(value_usd) AS value_usd
            FROM base
            WHERE asset_bucket <> 'derivatives'
            GROUP BY display_key
        ),
        ranked AS (
            SELECT value_usd,
                   ROW_NUMBER() OVER (ORDER BY value_usd DESC NULLS LAST) AS rn,
                   SUM(value_usd) OVER () AS total_value
            FROM long_rows
        )
        SELECT
            (SELECT COALESCE(SUM(value_usd), 0) FROM long_rows) AS total_value_usd,
            (SELECT COUNT(*) FROM long_rows) AS holding_count,
            (SELECT COUNT(*) FROM base WHERE asset_bucket = 'derivatives') AS derivative_count,
            (SELECT SUM(value_usd / NULLIF(total_value, 0)) FROM ranked WHERE rn <= 5) AS top5_weight,
            (SELECT SUM(value_usd / NULLIF(total_value, 0)) FROM ranked WHERE rn <= 10) AS top10_weight,
            (SELECT MAX(value_usd / NULLIF(total_value, 0)) FROM ranked) AS max_position_weight,
            (SELECT SUM((value_usd / NULLIF(total_value, 0)) * (value_usd / NULLIF(total_value, 0))) FROM ranked) AS hhi,
            (
                SELECT COALESCE(SUM(value_usd), 0)
                FROM base
                WHERE asset_bucket <> 'derivatives'
                  AND (issuer_resolution_status <> 'resolved' OR issuer_resolution_status IS NULL)
            ) AS unresolved_value_usd
        """,
        manager_cik,
        period,
    )
    total_value = float(totals["total_value_usd"] or metrics["long_market_value"] or metrics["portfolio_value_market"] or 0)
    holdings_raw = await conn.fetch(
        """
        WITH split_factors AS (
            SELECT ticker,
                   EXP(SUM(LN(split_ratio::numeric)))::numeric AS split_factor
            FROM fact_stock_split_event
            WHERE jurisdiction = 'US'
              AND $3::date IS NOT NULL
              AND effective_date > $3::date
              AND effective_date <= $2::date
              AND split_ratio > 0
            GROUP BY ticker
        ),
        current_raw AS (
            SELECT CASE
                       WHEN bucket.asset_bucket = 'derivatives'
                           THEN 'derivative:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                          upper(h.cusip), h.issuer_name, '') || ':'
                                               || COALESCE(h.put_call, '') || ':' || COALESCE(upper(h.cusip), '')
                       ELSE 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                   NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                   upper(h.cusip), h.issuer_name, '')
                   END AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   sec.isin,
                   COALESCE(d.name, sec.issuer_name, h.issuer_name) AS company_name,
                   COALESCE(sec.issuer_cik, h.issuer_cik) AS issuer_cik,
                   h.issuer_name, h.title_of_class, upper(h.cusip) AS cusip,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS value_usd,
                   h.price_at_filing, h.shares_or_principal, h.put_call, h.sh_prn_flag,
                   bucket.asset_bucket,
                   CASE
                       WHEN bucket.asset_bucket = 'derivatives' THEN 'derivatives'
                       ELSE COALESCE(d.gics_sector_name, sec.sector)
                   END AS gics_sector_name,
                   CASE
                       WHEN bucket.asset_bucket = 'derivatives' THEN NULL
                       ELSE COALESCE(d.gics_industry_group_name, sec.industry_group)
                   END AS gics_industry_group_name,
                   COALESCE(sec.resolution_status, h.issuer_resolution_status) AS resolution_status,
                   ofi.openfigi_security_type AS openfigi_security_type
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
            LEFT JOIN fact_13f_openfigi_identifier_enrichment ofi
                   ON ofi.cusip = upper(h.cusip) AND ofi.status = 'accepted'
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND h.report_period = $2
              AND h.is_latest_amendment
        ),
        prev_raw AS (
            SELECT CASE
                       WHEN bucket.asset_bucket = 'derivatives'
                           THEN 'derivative:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                          upper(h.cusip), h.issuer_name, '') || ':'
                                               || COALESCE(h.put_call, '') || ':' || COALESCE(upper(h.cusip), '')
                       ELSE 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                   NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                   upper(h.cusip), h.issuer_name, '')
                   END AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   h.shares_or_principal
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND $3::date IS NOT NULL
              AND h.report_period = $3
              AND h.is_latest_amendment
        ),
        prev_grouped AS (
            SELECT p.display_key,
                   SUM(p.shares_or_principal * COALESCE(sf.split_factor, 1)) AS prev_shares,
                   MAX(COALESCE(sf.split_factor, 1)) AS split_adjustment_factor
            FROM prev_raw p
            LEFT JOIN split_factors sf ON sf.ticker = p.issuer_ticker
            GROUP BY p.display_key
        ),
        grouped AS (
            SELECT c.display_key,
                   MAX(c.issuer_ticker) AS issuer_ticker,
                   MAX(c.isin) AS isin,
                   MAX(c.company_name) AS company_name,
                   MAX(c.issuer_cik) AS issuer_cik,
                   MAX(c.issuer_name) AS issuer_name,
                   MAX(c.title_of_class) AS title_of_class,
                   MIN(c.cusip) AS cusip,
                   SUM(c.value_usd) AS market_value_usd,
                   NULL::numeric AS value_reported,
                   CASE
                       WHEN SUM(c.shares_or_principal) > 0 THEN SUM(c.value_usd) / NULLIF(SUM(c.shares_or_principal), 0)
                       ELSE MAX(c.price_at_filing)
                   END AS price_at_filing,
                   SUM(c.shares_or_principal) AS shares_or_principal,
                   MAX(c.put_call) AS put_call,
                   MAX(c.sh_prn_flag) AS sh_prn_flag,
                   MAX(c.asset_bucket) AS asset_bucket,
                   MAX(c.gics_sector_name) AS gics_sector_name,
                   MAX(c.gics_industry_group_name) AS gics_industry_group_name,
                   MAX(c.resolution_status) AS resolution_status,
                   MAX(c.openfigi_security_type) AS openfigi_security_type,
                   MAX(p.prev_shares) AS prev_shares,
                   MAX(COALESCE(p.split_adjustment_factor, 1)) AS split_adjustment_factor
            FROM current_raw c
            LEFT JOIN prev_grouped p ON p.display_key = c.display_key
            GROUP BY c.display_key
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY CASE WHEN asset_bucket = 'derivatives' THEN 'derivatives' ELSE 'holdings' END
                       ORDER BY market_value_usd DESC NULLS LAST
                   ) AS rn
            FROM grouped
        )
        SELECT issuer_ticker, isin, company_name, issuer_cik, issuer_name, title_of_class, cusip,
               market_value_usd, value_reported, price_at_filing, shares_or_principal,
               put_call, sh_prn_flag, asset_bucket, gics_sector_name,
               gics_industry_group_name, resolution_status, openfigi_security_type,
               prev_shares, split_adjustment_factor
        FROM ranked
        WHERE (asset_bucket = 'derivatives' AND rn <= 50)
           OR (asset_bucket <> 'derivatives' AND rn <= 250)
        ORDER BY CASE WHEN asset_bucket = 'derivatives' THEN 1 ELSE 0 END,
                 market_value_usd DESC NULLS LAST
        """,
        manager_cik,
        period,
        previous_period,
    )
    holdings = []
    derivatives = []

    def holding_item(r) -> dict:
        value_usd = _usd_from_x1000(r["market_value_usd"] or r["value_reported"])
        shares = _float(r["shares_or_principal"])
        shares_source = "filing" if shares is not None else None
        price_at_filing = _float(r["price_at_filing"])
        if shares is None and price_at_filing and price_at_filing > 0 and value_usd:
            shares = float(value_usd) / price_at_filing
            shares_source = "inferred"
        prev_shares = _float(r["prev_shares"])
        shares_change = (shares - prev_shares) if shares is not None and prev_shares is not None else None
        # openfigi_security_type comes from the OpenFIGI evidence JOIN added to both
        # holding queries (manager packet + sector-top). May be NULL if no FIGI match.
        openfigi_type = r["openfigi_security_type"]
        return {
            "ticker": r["issuer_ticker"],
            "isin": r["isin"],
            "company_name": r["company_name"] or r["issuer_name"] or r["cusip"],
            "issuer_cik": r["issuer_cik"],
            "cusip": r["cusip"],
            "title_of_class": r["title_of_class"],
            "value_usd": value_usd,
            "shares": shares,
            "shares_source": shares_source,
            "price_at_filing": price_at_filing,
            "weight": _safe_div(value_usd, total_value),
            "put_call": r["put_call"],
            "sh_prn_flag": r["sh_prn_flag"],
            "sector": _friendly_sector(r["gics_sector_name"], r["asset_bucket"]),
            "industry_group": r["gics_industry_group_name"],
            "asset_bucket": r["asset_bucket"],
            "resolution_status": r["resolution_status"],
            "openfigi_security_type": openfigi_type,
            "bucket": _heatmap_bucket(r["put_call"], r["asset_bucket"], openfigi_type),
            "shares_change_pct": _safe_div(shares_change, prev_shares) if shares_change is not None and prev_shares else None,
            "shares_change": shares_change,
            "split_adjustment_factor": _float(r["split_adjustment_factor"]),
            "is_new": prev_shares is None,
        }

    for r in holdings_raw:
        item = holding_item(r)
        if r["asset_bucket"] == "derivatives":
            derivatives.append(item)
        else:
            holdings.append(item)

    hhi = _float(totals["hhi"]) if totals else None
    summary = {
        "report_period": str(period),
        "previous_report_period": str(previous_period) if previous_period else None,
        "total_value_usd": _usd_from_x1000(totals["total_value_usd"] if totals else metrics["long_market_value"]),
        "holding_count": int(totals["holding_count"] or 0) if totals else metrics["position_count"],
        "derivative_count": int(totals["derivative_count"] or 0) if totals else metrics["derivative_position_count"],
        "top5_weight": _float(totals["top5_weight"]) if totals else _float(metrics["top_5_concentration"]),
        "top10_weight": _float(totals["top10_weight"]) if totals else _float(metrics["top_10_concentration"]),
        "max_position_weight": _float(totals["max_position_weight"]) if totals else _float(metrics["max_position_weight"]),
        "hhi": hhi,
        "effective_holdings": (1.0 / hhi) if hhi else None,
        "unresolved_value_usd": _usd_from_x1000(totals["unresolved_value_usd"] if totals else metrics["unresolved_value"]),
        "unresolved_weight": _safe_div(float(totals["unresolved_value_usd"] or 0), total_value) if totals else _float(metrics["unresolved_weight"]),
        "asset_mix": {
            "equity_pct": _float(metrics["equity_pct"]),
            "fixed_income_pct": _float(metrics["fixed_income_pct"]),
            "fund_etf_pct": _float(metrics["fund_etf_pct"]),
            "derivatives_pct": _float(metrics["derivatives_pct"]),
            "other_pct": _float(metrics["other_pct"]),
        },
    }
    sectors_raw = await conn.fetch(
        """
        WITH base AS (
            SELECT h.report_period,
                   COALESCE(
                       NULLIF(d.gics_sector_name, ''),
                       NULLIF(sec.sector, ''),
                       CASE bucket.asset_bucket
                           WHEN 'derivatives'  THEN 'Derivatives'
                           WHEN 'fund_etf'     THEN 'Fund / ETF'
                           WHEN 'fixed_income' THEN 'Fixed Income'
                           ELSE NULL
                       END,
                       'Unresolved'
                   ) AS sector,
                   bucket.asset_bucket,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS value_usd,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                            NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                            upper(h.cusip)) AS name_key
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND (h.report_period = $2 OR ($3::date IS NOT NULL AND h.report_period = $3))
              AND h.is_latest_amendment
        ),
        non_derivative AS (
            SELECT *
            FROM base
            WHERE asset_bucket <> 'derivatives'
        ),
        totals AS (
            SELECT report_period, SUM(value_usd) AS total_value
            FROM non_derivative
            GROUP BY report_period
        ),
        sector_values AS (
            SELECT report_period, sector,
                   SUM(value_usd) AS value_usd,
                   COUNT(DISTINCT name_key) AS name_count
            FROM non_derivative
            GROUP BY report_period, sector
        )
        SELECT cur.sector,
               cur.value_usd,
               cur.name_count,
               cur.value_usd / NULLIF(cur_total.total_value, 0) AS weight,
               prev.value_usd AS previous_value_usd,
               prev.name_count AS previous_name_count,
               prev.value_usd / NULLIF(prev_total.total_value, 0) AS previous_weight
        FROM sector_values cur
        JOIN totals cur_total ON cur_total.report_period = cur.report_period
        LEFT JOIN sector_values prev
          ON prev.report_period = $3
         AND prev.sector = cur.sector
        LEFT JOIN totals prev_total ON prev_total.report_period = prev.report_period
        WHERE cur.report_period = $2
        ORDER BY cur.value_usd DESC NULLS LAST
        LIMIT 20
        """,
        manager_cik,
        period,
        previous_period,
    )
    sector_top_rows = await conn.fetch(
        """
        WITH split_factors AS (
            SELECT ticker,
                   EXP(SUM(LN(split_ratio::numeric)))::numeric AS split_factor
            FROM fact_stock_split_event
            WHERE jurisdiction = 'US'
              AND $3::date IS NOT NULL
              AND effective_date > $3::date
              AND effective_date <= $2::date
              AND split_ratio > 0
            GROUP BY ticker
        ),
        current_raw AS (
            SELECT 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                          upper(h.cusip), h.issuer_name, '') AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   sec.isin,
                   COALESCE(d.name, sec.issuer_name, h.issuer_name) AS company_name,
                   COALESCE(sec.issuer_cik, h.issuer_cik) AS issuer_cik,
                   h.issuer_name, h.title_of_class, upper(h.cusip) AS cusip,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS value_usd,
                   h.price_at_filing, h.shares_or_principal, h.put_call, h.sh_prn_flag,
                   bucket.asset_bucket,
                   COALESCE(
                       NULLIF(d.gics_sector_name, ''),
                       NULLIF(sec.sector, ''),
                       CASE bucket.asset_bucket
                           WHEN 'derivatives'  THEN 'Derivatives'
                           WHEN 'fund_etf'     THEN 'Fund / ETF'
                           WHEN 'fixed_income' THEN 'Fixed Income'
                           ELSE NULL
                       END,
                       'Unresolved'
                   ) AS gics_sector_name,
                   COALESCE(d.gics_industry_group_name, sec.industry_group) AS gics_industry_group_name,
                   COALESCE(sec.resolution_status, h.issuer_resolution_status) AS resolution_status,
                   ofi.openfigi_security_type AS openfigi_security_type
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
            LEFT JOIN fact_13f_openfigi_identifier_enrichment ofi
                   ON ofi.cusip = upper(h.cusip) AND ofi.status = 'accepted'
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND h.report_period = $2
              AND h.is_latest_amendment
        ),
        prev_raw AS (
            SELECT 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                          upper(h.cusip), h.issuer_name, '') AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   h.shares_or_principal,
                   bucket.asset_bucket
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND $3::date IS NOT NULL
              AND h.report_period = $3
              AND h.is_latest_amendment
        ),
        prev_grouped AS (
            SELECT p.display_key,
                   SUM(p.shares_or_principal * COALESCE(sf.split_factor, 1)) AS prev_shares,
                   MAX(COALESCE(sf.split_factor, 1)) AS split_adjustment_factor
            FROM prev_raw p
            LEFT JOIN split_factors sf ON sf.ticker = p.issuer_ticker
            WHERE p.asset_bucket <> 'derivatives'
            GROUP BY p.display_key
        ),
        grouped AS (
            SELECT c.display_key,
                   MAX(c.issuer_ticker) AS issuer_ticker,
                   MAX(c.isin) AS isin,
                   MAX(c.company_name) AS company_name,
                   MAX(c.issuer_cik) AS issuer_cik,
                   MAX(c.issuer_name) AS issuer_name,
                   MAX(c.title_of_class) AS title_of_class,
                   MIN(c.cusip) AS cusip,
                   SUM(c.value_usd) AS market_value_usd,
                   NULL::numeric AS value_reported,
                   CASE
                       WHEN SUM(c.shares_or_principal) > 0 THEN SUM(c.value_usd) / NULLIF(SUM(c.shares_or_principal), 0)
                       ELSE MAX(c.price_at_filing)
                   END AS price_at_filing,
                   SUM(c.shares_or_principal) AS shares_or_principal,
                   MAX(c.put_call) AS put_call,
                   MAX(c.sh_prn_flag) AS sh_prn_flag,
                   MAX(c.asset_bucket) AS asset_bucket,
                   MAX(c.gics_sector_name) AS gics_sector_name,
                   MAX(c.gics_industry_group_name) AS gics_industry_group_name,
                   MAX(c.resolution_status) AS resolution_status,
                   MAX(c.openfigi_security_type) AS openfigi_security_type,
                   MAX(p.prev_shares) AS prev_shares,
                   MAX(COALESCE(p.split_adjustment_factor, 1)) AS split_adjustment_factor
            FROM current_raw c
            LEFT JOIN prev_grouped p ON p.display_key = c.display_key
            WHERE c.asset_bucket <> 'derivatives'
            GROUP BY c.display_key
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY gics_sector_name
                       ORDER BY market_value_usd DESC NULLS LAST
                   ) AS rn
            FROM grouped
        )
        SELECT *
        FROM ranked
        WHERE rn <= 5
        ORDER BY gics_sector_name, rn
        """,
        manager_cik,
        period,
        previous_period,
    )
    sector_top: dict[str, list[dict]] = {}
    for r in sector_top_rows:
        sector_top.setdefault(_friendly_sector(r["gics_sector_name"], r["asset_bucket"]), []).append(holding_item(r))
    sectors = [
        {
            "sector": r["sector"],
            "value_usd": _usd_from_x1000(r["value_usd"]),
            "weight": _float(r["weight"]),
            "name_count": int(r["name_count"] or 0),
            "previous_weight": _float(r["previous_weight"]),
            "weight_change_abs_pp": (
                abs(float(r["weight"] or 0) - float(r["previous_weight"])) * 100.0
                if r["previous_weight"] is not None and r["weight"] is not None
                else None
            ),
            "weight_change_pp": (
                (float(r["weight"] or 0) - float(r["previous_weight"])) * 100.0
                if r["previous_weight"] is not None and r["weight"] is not None
                else None
            ),
            "top_holdings": sector_top.get(r["sector"], []),
        }
        for r in sectors_raw
    ]

    # Attach 8-quarter return series for the top 6 sectors (those rendered as tiles).
    try:
        top_sector_names = [s["sector"] for s in sectors[:6] if s.get("sector")]
        if top_sector_names:
            qreturns = await _sector_quarterly_returns(conn, top_sector_names, period, n_quarters=8)
            for s in sectors:
                s["quarterly_returns"] = qreturns.get(s["sector"], [])
    except Exception:
        for s in sectors:
            s.setdefault("quarterly_returns", [])

    heatmap_pairs = list(dict.fromkeys(
        (h["cusip"], h["ticker"]) for h in holdings[:250] if h.get("cusip") and h.get("ticker")
    ))
    heatmap_tickers = list(dict.fromkeys(h["ticker"] for h in holdings[:250] if h.get("ticker")))
    if heatmap_tickers:
        one_year_start = period - timedelta(days=365)
        prices_by_key: dict[tuple[str, str], list[dict]] = {}
        if heatmap_pairs and await _has_relation(conn, "fact_13f_prices_yahoo"):
            price_rows_13f = await conn.fetch(
                """
                WITH wanted AS (
                    SELECT * FROM unnest($1::text[], $2::text[]) AS w(cusip, ticker)
                )
                SELECT p.cusip, p.ticker, p.date, COALESCE(p.adj_close, p.close) AS close
                FROM fact_13f_prices_yahoo p
                JOIN wanted w ON w.cusip = p.cusip AND w.ticker = p.ticker
                WHERE p.date BETWEEN ($3::date - INTERVAL '7 days') AND ($4::date + INTERVAL '7 days')
                  AND COALESCE(p.adj_close, p.close) IS NOT NULL
                ORDER BY p.cusip, p.ticker, p.date
                """,
                [cusip for cusip, _ in heatmap_pairs],
                [ticker for _, ticker in heatmap_pairs],
                one_year_start,
                period,
            )
            for row in price_rows_13f:
                prices_by_key.setdefault((row["cusip"], row["ticker"]), []).append(
                    {"date": row["date"], "close": float(row["close"])}
                )

        price_rows_us = await conn.fetch(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM fact_prices_us
            WHERE ticker = ANY($1::text[])
              AND date BETWEEN ($2::date - INTERVAL '7 days') AND ($3::date + INTERVAL '7 days')
              AND COALESCE(adj_close, close) IS NOT NULL
            ORDER BY ticker, date
            """,
            heatmap_tickers,
            one_year_start,
            period,
        )
        prices_by_ticker: dict[str, list[dict]] = {}
        for row in price_rows_us:
            prices_by_ticker.setdefault(row["ticker"], []).append(
                {"date": row["date"], "close": float(row["close"])}
            )

        def downsample(points: list[dict], max_points: int = 32) -> list[dict]:
            if len(points) <= max_points:
                return points
            step = (len(points) - 1) / float(max_points - 1)
            out = []
            seen = set()
            for i in range(max_points):
                idx = round(i * step)
                if idx not in seen:
                    out.append(points[idx])
                    seen.add(idx)
            return out

        for holding in holdings[:250]:
            ticker = holding.get("ticker")
            if not ticker:
                continue
            rows = prices_by_key.get((holding.get("cusip"), ticker)) or prices_by_ticker.get(ticker) or []
            if len(rows) < 2:
                continue
            start = min(rows, key=lambda p: abs((p["date"] - one_year_start).days))
            end = min(rows, key=lambda p: abs((p["date"] - period).days))
            if not start["close"]:
                continue
            in_period = [p for p in rows if one_year_start <= p["date"] <= period]
            sparkline = downsample(in_period if len(in_period) >= 2 else rows)
            holding["one_year_return_pct"] = (end["close"] / start["close"]) - 1.0
            holding["sparkline_prices"] = [
                {"date": str(p["date"]), "close": p["close"]} for p in sparkline
            ]
    factor = await _core_factor_exposure(conn, manager_cik, period, total_value)
    # Attach prior-quarter factor exposures so the dashboard can display deltas.
    if previous_period is not None:
        prev_total_value = 0.0
        for q in quarters:
            if q["report_period"] == str(previous_period):
                prev_total_value = float(q.get("value_usd") or 0)
                break
        if prev_total_value > 0:
            try:
                prev_factor = await _core_factor_exposure(
                    conn, manager_cik, previous_period, prev_total_value
                )
                factor["previous_exposures"] = prev_factor.get("exposures") or {}
            except Exception:
                pass
    risk = await _risk_summary(conn, factor, summary, manager_cik, period)
    history = [
        {"report_period": q["report_period"], "value_usd": q["value_usd"], "holdings": q["rows"], "rows": q["rows"]}
        for q in reversed(quarters)
    ]
    data_gaps = []
    if summary["unresolved_weight"] and summary["unresolved_weight"] > 0.15:
        data_gaps.append("Issuer resolution coverage is incomplete for this standardized 13F period.")
    if factor["coverage_weight"] < 0.75:
        data_gaps.append("Factor coverage is incomplete for this standardized 13F period.")

    return {
        "available": True,
        "manager": dict(manager) if manager else {"manager_cik": manager_cik, "manager_name": manager_cik},
        "quarters": quarters,
        "summary": summary,
        "holdings": holdings[:250],
        "derivatives": derivatives[:50],
        "sectors": sectors,
        "factor_exposure": factor,
        "risk": risk,
        "history": history,
        "weighted_metrics": [],
        "ownership_13dg": [],
        "data_gaps": data_gaps,
    }


async def _factor_exposure(conn, holdings: list[dict], total_value: float) -> dict:
    tickers = [h["ticker"] for h in holdings if h.get("ticker")]
    if not tickers or total_value <= 0:
        return {
            "coverage_weight": 0.0,
            "exposures": {k: 0.0 for k in FACTOR_COLUMNS},
            "significance": {k: _empty_significance() for k in FACTOR_COLUMNS},
            "rows": [],
        }

    rows = await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (ticker) ticker, window_end, model, beta_mkt, beta_smb, beta_hml,
                   beta_mom, beta_rmw, beta_cma,
                   t_mkt, t_smb, t_hml, t_mom, t_rmw, t_cma
            FROM fact_factor_loadings
            WHERE jurisdiction = 'US' AND model = 'FF6' AND ticker = ANY($1::text[])
            ORDER BY ticker, window_end DESC
        )
        SELECT * FROM latest
        """,
        tickers,
    )
    by_ticker = {r["ticker"]: r for r in rows}
    exposures = {k: 0.0 for k in FACTOR_COLUMNS}
    sig_weight = {k: 0.0 for k in FACTOR_COLUMNS}
    insig_weight = {k: 0.0 for k in FACTOR_COLUMNS}
    sig_value_usd = {k: 0.0 for k in FACTOR_COLUMNS}
    insig_value_usd = {k: 0.0 for k in FACTOR_COLUMNS}
    covered_value = 0.0
    out_rows = []
    for h in holdings:
        ticker = h.get("ticker")
        row = by_ticker.get(ticker)
        if not row:
            continue
        value_usd = float(h["value_usd"] or 0)
        weight = value_usd / total_value
        covered_value += value_usd
        item = {"ticker": ticker, "weight": weight, "window_end": str(row["window_end"])}
        for key, col in FACTOR_COLUMNS.items():
            val = _float(row[col])
            t_val = _float(row[T_STAT_COLUMNS[key]])
            item[key] = val
            item[f"t_{key}"] = t_val
            if val is not None:
                exposures[key] += weight * val
            if t_val is not None:
                if abs(t_val) >= T_STAT_SIGNIFICANCE_THRESHOLD:
                    sig_weight[key] += weight
                    sig_value_usd[key] += value_usd
                else:
                    insig_weight[key] += weight
                    insig_value_usd[key] += value_usd
        out_rows.append(item)
    significance = {
        key: {
            "significant_weight": sig_weight[key],
            "insignificant_weight": insig_weight[key],
            "significant_value_usd": sig_value_usd[key],
            "insignificant_value_usd": insig_value_usd[key],
        }
        for key in FACTOR_COLUMNS
    }
    return {
        "coverage_weight": covered_value / total_value if total_value else 0.0,
        "exposures": exposures,
        "significance": significance,
        "rows": out_rows[:100],
    }


def _empty_significance() -> dict:
    return {
        "significant_weight": 0.0,
        "insignificant_weight": 0.0,
        "significant_value_usd": 0.0,
        "insignificant_value_usd": 0.0,
    }


async def _sector_quarterly_returns(
    conn,
    sector_names: list[str],
    anchor: date,
    n_quarters: int = 8,
    jurisdiction: str = "US",
) -> dict[str, list[dict]]:
    """Per-sector calendar-quarter total returns, compounded from daily
    `cap_weighted_return` rows in `fact_sector_returns` (matching the home
    site's chained-return method in `sector_returns`).

    Returns { sector_name: [ { "quarter": "2024Q3", "return": 0.057 }, … ] },
    chronologically ordered, up to `n_quarters` complete quarters ending in
    or before the quarter containing `anchor`. Partial leading quarters
    (where the first observed trading day is past mid-month of the quarter's
    first month) are dropped so labels don't lie.
    """
    if not sector_names:
        return {}
    if not await _has_relation(conn, "fact_sector_returns"):
        return {sector: [] for sector in sector_names}

    # Pull (n_quarters + 2) quarters of daily data so we have a comfortable
    # buffer and can drop any partial leading quarter without losing coverage.
    months_lookback = (n_quarters + 2) * 3
    cutoff = anchor.replace(day=1)
    for _ in range(months_lookback):
        prev_month = cutoff - timedelta(days=1)
        cutoff = prev_month.replace(day=1)

    rows = await conn.fetch(
        """
        SELECT gics_name, date, cap_weighted_return
        FROM fact_sector_returns
        WHERE jurisdiction = $1
          AND grouping_level = 'sector'
          AND gics_name = ANY($2::text[])
          AND date >= $3
          AND date <= $4
          AND cap_weighted_return IS NOT NULL
        ORDER BY gics_name, date
        """,
        jurisdiction,
        sector_names,
        cutoff,
        anchor,
    )

    def _qkey(d: date) -> str:
        return f"{d.year}Q{((d.month - 1) // 3) + 1}"

    def _q_first_month(qkey: str) -> int:
        # 2024Q1 → 1, 2024Q2 → 4, etc.
        return ((int(qkey[-1]) - 1) * 3) + 1

    # Per sector: quarter → { "growth": running compound product, "first_date": earliest date seen }
    by_sector: dict[str, dict[str, dict]] = {s: {} for s in sector_names}
    for r in rows:
        sector = r["gics_name"]
        d: date = r["date"]
        ret = float(r["cap_weighted_return"])
        if sector not in by_sector:
            continue
        qkey = _qkey(d)
        bucket = by_sector[sector].setdefault(qkey, {"growth": 1.0, "first_date": d})
        bucket["growth"] *= (1.0 + ret)
        # Keep the earliest date seen so we can detect partial leading quarters.
        if d < bucket["first_date"]:
            bucket["first_date"] = d

    out: dict[str, list[dict]] = {}
    for sector in sector_names:
        per_quarter = by_sector.get(sector, {})
        # Sort quarters chronologically.
        ordered = sorted(per_quarter.items(), key=lambda kv: (int(kv[0][:4]), int(kv[0][-1])))
        # Drop partial leading quarter if first observation lands well into the
        # quarter (after the 15th of its first month) — its compound return
        # under-represents a real quarter.
        if ordered:
            q0, info0 = ordered[0]
            q0_first = date(int(q0[:4]), _q_first_month(q0), 1)
            if (info0["first_date"] - q0_first).days > 15:
                ordered = ordered[1:]
        series = [
            {"quarter": qkey, "return": info["growth"] - 1.0}
            for qkey, info in ordered
        ]
        out[sector] = series[-n_quarters:]
    return out



async def _core_factor_exposure(conn, manager_cik: str, period: date, total_value: float) -> dict:
    exposures = {k: 0.0 for k in FACTOR_COLUMNS}
    if total_value <= 0:
        return {
            "coverage_weight": 0.0,
            "exposures": exposures,
            "significance": {k: _empty_significance() for k in FACTOR_COLUMNS},
            "rows": [],
        }
    rows = await conn.fetch(
        """
        WITH base AS (
            SELECT COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''), d.primary_ticker) AS ticker,
                   COALESCE(sec.issuer_cik, h.issuer_cik, d.cik) AS issuer_cik,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS value_usd,
                   bucket.asset_bucket
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
            LEFT JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
            LEFT JOIN LATERAL (
                SELECT CASE
                    WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                    WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                    ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                END AS asset_bucket
            ) bucket ON true
            WHERE h.manager_cik = $1
              AND h.report_period = $2
              AND h.is_latest_amendment
        ),
        grouped AS (
            SELECT issuer_cik, ticker, SUM(value_usd) AS value_usd
            FROM base
            WHERE ticker IS NOT NULL
              AND asset_bucket <> 'derivatives'
            GROUP BY issuer_cik, ticker
        )
        SELECT g.issuer_cik, g.ticker, g.value_usd,
               fl.window_end, fl.model, fl.beta_mkt, fl.beta_smb, fl.beta_hml,
               fl.beta_mom, fl.beta_rmw, fl.beta_cma,
               fl.t_mkt, fl.t_smb, fl.t_hml, fl.t_mom, fl.t_rmw, fl.t_cma
        FROM grouped g
        LEFT JOIN LATERAL (
            SELECT window_end, model, beta_mkt, beta_smb, beta_hml,
                   beta_mom, beta_rmw, beta_cma,
                   t_mkt, t_smb, t_hml, t_mom, t_rmw, t_cma
            FROM fact_factor_loadings l
            WHERE l.jurisdiction = 'US'
              AND l.model = 'FF6'
              AND l.ticker = g.ticker
            ORDER BY l.window_end DESC
            LIMIT 1
        ) fl ON true
        ORDER BY g.value_usd DESC NULLS LAST
        """,
        manager_cik,
        period,
    )
    covered_value = 0.0
    sig_weight = {k: 0.0 for k in FACTOR_COLUMNS}
    insig_weight = {k: 0.0 for k in FACTOR_COLUMNS}
    sig_value_usd = {k: 0.0 for k in FACTOR_COLUMNS}
    insig_value_usd = {k: 0.0 for k in FACTOR_COLUMNS}
    out_rows = []
    for r in rows:
        value_usd = float(r["value_usd"] or 0)
        has_loading = r["window_end"] is not None
        if not has_loading:
            continue
        weight = value_usd / total_value
        covered_value += value_usd
        value_usd_scaled = _usd_from_x1000(value_usd)
        item = {
            "cik": r["issuer_cik"],
            "ticker": r["ticker"],
            "weight": weight,
            "value_usd": value_usd_scaled,
            "window_end": str(r["window_end"]),
        }
        for key, col in FACTOR_COLUMNS.items():
            val = _float(r[col])
            t_val = _float(r[T_STAT_COLUMNS[key]])
            item[key] = val
            item[f"t_{key}"] = t_val
            if val is not None:
                exposures[key] += weight * val
            if t_val is not None:
                if abs(t_val) >= T_STAT_SIGNIFICANCE_THRESHOLD:
                    sig_weight[key] += weight
                    sig_value_usd[key] += value_usd_scaled
                else:
                    insig_weight[key] += weight
                    insig_value_usd[key] += value_usd_scaled
        out_rows.append(item)
    significance = {
        key: {
            "significant_weight": sig_weight[key],
            "insignificant_weight": insig_weight[key],
            "significant_value_usd": sig_value_usd[key],
            "insignificant_value_usd": insig_value_usd[key],
        }
        for key in FACTOR_COLUMNS
    }
    return {
        "coverage_weight": covered_value / total_value if total_value else 0.0,
        "exposures": exposures,
        "significance": significance,
        "rows": out_rows[:250],
    }


async def _risk_summary(conn, factor: dict, concentration: dict, manager_cik: str | None = None, report_period: date | str | None = None) -> dict:
    exposures = factor.get("exposures") or {}
    rows = await conn.fetch(
        """
        WITH ff AS (
            SELECT date,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Research_Data_Factors_daily' AND factor = 'Mkt-RF') AS mkt,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Research_Data_Factors_daily' AND factor = 'SMB') AS smb,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Research_Data_Factors_daily' AND factor = 'HML') AS hml,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Momentum_Factor_daily' AND factor = 'Mom') AS mom,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Research_Data_5_Factors_2x3_daily' AND factor = 'RMW') AS rmw,
                   MAX(value) FILTER (WHERE dataset = 'F-F_Research_Data_5_Factors_2x3_daily' AND factor = 'CMA') AS cma
            FROM fact_fama_french
            WHERE date >= CURRENT_DATE - INTERVAL '5 years'
              AND dataset = ANY($1::text[])
              AND factor = ANY($2::text[])
            GROUP BY date
        )
        SELECT * FROM ff
        WHERE mkt IS NOT NULL
        ORDER BY date DESC
        LIMIT 1260
        """,
        [
            "F-F_Research_Data_Factors_daily",
            "F-F_Research_Data_5_Factors_2x3_daily",
            "F-F_Momentum_Factor_daily",
        ],
        list(FACTOR_NAMES.values()),
    )
    factor_returns = []
    for r in rows:
        ret = 0.0
        for key in FACTOR_COLUMNS:
            ret += (exposures.get(key) or 0.0) * float(r[key] or 0.0)
        factor_returns.append(ret)
    factor_returns = list(reversed(factor_returns))
    var = _historical_var(
        factor_returns,
        seed_key=f"{manager_cik or 'manager'}:{report_period or 'latest'}",
        coverage_weight=factor.get("coverage_weight", 0.0),
    )
    return {
        "concentration": concentration,
        "factor_var": var,
        "factor_observations": len(factor_returns),
        "factor_coverage_weight": factor.get("coverage_weight", 0.0),
    }


def _historical_var(returns: list[float], *, seed_key: str, coverage_weight: float) -> dict:
    if coverage_weight <= 0:
        return {"available": False, "reason": "Factor coverage is zero for this portfolio.", "horizons": []}
    if len(returns) < 126:
        return {"available": False, "reason": "Not enough factor-return observations.", "horizons": []}

    horizons = [
        _bootstrap_horizon_var(returns, key="1M", days=21, seed_key=seed_key),
        _bootstrap_horizon_var(returns, key="3M", days=63, seed_key=seed_key),
        _bootstrap_horizon_var(returns, key="6M", days=126, seed_key=seed_key),
    ]
    return {
        "available": True,
        "reason": None,
        "horizons": horizons,
        "paths": {h["key"]: h["paths"] for h in horizons},
    }


def _bootstrap_horizon_var(returns: list[float], *, key: str, days: int, seed_key: str, confidence: float = 0.95) -> dict:
    seed = int.from_bytes(hashlib.sha256(f"{seed_key}:{key}:{days}".encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    paths = []
    terminal_returns = []
    max_start = max(0, len(returns) - days)
    for _ in range(1000):
        cumulative = 0.0
        path = [{"step": 0, "value": 0.0}]
        start = rng.randrange(max_start + 1)
        window = returns[start:start + days]
        for step, daily_return in enumerate(window, start=1):
            cumulative = (1.0 + cumulative) * (1.0 + daily_return) - 1.0
            path.append({"step": step, "value": cumulative})
        paths.append(path)
        terminal_returns.append(cumulative)

    ordered_terminal = sorted(terminal_returns)
    cutoff_idx = max(0, min(len(ordered_terminal) - 1, int((1.0 - confidence) * len(ordered_terminal))))
    terminal_cutoff = ordered_terminal[cutoff_idx]
    tail = [ret for ret in ordered_terminal if ret <= terminal_cutoff]
    fan_points = []
    for step in range(days + 1):
        step_values = sorted(path[step]["value"] for path in paths)
        fan_points.append(
            {
                "step": step,
                "p5": _percentile_sorted(step_values, 0.05),
                "p50": _percentile_sorted(step_values, 0.50),
                "p95": _percentile_sorted(step_values, 0.95),
            }
        )

    return {
        "key": key,
        "days": days,
        "confidence": confidence,
        "var": max(0.0, -terminal_cutoff),
        "cvar": max(0.0, -mean(tail)) if tail else None,
        "paths": paths[:30],
        "fan": {
            "points": fan_points,
            "terminal_var_cutoff": terminal_cutoff,
        },
    }


def _percentile_sorted(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    idx = max(0, min(len(values) - 1, round(percentile * (len(values) - 1))))
    return values[idx]


async def _history(conn, manager_cik: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT report_period, COUNT(*) AS rows, COUNT(DISTINCT issuer_ticker) AS tickers,
               SUM(value_x1000) FILTER (WHERE COALESCE(put_call, '') = '' AND COALESCE(sh_prn_flag, 'SH') = 'SH') AS value_x1000
        FROM fact_13f_holdings
        WHERE manager_cik = $1 AND is_latest_amendment
        GROUP BY report_period
        ORDER BY report_period
        """,
        manager_cik,
    )
    return [{"report_period": str(r["report_period"]), "value_usd": _usd_from_x1000(r["value_x1000"]), "holdings": r["tickers"], "rows": r["rows"]} for r in rows]


def _classification_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return CLASSIFICATION_BY_SLUG.get(cleaned, cleaned)


def _asset_mix_dict(row) -> dict:
    total = float(row["value_x1000"] or 0)
    equity = float(row["equity_value_x1000"] or 0)
    derivatives = float(row["derivatives_value_x1000"] or 0)
    other = max(total - equity - derivatives, 0.0)
    return {
        "equity_pct": round((equity / total * 100.0), 1) if total else None,
        "other_pct": round((other / total * 100.0), 1) if total else None,
        "derivatives_pct": round((derivatives / total * 100.0), 1) if total else None,
    }


async def _weighted_metrics(conn, holdings: list[dict], total_value: float) -> list[dict]:
    tickers = [h["ticker"] for h in holdings if h.get("ticker")]
    if not tickers or total_value <= 0:
        return []
    metric_ids = [
        "revenue_growth_year_over_year",
        "revenue_compound_annual_growth_rate_5_year",
        "operating_margin",
        "gross_margin",
        "price_to_sales",
        "price_to_book",
        "free_cash_flow_yield",
        "market_beta",
    ]
    rows = await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (ticker, metric_id) ticker, metric_id, value, unit_type, period_end
            FROM fact_metrics_us
            WHERE ticker = ANY($1::text[]) AND fiscal_period = 'FY' AND metric_id = ANY($2::text[])
            ORDER BY ticker, metric_id, period_end DESC NULLS LAST, fiscal_year DESC
        )
        SELECT * FROM latest
        """,
        tickers,
        metric_ids,
    )
    weights: dict[str, float] = {}
    for h in holdings:
        if h.get("ticker"):
            weights[h["ticker"]] = weights.get(h["ticker"], 0.0) + h["value_usd"] / total_value
    accum: dict[str, dict] = {}
    for r in rows:
        weight = weights.get(r["ticker"], 0.0)
        val = _float(r["value"])
        if val is None:
            continue
        slot = accum.setdefault(r["metric_id"], {"metric_id": r["metric_id"], "unit_type": r["unit_type"], "weighted_value": 0.0, "coverage_weight": 0.0})
        slot["weighted_value"] += weight * val
        slot["coverage_weight"] += weight
    return list(accum.values())


async def _ownership_13dg(conn, holdings: list[dict]) -> list[dict]:
    if not await _has_relation(conn, "fact_13dg_ownership"):
        return []
    ciks = [h["issuer_cik"] for h in holdings if h.get("issuer_cik")]
    if not ciks:
        return []
    rows = await conn.fetch(
        """
        SELECT issuer_ticker, reporting_person_name, form_type, filed_date, percent_of_class,
               amount_beneficially_owned, purpose_of_transaction
        FROM fact_13dg_ownership
        WHERE issuer_cik = ANY($1::text[])
        ORDER BY filed_date DESC NULLS LAST
        LIMIT 20
        """,
        ciks,
    )
    return [dict(r) | {"filed_date": str(r["filed_date"]) if r["filed_date"] else None} for r in rows]


async def _institutional_manager_type_rollups(conn) -> list[dict]:
    if not await _has_relation(conn, "core_13f_manager_period"):
        return []
    has_classification = await _has_relation(conn, "core_13f_manager_classification")
    classification_join = """
        LEFT JOIN core_13f_manager_classification c
          ON c.manager_cik = p.manager_cik
         AND c.report_period = p.report_period
         AND c.classification_status = 'classified'
    """ if has_classification else ""
    classification_select = "COALESCE(NULLIF(c.primary_label, ''), 'Unclassified')" if has_classification else "'All managers'"
    rows = await conn.fetch(
        f"""
        WITH active_period AS (
            SELECT report_period
            FROM core_13f_manager_period
            WHERE COALESCE(portfolio_value_market, long_market_value, 0) > 0
            GROUP BY report_period
            ORDER BY report_period DESC
            LIMIT 1
        )
        SELECT p.report_period,
               {classification_select} AS manager_type,
               COUNT(DISTINCT p.manager_cik)::bigint AS managers,
               COALESCE(SUM(COALESCE(p.portfolio_value_market, p.long_market_value, 0)), 0) AS aum_usd
        FROM core_13f_manager_period p
        JOIN active_period ap ON ap.report_period = p.report_period
        {classification_join}
        WHERE COALESCE(p.portfolio_value_market, p.long_market_value, 0) > 0
        GROUP BY p.report_period, {classification_select}
        ORDER BY aum_usd DESC NULLS LAST, managers DESC
        """
    )
    return [
        {
            "report_period": str(r["report_period"]) if r["report_period"] else None,
            "manager_type": r["manager_type"],
            "managers": int(r["managers"] or 0),
            "aum_usd": _usd_from_x1000(r["aum_usd"]),
        }
        for r in rows
    ]


async def _institutional_aum_by_asset_class(conn) -> list[dict]:
    if not await _has_relation(conn, "core_13f_manager_period"):
        return []
    rows = await conn.fetch(
        """
        WITH periods AS (
            SELECT report_period
            FROM core_13f_manager_period
            WHERE COALESCE(portfolio_value_market, long_market_value, 0) > 0
            GROUP BY report_period
            ORDER BY report_period DESC
            LIMIT 6
        ),
        rolled AS (
            SELECT p.report_period,
                   COALESCE(SUM(p.equity_value), 0) AS equity_value,
                   COALESCE(SUM(p.fund_etf_value), 0) AS fund_etf_value,
                   COALESCE(SUM(p.fixed_income_value), 0) AS fixed_income_value,
                   COALESCE(SUM(p.derivatives_value), 0) AS derivatives_value,
                   COALESCE(SUM(p.other_value), 0) AS other_value
            FROM core_13f_manager_period p
            JOIN periods rp ON rp.report_period = p.report_period
            GROUP BY p.report_period
        )
        SELECT report_period,
               equity_value,
               fund_etf_value,
               fixed_income_value,
               derivatives_value,
               other_value,
               equity_value + fund_etf_value + fixed_income_value + derivatives_value + other_value AS total_aum_usd
        FROM rolled
        ORDER BY report_period ASC
        """
    )
    return [
        {
            "report_period": str(r["report_period"]) if r["report_period"] else None,
            "total_aum_usd": _usd_from_x1000(r["total_aum_usd"]),
            "asset_classes": [
                {"asset_class": "Equity", "value_usd": _usd_from_x1000(r["equity_value"])},
                {"asset_class": "Fund / ETF", "value_usd": _usd_from_x1000(r["fund_etf_value"])},
                {"asset_class": "Fixed Income", "value_usd": _usd_from_x1000(r["fixed_income_value"])},
                {"asset_class": "Derivatives", "value_usd": _usd_from_x1000(r["derivatives_value"])},
                {"asset_class": "Other", "value_usd": _usd_from_x1000(r["other_value"])},
            ],
        }
        for r in rows
    ]


@router.get("/status")
async def institutional_status() -> dict:
    async with acquire() as conn:
        if await _has_relation(conn, "recon_13f_period"):
            summary = await conn.fetchrow(
                """
                SELECT COUNT(*) AS periods,
                       MIN(report_period) AS min_period,
                       MAX(report_period) AS max_period,
                       COALESCE(SUM(dataset_count), 0) AS datasets,
                       COALESCE(SUM(downloaded_count), 0) AS downloaded,
                       COALESCE(SUM(parsed_count), 0) AS parsed,
                       COALESCE(SUM(standardized_count), 0) AS standardized,
                       COALESCE(SUM(classified_managers), 0) AS classified_manager_periods,
                       COALESCE(SUM(holdings), 0) AS holdings
                FROM recon_13f_period
                """
            )
            recent = await conn.fetch(
                """
                SELECT report_period, dataset_count, downloaded_count, parsed_count,
                       standardized_count, classified_managers, filings, latest_filings,
                       holdings, managers, issuer_resolved_weight, price_coverage_weight,
                       factor_coverage_weight, warnings, computed_at
                FROM recon_13f_period
                ORDER BY report_period DESC
                LIMIT 12
                """
            )
            out = {
                "core_13f": {
                    **{k: (str(summary[k]) if isinstance(summary[k], date) else summary[k]) for k in summary.keys()},
                    "recent_periods": [
                        {k: (str(r[k]) if isinstance(r[k], date) else _float(r[k]) if k.endswith("_weight") else r[k]) for k in r.keys()}
                        for r in recent
                    ],
                }
            }
            coverage = await _security_13f_coverage(conn)
            if coverage:
                out["security_13f_us"] = coverage
            out["manager_type_rollups"] = await _institutional_manager_type_rollups(conn)
            out["aum_by_asset_class"] = await _institutional_aum_by_asset_class(conn)
            return out
        out = {}
        for table in (
            "source_13f_dataset_state",
            "fact_13f_holdings",
            "dim_13f_manager",
            "dim_security_identifier_us",
            "fact_security_identifier_evidence_us",
            "source_13dg_filing_state",
            "fact_13dg_ownership",
        ):
            if await _has_relation(conn, table):
                if table == "fact_13f_holdings":
                    out[table] = await _relation_row_estimate(conn, table)
                    out[f"{table}_estimated"] = True
                else:
                    row = await conn.fetchrow(f"SELECT COUNT(*) AS n FROM {table}")
                    out[table] = row["n"]
            else:
                out[table] = None
        if await _has_relation(conn, "source_13f_dataset_state"):
            coverage = await conn.fetchrow(
                """
                SELECT COUNT(*) AS datasets,
                       COUNT(*) FILTER (WHERE downloaded) AS downloaded,
                       COUNT(*) FILTER (WHERE parsed) AS parsed,
                       COALESCE(SUM(rows_parsed), 0) AS rows_parsed
                FROM source_13f_dataset_state
                """
            )
            out["13f_dataset_coverage"] = dict(coverage)
            span = await conn.fetchrow(
                """
                SELECT MIN((metadata->>'quarter_end')::date) FILTER (
                           WHERE metadata->>'quarter_end' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                       ) AS min_period,
                       MAX((metadata->>'quarter_end')::date) FILTER (
                           WHERE metadata->>'quarter_end' ~ '^\\d{4}-\\d{2}-\\d{2}$'
                       ) AS max_period,
                       COUNT(*) FILTER (WHERE parsed) AS parsed_periods
                FROM source_13f_dataset_state
                """
            )
            out["13f_span"] = {k: str(span[k]) if isinstance(span[k], date) else span[k] for k in span.keys()}
        if await _has_relation(conn, "dim_13f_manager"):
            manager_quality = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE manager_name = manager_cik) AS cik_name_placeholders,
                       COUNT(*) FILTER (WHERE street1 IS NULL) AS missing_address,
                       COUNT(*) FILTER (WHERE name_source = 'other_manager') AS other_manager_only
                FROM dim_13f_manager
                """
            )
            out["manager_quality"] = dict(manager_quality)
        if await _has_relation(conn, "dim_security_identifier_us"):
            coverage = await conn.fetchrow(
                """
                SELECT COUNT(*) AS observed_cusips,
                       COUNT(*) FILTER (WHERE resolution_status = 'resolved') AS resolved_cusips,
                       COUNT(*) FILTER (WHERE resolution_status = 'ambiguous') AS ambiguous_cusips,
                       COUNT(*) FILTER (WHERE resolution_status = 'non_company_security') AS non_company_cusips,
                       COUNT(*) FILTER (WHERE resolution_status = 'unresolved') AS unresolved_cusips
                FROM dim_security_identifier_us
                """
            )
            out["13f_issuer_resolution"] = dict(coverage) | {
                "basis": "distinct observed CUSIPs; exact row/value coverage is computed during backfill jobs"
            }
        coverage = await _security_13f_coverage(conn)
        if coverage:
            out["security_13f_us"] = coverage
        if await _has_relation(conn, "dim_security_identifier_us"):
            rows = await conn.fetch(
                """
                SELECT resolution_status, security_type, COUNT(*) AS n
                FROM dim_security_identifier_us
                GROUP BY resolution_status, security_type
                ORDER BY resolution_status, security_type
                """
            )
            out["security_identifier_us"] = [dict(r) for r in rows]
        if await _has_relation(conn, "fact_security_identifier_evidence_us"):
            rows = await conn.fetch(
                """
                SELECT source_name, COUNT(*) AS n
                FROM fact_security_identifier_evidence_us
                GROUP BY source_name
                ORDER BY source_name
                """
            )
            out["security_identifier_evidence_us"] = [dict(r) for r in rows]
        if await _has_relation(conn, "fact_13dg_ownership"):
            coverage = await conn.fetchrow(
                """
                SELECT COUNT(*) AS rows,
                       COUNT(*) FILTER (WHERE issuer_cik IS NOT NULL) AS resolved_rows
                FROM fact_13dg_ownership
                """
            )
            out["13dg_issuer_resolution"] = dict(coverage)
        out["manager_type_rollups"] = await _institutional_manager_type_rollups(conn)
        out["aum_by_asset_class"] = await _institutional_aum_by_asset_class(conn)
    return out


@router.get("/managers")
async def managers(
    q: Optional[str] = Query(None),
    limit: int = Query(48),
    classification: Optional[str] = Query(None),
) -> dict:
    q_like = f"%{q.strip()}%" if q else None
    classification_value = _classification_value(classification)
    async with acquire() as conn:
        if await _has_relation(conn, "core_13f_manager_period"):
            rows = await conn.fetch(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (p.manager_cik) p.*
                    FROM core_13f_manager_period p
                    ORDER BY p.manager_cik, p.report_period DESC
                )
                SELECT m.manager_cik,
                       m.legal_name AS manager_name,
                       NULL::text AS manager_type,
                       m.metadata_source AS name_source,
                       m.filing_count_primary,
                       m.filing_count_other,
                       m.filing_count_total,
                       p.report_period,
                       COALESCE(p.long_market_value, p.portfolio_value_market) AS value_usd,
                       p.position_count AS holdings,
                       NULL::integer AS resolved_tickers,
                       p.equity_pct,
                       p.fixed_income_pct,
                       p.fund_etf_pct,
                       p.derivatives_pct,
                       p.other_pct,
                       p.factor_coverage_weight,
                       p.beta_mkt,
                       c.primary_label
                FROM latest p
                JOIN core_13f_manager m ON m.manager_cik = p.manager_cik
                LEFT JOIN core_13f_manager_classification c
                  ON c.manager_cik = p.manager_cik
                 AND c.report_period = p.report_period
                 AND c.classification_status = 'classified'
                WHERE ($1::text IS NULL OR m.legal_name ILIKE $1 OR m.manager_cik LIKE $1)
                  AND ($3::text IS NULL OR c.primary_label = $3)
                ORDER BY COALESCE(p.long_market_value, p.portfolio_value_market) DESC NULLS LAST
                LIMIT $2
                """,
                q_like,
                limit,
                classification_value,
            )
            return {
                "managers": [
                    {
                        "manager_cik": r["manager_cik"],
                        "manager_name": r["manager_name"],
                        "manager_type": r["manager_type"],
                        "name_source": r["name_source"],
                        "filing_count_primary": r["filing_count_primary"],
                        "filing_count_other": r["filing_count_other"],
                        "filing_count_total": r["filing_count_total"],
                        "report_period": str(r["report_period"]) if r["report_period"] else None,
                        "value_usd": _usd_from_x1000(r["value_usd"]),
                        "holdings": r["holdings"],
                        "resolved_tickers": r["resolved_tickers"],
                        "asset_mix": {
                            "equity_pct": round(float(r["equity_pct"] or 0) * 100.0, 1) if r["equity_pct"] is not None else None,
                            "fixed_income_pct": round(float(r["fixed_income_pct"] or 0) * 100.0, 1) if r["fixed_income_pct"] is not None else None,
                            "fund_etf_pct": round(float(r["fund_etf_pct"] or 0) * 100.0, 1) if r["fund_etf_pct"] is not None else None,
                            "other_pct": round(float(r["other_pct"] or 0) * 100.0, 1) if r["other_pct"] is not None else None,
                            "derivatives_pct": round(float(r["derivatives_pct"] or 0) * 100.0, 1) if r["derivatives_pct"] is not None else None,
                        },
                        "factor_summary": {
                            "coverage_weight": _float(r["factor_coverage_weight"]),
                            "beta_mkt": _float(r["beta_mkt"]),
                        },
                    }
                    for r in rows
                ]
            }
        has_classification = await _has_relation(conn, "fact_13f_manager_classification")
        classification_cols = """
                   cls.primary_label
        """ if has_classification else """
                   NULL::text AS primary_label
        """
        final_classification_cols = """
                   m.primary_label
        """
        classification_join = """
            LEFT JOIN LATERAL (
                SELECT c.primary_label
                FROM fact_13f_manager_classification c
                WHERE c.manager_cik = m.manager_cik
                  AND c.classification_status = 'classified'
                ORDER BY c.report_period DESC, c.created_at DESC
                LIMIT 1
            ) cls ON true
        """ if has_classification else ""
        classification_filter = "AND ($3::text IS NULL OR cls.primary_label = $3)" if has_classification else "AND $3::text IS NULL"
        rows = await conn.fetch(
            f"""
            WITH manager_base AS (
                SELECT m.manager_cik, m.manager_name, m.manager_type, m.name_source,
                       m.filing_count_primary, m.filing_count_other, m.filing_count_total,
                       {classification_cols}
                FROM dim_13f_manager m
                {classification_join}
                WHERE ($1::text IS NULL OR m.manager_name ILIKE $1 OR m.manager_cik LIKE $1)
                  {classification_filter}
            )
            SELECT m.manager_cik, m.manager_name, m.manager_type, m.name_source,
                   m.filing_count_primary, m.filing_count_other, m.filing_count_total,
                   latest.report_period, values.value_x1000, values.holdings,
                   values.equity_value_x1000, values.derivatives_value_x1000,
                   NULL::integer AS resolved_tickers,
                   {final_classification_cols}
            FROM manager_base m
            JOIN LATERAL (
                SELECT report_period
                FROM fact_13f_holdings h
                WHERE h.manager_cik = m.manager_cik
                  AND h.is_latest_amendment
                ORDER BY report_period DESC
                LIMIT 1
            ) latest ON true
            JOIN LATERAL (
                SELECT SUM(h.value_x1000) FILTER (WHERE h.value_x1000 IS NOT NULL) AS value_x1000,
                       COUNT(*) AS holdings,
                       SUM(h.value_x1000) FILTER (
                           WHERE COALESCE(h.put_call, '') = ''
                             AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                       ) AS equity_value_x1000,
                       SUM(h.value_x1000) FILTER (
                           WHERE UPPER(COALESCE(h.put_call, '')) IN ('PUT', 'CALL')
                       ) AS derivatives_value_x1000
                FROM fact_13f_holdings h
                WHERE h.manager_cik = m.manager_cik
                  AND h.report_period = latest.report_period
                  AND h.is_latest_amendment
            ) values ON true
            ORDER BY values.value_x1000 DESC NULLS LAST
            LIMIT $2
            """,
            q_like,
            limit,
            classification_value,
        )
    return {
        "managers": [
            {
                "manager_cik": r["manager_cik"],
                "manager_name": r["manager_name"],
                "manager_type": r["manager_type"],
                "name_source": r["name_source"],
                "filing_count_primary": r["filing_count_primary"],
                "filing_count_other": r["filing_count_other"],
                "filing_count_total": r["filing_count_total"],
                "report_period": str(r["report_period"]) if r["report_period"] else None,
                "value_usd": _usd_from_x1000(r["value_x1000"]),
                "holdings": r["holdings"],
                "resolved_tickers": r["resolved_tickers"],
                "asset_mix": _asset_mix_dict(r),
            }
            for r in rows
        ]
    }


@router.get("/manager/{manager_cik}")
async def manager_detail(manager_cik: str = Path(...), report_period: Optional[str] = Query(None)) -> dict:
    async with acquire() as conn:
        return await _manager_packet(conn, manager_cik, report_period)


@router.post("/manager/{manager_cik}/narrative")
async def generate_narrative(manager_cik: str = Path(...), report_period: Optional[str] = Query(None)) -> dict:
    async with acquire() as conn:
        packet = await _manager_packet(conn, manager_cik, report_period)
        if not packet.get("available"):
            raise HTTPException(status_code=404, detail=packet.get("reason") or "Manager data unavailable.")
        period = date.fromisoformat(packet["summary"]["report_period"])
        payload = await _portfolio_narrative_payload(conn, packet)
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        cached = await conn.fetchrow(
            """
            SELECT narrative, model, created_at, analytics_packet
            FROM fact_institutional_narrative
            WHERE manager_cik = $1 AND report_period = $2 AND input_hash = $3
              AND length(btrim(narrative)) > 0
            ORDER BY created_at DESC
            LIMIT 1
            """,
            manager_cik.zfill(10),
            period,
            input_hash,
        )
        if cached:
            analytics_packet = cached["analytics_packet"] or {}
            if isinstance(analytics_packet, str):
                try:
                    analytics_packet = json.loads(analytics_packet)
                except json.JSONDecodeError:
                    analytics_packet = {}
            return {
                "cached": True,
                "narrative": cached["narrative"],
                "model": cached["model"],
                "created_at": str(cached["created_at"]),
            }

    prov = llm_providers.get(None)   # AI_ANALYST_LLM_PROVIDER, else DeepSeek
    api_key = llm_runtime.resolve_env_key(prov.id)
    if not api_key:
        raise HTTPException(status_code=400, detail=f"{prov.env[0]} is not configured.")
    model = llm_providers.chat_model(prov.id, os.environ.get("DEEPSEEK_MODEL"))
    narrative = await _call_llm(prov.id, api_key, model, payload)
    if not narrative:
        raise HTTPException(status_code=502, detail=f"{prov.label} returned an empty narrative.")

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO fact_institutional_narrative
                (manager_cik, report_period, input_hash, model, narrative, analytics_packet)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (manager_cik, report_period, input_hash) DO UPDATE SET
                narrative = EXCLUDED.narrative,
                model = EXCLUDED.model,
                analytics_packet = EXCLUDED.analytics_packet
            """,
            manager_cik.zfill(10),
            period,
            input_hash,
            model,
            narrative,
            json.dumps(payload, default=str),
        )
    return {"cached": False, "narrative": narrative, "model": model, "created_at": None}


async def _portfolio_narrative_payload(conn, packet: dict) -> dict:
    holdings = [
        {
            "ticker": h.get("ticker"),
            "company_name": h.get("company_name"),
            "sector": h.get("sector"),
            "value_usd": h.get("value_usd"),
            "weight": h.get("weight"),
            "shares": h.get("shares"),
            "shares_change": h.get("shares_change"),
            "shares_change_pct": h.get("shares_change_pct"),
            "price_at_filing": h.get("price_at_filing"),
            "one_year_return_pct": h.get("one_year_return_pct"),
            "estimated_trade_value_usd": (
                h.get("shares_change") * h.get("price_at_filing")
                if h.get("shares_change") is not None and h.get("price_at_filing")
                else None
            ),
            "directional_trade_return_context_usd": (
                h.get("shares_change") * h.get("price_at_filing") * h.get("one_year_return_pct")
                if h.get("shares_change") is not None and h.get("price_at_filing") and h.get("one_year_return_pct") is not None
                else None
            ),
        }
        for h in packet.get("holdings", [])
        if h.get("shares_change") is not None
    ]
    added = sorted((h for h in holdings if (h.get("shares_change") or 0) > 0), key=lambda h: h.get("shares_change") or 0, reverse=True)[:12]
    reduced = sorted((h for h in holdings if (h.get("shares_change") or 0) < 0), key=lambda h: h.get("shares_change") or 0)[:12]
    biggest_trades = sorted(
        holdings,
        key=lambda h: abs(h.get("estimated_trade_value_usd") or h.get("shares_change") or 0),
        reverse=True,
    )[:15]
    sectors = [
        {
            "sector": s.get("sector"),
            "current_weight": s.get("weight"),
            "previous_weight": s.get("previous_weight"),
            "weight_change_pp": s.get("weight_change_pp"),
            "value_usd": s.get("value_usd"),
            "name_count": s.get("name_count"),
            "top_holdings": [
                {
                    "ticker": h.get("ticker"),
                    "company_name": h.get("company_name"),
                    "weight": h.get("weight"),
                    "value_usd": h.get("value_usd"),
                    "shares_change": h.get("shares_change"),
                }
                for h in (s.get("top_holdings") or [])[:5]
            ],
        }
        for s in packet.get("sectors", [])
    ]
    macro_backdrop = await _portfolio_macro_backdrop(conn, packet)
    performance_context = await _portfolio_sector_performance_context(conn, packet)
    factor_change = await _portfolio_factor_change(conn, packet)
    return {
        "narrative_version": 4,
        "narrative_kind": "portfolio_sections",
        "manager": packet.get("manager"),
        "report_period": packet.get("summary", {}).get("report_period"),
        "previous_report_period": packet.get("summary", {}).get("previous_report_period"),
        "summary": packet.get("summary"),
        "top_holdings": packet.get("holdings", [])[:25],
        "sectors": sectors,
        "factor_exposure": packet.get("factor_exposure", {}).get("exposures"),
        "factor_exposure_change": factor_change,
        "risk": packet.get("risk"),
        "weighted_metrics": packet.get("weighted_metrics", []),
        "biggest_trades": biggest_trades,
        "most_added_names": added,
        "most_reduced_names": reduced,
        "macro_backdrop": macro_backdrop,
        "sector_performance_context": performance_context,
        "data_gaps": packet.get("data_gaps", []),
    }


async def _portfolio_macro_backdrop(conn, packet: dict) -> list[dict]:
    report_period = packet.get("summary", {}).get("report_period")
    previous_report_period = packet.get("summary", {}).get("previous_report_period")
    if not report_period or not previous_report_period:
        return []
    try:
        period = date.fromisoformat(str(report_period))
        previous_period = date.fromisoformat(str(previous_report_period))
    except ValueError:
        return []
    if not await _has_relation(conn, "ref_macro_series") or not await _has_relation(conn, "fact_macro"):
        return []
    rows = await conn.fetch(
        """
        WITH targets AS (
            SELECT series_id, name, category, units, frequency, importance, story_tile_slot
            FROM ref_macro_series
            WHERE jurisdiction = 'US'
              AND is_active
              AND importance <= 1
              AND story_tile_slot IS NOT NULL
            ORDER BY importance, category, story_tile_slot
            LIMIT 16
        )
        SELECT t.series_id, t.name, t.category, t.units, t.frequency,
               cur.date AS current_date, cur.value AS current_value,
               prev.date AS previous_date, prev.value AS previous_value,
               cur.value - prev.value AS value_change
        FROM targets t
        LEFT JOIN LATERAL (
            SELECT date, value
            FROM fact_macro f
            WHERE f.series_id = t.series_id
              AND f.date <= $1
              AND f.value IS NOT NULL
            ORDER BY f.date DESC
            LIMIT 1
        ) cur ON true
        LEFT JOIN LATERAL (
            SELECT date, value
            FROM fact_macro f
            WHERE f.series_id = t.series_id
              AND f.date <= $2
              AND f.value IS NOT NULL
            ORDER BY f.date DESC
            LIMIT 1
        ) prev ON true
        WHERE cur.value IS NOT NULL
          AND prev.value IS NOT NULL
        ORDER BY t.importance, ABS(cur.value - prev.value) DESC NULLS LAST
        LIMIT 10
        """,
        period,
        previous_period,
    )
    return [
        {
            "series_id": r["series_id"],
            "name": r["name"],
            "category": r["category"],
            "units": r["units"],
            "frequency": r["frequency"],
            "current_date": str(r["current_date"]) if r["current_date"] else None,
            "current_value": _float(r["current_value"]),
            "previous_date": str(r["previous_date"]) if r["previous_date"] else None,
            "previous_value": _float(r["previous_value"]),
            "change": _float(r["value_change"]),
        }
        for r in rows
    ]


async def _portfolio_sector_performance_context(conn, packet: dict) -> list[dict]:
    report_period = packet.get("summary", {}).get("report_period")
    previous_report_period = packet.get("summary", {}).get("previous_report_period")
    sectors = packet.get("sectors") or []
    sector_names = [s.get("sector") for s in sectors if s.get("sector")]
    if not report_period or not previous_report_period or not sector_names:
        return []
    try:
        period = date.fromisoformat(str(report_period))
        previous_period = date.fromisoformat(str(previous_report_period))
    except ValueError:
        return []
    if not await _has_relation(conn, "fact_sector_returns"):
        return []
    rows = await conn.fetch(
        """
        WITH sectors AS (
            SELECT unnest($1::text[]) AS sector
        )
        SELECT s.sector,
               cur.date AS current_date, cur.level AS current_level,
               prev.date AS previous_date, prev.level AS previous_level,
               cur.level / NULLIF(prev.level, 0) - 1.0 AS sector_return
        FROM sectors s
        LEFT JOIN LATERAL (
            SELECT date, level
            FROM fact_sector_returns r
            WHERE r.jurisdiction = 'US'
              AND r.grouping_level = 'sector'
              AND r.gics_name = s.sector
              AND r.date <= $2
              AND r.level IS NOT NULL
            ORDER BY r.date DESC
            LIMIT 1
        ) cur ON true
        LEFT JOIN LATERAL (
            SELECT date, level
            FROM fact_sector_returns r
            WHERE r.jurisdiction = 'US'
              AND r.grouping_level = 'sector'
              AND r.gics_name = s.sector
              AND r.date <= $3
              AND r.level IS NOT NULL
            ORDER BY r.date DESC
            LIMIT 1
        ) prev ON true
        WHERE cur.level IS NOT NULL
          AND prev.level IS NOT NULL
        """,
        sector_names,
        period,
        previous_period,
    )
    returns_by_sector = {r["sector"]: _float(r["sector_return"]) for r in rows}
    out = []
    for sector in sectors:
        sector_name = sector.get("sector")
        sector_return = returns_by_sector.get(sector_name)
        previous_weight = sector.get("previous_weight")
        current_weight = sector.get("weight")
        weight_change_pp = sector.get("weight_change_pp")
        contribution = None
        reallocation_effect = None
        if sector_return is not None:
            contribution = (current_weight or 0.0) * sector_return if current_weight is not None else None
            reallocation_effect = ((weight_change_pp or 0.0) / 100.0) * sector_return if weight_change_pp is not None else None
        out.append(
            {
                "sector": sector_name,
                "current_weight": current_weight,
                "previous_weight": previous_weight,
                "weight_change_pp": weight_change_pp,
                "sector_return": sector_return,
                "current_weight_return_contribution": contribution,
                "reallocation_effect_estimate": reallocation_effect,
            }
        )
    return sorted(out, key=lambda x: abs(x.get("reallocation_effect_estimate") or 0.0), reverse=True)[:10]


async def _portfolio_factor_change(conn, packet: dict) -> dict:
    previous_report_period = packet.get("summary", {}).get("previous_report_period")
    manager_cik = (packet.get("manager") or {}).get("manager_cik")
    current = (packet.get("factor_exposure") or {}).get("exposures") or {}
    if not manager_cik or not previous_report_period:
        return {"available": False, "reason": "Previous report period unavailable.", "exposures": {}, "changes": {}}
    try:
        previous_period = date.fromisoformat(str(previous_report_period))
    except ValueError:
        return {"available": False, "reason": "Previous report period is invalid.", "exposures": {}, "changes": {}}

    previous_value = None
    for item in packet.get("history", []):
        if item.get("report_period") == previous_report_period:
            previous_value = item.get("value_usd")
            break
    if not previous_value:
        return {"available": False, "reason": "Previous portfolio value unavailable.", "exposures": {}, "changes": {}}

    previous = await _core_factor_exposure(conn, str(manager_cik).zfill(10), previous_period, float(previous_value))
    previous_exposures = previous.get("exposures") or {}
    changes = {
        key: (current.get(key) or 0.0) - (previous_exposures.get(key) or 0.0)
        for key in FACTOR_COLUMNS
    }
    return {
        "available": previous.get("coverage_weight", 0.0) > 0,
        "previous_report_period": previous_report_period,
        "previous_coverage_weight": previous.get("coverage_weight"),
        "current_coverage_weight": (packet.get("factor_exposure") or {}).get("coverage_weight"),
        "exposures": previous_exposures,
        "changes": changes,
    }


async def _call_llm(provider: str, api_key: str, model: str, payload: dict) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are an institutional portfolio risk analyst. Write concise, evidence-based commentary using only "
                "the supplied deterministic analytics packet. Do not invent facts, flows, execution prices, motives, "
                "or macro data. When the packet only supports an inference, say 'likely' or 'may'. Use the exact "
                "section headings requested. Wrap important companies, sectors, factors, and macro variables in "
                "**double asterisks**. Wrap positive numeric changes in [[green:+value]] and negative numeric changes "
                "in [[red:-value]]. Do not use HTML."
            ),
        },
        {
            "role": "user",
            "content": (
                "Write a portfolio narrative with exactly these six markdown headings, in this order:\n"
                "## Executive Summary\n"
                "## Macro\n"
                "## Trading Activity/Biggest Trades\n"
                "## Performance Impact of Trades\n"
                "## Risk Factor Changes\n"
                "## Sector level changes\n\n"
                "In Executive Summary, provide EXACTLY 5 bullet points (one '- ' per line, no sub-bullets) in this order: "
                "(1) macro stance, (2) sector allocations, (3) risk-factor exposures, (4) trading activity, "
                "(5) most notable single-name move. Keep each bullet under 22 words; cite a specific number, sector, "
                "factor, or ticker per bullet. This section is the scannable TL;DR — do not repeat it verbatim later.\n\n"
                "Use 2-4 tight bullets per remaining section. In Macro, summarize the macro_backdrop and explain how rate, "
                "growth, inflation, labor, or liquidity changes may have shaped positioning. In Trading Activity/Biggest "
                "Trades, cite the largest additions/reductions from biggest_trades, most_added_names, and most_reduced_names. "
                "In Performance Impact of Trades, use one_year_return_pct, directional_trade_return_context_usd, and "
                "sector_performance_context only as directional context; do not claim realized P&L unless the packet says so. "
                "In Risk Factor Changes, use factor_exposure_change and current factor exposure/VaR to explain how the risk "
                "profile moved. In Sector level changes, explain sector weight changes and connect them back to the macro "
                "environment where the packet supports it. Mention data gaps briefly only if they affect interpretation.\n\n"
                "Analytics packet:\n" + json.dumps(payload, default=str)
            ),
        },
    ]
    return await _narrative_text(provider, api_key, model, messages)


async def _narrative_text(provider: str, api_key: str, model: str, messages: list[dict]) -> str:
    """One narrative call, retried once with a bigger budget if it came back empty.

    The previous DeepSeek-only client inspected ``finish_reason == "length"`` on
    the raw response; the shared runtime returns just the assistant message, so
    an empty answer (the observable symptom of a truncated one) triggers the
    same retry.
    """
    try:
        msg = await llm_runtime.chat_once(
            api_key=api_key, provider=provider, model=model, messages=messages,
            temperature=0.2, max_tokens=2600,
        )
        content = str(msg.get("content") or "").strip()
        if content:
            return content
        msg = await llm_runtime.chat_once(
            api_key=api_key, provider=provider, model=model, messages=messages,
            temperature=0.2, max_tokens=3200,
        )
        return str(msg.get("content") or "").strip()
    except llm_runtime.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Top holders (Newsletter chart 7 — per-ticker 13F)
# ---------------------------------------------------------------------------

_INDEX_PROVIDER_PATTERNS = (
    "vanguard", "blackrock", "state street", "ssga", "geode capital",
    "northern trust", "schwab investment management",
)


# Map the long primary_label values stored in core_13f_manager_classification
# to the same short slugs used by Portfolio Analytics
# (INSTITUTIONAL_MANAGER_STYLES in web/src/components/top-tabs.tsx) so the
# frontend can colour-code top holders consistently.
_LABEL_TO_SLUG = {
    "Asset Management: Alternative (Speculative/Trading)": "alternative",
    "Asset Management: Traditional (Long-Term Capital)":   "traditional",
    "Banking: Wealth & Trust (Investment)":                "wealth_trust",
    "Banking: Capital Markets & Trading (Speculative)":    "capital_markets",
    "Insurance: General Account (Long-Term Capital)":      "insurance",
}


def _classification_slug(primary_label: str | None) -> str | None:
    """Translate the long Portfolio-Analytics label into a short slug."""
    if not primary_label:
        return None
    return _LABEL_TO_SLUG.get(primary_label)


def _holder_type(manager_name: str | None, primary_label: str | None) -> str:
    """Heuristic 3-way classification used by the top-holders chart."""
    n = (manager_name or "").lower()
    for needle in _INDEX_PROVIDER_PATTERNS:
        if needle in n:
            return "Index"
    label = (primary_label or "").lower()
    if "alternative" in label or "capital markets" in label or "hedge" in label:
        return "HF"
    return "Active"


_GICS_SECTOR_LABEL_BY_SLUG: dict[str, str] = {
    "energy":                 "Energy",
    "materials":              "Materials",
    "industrials":            "Industrials",
    "consumer-discretionary": "Consumer Discretionary",
    "consumer-staples":       "Consumer Staples",
    "health-care":            "Health Care",
    "financials":             "Financials",
    "information-technology": "Information Technology",
    "communication-services": "Communication Services",
    "utilities":              "Utilities",
    "real-estate":            "Real Estate",
}


@router.get("/sector/{slug}")
async def institutional_sector(
    slug: str = Path(..., description="GICS sector slug, e.g. 'energy'"),
    top_n: int = Query(12, ge=3, le=30),
) -> dict:
    """Sector intelligence: 8Q performance + top owning managers for the latest period."""
    sector_label = _GICS_SECTOR_LABEL_BY_SLUG.get(slug)
    if not sector_label:
        raise HTTPException(status_code=404, detail=f"Unknown sector slug: {slug}")

    async with acquire() as conn:
        # Find the latest report_period that has any holdings (cheap probe).
        latest = await conn.fetchrow(
            """
            SELECT MAX(report_period) AS rp
            FROM   core_13f_holding
            WHERE  is_latest_amendment
            """,
        )
        anchor: Optional[date] = latest["rp"] if latest else None

        quarterly_returns: list[dict] = []
        if anchor is not None:
            qmap = await _sector_quarterly_returns(conn, [sector_label], anchor, n_quarters=8)
            quarterly_returns = qmap.get(sector_label, [])

        top_managers: list[dict] = []
        if anchor is not None:
            rows = await conn.fetch(
                """
                WITH base AS (
                    SELECT h.manager_cik,
                           COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS value_usd,
                           COALESCE(d.gics_sector_name, sec.sector) AS sector,
                           bucket.asset_bucket
                    FROM   core_13f_holding h
                    LEFT   JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
                    LEFT   JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
                    LEFT   JOIN LATERAL (
                        SELECT CASE
                            WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                            WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                            ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                        END AS asset_bucket
                    ) bucket ON true
                    WHERE  h.report_period = $1
                      AND  h.is_latest_amendment
                ),
                in_sector AS (
                    SELECT manager_cik,
                           SUM(value_usd) FILTER (WHERE sector = $2 AND asset_bucket <> 'derivatives') AS sector_value,
                           SUM(value_usd) FILTER (WHERE asset_bucket <> 'derivatives') AS portfolio_value
                    FROM   base
                    GROUP  BY manager_cik
                )
                SELECT s.manager_cik,
                       COALESCE(m.legal_name, dm.manager_name, s.manager_cik) AS manager_name,
                       s.sector_value,
                       s.portfolio_value,
                       CASE WHEN s.portfolio_value > 0
                            THEN s.sector_value / s.portfolio_value
                            ELSE NULL END AS sector_weight_in_portfolio
                FROM   in_sector s
                LEFT   JOIN core_13f_manager m ON m.manager_cik = s.manager_cik
                LEFT   JOIN dim_13f_manager dm ON dm.manager_cik = s.manager_cik
                WHERE  s.sector_value IS NOT NULL AND s.sector_value > 0
                ORDER  BY s.sector_value DESC NULLS LAST
                LIMIT  $3
                """,
                anchor,
                sector_label,
                top_n,
            )
            top_managers = [
                {
                    "manager_cik": r["manager_cik"],
                    "manager_name": r["manager_name"],
                    "sector_value_usd": _usd_from_x1000(r["sector_value"]),
                    "portfolio_value_usd": _usd_from_x1000(r["portfolio_value"]),
                    "sector_weight_in_portfolio": _float(r["sector_weight_in_portfolio"]),
                }
                for r in rows
            ]

    return {
        "slug": slug,
        "sector": sector_label,
        "as_of": str(anchor) if anchor else None,
        "quarterly_returns": quarterly_returns,
        "top_managers": top_managers,
        "notable_trades": [],
    }


@router.get("/{ticker}/top-holders")
async def institutional_top_holders(
    ticker: str = Path(..., description="Issuer ticker, e.g. AAPL"),
    report_period: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to latest"),
    top_n: int = Query(10, ge=3, le=200),
    classification: Optional[str] = Query(
        None,
        description=(
            "Restrict holders to one Portfolio-Analytics classification slug: "
            "alternative | traditional | wealth_trust | capital_markets | insurance"
        ),
    ),
    year_max: Optional[int] = Query(
        None,
        description=(
            "Constrain the auto-resolved 'latest report period' to the most "
            "recent 13F whose report_period falls inside this calendar year. "
            "Used to keep top holders aligned with the Filing-Coverage year "
            "range on the equities page."
        ),
    ),
) -> dict:
    """Top N 13F holders for an issuer, with QoQ share-count delta and type.

    Reads from the lean-core tables — the same source Portfolio Analytics
    uses, because the upstream fact_13f_holdings is currently empty in this
    deployment.

    Issuer resolution is done by CUSIP (not by core_13f_holding.issuer_cik),
    because that column is only populated when lean-core managed to resolve
    the CUSIP at write time — many rows have it NULL even when they belong
    to the issuer. Portfolio Analytics goes the other way (manager_cik →
    holdings), so this path mirrors it: resolve the issuer's CUSIP set via
    dim_13f_security_us, then aggregate holdings whose `upper(cusip)` is in
    that set.
    """
    async with acquire() as conn:
        scope = await _institutional_security_scope(conn, ticker)
        cusips = scope["cusips"]
        cik = scope["cik"]
        if not cusips and not cik:
            return {"ticker": ticker, "report_period": None, "rows": []}

        # Resolve every CUSIP for this issuer via dim_13f_security_us (any
        # share class — AAPL has historically had multiple CUSIPs across
        # splits/share-class events). Use both primary_ticker AND issuer_cik
        # so we don't miss rows where one of those is null.
        # Build a unified WHERE clause that matches a holding to this issuer
        # via EITHER (a) its CUSIP being in the resolved set, OR (b) the
        # row's own issuer_cik directly equalling the company's CIK (covers
        # rows whose CUSIP isn't in dim_13f_security_us yet but where
        # lean-core managed to resolve issuer_cik some other way).
        issuer_pred = (
            "(upper(h.cusip) = ANY($1::text[]) OR h.issuer_cik = $2)"
            if cik
            else "upper(h.cusip) = ANY($1::text[])"
        )
        base_params = [cusips, cik] if cik else [cusips]

        # Resolve the target_label (full long form) the user wants to filter
        # by, given the slug they passed. Invalid slug → no filter (full set).
        target_label = None
        if classification:
            for full_label, slug in _LABEL_TO_SLUG.items():
                if slug == classification:
                    target_label = full_label
                    break

        # When year_max is supplied, restrict 'latest' to filings whose
        # report_period falls in that calendar year or before. Otherwise
        # take the overall max.
        year_max_cap = date(year_max, 12, 31) if year_max else None

        if report_period:
            target = date.fromisoformat(report_period)
        else:
            if cik:
                if year_max_cap:
                    latest = await conn.fetchrow(
                        f"""
                        SELECT MAX(report_period) AS rp
                        FROM   core_13f_holding h
                        WHERE  {issuer_pred}
                          AND  h.is_latest_amendment = TRUE
                          AND  h.report_period <= ${len(base_params) + 1}::date
                        """,
                        *base_params,
                        year_max_cap,
                    )
                else:
                    latest = await conn.fetchrow(
                        f"""
                        SELECT MAX(report_period) AS rp
                        FROM   core_13f_holding h
                        WHERE  {issuer_pred} AND h.is_latest_amendment = TRUE
                        """,
                        *base_params,
                    )
                if not latest or not latest["rp"]:
                    return {"ticker": ticker, "report_period": None, "rows": []}
                target = latest["rp"]
            else:
                target = await _latest_issuer_holding_period(conn, cusips, cik, year_max_cap=year_max_cap)
                if not target:
                    return {"ticker": ticker, "report_period": None, "rows": []}

        # Sum shares per manager for this period and the prior period (for QoQ).
        # When a classification filter is supplied we apply it in the final
        # SELECT so the QoQ delta is still computed against the same manager's
        # prior-period holding rather than dropping pre-aggregation.
        target_param = len(base_params) + 1
        top_n_param = len(base_params) + 2
        class_param = len(base_params) + 3
        cls_filter_sql = f" WHERE primary_label = ${class_param} " if target_label else ""
        prev_period_sql = f"""
                SELECT MAX(h.report_period) AS rp
                FROM   core_13f_holding h
                WHERE  {issuer_pred}
                  AND  h.report_period < ${target_param}
                  AND  h.is_latest_amendment = TRUE
            """ if cik else f"""
                SELECT MAX(rp) AS rp
                FROM (
                    SELECT MAX(h.report_period) AS rp
                    FROM   core_13f_holding h
                    WHERE  upper(h.cusip) = ANY($1::text[])
                      AND  h.report_period < ${target_param}
                      AND  h.is_latest_amendment = TRUE
                    GROUP  BY upper(h.cusip)
                    {
                        f'''
                    UNION ALL
                    SELECT MAX(h.report_period) AS rp
                    FROM   core_13f_holding h
                    WHERE  h.issuer_cik = $2
                      AND  h.report_period < ${target_param}
                      AND  h.is_latest_amendment = TRUE
                    '''
                        if cik
                        else ''
                    }
                ) periods
            """

        rows_raw = await conn.fetch(
            f"""
            WITH this_period AS (
                SELECT h.manager_cik, SUM(h.shares_or_principal)::numeric AS shares
                FROM   core_13f_holding h
                WHERE  {issuer_pred}
                  AND  h.report_period = ${target_param}
                  AND  h.is_latest_amendment = TRUE
                  AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND  COALESCE(h.put_call, '') = ''
                GROUP  BY h.manager_cik
            ),
            prev_period_row AS (
                {prev_period_sql}
            ),
            prev AS (
                SELECT h.manager_cik, SUM(h.shares_or_principal)::numeric AS shares_prev
                FROM   core_13f_holding h
                CROSS  JOIN prev_period_row p
                WHERE  {issuer_pred}
                  AND  p.rp IS NOT NULL
                  AND  h.report_period = p.rp
                  AND  h.is_latest_amendment = TRUE
                  AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND  COALESCE(h.put_call, '') = ''
                GROUP  BY h.manager_cik
            ),
            joined AS (
                SELECT  t.manager_cik,
                        m.legal_name AS manager_name,
                        t.shares,
                        (t.shares - COALESCE(p.shares_prev, 0)) AS qoq_change,
                        p.shares_prev,
                        cls.primary_label
                FROM    this_period t
                JOIN    core_13f_manager m  ON m.manager_cik = t.manager_cik
                LEFT    JOIN prev p         ON p.manager_cik = t.manager_cik
                LEFT    JOIN LATERAL (
                    SELECT c.primary_label
                    FROM core_13f_manager_classification c
                    WHERE c.manager_cik = t.manager_cik
                    ORDER BY c.report_period DESC, c.created_at DESC
                    LIMIT 1
                ) cls ON true
            )
            SELECT * FROM joined
            {cls_filter_sql}
            ORDER   BY shares DESC NULLS LAST
            LIMIT   ${top_n_param}
            """,
            *(
                [*base_params, target, top_n, target_label]
                if target_label
                else [*base_params, target, top_n]
            ),
        )

    if not rows_raw:
        return {"ticker": ticker, "report_period": target.isoformat(), "rows": []}

    max_shares = float(rows_raw[0]["shares"] or 0) or 1.0
    out = []
    for i, r in enumerate(rows_raw):
        shares_m = float(r["shares"]) / 1_000_000.0 if r["shares"] is not None else 0.0
        prev_m = float(r["shares_prev"]) / 1_000_000.0 if r["shares_prev"] is not None else None
        delta_m = (shares_m - prev_m) if prev_m is not None else None
        out.append({
            "rank": i + 1,
            "manager_cik": r["manager_cik"],
            "manager_name": r["manager_name"],
            "shares_millions": round(shares_m, 1),
            "qoq_change_millions": round(delta_m, 1) if delta_m is not None else None,
            # Legacy 3-way bucket (Index/Active/HF) — kept for clients that
            # don't yet read the new fields below.
            "manager_type": _holder_type(r["manager_name"], r["primary_label"]),
            # New Portfolio-Analytics-aligned fields:
            #   classification: the short slug used by the nav dropdowns
            #     (alternative | traditional | wealth_trust | capital_markets
            #     | insurance | None)
            #   classification_label: full human label, or None when unclassified
            "classification": _classification_slug(r["primary_label"]),
            "classification_label": r["primary_label"],
            "bar_pct": round(100.0 * float(r["shares"] or 0) / max_shares, 1),
        })
    return {"ticker": ticker, "report_period": target.isoformat(), "rows": out}


@router.get("/{ticker}")
async def get_institutional(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query("US"),
    quarter: Optional[str] = Query(None),
    limit: int = Query(30),
) -> dict:
    if jurisdiction != "US":
        return {"ticker": ticker, "quarter": None, "quarters": [], "holders": [], "summary": None}

    async with acquire() as conn:
        scope = await _institutional_security_scope(conn, ticker)
        cusips = scope["cusips"]
        cik = scope["cik"]
        if not cusips and not cik:
            return {"ticker": ticker, "quarter": None, "quarters": [], "holders": [], "summary": None}
        issuer_pred = (
            "(upper(h.cusip) = ANY($1::text[]) OR h.issuer_cik = $2)"
            if cik
            else "upper(h.cusip) = ANY($1::text[])"
        )
        base_params = [cusips, cik] if cik else [cusips]

        if quarter:
            target = date.fromisoformat(quarter)
        else:
            if cik:
                latest = await conn.fetchrow(
                    f"""
                    SELECT MAX(report_period) AS rp
                    FROM   core_13f_holding h
                    WHERE  {issuer_pred}
                      AND  h.is_latest_amendment = TRUE
                      AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                      AND  COALESCE(h.put_call, '') = ''
                    """,
                    *base_params,
                )
                if not latest or not latest["rp"]:
                    return {"ticker": ticker, "quarter": None, "quarters": [], "holders": [], "summary": None}
                target = latest["rp"]
            else:
                target = await _latest_issuer_holding_period(conn, cusips, cik, shares_only=True)
                if not target:
                    return {"ticker": ticker, "quarter": None, "quarters": [], "holders": [], "summary": None}
        target_str = target.isoformat()
        # Return the full historical list of quarters for this issuer so the
        # client-side range picker can offer multi-period comparisons. Limit
        # to a reasonable horizon (~6 years).
        #
        # Use a recursive "loose index scan" (skip-scan emulation): repeatedly
        # ask PostgreSQL for MAX(report_period < previous). With the CUSIP +
        # report_period indexes this walks distinct periods in O(periods)
        # index lookups instead of scanning every row. The naïve
        # `SELECT DISTINCT ... ORDER BY ... LIMIT 24` form timed out on
        # high-volume issuers (AAPL, MSFT) because the planner had to read
        # every matching row before sorting.
        quarter_rows: list = []
        if cusips:
            quarter_rows = await conn.fetch(
                """
                WITH RECURSIVE periods AS (
                    (SELECT h.report_period
                     FROM   core_13f_holding h
                     WHERE  upper(h.cusip) = ANY($1::text[])
                       AND  h.is_latest_amendment = TRUE
                       AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                       AND  COALESCE(h.put_call, '') = ''
                     ORDER BY h.report_period DESC
                     LIMIT 1)
                    UNION ALL
                    SELECT (
                        SELECT MAX(h.report_period)
                        FROM   core_13f_holding h
                        WHERE  upper(h.cusip) = ANY($1::text[])
                          AND  h.is_latest_amendment = TRUE
                          AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                          AND  COALESCE(h.put_call, '') = ''
                          AND  h.report_period < periods.report_period
                    )
                    FROM   periods
                    WHERE  periods.report_period IS NOT NULL
                )
                SELECT report_period
                FROM   periods
                WHERE  report_period IS NOT NULL
                LIMIT  24
                """,
                cusips,
            )
        if not quarter_rows and cik:
            quarter_rows = await conn.fetch(
                """
                WITH RECURSIVE periods AS (
                    (SELECT h.report_period
                     FROM   core_13f_holding h
                     WHERE  h.issuer_cik = $1
                       AND  h.is_latest_amendment = TRUE
                       AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                       AND  COALESCE(h.put_call, '') = ''
                     ORDER BY h.report_period DESC
                     LIMIT 1)
                    UNION ALL
                    SELECT (
                        SELECT MAX(h.report_period)
                        FROM   core_13f_holding h
                        WHERE  h.issuer_cik = $1
                          AND  h.is_latest_amendment = TRUE
                          AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                          AND  COALESCE(h.put_call, '') = ''
                          AND  h.report_period < periods.report_period
                    )
                    FROM   periods
                    WHERE  periods.report_period IS NOT NULL
                )
                SELECT report_period
                FROM   periods
                WHERE  report_period IS NOT NULL
                LIMIT  24
                """,
                cik,
            )
        quarters = [r["report_period"].isoformat() for r in quarter_rows] or [target_str]

        holders_raw = await conn.fetch(
            f"""
            WITH grouped AS (
                SELECT
                    COALESCE(m.legal_name, dm.manager_name, h.manager_cik) AS manager_name,
                    h.manager_cik,
                    SUM(COALESCE(h.market_value_usd, h.value_reported * 1000, 0))::numeric AS value_usd,
                    SUM(h.shares_or_principal)::numeric AS shares_or_principal,
                    NULLIF(MAX(COALESCE(h.put_call, '')), '') AS put_call,
                    NULLIF(MAX(COALESCE(h.investment_discretion, '')), '') AS investment_discretion
                FROM   core_13f_holding h
                LEFT   JOIN core_13f_manager m ON m.manager_cik = h.manager_cik
                LEFT   JOIN dim_13f_manager dm ON dm.manager_cik = h.manager_cik
                WHERE  {issuer_pred}
                  AND  h.report_period = ${len(base_params) + 1}
                  AND  h.is_latest_amendment = TRUE
                  AND  COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND  COALESCE(h.put_call, '') = ''
                GROUP  BY h.manager_cik, COALESCE(m.legal_name, dm.manager_name, h.manager_cik)
            )
            SELECT *,
                   COUNT(*) OVER() AS num_holders,
                   SUM(value_usd) OVER() AS total_value,
                   SUM(shares_or_principal) OVER() AS total_shares
            FROM   grouped
            ORDER  BY value_usd DESC NULLS LAST
            LIMIT  ${len(base_params) + 2}
            """,
            *base_params,
            target,
            limit,
        )

    holders = [
        {
            "manager_cik": r["manager_cik"],
            "manager_name": r["manager_name"],
            "value_usd": float(r["value_usd"] or 0),
            "shares": int(r["shares_or_principal"] or 0),
            "put_call": r["put_call"],
            "investment_discretion": r["investment_discretion"],
        }
        for r in holders_raw
    ]

    summary_row = holders_raw[0] if holders_raw else None
    summary = {
        "quarter": target_str,
        "num_holders": summary_row["num_holders"],
        "total_value_usd": float(summary_row["total_value"] or 0),
        "total_shares": int(summary_row["total_shares"] or 0),
    } if summary_row else None

    return {"ticker": ticker, "quarter": target_str, "quarters": quarters, "holders": holders, "summary": summary}
