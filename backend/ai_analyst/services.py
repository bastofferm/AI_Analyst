"""Deterministic DB-backed data packets for AI Analyst."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from ._db import read_sql
from . import dcf_engine


PEER_METRIC_IDS = (
    "revenue_growth_year_over_year",
    "earnings_before_interest_taxes_depreciation_amortization_margin",
    "operating_margin",
    "net_profit_margin",
    "return_on_invested_capital",
)

CORE_LINE_ITEMS = (
    "revenue",
    "gross_profit",
    "earnings_before_interest_taxes_depreciation_amortization",
    "earnings_before_interest_taxes",
    "net_income",
    "cash_flow_from_operations",
    "capital_expenditures",
    "free_cash_flow",
    "cash_and_cash_equivalents",
    "total_assets",
    "total_financial_debt",
    "net_debt",
    "total_equity",
)

INSURANCE_LINE_ITEMS = (
    "net_premiums_earned",
    "net_investment_income_insurance",
    "claims_and_losses_incurred",
    "insurance_underwriting_expense",
    "underwriting_income_loss",
    "earnings_before_taxes",
    "income_tax_provision",
    "net_income",
    "net_income_attributable_to_common",
    "cash_and_cash_equivalents",
    "total_assets",
    "total_financial_debt",
    "net_debt",
    "total_equity",
)

INSURANCE_YFINANCE_CROSS_CHECK_ITEMS = (
    "net_income",
    "cash_and_cash_equivalents",
    "total_assets",
    "total_financial_debt",
    "net_debt",
    "total_equity",
)

INSURANCE_PEER_METRIC_IDS = (
    "net_income_growth_year_over_year",
    "net_profit_margin",
    "return_on_equity",
    "price_to_book",
    "total_financial_debt_to_equity",
)

_YFINANCE_SOURCE_TABLE = "fact_yfinance_fundamental_snapshot"

_YF_LINE_ITEM_ALIASES = {
    "revenue": {
        "total_revenue", "totalRevenue", "revenue", "sales", "operating_revenue",
        "operatingRevenue",
    },
    "gross_profit": {
        "gross_profit", "grossProfit",
    },
    "earnings_before_interest_taxes_depreciation_amortization": {
        "ebitda", "normalized_ebitda", "normalizedEBITDA",
    },
    "earnings_before_interest_taxes": {
        "ebit", "operating_income", "operatingIncome", "operating_profit",
        "income_from_operations",
    },
    "net_income": {
        "net_income", "netIncome", "net_income_common_stockholders",
        "netIncomeCommonStockholders",
    },
    "cash_flow_from_operations": {
        "operating_cash_flow", "operatingCashFlow",
        "total_cash_from_operating_activities", "cash_flow_from_continuing_operating_activities",
    },
    "capital_expenditures": {
        "capital_expenditure", "capitalExpenditure", "capital_expenditures",
        "capital_expenditures_reported",
    },
    "free_cash_flow": {
        "free_cash_flow", "freeCashFlow",
    },
    "cash_and_cash_equivalents": {
        "cash_and_cash_equivalents", "cashAndCashEquivalents",
        "cash_cash_equivalents_and_short_term_investments",
        "cashCashEquivalentsAndShortTermInvestments",
    },
    "total_assets": {
        "total_assets", "totalAssets",
    },
    "total_financial_debt": {
        "total_debt", "totalDebt", "long_term_debt_and_capital_lease_obligation",
        "longTermDebtAndCapitalLeaseObligation",
    },
    "net_debt": {
        "net_debt", "netDebt",
    },
    "total_equity": {
        "stockholders_equity", "stockholdersEquity", "total_stockholder_equity",
        "totalStockholderEquity", "total_equity_gross_minority_interest",
    },
}

_YF_ALIAS_LOOKUP = {
    re.sub(r"[^a-z0-9]+", "", alias.lower()): line_item
    for line_item, aliases in _YF_LINE_ITEM_ALIASES.items()
    for alias in {*aliases, line_item}
}

_ABSOLUTE_COMPARISON_ITEMS = {"capital_expenditures"}


_INTL_YAHOO_SECTOR_TO_CANONICAL = {
    # Yahoo raw sector labels (as they appear in dim_company_intl.mapping_sector and
    # .sector) → canonical taxonomy used by ai_analyst downstream.
    "financial services": "non_bank_financial",
    "financials":         "non_bank_financial",
    "banks":              "bank_financial",
    "real estate":        "non_bank_financial",  # further sub-branching disabled for INTL
    "insurance":          "non_bank_financial",
}


def sector_scope_from_company(company: dict[str, Any] | None) -> str:
    """Mirror the SEC/EDINET standardizer's sector hierarchy for display/DQ.

    For INTL companies, `mapping_sector` currently holds a raw Yahoo sector label
    (e.g. "Financial Services") — semantically incompatible with the canonical
    corp|bank_financial|non_bank_financial taxonomy. Normalize here so downstream
    services never see the raw label.
    """
    company = company or {}
    mapping_sector = str(company.get("mapping_sector") or "corp").strip() or "corp"

    # INTL normalization: if the value is a Yahoo sector label (contains a space
    # or is not one of the canonical set), map it.
    canonical_set = {"corp", "bank_financial", "non_bank_financial"}
    if mapping_sector not in canonical_set:
        lower = mapping_sector.lower()
        mapping_sector = _INTL_YAHOO_SECTOR_TO_CANONICAL.get(lower, "corp")

    if mapping_sector != "non_bank_financial":
        return mapping_sector

    sector_code = str(company.get("gics_sector_code") or company.get("gics_sector") or "").strip()
    group_code = str(company.get("gics_industry_group_code") or company.get("gics_industry_group") or "").strip()
    sector_name = str(company.get("gics_sector_name") or "").strip().lower()
    group_name = str(company.get("gics_industry_group_name") or "").strip().lower()

    if sector_code == "60" or sector_name == "real estate":
        return "reit"
    if group_code == "4030" or group_name == "insurance":
        return "insurance"
    if group_code == "4020" or sector_code == "40" or sector_name == "financials":
        return "asset_manager_other_financial"
    return "asset_manager_other_financial"


def line_items_for_sector(sector_scope: str | None) -> tuple[str, ...]:
    if sector_scope == "insurance":
        return INSURANCE_LINE_ITEMS
    return CORE_LINE_ITEMS


def yfinance_cross_check_items_for_sector(sector_scope: str | None) -> tuple[str, ...]:
    if sector_scope == "insurance":
        return INSURANCE_YFINANCE_CROSS_CHECK_ITEMS
    return CORE_LINE_ITEMS


def peer_metric_ids_for_sector(sector_scope: str | None) -> tuple[str, ...]:
    if sector_scope == "insurance":
        return INSURANCE_PEER_METRIC_IDS
    return PEER_METRIC_IDS


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        clean = {}
        for key, value in row.items():
            if isinstance(value, (list, tuple)):
                clean[key] = list(value)
            elif isinstance(value, dict):
                clean[key] = value
            elif pd.isna(value):
                clean[key] = None
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            else:
                clean[key] = float(value) if isinstance(value, float) else value
        out.append(clean)
    return out


def company_overview(ticker: str) -> dict[str, Any]:
    df = read_sql(
        """
        SELECT jurisdiction, uid, cik, edinet_code, ticker, name, exchange, country_code,
               gics_sector_name, gics_industry_group_name, gics_industry_name,
               gics_sub_industry_name, mapping_sector
        FROM v_dim_company
        WHERE UPPER(ticker) = UPPER(%(ticker)s)
        LIMIT 1
        """,
        {"ticker": ticker},
    )
    if df.empty:
        return {"ticker": ticker, "found": False}
    row = df.iloc[0].to_dict()
    row["found"] = True
    return row


def modeled_statement_snapshot(ticker: str, years: int = 5) -> dict[str, Any]:
    overview = company_overview(ticker)
    if not overview.get("found"):
        return {"ticker": ticker, "rows": []}
    jurisdiction = overview["jurisdiction"]
    if jurisdiction == "US":
        table = "fact_fundamentals_std_us"
        join_key = "s.cik = dc.cik"
        entity_expr = "s.cik"
        accounting_standard = "US_GAAP"
    else:
        table = "fact_fundamentals_std_jp"
        join_key = "s.edinet_code = dc.edinet_code"
        entity_expr = "s.edinet_code"
        accounting_standard = "JP_GAAP"
    sector_scope = sector_scope_from_company(overview)
    fallback_items = line_items_for_sector(sector_scope)
    df = read_sql(
        f"""
        WITH profile_rows AS (
            SELECT *
            FROM ref_std_statement_display_profile
            WHERE accounting_standard = %(accounting_standard)s
              AND sector_scope = %(sector_scope)s
        ),
        profile_any AS (
            SELECT EXISTS (SELECT 1 FROM profile_rows) AS has_profile
        ),
        latest_years AS (
            SELECT DISTINCT s.fiscal_year
            FROM {table} s
            JOIN v_dim_company dc ON dc.jurisdiction = %(jurisdiction)s AND {join_key}
            LEFT JOIN ref_standardized_line_items li ON li.line_item_id = s.line_item_id
            LEFT JOIN profile_rows dp
              ON dp.statement_type = li.statement_type
             AND dp.line_item_id = s.line_item_id
            CROSS JOIN profile_any pa
            WHERE UPPER(dc.ticker) = UPPER(%(ticker)s)
              AND s.fiscal_period IN ('FY','Annual')
              AND (
                    (pa.has_profile AND dp.display_policy = 'MAIN')
                 OR (NOT pa.has_profile AND s.line_item_id = ANY(%(items)s))
              )
            ORDER BY s.fiscal_year DESC
            LIMIT %(years)s
        ),
        profile_exists AS (
            SELECT statement_type, TRUE AS has_profile
            FROM profile_rows
            GROUP BY statement_type
        )
        SELECT s.fiscal_year, s.period_end, s.line_item_id,
               COALESCE(li.label, s.line_item_id) AS label,
               li.statement_type, li.category, s.metric_type, s.value::double precision AS value,
               s.currency, dp.display_role, dp.display_policy, dp.display_order,
               COALESCE(vio.identity_violation_count, 0) AS identity_violation_count,
               vio.identity_violation_checks,
               s.source_concept_id, s.concept_path, s.std_concept_path
        FROM {table} s
        JOIN v_dim_company dc ON dc.jurisdiction = %(jurisdiction)s AND {join_key}
        JOIN latest_years y ON y.fiscal_year = s.fiscal_year
        LEFT JOIN ref_standardized_line_items li ON li.line_item_id = s.line_item_id
        LEFT JOIN profile_rows dp
          ON dp.statement_type = li.statement_type
         AND dp.line_item_id = s.line_item_id
        LEFT JOIN profile_exists pe
          ON pe.statement_type = li.statement_type
        LEFT JOIN LATERAL (
            SELECT COUNT(*)::int AS identity_violation_count,
                   ARRAY_AGG(DISTINCT v.check_id ORDER BY v.check_id) AS identity_violation_checks
            FROM ref_std_identity_violation v
            JOIN ref_std_identity_check ic ON ic.check_id = v.check_id
            WHERE v.entity_id = {entity_expr}
              AND v.jurisdiction = %(jurisdiction)s
              AND v.fiscal_year = s.fiscal_year
              AND v.fiscal_period = s.fiscal_period
              AND (
                    ic.lhs_item_id = s.line_item_id
                 OR s.line_item_id = ANY(ic.rhs_item_ids)
              )
        ) vio ON TRUE
        WHERE UPPER(dc.ticker) = UPPER(%(ticker)s)
          AND s.fiscal_period IN ('FY','Annual')
          AND (
                (COALESCE(pe.has_profile, FALSE) AND dp.display_policy = 'MAIN')
             OR (NOT COALESCE(pe.has_profile, FALSE) AND s.line_item_id = ANY(%(items)s))
          )
        ORDER BY s.fiscal_year DESC, COALESCE(li.statement_type, ''),
                 COALESCE(dp.display_order, li.display_order, 9999), s.line_item_id
        """,
        {
            "ticker": ticker,
            "jurisdiction": jurisdiction,
            "years": int(years),
            "items": list(fallback_items),
            "accounting_standard": accounting_standard,
            "sector_scope": sector_scope,
        },
    )
    return {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "accounting_standard": accounting_standard,
        "sector_scope": sector_scope,
        "rows": _records(df),
    }


def modeled_statement_snapshot_intl(ticker: str, years: int = 5) -> dict[str, Any]:
    """INTL analogue of modeled_statement_snapshot.

    Reduces annual rows from fact_yahoo_statement_item to canonical line items via
    _YF_ALIAS_LOOKUP, joins ref_standardized_line_items for label/statement_type/category,
    and returns the same 'rows' shape the DCF engine expects. Accounting standard is
    fixed to 'IFRS' since Yahoo does not disclose it reliably.
    """
    overview = company_overview(ticker)
    if not overview.get("found") or overview.get("jurisdiction") != "INTL":
        return {"ticker": ticker, "rows": []}
    sector_scope = sector_scope_from_company(overview)
    df = read_sql(
        """
        WITH latest_years AS (
            SELECT DISTINCT EXTRACT(YEAR FROM period_end)::int AS fiscal_year
            FROM   fact_yahoo_statement_item ysi
            JOIN   dim_company_intl d ON d.intl_company_id = ysi.intl_company_id
            WHERE  UPPER(d.primary_ticker) = UPPER(%(ticker)s)
              AND  ysi.period_type = 'annual'
              AND  ysi.value IS NOT NULL
            ORDER  BY 1 DESC
            LIMIT  %(years)s
        )
        SELECT EXTRACT(YEAR FROM ysi.period_end)::int AS fiscal_year,
               ysi.period_end,
               ysi.line_item AS raw_label,
               ysi.statement_type AS raw_statement_type,
               ysi.value::double precision AS value
        FROM   fact_yahoo_statement_item ysi
        JOIN   dim_company_intl d ON d.intl_company_id = ysi.intl_company_id
        JOIN   latest_years y ON y.fiscal_year = EXTRACT(YEAR FROM ysi.period_end)::int
        WHERE  UPPER(d.primary_ticker) = UPPER(%(ticker)s)
          AND  ysi.period_type = 'annual'
          AND  ysi.value IS NOT NULL
        """,
        {"ticker": ticker, "years": int(years)},
    )
    raw = _records(df)

    from xbrl_sec.sec.metrics.compute_intl import _YF_ALIAS_LOOKUP, _norm
    # De-duplicate: multiple raw labels may collide onto the same canonical item;
    # prefer the row with the largest |value| for each (fy, line_item).
    best: dict[tuple[int, str], dict[str, Any]] = {}
    for r in raw:
        canonical = _YF_ALIAS_LOOKUP.get(_norm(str(r.get("raw_label") or "")))
        if not canonical:
            continue
        key = (int(r["fiscal_year"]), canonical)
        prev = best.get(key)
        if prev is None or abs(float(r["value"])) > abs(float(prev["value"])):
            best[key] = {**r, "line_item_id": canonical}

    # Enrich with ref_standardized_line_items for label/statement_type/category.
    line_items = sorted({v["line_item_id"] for v in best.values()})
    labels: dict[str, dict[str, Any]] = {}
    if line_items:
        li_df = read_sql(
            """
            SELECT line_item_id, label, statement_type, category
            FROM   ref_standardized_line_items
            WHERE  line_item_id = ANY(%(items)s)
            """,
            {"items": line_items},
        )
        labels = {r["line_item_id"]: r for r in _records(li_df)}

    rows: list[dict[str, Any]] = []
    for (fy, line_item_id), r in sorted(best.items(), key=lambda kv: (-kv[0][0], kv[0][1])):
        meta = labels.get(line_item_id, {})
        rows.append({
            "fiscal_year": fy,
            "period_end": r.get("period_end"),
            "line_item_id": line_item_id,
            "label": meta.get("label") or line_item_id.replace("_", " ").title(),
            "statement_type": meta.get("statement_type") or r.get("raw_statement_type"),
            "category": meta.get("category"),
            "metric_type": None,
            "value": float(r["value"]),
            "currency": None,  # Yahoo statement items are in the company's reporting currency
            "display_role": None,
            "display_policy": "MAIN",
            "display_order": None,
            "identity_violation_count": 0,
            "identity_violation_checks": None,
            "source_concept_id": None,
            "concept_path": None,
            "std_concept_path": None,
        })
    return {
        "ticker": ticker,
        "jurisdiction": "INTL",
        "accounting_standard": "IFRS",
        "sector_scope": sector_scope,
        "rows": rows,
    }


def peer_group_intl(ticker: str, limit: int = 10) -> dict[str, Any]:
    """Peer group for INTL: top N by market cap in the same Yahoo industry, region-first."""
    overview = company_overview(ticker)
    if not overview.get("found") or overview.get("jurisdiction") != "INTL":
        return {"ticker": ticker, "peers": []}
    df = read_sql(
        """
        WITH t AS (
            SELECT intl_company_id, industry, region, market_cap
            FROM   dim_company_intl
            WHERE  UPPER(primary_ticker) = UPPER(%(ticker)s)
            LIMIT  1
        )
        SELECT d.primary_ticker AS ticker, d.name, d.country_code, d.industry, d.sector,
               d.market_cap::double precision AS market_cap
        FROM   dim_company_intl d, t
        WHERE  d.intl_company_id <> t.intl_company_id
          AND  COALESCE(d.include_in_pipeline, true)
          AND  d.industry = t.industry
          AND  (t.region IS NULL OR d.region = t.region)
        ORDER  BY d.market_cap DESC NULLS LAST
        LIMIT  %(limit)s
        """,
        {"ticker": ticker, "limit": int(limit)},
    )
    peers = _records(df)
    # If fewer than 5, widen to sector (drop industry constraint).
    if len(peers) < 5:
        df2 = read_sql(
            """
            WITH t AS (
                SELECT intl_company_id, sector, region
                FROM   dim_company_intl
                WHERE  UPPER(primary_ticker) = UPPER(%(ticker)s)
                LIMIT  1
            )
            SELECT d.primary_ticker AS ticker, d.name, d.country_code, d.industry, d.sector,
                   d.market_cap::double precision AS market_cap
            FROM   dim_company_intl d, t
            WHERE  d.intl_company_id <> t.intl_company_id
              AND  COALESCE(d.include_in_pipeline, true)
              AND  d.sector = t.sector
              AND  (t.region IS NULL OR d.region = t.region)
            ORDER  BY d.market_cap DESC NULLS LAST
            LIMIT  %(limit)s
            """,
            {"ticker": ticker, "limit": int(limit)},
        )
        peers = _records(df2)
    return {"ticker": ticker, "jurisdiction": "INTL", "peers": peers}


def metric_panel(ticker: str, years: int = 5) -> dict[str, Any]:
    overview = company_overview(ticker)
    if not overview.get("found"):
        return {"ticker": ticker, "rows": []}
    juris = overview["jurisdiction"]
    table = {"US": "fact_metrics_us", "JP": "fact_metrics_jp", "INTL": "fact_metrics_intl"}.get(juris, "fact_metrics_us")
    df = read_sql(
        f"""
        WITH latest_years AS (
            SELECT DISTINCT fiscal_year
            FROM {table}
            WHERE UPPER(ticker) = UPPER(%(ticker)s)
              AND fiscal_period IN ('FY','Annual')
            ORDER BY fiscal_year DESC
            LIMIT %(years)s
        )
        SELECT fiscal_year, period_end, metric_id, formula, metric_type, category,
               unit_type, value, currency
        FROM {table}
        WHERE UPPER(ticker) = UPPER(%(ticker)s)
          AND fiscal_period IN ('FY','Annual')
          AND value IS NOT NULL
          AND fiscal_year IN (SELECT fiscal_year FROM latest_years)
        ORDER BY fiscal_year DESC, importance NULLS LAST, metric_id
        LIMIT 400
        """,
        {"ticker": ticker, "years": int(years)},
    )
    return {"ticker": ticker, "jurisdiction": overview["jurisdiction"], "rows": _records(df)}


def market_metrics(ticker: str) -> dict[str, Any]:
    df = read_sql(
        """
        SELECT jurisdiction, ticker, fiscal_year, fiscal_period, period_end, market_date,
               metric_id, value::double precision AS value, currency, source
        FROM fact_market_metrics
        WHERE UPPER(ticker) = UPPER(%(ticker)s)
        ORDER BY market_date DESC NULLS LAST, fiscal_year DESC, metric_id
        LIMIT 80
        """,
        {"ticker": ticker},
    )
    return {"ticker": ticker, "rows": _records(df)}


def yfinance_fundamental_snapshot(ticker: str, years: int = 5, quarters: int = 8) -> dict[str, Any]:
    """Latest Yahoo Finance statement snapshot, normalized enough for cross-checking.

    Yahoo is intentionally advisory here: SEC/EDINET standardized facts remain the
    canonical source, while this packet exposes independent full-year/quarterly rows
    and maps common Yahoo metric names onto the committee line-item vocabulary.
    """
    overview = company_overview(ticker)
    jurisdiction = overview.get("jurisdiction") if overview.get("found") else None
    try:
        df = read_sql(
            f"""
            WITH latest_snapshot AS (
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM {_YFINANCE_SOURCE_TABLE}
                WHERE UPPER(ticker) = UPPER(%(ticker)s)
                  AND (%(jurisdiction)s IS NULL OR jurisdiction = %(jurisdiction)s)
            )
            SELECT y.jurisdiction, y.ticker, y.snapshot_date, y.period_type, y.period_key,
                   y.statement_type, y.metric_id, y.source_metric_key, y.period_end,
                   y.fiscal_year, y.value::double precision AS value, y.currency
            FROM {_YFINANCE_SOURCE_TABLE} y
            JOIN latest_snapshot ls ON ls.snapshot_date = y.snapshot_date
            WHERE UPPER(y.ticker) = UPPER(%(ticker)s)
              AND (%(jurisdiction)s IS NULL OR y.jurisdiction = %(jurisdiction)s)
              AND y.value IS NOT NULL
            ORDER BY y.snapshot_date DESC NULLS LAST,
                     y.fiscal_year DESC NULLS LAST,
                     y.period_end DESC NULLS LAST,
                     y.period_type, y.statement_type, y.metric_id
            LIMIT 2500
            """,
            {"ticker": ticker, "jurisdiction": jurisdiction},
        )
    except Exception as exc:  # noqa: BLE001 - Yahoo is an advisory source
        return {
            "ticker": ticker,
            "jurisdiction": jurisdiction,
            "source_table": _YFINANCE_SOURCE_TABLE,
            "available": False,
            "note": f"Yahoo fundamentals unavailable: {exc.__class__.__name__}: {str(exc)[:180]}",
            "annual_rows": [],
            "quarterly_rows": [],
            "snapshot_rows": [],
        }

    if df.empty:
        return {
            "ticker": ticker,
            "jurisdiction": jurisdiction,
            "source_table": _YFINANCE_SOURCE_TABLE,
            "available": False,
            "note": "No Yahoo fundamental snapshot rows found for ticker.",
            "annual_rows": [],
            "quarterly_rows": [],
            "snapshot_rows": [],
        }

    df = df.copy()
    df["_period_bucket"] = [_yfinance_period_bucket(row) for row in df.to_dict("records")]
    df["canonical_line_item_id"] = [_canonical_yfinance_line_item(row) for row in df.to_dict("records")]

    annual = _limit_yfinance_periods(df, "annual", years)
    quarterly = _limit_yfinance_periods(df, "quarterly", quarters)
    snapshot_rows = df[df["_period_bucket"] == "snapshot"].head(160).drop(columns=["_period_bucket"], errors="ignore")

    annual_records = _records(annual.drop(columns=["_period_bucket"], errors="ignore"))
    quarterly_records = _records(quarterly.drop(columns=["_period_bucket"], errors="ignore"))
    latest_annual = _latest_yfinance_metric_map(annual_records)
    latest_quarter = _latest_yfinance_metric_map(quarterly_records)
    snapshot_date = _records(df[["snapshot_date"]].head(1))[0].get("snapshot_date")

    return {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "source_table": _YFINANCE_SOURCE_TABLE,
        "available": True,
        "snapshot_date": snapshot_date,
        "annual_rows": annual_records,
        "quarterly_rows": quarterly_records,
        "snapshot_rows": _records(snapshot_rows),
        "latest_annual": latest_annual,
        "latest_quarter": latest_quarter,
        "raw_payload_in_table": True,
    }


def yfinance_fundamental_cross_check(
    ticker: str,
    *,
    modeled: dict[str, Any] | None = None,
    yahoo: dict[str, Any] | None = None,
    years: int = 5,
) -> dict[str, Any]:
    """Compare Yahoo annual statement rows with SEC/EDINET standardized facts."""
    modeled = modeled or modeled_statement_snapshot(ticker, years=years)
    yahoo = yahoo or yfinance_fundamental_snapshot(ticker, years=years)
    if not yahoo.get("available"):
        return {
            "ticker": ticker,
            "available": False,
            "source_table": _YFINANCE_SOURCE_TABLE,
            "note": yahoo.get("note") or "Yahoo fundamentals unavailable.",
            "rows": [],
        }

    modeled_by_item = _modeled_line_item_latest(modeled.get("rows") or [])
    yahoo_by_item = _yfinance_line_item_by_year(yahoo.get("annual_rows") or [])
    checks: list[dict[str, Any]] = []
    comparison_items = yfinance_cross_check_items_for_sector(modeled.get("sector_scope"))
    for line_item_id in comparison_items:
        sec_row = modeled_by_item.get(line_item_id)
        yf_years = yahoo_by_item.get(line_item_id) or {}
        if not sec_row or not yf_years:
            continue
        sec_year = _safe_int(sec_row.get("fiscal_year"))
        yf_row = yf_years.get(sec_year) if sec_year is not None else None
        matched_fiscal_year = sec_year
        if yf_row is None:
            matched_fiscal_year, yf_row = max(
                yf_years.items(),
                key=lambda item: (-1 if item[0] is None else item[0], str(item[1].get("period_end") or "")),
            )
        sec_value = _safe_float(sec_row.get("value"))
        yf_value = _safe_float(yf_row.get("value"))
        if sec_value is None or yf_value is None:
            continue
        comparison_basis = "absolute value" if line_item_id in _ABSOLUTE_COMPARISON_ITEMS else "reported sign"
        provider_reported_value = None
        provider_reported_metric_id = None
        provider_reported_source_metric_key = None
        if line_item_id == "net_debt":
            adjusted = _yfinance_net_debt_on_standardized_basis(
                yahoo.get("annual_rows") or [],
                matched_fiscal_year,
            )
            if adjusted is not None:
                provider_reported_value = yf_value
                provider_reported_metric_id = yf_row.get("metric_id")
                provider_reported_source_metric_key = yf_row.get("source_metric_key")
                yf_value = adjusted["value"]
                yf_row = {**yf_row, **adjusted["row"]}
                comparison_basis = adjusted["comparison_basis"]
        compare_sec = abs(sec_value) if line_item_id in _ABSOLUTE_COMPARISON_ITEMS else sec_value
        compare_yf = abs(yf_value) if line_item_id in _ABSOLUTE_COMPARISON_ITEMS else yf_value
        delta = compare_yf - compare_sec
        pct_delta = (delta / abs(compare_sec) * 100.0) if compare_sec else None
        currency_mismatch = (
            bool(sec_row.get("currency"))
            and bool(yf_row.get("currency"))
            and str(sec_row.get("currency")).upper() != str(yf_row.get("currency")).upper()
        )
        severity = _yfinance_check_severity(pct_delta, currency_mismatch=currency_mismatch)
        checks.append({
            "line_item_id": line_item_id,
            "label": sec_row.get("label") or line_item_id,
            "standardized_source": "SEC/EDINET standardized facts",
            "standardized_fiscal_year": sec_year,
            "standardized_period_end": sec_row.get("period_end"),
            "standardized_value": sec_value,
            "standardized_currency": sec_row.get("currency"),
            "yahoo_fiscal_year": matched_fiscal_year,
            "yahoo_period_end": yf_row.get("period_end"),
            "yahoo_metric_id": yf_row.get("metric_id"),
            "yahoo_source_metric_key": yf_row.get("source_metric_key"),
            "yahoo_value": yf_value,
            "yahoo_currency": yf_row.get("currency"),
            "absolute_delta": delta,
            "pct_delta": pct_delta,
            "severity": severity,
            "currency_mismatch": currency_mismatch,
            "comparison_basis": comparison_basis,
            "provider_reported_value": provider_reported_value,
            "provider_reported_metric_id": provider_reported_metric_id,
            "provider_reported_source_metric_key": provider_reported_source_metric_key,
        })

    material_count = sum(1 for row in checks if row["severity"] == "material")
    watch_count = sum(1 for row in checks if row["severity"] in {"watch", "currency_mismatch"})
    compared = len(checks)
    if compared:
        summary = (
            f"Yahoo cross-check compared {compared} overlapping FY line items: "
            f"{material_count} material discrepancy/discrepancies and {watch_count} watch item(s)."
        )
    else:
        summary = "Yahoo fundamentals are available, but no canonical SEC/EDINET line items overlapped."

    return {
        "ticker": ticker,
        "jurisdiction": modeled.get("jurisdiction") or yahoo.get("jurisdiction"),
        "source_table": _YFINANCE_SOURCE_TABLE,
        "available": bool(compared),
        "basis": "latest Yahoo annual snapshot vs latest standardized SEC/EDINET full-year facts",
        "snapshot_date": yahoo.get("snapshot_date"),
        "compared_line_items": compared,
        "material_count": material_count,
        "watch_count": watch_count,
        "rows": checks,
        "summary": summary,
    }


def _yfinance_net_debt_on_standardized_basis(rows: list[dict[str, Any]], fiscal_year: int | None) -> dict[str, Any] | None:
    """Compute Yahoo net debt on the warehouse basis when enough components exist.

    Yahoo's reported "Net Debt" usually subtracts cash only. The standardized layer
    uses total_financial_debt - cash - short_term_investments, so prefer Yahoo's
    cash+short-term-investments row when it is available.
    """
    debt = _latest_yfinance_row_for_aliases(
        rows,
        fiscal_year,
        {
            "total_debt",
            "totalDebt",
            "long_term_debt_and_capital_lease_obligation",
            "longTermDebtAndCapitalLeaseObligation",
        },
    )
    liquidity = _latest_yfinance_row_for_aliases(
        rows,
        fiscal_year,
        {
            "cash_cash_equivalents_and_short_term_investments",
            "cashCashEquivalentsAndShortTermInvestments",
        },
    )
    debt_value = _safe_float((debt or {}).get("value"))
    liquidity_value = _safe_float((liquidity or {}).get("value"))
    if debt_value is None or liquidity_value is None:
        return None
    return {
        "value": debt_value - liquidity_value,
        "comparison_basis": "standardized net debt basis: total debt minus cash and short-term investments",
        "row": {
            "metric_id": "net_debt_standardized_basis",
            "source_metric_key": "Total Debt - Cash Cash Equivalents And Short Term Investments",
            "period_end": (liquidity or debt or {}).get("period_end"),
            "currency": (debt or liquidity or {}).get("currency"),
        },
    }


def _latest_yfinance_row_for_aliases(
    rows: list[dict[str, Any]],
    fiscal_year: int | None,
    aliases: set[str],
) -> dict[str, Any] | None:
    tokens = {_metric_token(alias) for alias in aliases}
    matches = []
    for row in rows:
        if fiscal_year is not None and _safe_int(row.get("fiscal_year")) != fiscal_year:
            continue
        if any(_metric_token(row.get(key)) in tokens for key in ("metric_id", "source_metric_key")):
            matches.append(row)
    if not matches:
        return None
    return max(matches, key=_yfinance_sort_key)


def _canonical_yfinance_line_item(row: dict[str, Any]) -> str | None:
    for key in (row.get("metric_id"), row.get("source_metric_key")):
        token = _metric_token(key)
        if token in _YF_ALIAS_LOOKUP:
            return _YF_ALIAS_LOOKUP[token]
    return None


def _metric_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or pd.isna(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _yfinance_period_bucket(row: dict[str, Any]) -> str:
    period_type = str(row.get("period_type") or "").lower()
    period_key = str(row.get("period_key") or "").lower()
    statement_type = str(row.get("statement_type") or "").lower()
    joined = f"{period_type} {period_key} {statement_type}"
    token = _metric_token(joined)
    if (
        "quarter" in joined
        or re.search(r"(^|[^a-z0-9])q[1-4]([^a-z0-9]|$)", joined)
        or token in {"q", "quarterly"}
    ):
        return "quarterly"
    if (
        "annual" in joined
        or "year" in joined
        or re.search(r"(^|[^a-z0-9])fy([^a-z0-9]|$)", joined)
        or token in {"fy", "fiscalyear", "yearly", "fullyear"}
    ):
        return "annual"
    return "snapshot"


def _yfinance_period_id(row: dict[str, Any], bucket: str) -> str:
    if bucket == "annual":
        fy = _safe_int(row.get("fiscal_year"))
        if fy is not None:
            return f"FY{fy}"
    return str(row.get("period_key") or row.get("period_end") or row.get("snapshot_date") or "")


def _limit_yfinance_periods(df: pd.DataFrame, bucket: str, limit: int) -> pd.DataFrame:
    part = df[df["_period_bucket"] == bucket].copy()
    if part.empty:
        return part
    part["_period_id"] = [_yfinance_period_id(row, bucket) for row in part.to_dict("records")]
    sort_cols = ["fiscal_year", "period_end", "period_key", "statement_type", "metric_id"]
    asc = [False, False, False, True, True]
    part = part.sort_values(sort_cols, ascending=asc, na_position="last")
    ordered_ids = []
    for period_id in part["_period_id"].tolist():
        if period_id not in ordered_ids:
            ordered_ids.append(period_id)
        if len(ordered_ids) >= limit:
            break
    limited = part[part["_period_id"].isin(set(ordered_ids))].copy()
    return limited.drop(columns=["_period_id"], errors="ignore")


def _latest_yfinance_metric_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        line_item = row.get("canonical_line_item_id") or _canonical_yfinance_line_item(row)
        if not line_item:
            continue
        current = best.get(line_item)
        if current is None or _yfinance_sort_key(row) > _yfinance_sort_key(current):
            best[line_item] = row
    return best


def _yfinance_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return (
        _safe_int(row.get("fiscal_year")) or -1,
        str(row.get("period_end") or ""),
        str(row.get("period_key") or ""),
    )


def _modeled_line_item_latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        line_item = row.get("line_item_id")
        if not line_item or row.get("value") is None:
            continue
        current = best.get(line_item)
        if current is None or (_safe_int(row.get("fiscal_year")) or -1) > (_safe_int(current.get("fiscal_year")) or -1):
            best[str(line_item)] = row
    return best


def _yfinance_line_item_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[int | None, dict[str, Any]]]:
    out: dict[str, dict[int | None, dict[str, Any]]] = {}
    for row in rows:
        line_item = row.get("canonical_line_item_id") or _canonical_yfinance_line_item(row)
        if not line_item or row.get("value") is None:
            continue
        fy = _safe_int(row.get("fiscal_year"))
        bucket = out.setdefault(line_item, {})
        current = bucket.get(fy)
        if current is None or _yfinance_sort_key(row) > _yfinance_sort_key(current):
            bucket[fy] = row
    return out


def _yfinance_check_severity(pct_delta: float | None, *, currency_mismatch: bool = False) -> str:
    if currency_mismatch:
        return "currency_mismatch"
    if pct_delta is None:
        return "informational"
    abs_pct = abs(pct_delta)
    if abs_pct <= 5.0:
        return "ok"
    if abs_pct <= 15.0:
        return "watch"
    return "material"


def _latest_market_caps() -> pd.DataFrame:
    return read_sql(
        """
        SELECT DISTINCT ON (ticker, jurisdiction)
               jurisdiction, ticker, value::double precision AS market_cap,
               market_date, currency
        FROM fact_market_metrics
        WHERE metric_id = 'market_capitalization'
          AND value IS NOT NULL
        ORDER BY ticker, jurisdiction, market_date DESC NULLS LAST, fiscal_year DESC
        """
    )


def peer_group(ticker: str, limit: int = 10) -> dict[str, Any]:
    target = company_overview(ticker)
    if not target.get("found"):
        return {"ticker": ticker, "peers": []}
    caps = _latest_market_caps()
    companies = read_sql(
        """
        SELECT jurisdiction, ticker, name, gics_sector_name, mapping_sector
        FROM v_dim_company
        WHERE ticker IS NOT NULL AND ticker <> ''
        """
    )
    universe = companies.merge(caps, on=["jurisdiction", "ticker"], how="inner")
    exclude_target = universe["ticker"].str.upper() != str(ticker).upper()
    target_gics = target.get("gics_sector_name") or ""
    target_mapping = target.get("mapping_sector") or ""
    if target_gics:
        same_sector = universe[(universe["gics_sector_name"].fillna("") == target_gics) & exclude_target].copy()
    else:
        same_sector = universe[(universe["mapping_sector"].fillna("") == target_mapping) & exclude_target].copy()
    primary = same_sector[same_sector["jurisdiction"] == target["jurisdiction"]]
    selected = primary.sort_values("market_cap", ascending=False).head(limit)
    if len(selected) < limit:
        extra = same_sector[~same_sector.index.isin(selected.index)].sort_values("market_cap", ascending=False)
        selected = pd.concat([selected, extra.head(limit - len(selected))])
    peers = selected.head(limit).copy()
    metric_rows = []
    for row in peers.to_dict("records"):
        metric_rows.append(_peer_metric_row(row["ticker"], row["jurisdiction"], row["market_cap"]))
    selection_rule = (
        "same GICS sector; same jurisdiction first; 10 largest by latest market_capitalization"
        if target_gics else
        "mapping_sector fallback because GICS sector is unavailable; same jurisdiction first; 10 largest by latest market_capitalization"
    )
    return {
        "ticker": ticker,
        "target_sector": target.get("gics_sector_name") or target.get("mapping_sector"),
        "selection_rule": selection_rule,
        "peers": [{**p, **m} for p, m in zip(_records(peers), metric_rows)],
    }


def _peer_metric_row(ticker: str, jurisdiction: str, market_cap: float | None) -> dict[str, Any]:
    overview = company_overview(ticker)
    sector_scope = sector_scope_from_company(overview)
    metric_ids = peer_metric_ids_for_sector(sector_scope)
    line_items = line_items_for_sector(sector_scope)
    metric_table = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    std_table = "fact_fundamentals_std_us" if jurisdiction == "US" else "fact_fundamentals_std_jp"
    join_key = "s.cik = dc.cik" if jurisdiction == "US" else "s.edinet_code = dc.edinet_code"
    metrics = read_sql(
        f"""
        SELECT DISTINCT ON (metric_id) metric_id, value
        FROM {metric_table}
        WHERE UPPER(ticker) = UPPER(%(ticker)s)
          AND metric_id = ANY(%(metrics)s)
          AND value IS NOT NULL
          AND fiscal_period IN ('FY','Annual')
        ORDER BY metric_id, fiscal_year DESC
        """,
        {"ticker": ticker, "metrics": list(metric_ids)},
    )
    facts = read_sql(
        f"""
        SELECT DISTINCT ON (s.line_item_id) s.line_item_id, s.value::double precision AS value
        FROM {std_table} s
        JOIN v_dim_company dc ON dc.jurisdiction = %(jurisdiction)s AND {join_key}
        WHERE UPPER(dc.ticker) = UPPER(%(ticker)s)
          AND s.fiscal_period IN ('FY','Annual')
          AND s.line_item_id = ANY(%(items)s)
          AND s.value IS NOT NULL
        ORDER BY s.line_item_id, s.fiscal_year DESC
        """,
        {"ticker": ticker, "jurisdiction": jurisdiction, "items": list(line_items)},
    )
    metric_map = {r["metric_id"]: float(r["value"]) for r in _records(metrics)}
    fact_map = {r["line_item_id"]: float(r["value"]) for r in _records(facts)}
    ev = (market_cap or 0.0) + (fact_map.get("net_debt") or 0.0)
    revenue = fact_map.get("revenue")
    ebitda = fact_map.get("earnings_before_interest_taxes_depreciation_amortization")
    ebit = fact_map.get("earnings_before_interest_taxes")
    net_income = fact_map.get("net_income")
    equity = fact_map.get("total_equity")
    fcf = fact_map.get("free_cash_flow")
    return {
        "revenue_growth": metric_map.get("revenue_growth_year_over_year"),
        "ebitda_margin": metric_map.get("earnings_before_interest_taxes_depreciation_amortization_margin"),
        "ebit_margin": metric_map.get("operating_margin"),
        "net_margin": metric_map.get("net_profit_margin"),
        "roic": metric_map.get("return_on_invested_capital"),
        "pe": (market_cap / net_income) if market_cap and net_income else None,
        "ev_revenue": (ev / revenue) if ev and revenue else None,
        "ev_ebitda": (ev / ebitda) if ev and ebitda else None,
        "ev_ebit": (ev / ebit) if ev and ebit and ebit > 0 else None,
        "ev_fcf": (ev / fcf) if ev and fcf and fcf > 0 else None,
        "pb": (market_cap / equity) if market_cap and equity else None,
        "fcf_yield": (fcf / market_cap) if market_cap and fcf else None,
    }


def factor_exposure(ticker: str) -> dict[str, Any]:
    df = read_sql(
        """
        SELECT l.jurisdiction, l.ticker, l.model, l.window_start, l.window_end,
               l.ff_region, l.alpha, l.beta_mkt, l.beta_smb, l.beta_hml, l.beta_mom,
               l.beta_rmw, l.beta_cma, m.adj_r2, m.residual_vol, m.quality_score, m.n_obs
        FROM fact_factor_loadings l
        JOIN fact_factor_reg_meta m
          ON m.jurisdiction = l.jurisdiction
         AND m.ticker = l.ticker
         AND m.window_end = l.window_end
         AND m.model = l.model
        WHERE UPPER(l.ticker) = UPPER(%(ticker)s)
        ORDER BY l.window_end DESC, CASE WHEN l.model='FF6' THEN 0 ELSE 1 END
        LIMIT 2
        """,
        {"ticker": ticker},
    )
    return {"ticker": ticker, "rows": _records(df)}


def recon_flags(ticker: str, limit: int = 20) -> dict[str, Any]:
    overview = company_overview(ticker)
    if not overview.get("found"):
        return {"ticker": ticker, "rows": []}
    table = "fact_metrics_recon_us" if overview["jurisdiction"] == "US" else "fact_metrics_recon_jp"
    canonical_ticker = str(overview.get("ticker") or ticker).upper()
    entity_col = "cik" if overview["jurisdiction"] == "US" else "edinet_code"
    entity_id = overview.get("cik") if overview["jurisdiction"] == "US" else overview.get("edinet_code")
    entity_filter = f"AND {entity_col} = %(entity_id)s" if entity_id else ""
    df = read_sql(
        f"""
        SELECT fiscal_year, period_end, metric_id, value, trace_quality,
               formula_with_values
        FROM {table}
        WHERE ticker = %(ticker)s
          {entity_filter}
        ORDER BY fiscal_year DESC, metric_id
        LIMIT %(limit)s
        """,
        {"ticker": canonical_ticker, "entity_id": entity_id, "limit": int(limit)},
    )
    return {"ticker": ticker, "rows": _records(df)}


def corporate_dcf(ticker: str, assumptions: dict[str, Any] | None = None) -> dict[str, Any]:
    overview = company_overview(ticker)
    if not overview.get("found"):
        return {"ticker": ticker, "error": "ticker not found"}
    if overview.get("mapping_sector") != "corp":
        return {"ticker": ticker, "implemented": False, "message": "sector valuation model not implemented yet"}
    fundamentals = modeled_statement_snapshot(ticker, years=5)
    compact = _fundamental_pivot(fundamentals["rows"])
    hist = dcf_engine.build_historicals_from_fundamentals(compact)
    if hist is None:
        return {"ticker": ticker, "implemented": False, "message": "insufficient modeled statements for corporate DCF"}
    latest_market = market_metrics(ticker)["rows"]
    price = next((r["value"] for r in latest_market if r.get("metric_id") == "stock_price"), None)
    market_cap = next((r["value"] for r in latest_market if r.get("metric_id") == "market_capitalization"), None)
    shares_mm = ((market_cap / price) / 1_000_000.0) if market_cap and price else 0.0
    default_assumptions = {
        # 7-year explicit fade toward the terminal growth (matches DCF_HORIZON_YEARS).
        "rev_growth_pct": [5.0, 4.5, 4.0, 3.5, 3.0, 2.75, 2.5],
        "terminal_growth_pct": 2.5,
        "ebit_margin_pct": (hist.ebit / hist.revenue * 100.0) if hist.ebit and hist.revenue else 18.0,
        "tax_rate_pct": 21.0,
        "capex_pct_of_rev": hist.capex_pct_of_rev * 100.0,
        "nwc_pct_of_rev": hist.nwc_pct_of_rev * 100.0,
        "wacc_pct": 9.0,
        "share_count_mm": shares_mm,
        "rationale": "Deterministic baseline from latest modeled statements and market metrics.",
    }
    if assumptions:
        default_assumptions.update(assumptions)
    result = dcf_engine.run(default_assumptions, hist, current_price=price)
    result["implemented"] = True
    return result


def _fundamental_pivot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"columns": [], "rows": []}
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="fiscal_year", columns="line_item_id", values="value", aggfunc="first").reset_index()
    pivot = pivot.sort_values("fiscal_year", ascending=False)
    return {"columns": list(pivot.columns), "rows": pivot.values.tolist()}


def report_data_packet(ticker: str) -> dict[str, Any]:
    overview = company_overview(ticker)
    # INTL dispatch: Yahoo-backed companies get a reduced packet (no XBRL raw layer,
    # no US-style yahoo_cross_check, no factor exposure, no recon flags).
    if overview.get("found") and overview.get("jurisdiction") == "INTL":
        return _report_data_packet_intl(ticker, overview)
    modeled = modeled_statement_snapshot(ticker, years=5)
    yahoo = yfinance_fundamental_snapshot(ticker, years=5, quarters=8)
    yahoo_cross_check = yfinance_fundamental_cross_check(ticker, modeled=modeled, yahoo=yahoo, years=5)
    return {
        "ticker": ticker,
        "company": overview,
        "modeled_statements": modeled,
        "yahoo_fundamentals": yahoo,
        "yahoo_cross_check": yahoo_cross_check,
        "metrics": metric_panel(ticker, years=5),
        "market_metrics": market_metrics(ticker),
        "peer_group": peer_group(ticker, limit=10),
        "factor_exposure": factor_exposure(ticker),
        "recon_flags": recon_flags(ticker),
        "dcf": corporate_dcf(ticker),
    }


def _report_data_packet_intl(ticker: str, overview: dict[str, Any]) -> dict[str, Any]:
    """Reduced report_data_packet for INTL companies (Yahoo-backed).

    Populates the same keys as report_data_packet so downstream committee code can
    read either uniformly, but with INTL-appropriate sources / empty stubs for
    XBRL-native fields.
    """
    modeled = modeled_statement_snapshot_intl(ticker, years=5)
    return {
        "ticker": ticker,
        "company": overview,
        "modeled_statements": modeled,
        # Yahoo cross-check compares SEC/EDINET std facts against yfinance snapshot
        # rows keyed by ticker. INTL statements *are* Yahoo — there is nothing to
        # cross-check. Emit empty stubs so downstream code doesn't crash.
        "yahoo_fundamentals": {"ticker": ticker, "annual_rows": [], "quarterly_rows": [],
                                "snapshot_rows": [], "latest_annual": None, "latest_quarter": None},
        "yahoo_cross_check": {"ticker": ticker, "rows": [], "material_count": 0, "watch_count": 0,
                                "summary": "Not applicable for INTL (source = Yahoo)."},
        "metrics": metric_panel(ticker, years=5),        # dispatches to fact_metrics_intl (widened above)
        "market_metrics": market_metrics(ticker),         # reads fact_market_metrics WHERE ticker=INTL rows
        "peer_group": peer_group_intl(ticker, limit=10),
        "factor_exposure": {"ticker": ticker, "rows": []},   # Fama-French coverage is US-only
        "recon_flags": {"ticker": ticker, "rows": []},        # every INTL metric row is trace_quality='computed_only'
        "dcf": _corporate_dcf_intl(ticker, modeled),
    }


def _corporate_dcf_intl(ticker: str, modeled: dict[str, Any]) -> dict[str, Any]:
    """INTL DCF: same dcf_engine but with a sector-default WACC (no factor regression).

    Falls back to `implemented: False` if the reduced modeled statements aren't
    enough to build historicals (e.g. Yahoo statement rows missing revenue or EBIT
    for the ticker).
    """
    compact = _fundamental_pivot(modeled.get("rows") or [])
    hist = dcf_engine.build_historicals_from_fundamentals(compact)
    if hist is None:
        return {"ticker": ticker, "implemented": False,
                "message": "Insufficient Yahoo statement data for INTL DCF."}
    sector_scope = modeled.get("sector_scope") or "corp"
    wacc_pct = _load_sector_default_wacc(sector_scope)
    latest_market = market_metrics(ticker).get("rows") or []
    price = next((r["value"] for r in latest_market if r.get("metric_id") == "stock_price"), None)
    market_cap = next((r["value"] for r in latest_market if r.get("metric_id") == "market_capitalization"), None)
    shares_mm = ((market_cap / price) / 1_000_000.0) if market_cap and price else 0.0
    assumptions = {
        "rev_growth_pct": [5.0, 4.5, 4.0, 3.5, 3.0, 2.75, 2.5],
        "terminal_growth_pct": 2.5,
        "ebit_margin_pct": (hist.ebit / hist.revenue * 100.0) if hist.ebit and hist.revenue else 15.0,
        "tax_rate_pct": 25.0,
        "capex_pct_of_rev": hist.capex_pct_of_rev * 100.0,
        "nwc_pct_of_rev": hist.nwc_pct_of_rev * 100.0,
        "wacc_pct": wacc_pct,
        "share_count_mm": shares_mm,
        "rationale": ("INTL baseline: Yahoo statements + sector-default WACC "
                      f"({sector_scope}={wacc_pct}%). Wider uncertainty band than XBRL runs."),
    }
    result = dcf_engine.run(assumptions, hist, current_price=price)
    result["implemented"] = True
    result["wacc_source"] = "sector_default"
    result["wacc_sector_scope"] = sector_scope
    return result


def _load_sector_default_wacc(sector_scope: str) -> float:
    try:
        df = read_sql(
            "SELECT wacc_pct::double precision AS wacc FROM ref_wacc_sector_default WHERE sector_scope = %(s)s",
            {"s": sector_scope},
        )
        if not df.empty:
            return float(df.iloc[0]["wacc"])
    except Exception:  # noqa: BLE001 — table may not be migrated yet
        pass
    return 9.0  # safe generic default
