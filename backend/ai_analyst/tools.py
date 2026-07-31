"""Read-only DB tools exposed to the LLM via function calling.

Each tool returns a plain dict / list of dicts ready to JSON-serialise. Row caps
keep responses compact so the LLM context isn't blown out.
"""
from __future__ import annotations

import re
import json
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from ._db import read_sql, fetchall_dict
from . import services


MAX_ROWS = 200

_QUARTER_ENDS = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}
_RELATION_EXISTS_CACHE: dict[str, bool] = {}
_MANAGER_CLASSIFICATION_BY_SLUG = {
    "alternative": "Asset Management: Alternative (Speculative/Trading)",
    "alternatives": "Asset Management: Alternative (Speculative/Trading)",
    "hedge": "Asset Management: Alternative (Speculative/Trading)",
    "hedge_fund": "Asset Management: Alternative (Speculative/Trading)",
    "traditional": "Asset Management: Traditional (Long-Term Capital)",
    "long_only": "Asset Management: Traditional (Long-Term Capital)",
    "wealth": "Banking: Wealth & Trust (Investment)",
    "wealth_trust": "Banking: Wealth & Trust (Investment)",
    "capital_markets": "Banking: Capital Markets & Trading (Speculative)",
    "trading": "Banking: Capital Markets & Trading (Speculative)",
    "insurance": "Insurance: General Account (Long-Term Capital)",
}
_MANAGER_SLUG_BY_CLASSIFICATION = {
    value: key
    for key, value in _MANAGER_CLASSIFICATION_BY_SLUG.items()
    if key in {"alternative", "traditional", "wealth_trust", "capital_markets", "insurance"}
}


def _df_to_compact(df: pd.DataFrame, max_rows: int = MAX_ROWS) -> dict:
    if df is None or df.empty:
        return {"columns": list(df.columns) if df is not None else [], "rows": [], "row_count": 0}
    if len(df) > max_rows:
        df = df.head(max_rows)
        truncated = True
    else:
        truncated = False
    cols = [str(c) for c in df.columns]
    out_rows: list[list[Any]] = []
    for _, r in df.iterrows():
        row = []
        for c in cols:
            v = r[c]
            if isinstance(v, float) and pd.isna(v):
                row.append(None)
            elif hasattr(v, "isoformat"):
                row.append(v.isoformat())
            else:
                row.append(v if not isinstance(v, (int, float, str, bool)) or pd.isna(v) is False else v)
        out_rows.append(row)
    return {
        "columns": cols,
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": truncated,
    }


def _norm_juris(j: str | None) -> str:
    if not j:
        return ""
    j = j.strip().upper()
    return "JP" if j in ("JP", "JAPAN", "JPN", "EDINET") else "US"


def _clean_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _df_to_records(df: pd.DataFrame, max_rows: int = MAX_ROWS) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    if len(df) > max_rows:
        df = df.head(max_rows)
    return [
        {str(k): _clean_json_value(v) for k, v in row.items()}
        for row in df.to_dict("records")
    ]


def _parse_13f_quarter(quarter: str | None) -> str | None:
    """Return a YYYY-MM-DD report-period string from YYYYQn or ISO date input."""
    if quarter is None or str(quarter).strip() == "":
        return None
    q = str(quarter).strip().upper()
    match = re.fullmatch(r"(\d{4})[-\s]?Q([1-4])", q)
    if match:
        year = int(match.group(1))
        month, day = _QUARTER_ENDS[int(match.group(2))]
        return date(year, month, day).isoformat()
    try:
        return date.fromisoformat(q).isoformat()
    except ValueError as exc:
        raise ValueError("quarter must be YYYYQn, YYYY-Qn, or YYYY-MM-DD") from exc


def _norm_cik(cik: str | None) -> str:
    value = str(cik or "").strip()
    return value.zfill(10) if value.isdigit() else value


def _manager_type_label(manager_type: str | None) -> str | None:
    if manager_type is None or str(manager_type).strip() == "":
        return None
    value = str(manager_type).strip()
    if value in _MANAGER_SLUG_BY_CLASSIFICATION:
        return value
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    label = _MANAGER_CLASSIFICATION_BY_SLUG.get(slug)
    if label:
        return label
    raise ValueError(
        "manager_type must be one of: alternative, traditional, wealth_trust, "
        "capital_markets, insurance"
    )


def _manager_type_slug(primary_label: str | None) -> str | None:
    if not primary_label:
        return None
    return _MANAGER_SLUG_BY_CLASSIFICATION.get(primary_label)


def _relation_exists(name: str) -> bool:
    if name in _RELATION_EXISTS_CACHE:
        return _RELATION_EXISTS_CACHE[name]
    try:
        rows = fetchall_dict("SELECT to_regclass(%s) AS rel", (name,))
        exists = bool(rows and rows[0].get("rel"))
    except Exception:
        exists = False
    _RELATION_EXISTS_CACHE[name] = exists
    return exists


def _require_core_13f() -> dict[str, str] | None:
    required = ("core_13f_holding", "core_13f_manager", "core_13f_manager_period", "dim_13f_security_us")
    missing = [name for name in required if not _relation_exists(name)]
    if missing:
        return {"error": f"13F core tables unavailable: {', '.join(missing)}"}
    return None


def _issuer_scope(ticker: str) -> dict[str, Any]:
    t = str(ticker or "").strip().upper()
    if not t:
        return {"ticker": "", "cik": None, "cusips": []}
    cik_rows = fetchall_dict(
        """
        SELECT cik::text AS cik
        FROM dim_company_us
        WHERE UPPER(primary_ticker) = %s
        LIMIT 1
        """,
        (t,),
    )
    cik = _norm_cik(cik_rows[0]["cik"]) if cik_rows and cik_rows[0].get("cik") else None
    cusip_rows = fetchall_dict(
        """
        SELECT DISTINCT UPPER(cusip) AS cusip
        FROM dim_13f_security_us
        WHERE cusip IS NOT NULL
          AND (
              UPPER(primary_ticker) = %s
              OR (%s::text IS NOT NULL AND (issuer_cik = %s OR issuer_cik = LPAD(%s, 10, '0')))
          )
        """,
        (t, cik, cik, cik),
    )
    return {
        "ticker": t,
        "cik": cik,
        "cusips": [r["cusip"] for r in cusip_rows if r.get("cusip")],
    }


def _issuer_predicate(alias: str, scope: dict[str, Any]) -> str:
    if scope.get("cik"):
        return f"(UPPER({alias}.cusip) = ANY(%(cusips)s::text[]) OR {alias}.issuer_cik = %(issuer_cik)s)"
    return f"UPPER({alias}.cusip) = ANY(%(cusips)s::text[])"


def _latest_issuer_period(scope: dict[str, Any], quarter: str | None, *, shares_only: bool = True) -> str | None:
    requested = _parse_13f_quarter(quarter)
    if requested:
        return requested
    if not scope.get("cusips") and not scope.get("cik"):
        return None
    shares_sql = """
      AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
      AND COALESCE(h.put_call, '') = ''
    """ if shares_only else ""
    params = {"cusips": scope.get("cusips") or [], "issuer_cik": scope.get("cik")}
    rows = fetchall_dict(
        f"""
        SELECT MAX(h.report_period) AS report_period
        FROM core_13f_holding h
        WHERE {_issuer_predicate("h", scope)}
          AND h.is_latest_amendment
          {shares_sql}
        """,
        params,
    )
    value = rows[0]["report_period"] if rows else None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _latest_manager_period(manager_cik: str, quarter: str | None = None) -> str | None:
    requested = _parse_13f_quarter(quarter)
    if requested:
        return requested
    rows = fetchall_dict(
        """
        SELECT MAX(report_period) AS report_period
        FROM core_13f_manager_period
        WHERE manager_cik = %s
        """,
        (_norm_cik(manager_cik),),
    )
    value = rows[0]["report_period"] if rows else None
    return value.isoformat() if hasattr(value, "isoformat") else value


# ── Tool implementations ─────────────────────────────────────────────────────


def list_companies(name_query: str | None = None,
                   ticker_prefix: str | None = None,
                   jurisdiction: str | None = None,
                   sector: str | None = None,
                   limit: int = 50) -> dict:
    limit = max(1, min(int(limit), 50))
    where = ["1=1"]
    params: dict[str, Any] = {}
    if jurisdiction:
        where.append("jurisdiction = %(j)s")
        params["j"] = _norm_juris(jurisdiction)
    if name_query:
        where.append("name ILIKE %(n)s")
        params["n"] = f"%{name_query}%"
    if ticker_prefix:
        where.append("ticker ILIKE %(tp)s")
        params["tp"] = f"{ticker_prefix}%"
    if sector:
        where.append("(gics_sector_name ILIKE %(s)s OR mapping_sector ILIKE %(s)s)")
        params["s"] = f"%{sector}%"
    sql = f"""
        SELECT jurisdiction, ticker, name, exchange, gics_sector_name, mapping_sector, country_code
        FROM v_dim_company
        WHERE {' AND '.join(where)}
        ORDER BY ticker
        LIMIT {limit}
    """
    df = read_sql(sql, params=params)
    return _df_to_compact(df)


def get_company_overview(ticker: str) -> dict:
    df = read_sql("""
        SELECT jurisdiction, ticker, name, exchange, country_code,
               gics_sector_name, gics_industry_group_name, mapping_sector
        FROM v_dim_company
        WHERE ticker = %(t)s
        LIMIT 5
    """, params={"t": ticker})
    if df.empty:
        return {"ticker": ticker, "found": False}
    row = df.iloc[0].to_dict()
    juris = row.get("jurisdiction")
    tbl = "fact_metrics_us" if juris == "US" else "fact_metrics_jp"
    yrs = read_sql(f"""
        SELECT MIN(fiscal_year) AS min_fy, MAX(fiscal_year) AS max_fy
        FROM {tbl}
        WHERE ticker = %(t)s
    """, params={"t": ticker})
    if not yrs.empty:
        row["min_fiscal_year"] = int(yrs.iloc[0]["min_fy"]) if pd.notna(yrs.iloc[0]["min_fy"]) else None
        row["max_fiscal_year"] = int(yrs.iloc[0]["max_fy"]) if pd.notna(yrs.iloc[0]["max_fy"]) else None
    row["found"] = True
    return row


_STD_LINE_ITEMS = (
    "revenue", "gross_profit",
    "earnings_before_interest_taxes_depreciation_amortization",
    "earnings_before_interest_taxes",
    "net_income",
    "cash_flow_from_operations", "free_cash_flow",
    "capital_expenditures",
    "total_assets", "total_financial_debt", "net_debt",
    "total_equity", "cash_and_cash_equivalents",
)


def get_fundamentals(ticker: str,
                     start_year: int | None = None,
                     end_year: int | None = None,
                     line_items: list[str] | None = None) -> dict:
    juris = _juris_for_ticker(ticker)
    if not juris:
        return {"error": f"Ticker {ticker!r} not found."}
    tbl = "fact_fundamentals_std_us" if juris == "US" else "fact_fundamentals_std_jp"
    items = tuple(line_items) if line_items else _STD_LINE_ITEMS
    where = ["s.line_item_id IN %(items)s", "s.fiscal_period IN ('FY','Annual')"]
    params: dict[str, Any] = {"items": items}
    if juris == "US":
        where.append("dc.ticker = %(t)s")
        join = "JOIN v_dim_company dc ON dc.uid = s.cik AND dc.jurisdiction = 'US'"
    else:
        where.append("dc.ticker = %(t)s")
        join = "JOIN v_dim_company dc ON dc.edinet_code = s.edinet_code AND dc.jurisdiction = 'JP'"
    params["t"] = ticker
    if start_year:
        where.append("s.fiscal_year >= %(sy)s"); params["sy"] = int(start_year)
    if end_year:
        where.append("s.fiscal_year <= %(ey)s"); params["ey"] = int(end_year)
    sql = f"""
        SELECT s.fiscal_year, s.line_item_id, s.value
        FROM {tbl} s
        {join}
        WHERE {' AND '.join(where)}
        ORDER BY s.fiscal_year DESC, s.line_item_id
        LIMIT 600
    """
    df = read_sql(sql, params=params)
    if df.empty:
        return {"ticker": ticker, "jurisdiction": juris, "rows": [], "row_count": 0}
    pivot = df.pivot_table(index="fiscal_year", columns="line_item_id",
                            values="value", aggfunc="first").reset_index()
    pivot = pivot.sort_values("fiscal_year", ascending=False)
    out = _df_to_compact(pivot)
    out["ticker"] = ticker
    out["jurisdiction"] = juris
    out["currency"] = "USD" if juris == "US" else "JPY"
    return out


def get_raw_fundamentals(ticker: str,
                         fiscal_year: int | None = None,
                         fiscal_period: str | None = None,
                         statement_type: str | None = None,
                         as_of: str | None = None,
                         all_vintages: bool = False) -> dict:
    """Raw XBRL facts for a ticker.

    The US raw table is bitemporal (one row per filing vintage of each period;
    see migration 113). By default this returns the latest-known vintage per
    period (one row each). Pass as_of='YYYY-MM-DD' for the value as known on that
    date (point-in-time, no look-ahead), or all_vintages=True to return every
    filing's version with its filing_id/filed_date ("as first reported" vs
    restated). fiscal_year filters by the period-aligned year (from period_end),
    not the filing year.
    """
    juris = _juris_for_ticker(ticker)
    if not juris:
        return {"error": f"Ticker {ticker!r} not found."}
    # Period-aligned fiscal year: f.fiscal_year is the SEC companyfacts FILING
    # year, not the fact's period year, so a 10-K's comparatives are binned under
    # the filing's fy. Derive the real period year from period_end.
    _period_fy = ("CASE WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL "
                  "THEN EXTRACT(YEAR FROM f.period_end)::int ELSE f.fiscal_year END")
    if juris == "US":
        if all_vintages:
            vintage_from, distinct, distinct_order, asof_filter = "fact_fundamentals_us", "", "", ""
        elif as_of:
            vintage_from = "fact_fundamentals_us"
            distinct = "DISTINCT ON (f.cik, f.concept_id, f.period_end, f.fiscal_period)"
            distinct_order = ("ORDER BY f.cik, f.concept_id, f.period_end, f.fiscal_period, "
                              "f.filed_date DESC NULLS LAST, f.filing_id DESC")
            asof_filter = "AND f.filed_date IS NOT NULL AND f.filed_date <= %(asof)s::date"
        else:
            vintage_from, distinct, distinct_order, asof_filter = "v_fact_fundamentals_us_latest", "", "", ""
        sql = f"""
            WITH src AS (
                SELECT {distinct} f.concept_id,
                       {_period_fy} AS fiscal_year,
                       f.fiscal_period, f.period_end, f.value, f.unit,
                       f.statement_type, f.form, f.filing_id, f.filed_date, rcu.description
                FROM {vintage_from} f
                JOIN dim_company_us_test d ON d.cik = f.cik
                LEFT JOIN (
                    SELECT concept_id, mapping_sector, description FROM ref_concept_universe_corp
                    UNION ALL
                    SELECT concept_id, mapping_sector, description FROM ref_concept_universe_bank_financial
                    UNION ALL
                    SELECT concept_id, mapping_sector, description FROM ref_concept_universe_non_bank_financial
                ) rcu ON rcu.concept_id = f.concept_id AND rcu.mapping_sector = d.mapping_sector
                WHERE d.primary_ticker = %(t)s
                  AND (%(fy)s::int IS NULL OR ({_period_fy}) = %(fy)s::int)
                  AND (%(fp)s::text IS NULL OR f.fiscal_period = %(fp)s::text)
                  AND (%(st)s::text IS NULL OR f.statement_type = %(st)s::text)
                  {asof_filter}
                {distinct_order}
            )
            SELECT * FROM src
            ORDER BY period_end DESC NULLS LAST, fiscal_period, statement_type, concept_id
            LIMIT 200
        """
    else:
        sql = """
            SELECT f.concept_id, f.fiscal_year, f.fiscal_period, f.value, f.unit,
                   f.form, f.filed_date, rcu.description
            FROM fact_fundamentals_jp f
            JOIN dim_company_jp_test d ON d.edinet_code = f.edinet_code
            LEFT JOIN (
                SELECT concept_id, mapping_sector, description FROM ref_concept_universe_corp
                UNION ALL
                SELECT concept_id, mapping_sector, description FROM ref_concept_universe_bank_financial
                UNION ALL
                SELECT concept_id, mapping_sector, description FROM ref_concept_universe_non_bank_financial
            ) rcu ON rcu.concept_id = f.concept_id AND rcu.mapping_sector = d.mapping_sector
            WHERE d.primary_ticker = %(t)s
              AND (%(fy)s::int IS NULL OR f.fiscal_year = %(fy)s::int)
              AND (%(fp)s::text IS NULL OR f.fiscal_period = %(fp)s::text)
            ORDER BY f.fiscal_year DESC NULLS LAST, f.fiscal_period, f.concept_id
            LIMIT 200
        """
    df = read_sql(sql, params={"t": ticker, "fy": fiscal_year,
                                 "fp": fiscal_period, "st": statement_type,
                                 "asof": as_of})
    out = _df_to_compact(df)
    out["ticker"] = ticker
    out["jurisdiction"] = juris
    out["vintage"] = "all" if all_vintages else (f"as_of:{as_of}" if as_of else "latest")
    return out


_DEFAULT_METRIC_IDS = (
    "price_to_earnings_trailing",
    "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
    "enterprise_value_to_earnings_before_interest_taxes",
    "enterprise_value_to_revenue",
    "price_to_free_cash_flow",
    "price_to_book",
    "gross_margin",
    "earnings_before_interest_taxes_depreciation_amortization_margin",
    "operating_margin",
    "free_cash_flow_margin",
    "net_profit_margin",
    "return_on_invested_capital",
    "return_on_equity",
    "revenue_growth_year_over_year",
    "revenue_compound_annual_growth_rate_3_year",
    "earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year",
    "net_income_growth_year_over_year",
    "free_cash_flow_growth_year_over_year",
    "net_debt_to_earnings_before_interest_taxes_depreciation_amortization",
    "free_cash_flow_conversion",
)


def get_metrics(ticker: str,
                metric_ids: list[str] | None = None,
                start_year: int | None = None,
                end_year: int | None = None) -> dict:
    juris = _juris_for_ticker(ticker)
    if not juris:
        return {"error": f"Ticker {ticker!r} not found."}
    tbl = "fact_metrics_us" if juris == "US" else "fact_metrics_jp"
    ids = tuple(metric_ids) if metric_ids else _DEFAULT_METRIC_IDS
    where = ["ticker = %(t)s", "metric_id IN %(ids)s", "value IS NOT NULL",
             "fiscal_period IN ('FY','Annual')"]
    params: dict[str, Any] = {"t": ticker, "ids": ids}
    if start_year:
        where.append("fiscal_year >= %(sy)s"); params["sy"] = int(start_year)
    if end_year:
        where.append("fiscal_year <= %(ey)s"); params["ey"] = int(end_year)
    sql = f"""
        SELECT fiscal_year, metric_id, value
        FROM {tbl}
        WHERE {' AND '.join(where)}
        ORDER BY fiscal_year DESC, metric_id
        LIMIT 800
    """
    df = read_sql(sql, params=params)
    if df.empty:
        return {"ticker": ticker, "jurisdiction": juris, "rows": [], "row_count": 0}
    pivot = (df.pivot_table(index="fiscal_year", columns="metric_id",
                            values="value", aggfunc="first")
               .reset_index().sort_values("fiscal_year", ascending=False))
    out = _df_to_compact(pivot)
    out["ticker"] = ticker
    out["jurisdiction"] = juris
    return out


def compare_metrics(tickers: list[str],
                    metric_id: str,
                    start_year: int | None = None,
                    end_year: int | None = None) -> dict:
    if not tickers:
        return {"error": "Provide at least one ticker."}
    rows: list[dict] = []
    for t in tickers[:10]:
        juris = _juris_for_ticker(t)
        if not juris:
            continue
        tbl = "fact_metrics_us" if juris == "US" else "fact_metrics_jp"
        where = ["ticker = %(t)s", "metric_id = %(m)s", "value IS NOT NULL",
                 "fiscal_period IN ('FY','Annual')"]
        params: dict[str, Any] = {"t": t, "m": metric_id}
        if start_year:
            where.append("fiscal_year >= %(sy)s"); params["sy"] = int(start_year)
        if end_year:
            where.append("fiscal_year <= %(ey)s"); params["ey"] = int(end_year)
        df = read_sql(f"""
            SELECT fiscal_year, value FROM {tbl}
            WHERE {' AND '.join(where)}
            ORDER BY fiscal_year DESC
            LIMIT 30
        """, params=params)
        for _, r in df.iterrows():
            rows.append({"ticker": t, "jurisdiction": juris,
                          "fiscal_year": int(r["fiscal_year"]),
                          "value": float(r["value"]) if pd.notna(r["value"]) else None})
    return {"metric_id": metric_id, "rows": rows, "row_count": len(rows)}


def get_prices(ticker: str,
               start_date: str | None = None,
               end_date: str | None = None,
               resample: str | None = None) -> dict:
    juris = _juris_for_ticker(ticker)
    if not juris:
        return {"error": f"Ticker {ticker!r} not found."}
    tbl = "fact_prices_us" if juris == "US" else "fact_prices_jp"
    where = ["ticker = %(t)s"]
    params: dict[str, Any] = {"t": ticker}
    if start_date:
        where.append("date::date >= %(sd)s::date"); params["sd"] = start_date
    if end_date:
        where.append("date::date <= %(ed)s::date"); params["ed"] = end_date
    df = read_sql(f"""
        SELECT date::date AS date, close
        FROM {tbl}
        WHERE {' AND '.join(where)} AND close IS NOT NULL
        ORDER BY date DESC
        LIMIT 4000
    """, params=params)
    if df.empty:
        return {"ticker": ticker, "jurisdiction": juris, "rows": [], "row_count": 0}
    df = df.sort_values("date")
    df["date"] = pd.to_datetime(df["date"])
    if resample:
        rule = {"D": "D", "W": "W-FRI", "M": "ME", "Q": "QE", "Y": "YE"}.get(resample.upper())
        if rule:
            df = df.set_index("date").resample(rule).last().dropna().reset_index()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df = df.tail(MAX_ROWS).reset_index(drop=True)
    return {"ticker": ticker, "jurisdiction": juris,
            "columns": ["date", "close"],
            "rows": df[["date", "close"]].values.tolist(),
            "row_count": len(df)}


def get_filings(ticker: str, limit: int = 30) -> dict:
    juris = _juris_for_ticker(ticker)
    if not juris:
        return {"error": f"Ticker {ticker!r} not found."}
    if juris == "US":
        df = read_sql("""
            SELECT DISTINCT f.accession, f.form, f.filed_date,
                   MAX(f.fiscal_year) AS fiscal_year
            FROM fact_fundamentals_us f
            JOIN dim_company_us_test d ON d.cik = f.cik
            WHERE d.primary_ticker = %(t)s
            GROUP BY f.accession, f.form, f.filed_date
            ORDER BY MAX(f.fiscal_year) DESC NULLS LAST, f.filed_date DESC
            LIMIT %(lim)s
        """, params={"t": ticker, "lim": min(int(limit), 60)})
    else:
        df = read_sql("""
            SELECT DISTINCT f.form, f.filed_date,
                   MAX(f.fiscal_year) AS fiscal_year
            FROM fact_fundamentals_jp f
            JOIN dim_company_jp_test d ON d.edinet_code = f.edinet_code
            WHERE d.primary_ticker = %(t)s
            GROUP BY f.form, f.filed_date
            ORDER BY MAX(f.fiscal_year) DESC NULLS LAST, f.filed_date DESC
            LIMIT %(lim)s
        """, params={"t": ticker, "lim": min(int(limit), 60)})
    return _df_to_compact(df)


def rank_universe(metric_id: str,
                  fiscal_year: int,
                  jurisdiction: str | None = None,
                  ascending: bool = False,
                  limit: int = 25,
                  sector: str | None = None) -> dict:
    j = _norm_juris(jurisdiction) if jurisdiction else None
    parts: list[str] = []
    base_params: dict[str, Any] = {"m": metric_id, "fy": int(fiscal_year)}
    if j != "JP":
        sec_filter = "AND dc.gics_sector_name ILIKE %(s)s" if sector else ""
        parts.append(f"""
            SELECT fm.ticker, dc.name, 'US' AS jurisdiction, dc.gics_sector_name AS sector,
                   fm.value
            FROM fact_metrics_us fm
            JOIN v_dim_company dc ON dc.ticker = fm.ticker AND dc.jurisdiction = 'US'
            WHERE fm.metric_id = %(m)s AND fm.fiscal_year = %(fy)s
              AND fm.value IS NOT NULL
              AND fm.fiscal_period IN ('FY','Annual')
              {sec_filter}
        """)
    if j != "US":
        sec_filter = "AND dc.gics_sector_name ILIKE %(s)s" if sector else ""
        parts.append(f"""
            SELECT fm.ticker, dc.name, 'JP' AS jurisdiction, dc.gics_sector_name AS sector,
                   fm.value
            FROM fact_metrics_jp fm
            JOIN v_dim_company dc ON dc.ticker = fm.ticker AND dc.jurisdiction = 'JP'
            WHERE fm.metric_id = %(m)s AND fm.fiscal_year = %(fy)s
              AND fm.value IS NOT NULL
              AND fm.fiscal_period IN ('FY','Annual')
              {sec_filter}
        """)
    if sector:
        base_params["s"] = f"%{sector}%"
    union_sql = " UNION ALL ".join(parts)
    order = "ASC" if ascending else "DESC"
    sql = f"""
        WITH ranked AS ( {union_sql} )
        SELECT * FROM ranked
        ORDER BY value {order} NULLS LAST
        LIMIT {max(1, min(int(limit), 100))}
    """
    df = read_sql(sql, params=base_params)
    return _df_to_compact(df)


def get_institutional_holders(ticker: str,
                              quarter: str | None = None,
                              manager_type: str | None = None) -> dict:
    """Top institutional holders for a US issuer, ranked by shares held."""
    core_error = _require_core_13f()
    if core_error:
        return core_error
    scope = _issuer_scope(ticker)
    if not scope["ticker"]:
        return {"error": "ticker is required"}
    if not scope["cusips"] and not scope["cik"]:
        return {"ticker": scope["ticker"], "quarter": None, "rows": [], "row_count": 0}
    period = _latest_issuer_period(scope, quarter, shares_only=True)
    if not period:
        return {"ticker": scope["ticker"], "quarter": None, "rows": [], "row_count": 0}

    manager_type_label = _manager_type_label(manager_type)
    params: dict[str, Any] = {
        "ticker": scope["ticker"],
        "cusips": scope["cusips"],
        "issuer_cik": scope["cik"],
        "period": period,
        "manager_type_label": manager_type_label,
    }
    issuer_pred = _issuer_predicate("h", scope)
    df = read_sql(
        f"""
        WITH current AS (
            SELECT h.manager_cik,
                   COALESCE(m.legal_name, dm.manager_name, h.manager_cik) AS manager_name,
                   MAX(cls.primary_label) AS classification_label,
                   SUM(h.shares_or_principal)::numeric AS shares_held,
                   SUM(COALESCE(h.market_value_usd, h.value_reported, 0))::numeric AS market_value_usd,
                   MAX(COALESCE(mp.long_market_value, mp.portfolio_value_market))::numeric AS portfolio_value_usd
            FROM core_13f_holding h
            LEFT JOIN core_13f_manager m ON m.manager_cik = h.manager_cik
            LEFT JOIN dim_13f_manager dm ON dm.manager_cik = h.manager_cik
            LEFT JOIN core_13f_manager_period mp
              ON mp.manager_cik = h.manager_cik
             AND mp.report_period = h.report_period
            LEFT JOIN core_13f_manager_classification cls
              ON cls.manager_cik = h.manager_cik
             AND cls.report_period = h.report_period
            WHERE {issuer_pred}
              AND h.report_period = %(period)s::date
              AND h.is_latest_amendment
              AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
              AND COALESCE(h.put_call, '') = ''
              AND (%(manager_type_label)s::text IS NULL OR cls.primary_label = %(manager_type_label)s)
            GROUP BY h.manager_cik, COALESCE(m.legal_name, dm.manager_name, h.manager_cik)
        ),
        prev_period AS (
            SELECT MAX(h.report_period) AS report_period
            FROM core_13f_holding h
            WHERE {issuer_pred}
              AND h.report_period < %(period)s::date
              AND h.is_latest_amendment
              AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
              AND COALESCE(h.put_call, '') = ''
        ),
        split_factor AS (
            SELECT COALESCE(EXP(SUM(LN(split_ratio::numeric))), 1)::numeric AS factor
            FROM fact_stock_split_event s
            CROSS JOIN prev_period p
            WHERE p.report_period IS NOT NULL
              AND s.jurisdiction = 'US'
              AND UPPER(s.ticker) = %(ticker)s
              AND s.effective_date > p.report_period
              AND s.effective_date <= %(period)s::date
              AND s.split_ratio > 0
        ),
        prev AS (
            SELECT h.manager_cik,
                   SUM(h.shares_or_principal * (SELECT factor FROM split_factor))::numeric AS prev_shares
            FROM core_13f_holding h
            CROSS JOIN prev_period p
            WHERE {issuer_pred}
              AND p.report_period IS NOT NULL
              AND h.report_period = p.report_period
              AND h.is_latest_amendment
              AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
              AND COALESCE(h.put_call, '') = ''
            GROUP BY h.manager_cik
        )
        SELECT current.manager_name,
               current.manager_cik,
               current.classification_label,
               current.shares_held,
               current.market_value_usd,
               CASE WHEN current.portfolio_value_usd > 0
                    THEN current.market_value_usd / current.portfolio_value_usd * 100.0
                    ELSE NULL END AS weight_pct,
               current.shares_held - COALESCE(prev.prev_shares, 0) AS shares_changed,
               %(period)s::date AS as_of_date
        FROM current
        LEFT JOIN prev ON prev.manager_cik = current.manager_cik
        ORDER BY current.shares_held DESC NULLS LAST
        LIMIT 50
        """,
        params=params,
    )
    rows = _df_to_records(df, max_rows=50)
    for row in rows:
        row["manager_type"] = _manager_type_slug(row.get("classification_label"))
    return {
        "ticker": scope["ticker"],
        "quarter": period,
        "manager_type": manager_type,
        "rows": rows,
        "row_count": len(rows),
    }


def get_manager_portfolio(manager_cik: str | None = None, quarter: str | None = None) -> dict:
    """Full 13F portfolio for one manager, ordered by market value."""
    core_error = _require_core_13f()
    if core_error:
        return core_error
    cik = _norm_cik(manager_cik)
    if not cik:
        return {"error": "manager_cik is required"}
    period = _latest_manager_period(cik, quarter)
    if not period:
        return {"manager_cik": cik, "quarter": None, "rows": [], "row_count": 0}

    previous_rows = fetchall_dict(
        """
        SELECT MAX(report_period) AS report_period
        FROM core_13f_holding
        WHERE manager_cik = %s
          AND report_period < %s::date
          AND is_latest_amendment
        """,
        (cik, period),
    )
    previous_period = previous_rows[0]["report_period"] if previous_rows else None
    previous_period = previous_period.isoformat() if hasattr(previous_period, "isoformat") else previous_period

    df = read_sql(
        """
        WITH current_raw AS (
            SELECT CASE
                       WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> ''
                           THEN 'derivative:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                          UPPER(h.cusip), h.issuer_name, '') || ':'
                                               || COALESCE(h.put_call, '') || ':' || COALESCE(UPPER(h.cusip), '')
                       ELSE 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                   NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                   UPPER(h.cusip), h.issuer_name, '')
                   END AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   COALESCE(d.name, sec.issuer_name, h.issuer_name) AS issuer_name,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS market_value_usd,
                   h.shares_or_principal,
                   CASE
                       WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> '' THEN 'derivatives'
                       WHEN h.asset_bucket IS NOT NULL AND h.asset_bucket <> 'other' THEN h.asset_bucket
                       ELSE COALESCE(sec.asset_bucket, h.asset_bucket, 'other')
                   END AS security_type
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = UPPER(h.cusip)
            LEFT JOIN dim_company_us d ON d.cik = COALESCE(sec.issuer_cik, h.issuer_cik)
            WHERE h.manager_cik = %(manager_cik)s
              AND h.report_period = %(period)s::date
              AND h.is_latest_amendment
        ),
        prev_raw AS (
            SELECT CASE
                       WHEN h.asset_bucket = 'derivatives' OR COALESCE(h.put_call, '') <> ''
                           THEN 'derivative:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                          NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                          UPPER(h.cusip), h.issuer_name, '') || ':'
                                               || COALESCE(h.put_call, '') || ':' || COALESCE(UPPER(h.cusip), '')
                       ELSE 'holding:' || COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, ''),
                                                   NULLIF(sec.issuer_cik, ''), NULLIF(h.issuer_cik, ''),
                                                   UPPER(h.cusip), h.issuer_name, '')
                   END AS display_key,
                   COALESCE(NULLIF(sec.primary_ticker, ''), NULLIF(h.issuer_ticker, '')) AS issuer_ticker,
                   h.shares_or_principal
            FROM core_13f_holding h
            LEFT JOIN dim_13f_security_us sec ON sec.cusip = UPPER(h.cusip)
            WHERE h.manager_cik = %(manager_cik)s
              AND %(previous_period)s::date IS NOT NULL
              AND h.report_period = %(previous_period)s::date
              AND h.is_latest_amendment
        ),
        split_factors AS (
            SELECT UPPER(ticker) AS ticker,
                   EXP(SUM(LN(split_ratio::numeric)))::numeric AS split_factor
            FROM fact_stock_split_event
            WHERE jurisdiction = 'US'
              AND %(previous_period)s::date IS NOT NULL
              AND effective_date > %(previous_period)s::date
              AND effective_date <= %(period)s::date
              AND split_ratio > 0
            GROUP BY UPPER(ticker)
        ),
        prev AS (
            SELECT p.display_key,
                   SUM(p.shares_or_principal * COALESCE(sf.split_factor, 1))::numeric AS prev_shares
            FROM prev_raw p
            LEFT JOIN split_factors sf ON sf.ticker = UPPER(p.issuer_ticker)
            GROUP BY p.display_key
        ),
        grouped AS (
            SELECT c.display_key,
                   MAX(c.issuer_ticker) AS issuer_ticker,
                   MAX(c.issuer_name) AS issuer_name,
                   SUM(c.shares_or_principal)::numeric AS shares_held,
                   SUM(c.market_value_usd)::numeric AS market_value_usd,
                   MAX(c.security_type) AS security_type,
                   MAX(p.prev_shares) AS prev_shares
            FROM current_raw c
            LEFT JOIN prev p ON p.display_key = c.display_key
            GROUP BY c.display_key
        ),
        totals AS (
            SELECT SUM(market_value_usd) AS total_value
            FROM grouped
            WHERE security_type <> 'derivatives'
        )
        SELECT issuer_ticker,
               issuer_name,
               shares_held,
               market_value_usd,
               CASE WHEN totals.total_value > 0
                    THEN grouped.market_value_usd / totals.total_value * 100.0
                    ELSE NULL END AS weight_pct,
               shares_held - COALESCE(prev_shares, 0) AS shares_changed,
               security_type
        FROM grouped
        CROSS JOIN totals
        ORDER BY market_value_usd DESC NULLS LAST
        LIMIT 200
        """,
        params={"manager_cik": cik, "period": period, "previous_period": previous_period},
    )
    rows = _df_to_records(df, max_rows=200)
    class_rows = fetchall_dict(
        """
        SELECT primary_label, confidence_score, route_tier
        FROM core_13f_manager_classification
        WHERE manager_cik = %s
          AND report_period = %s::date
        LIMIT 1
        """,
        (cik, period),
    ) if _relation_exists("core_13f_manager_classification") else []
    classification = class_rows[0] if class_rows else {}
    return {
        "manager_cik": cik,
        "quarter": period,
        "previous_quarter": previous_period,
        "manager_type": _manager_type_slug(classification.get("primary_label")),
        "classification_label": classification.get("primary_label"),
        "classification_confidence": _clean_json_value(classification.get("confidence_score")),
        "classification_route": classification.get("route_tier"),
        "rows": rows,
        "row_count": len(rows),
    }


def compare_13f_ownership(tickers: list[str],
                          quarter: str | None = None,
                          manager_type: str | None = None) -> dict:
    """Cross-ticker comparison of institutional ownership metrics."""
    core_error = _require_core_13f()
    if core_error:
        return core_error
    if not tickers:
        return {"error": "Provide at least one ticker."}
    manager_type_label = _manager_type_label(manager_type)
    rows: list[dict[str, Any]] = []
    for raw_ticker in [str(t).strip().upper() for t in tickers if str(t).strip()][:10]:
        scope = _issuer_scope(raw_ticker)
        if not scope["cusips"] and not scope["cik"]:
            rows.append({
                "ticker": raw_ticker,
                "total_holders": 0,
                "total_market_value_usd": 0,
                "avg_weight_pct": None,
                "total_shares_changed": None,
                "as_of_date": None,
            })
            continue
        period = _latest_issuer_period(scope, quarter, shares_only=True)
        if not period:
            continue
        params = {
            "ticker": scope["ticker"],
            "cusips": scope["cusips"],
            "issuer_cik": scope["cik"],
            "period": period,
            "manager_type_label": manager_type_label,
        }
        issuer_pred = _issuer_predicate("h", scope)
        df = read_sql(
            f"""
            WITH current AS (
                SELECT h.manager_cik,
                       SUM(h.shares_or_principal)::numeric AS shares_held,
                       SUM(COALESCE(h.market_value_usd, h.value_reported, 0))::numeric AS market_value_usd,
                       MAX(COALESCE(mp.long_market_value, mp.portfolio_value_market))::numeric AS portfolio_value_usd
                FROM core_13f_holding h
                LEFT JOIN core_13f_manager_period mp
                  ON mp.manager_cik = h.manager_cik
                 AND mp.report_period = h.report_period
                LEFT JOIN core_13f_manager_classification cls
                  ON cls.manager_cik = h.manager_cik
                 AND cls.report_period = %(period)s::date
                WHERE {issuer_pred}
                  AND h.report_period = %(period)s::date
                  AND h.is_latest_amendment
                  AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND COALESCE(h.put_call, '') = ''
                  AND (%(manager_type_label)s::text IS NULL OR cls.primary_label = %(manager_type_label)s)
                GROUP BY h.manager_cik
            ),
            prev_period AS (
                SELECT MAX(h.report_period) AS report_period
                FROM core_13f_holding h
                WHERE {issuer_pred}
                  AND h.report_period < %(period)s::date
                  AND h.is_latest_amendment
                  AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND COALESCE(h.put_call, '') = ''
            ),
            split_factor AS (
                SELECT COALESCE(EXP(SUM(LN(split_ratio::numeric))), 1)::numeric AS factor
                FROM fact_stock_split_event s
                CROSS JOIN prev_period p
                WHERE p.report_period IS NOT NULL
                  AND s.jurisdiction = 'US'
                  AND UPPER(s.ticker) = %(ticker)s
                  AND s.effective_date > p.report_period
                  AND s.effective_date <= %(period)s::date
                  AND s.split_ratio > 0
            ),
            prev AS (
                SELECT h.manager_cik,
                       SUM(h.shares_or_principal * (SELECT factor FROM split_factor))::numeric AS prev_shares
                FROM core_13f_holding h
                CROSS JOIN prev_period p
                LEFT JOIN core_13f_manager_classification cls
                  ON cls.manager_cik = h.manager_cik
                 AND cls.report_period = %(period)s::date
                WHERE {issuer_pred}
                  AND p.report_period IS NOT NULL
                  AND h.report_period = p.report_period
                  AND h.is_latest_amendment
                  AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                  AND COALESCE(h.put_call, '') = ''
                  AND (%(manager_type_label)s::text IS NULL OR cls.primary_label = %(manager_type_label)s)
                GROUP BY h.manager_cik
            ),
            joined AS (
                SELECT COALESCE(c.manager_cik, p.manager_cik) AS manager_cik,
                       c.market_value_usd,
                       c.portfolio_value_usd,
                       COALESCE(c.shares_held, 0) - COALESCE(p.prev_shares, 0) AS shares_changed
                FROM current c
                FULL OUTER JOIN prev p ON p.manager_cik = c.manager_cik
            )
            SELECT %(ticker)s AS ticker,
                   COUNT(*) FILTER (WHERE market_value_usd IS NOT NULL) AS total_holders,
                   COALESCE(SUM(market_value_usd), 0) AS total_market_value_usd,
                   AVG(CASE WHEN portfolio_value_usd > 0 THEN market_value_usd / portfolio_value_usd * 100.0 END) AS avg_weight_pct,
                   SUM(shares_changed) AS total_shares_changed,
                   %(period)s::date AS as_of_date
            FROM joined
            """,
            params=params,
        )
        rows.extend(_df_to_records(df, max_rows=1))
    return {
        "quarter": _parse_13f_quarter(quarter),
        "manager_type": manager_type,
        "rows": rows,
        "row_count": len(rows),
    }


def rank_institutional_activity(quarter: str,
                                direction: str = "buy",
                                sector: str | None = None,
                                manager_type: str | None = None,
                                pe_min: float | None = None,
                                pe_max: float | None = None,
                                performance_months: int | None = None,
                                limit: int = 25) -> dict:
    """Rank net buys, net sells, or most widely held tickers for a 13F quarter."""
    core_error = _require_core_13f()
    if core_error:
        return core_error
    period = _parse_13f_quarter(quarter)
    if not period:
        return {"error": "quarter is required"}
    direction_norm = str(direction or "buy").strip().lower()
    if direction_norm not in {"buy", "sell", "top_held"}:
        return {"error": "direction must be one of: buy, sell, top_held"}
    limit = max(1, min(int(limit), 100))
    manager_type_label = _manager_type_label(manager_type)
    perf_months = max(1, min(int(performance_months), 60)) if performance_months else None
    manager_join = "JOIN" if manager_type_label else "LEFT JOIN"
    order_sql = {
        "buy": "total_shares_changed DESC NULLS LAST, total_market_value_usd DESC NULLS LAST",
        "sell": "total_shares_changed ASC NULLS LAST, total_market_value_usd DESC NULLS LAST",
        "top_held": "holder_count DESC NULLS LAST, total_market_value_usd DESC NULLS LAST",
    }[direction_norm]

    df = read_sql(
        f"""
        WITH prev_period AS (
            SELECT MAX(report_period) AS report_period
            FROM core_13f_holding
            WHERE report_period < %(period)s::date
              AND is_latest_amendment
        ),
        split_factors AS (
            SELECT UPPER(ticker) AS ticker,
                   EXP(SUM(LN(split_ratio::numeric)))::numeric AS split_factor
            FROM fact_stock_split_event
            CROSS JOIN prev_period p
            WHERE p.report_period IS NOT NULL
              AND jurisdiction = 'US'
              AND effective_date > p.report_period
              AND effective_date <= %(period)s::date
              AND split_ratio > 0
            GROUP BY UPPER(ticker)
        ),
        latest_pe AS MATERIALIZED (
            SELECT DISTINCT ON (ticker)
                   ticker,
                   value::numeric AS pe_ratio,
                   fiscal_year AS pe_fiscal_year,
                   period_end AS pe_period_end
            FROM fact_metrics_us
            WHERE metric_id = 'price_to_earnings_trailing'
              AND value IS NOT NULL
            ORDER BY ticker, period_end DESC NULLS LAST, fiscal_year DESC
        ),
        eligible_pe AS MATERIALIZED (
            SELECT ticker
            FROM latest_pe
            WHERE (%(pe_min)s::numeric IS NULL OR pe_ratio >= %(pe_min)s::numeric)
              AND (%(pe_max)s::numeric IS NULL OR pe_ratio <= %(pe_max)s::numeric)
        ),
        eligible_security AS MATERIALIZED (
            SELECT UPPER(cusip) AS cusip,
                   primary_ticker AS ticker,
                   issuer_name,
                   sector
            FROM dim_13f_security_us
            WHERE cusip IS NOT NULL
              AND primary_ticker IS NOT NULL
              AND primary_ticker <> ''
              AND (
                    (%(pe_min)s::numeric IS NULL AND %(pe_max)s::numeric IS NULL)
                 OR primary_ticker IN (SELECT ticker FROM eligible_pe)
              )
        ),
        eligible_manager AS MATERIALIZED (
            SELECT manager_cik, primary_label
            FROM core_13f_manager_classification
            WHERE report_period = %(period)s::date
              AND (%(manager_type_label)s::text IS NULL OR primary_label = %(manager_type_label)s)
        ),
        current_agg AS (
            SELECT sec.ticker AS ticker,
                   MAX(COALESCE(d.name, sec.issuer_name, h.issuer_name)) AS issuer_name,
                   MAX(COALESCE(d.gics_sector_name, sec.sector)) AS sector,
                   MAX(em.primary_label) AS classification_label,
                   SUM(h.shares_or_principal)::numeric AS shares_held,
                   SUM(COALESCE(h.market_value_usd, h.value_reported, 0))::numeric AS market_value_usd,
                   COUNT(DISTINCT h.manager_cik) AS holder_count
            FROM eligible_security sec
            JOIN core_13f_holding h ON UPPER(h.cusip) = sec.cusip
            {manager_join} eligible_manager em ON em.manager_cik = h.manager_cik
            LEFT JOIN dim_company_us d ON d.primary_ticker = sec.ticker
            WHERE h.report_period = %(period)s::date
              AND h.is_latest_amendment
              AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
              AND COALESCE(h.put_call, '') = ''
              AND (%(sector)s::text IS NULL OR COALESCE(d.gics_sector_name, sec.sector) ILIKE %(sector_like)s)
            GROUP BY sec.ticker
        ),
        prev_agg AS (
            SELECT sec.ticker AS ticker,
                   MAX(COALESCE(d.name, sec.issuer_name, h.issuer_name)) AS issuer_name,
                   MAX(COALESCE(d.gics_sector_name, sec.sector)) AS sector,
                   MAX(em.primary_label) AS classification_label,
                   SUM(h.shares_or_principal * COALESCE(sf.split_factor, 1))::numeric AS shares_held
            FROM eligible_security sec
            JOIN core_13f_holding h ON UPPER(h.cusip) = sec.cusip
            CROSS JOIN prev_period p
            {manager_join} eligible_manager em ON em.manager_cik = h.manager_cik
            LEFT JOIN dim_company_us d ON d.primary_ticker = sec.ticker
            LEFT JOIN split_factors sf ON sf.ticker = UPPER(sec.ticker)
            WHERE p.report_period IS NOT NULL
              AND h.report_period = p.report_period
              AND h.is_latest_amendment
              AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
              AND COALESCE(h.put_call, '') = ''
              AND (%(sector)s::text IS NULL OR COALESCE(d.gics_sector_name, sec.sector) ILIKE %(sector_like)s)
            GROUP BY sec.ticker
        ),
        aggregated AS (
            SELECT COALESCE(c.ticker, p.ticker) AS ticker,
                   COALESCE(c.issuer_name, p.issuer_name) AS issuer_name,
                   COALESCE(c.shares_held, 0) - COALESCE(p.shares_held, 0) AS total_shares_changed,
                   COALESCE(c.market_value_usd, 0) AS total_market_value_usd,
                   COALESCE(c.holder_count, 0) AS holder_count,
                   COALESCE(c.sector, p.sector) AS sector,
                   COALESCE(c.classification_label, p.classification_label) AS classification_label
            FROM current_agg c
            FULL OUTER JOIN prev_agg p ON p.ticker = c.ticker
            WHERE COALESCE(c.ticker, p.ticker) IS NOT NULL
        ),
        ranked AS (
            SELECT ROW_NUMBER() OVER (ORDER BY {order_sql}) AS rank,
                   aggregated.ticker,
                   issuer_name,
                   total_shares_changed,
                   total_market_value_usd,
                   holder_count,
                   sector,
                   classification_label,
                   pe.pe_ratio,
                   pe.pe_fiscal_year,
                   pe.pe_period_end
            FROM aggregated
            LEFT JOIN latest_pe pe ON pe.ticker = aggregated.ticker
            WHERE (%(pe_min)s::numeric IS NULL OR pe.pe_ratio >= %(pe_min)s::numeric)
              AND (%(pe_max)s::numeric IS NULL OR pe.pe_ratio <= %(pe_max)s::numeric)
        ),
        limited AS (
            SELECT *
            FROM ranked
            ORDER BY rank
            LIMIT %(limit)s
        )
        SELECT limited.*,
               latest_price.date AS performance_end_date,
               start_price.date AS performance_start_date,
               CASE WHEN %(performance_months)s::int IS NOT NULL AND start_price.close > 0
                    THEN (latest_price.close / start_price.close - 1.0) * 100.0
                    ELSE NULL END AS stock_performance_pct
        FROM limited
        LEFT JOIN LATERAL (
            SELECT date::date AS date, close::numeric AS close
            FROM fact_prices_us p
            WHERE p.ticker = limited.ticker AND p.close IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
        ) latest_price ON %(performance_months)s::int IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT date::date AS date, close::numeric AS close
            FROM fact_prices_us p
            WHERE p.ticker = limited.ticker
              AND p.close IS NOT NULL
              AND latest_price.date IS NOT NULL
              AND p.date <= latest_price.date - (%(performance_months)s::text || ' months')::interval
            ORDER BY date DESC
            LIMIT 1
        ) start_price ON %(performance_months)s::int IS NOT NULL
        ORDER BY rank
        """,
        params={
            "period": period,
            "sector": sector,
            "sector_like": f"%{sector}%" if sector else None,
            "manager_type_label": manager_type_label,
            "pe_min": pe_min,
            "pe_max": pe_max,
            "performance_months": perf_months,
            "limit": limit,
        },
    )
    rows = _df_to_records(df, max_rows=limit)
    for row in rows:
        row["manager_type"] = _manager_type_slug(row.get("classification_label"))
    return {
        "quarter": period,
        "direction": direction_norm,
        "sector": sector,
        "manager_type": manager_type,
        "pe_min": pe_min,
        "pe_max": pe_max,
        "performance_months": perf_months,
        "rows": rows,
        "row_count": len(rows),
    }


def search_13f_managers(manager_type: str | None = None,
                        name_query: str | None = None,
                        quarter: str | None = None,
                        limit: int = 25) -> dict:
    """Search 13F managers by Portfolio Analytics classification."""
    core_error = _require_core_13f()
    if core_error:
        return core_error
    if not _relation_exists("core_13f_manager_classification"):
        return {"error": "core_13f_manager_classification is unavailable"}
    manager_type_label = _manager_type_label(manager_type)
    period = _parse_13f_quarter(quarter)
    if not period:
        rows = fetchall_dict(
            "SELECT MAX(report_period) AS report_period FROM core_13f_manager_classification"
        )
        value = rows[0]["report_period"] if rows else None
        period = value.isoformat() if hasattr(value, "isoformat") else value
    if not period:
        return {"quarter": None, "rows": [], "row_count": 0}
    limit = max(1, min(int(limit), 100))
    name_like = f"%{name_query}%" if name_query else None
    df = read_sql(
        """
        SELECT c.manager_cik,
               m.legal_name AS manager_name,
               c.primary_label AS classification_label,
               c.confidence_score,
               c.route_tier,
               COALESCE(p.long_market_value, p.portfolio_value_market) AS portfolio_value_usd,
               p.position_count,
               p.report_period AS as_of_date
        FROM core_13f_manager_classification c
        JOIN core_13f_manager m ON m.manager_cik = c.manager_cik
        LEFT JOIN core_13f_manager_period p
          ON p.manager_cik = c.manager_cik
         AND p.report_period = c.report_period
        WHERE c.report_period = %(period)s::date
          AND (%(manager_type_label)s::text IS NULL OR c.primary_label = %(manager_type_label)s)
          AND (%(name_like)s::text IS NULL OR m.legal_name ILIKE %(name_like)s OR c.manager_cik LIKE %(name_like)s)
        ORDER BY COALESCE(p.long_market_value, p.portfolio_value_market) DESC NULLS LAST,
                 m.legal_name
        LIMIT %(limit)s
        """,
        params={
            "period": period,
            "manager_type_label": manager_type_label,
            "name_like": name_like,
            "limit": limit,
        },
    )
    rows = _df_to_records(df, max_rows=limit)
    for row in rows:
        row["manager_type"] = _manager_type_slug(row.get("classification_label"))
    return {
        "quarter": period,
        "manager_type": manager_type,
        "name_query": name_query,
        "rows": rows,
        "row_count": len(rows),
    }


def get_modeled_statement_snapshot(ticker: str, years: int = 5) -> dict:
    return services.modeled_statement_snapshot(ticker, years=years)


def get_market_metrics(ticker: str) -> dict:
    return services.market_metrics(ticker)


def get_peer_group(ticker: str, limit: int = 10) -> dict:
    return services.peer_group(ticker, limit=limit)


def get_factor_exposure(ticker: str) -> dict:
    return services.factor_exposure(ticker)


def get_recon_flags(ticker: str) -> dict:
    return services.recon_flags(ticker)


def _clean_isin(value: str | None) -> str:
    return str(value or "").strip().upper()


def _resolve_etf_isin(identifier: str | None) -> str:
    value = _clean_isin(identifier)
    if not value:
        return ""
    rows = fetchall_dict(
        """
        SELECT d.isin
        FROM dim_etf d
        LEFT JOIN dim_etf_profile p ON p.isin = d.isin
        WHERE UPPER(d.isin) = %s
           OR UPPER(COALESCE(p.yf_ticker, '')) = %s
           OR EXISTS (
                SELECT 1 FROM dim_etf_listing l
                WHERE l.isin = d.isin AND UPPER(COALESCE(l.exchange_ticker, '')) = %s
           )
        ORDER BY CASE
            WHEN UPPER(d.isin) = %s THEN 0
            WHEN UPPER(COALESCE(p.yf_ticker, '')) = %s THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        (value, value, value, value, value),
    )
    return str(rows[0]["isin"]) if rows else value


def search_etfs(query: str | None = None,
                asset_class: str | None = None,
                limit: int = 12) -> dict:
    """Search ETF warehouse records by name, ISIN, ticker, index, or asset class."""
    q = str(query or "").strip()
    limit = max(1, min(int(limit or 12), 50))
    like = f"%{q}%"
    exact = q.upper()
    cls_like = f"%{asset_class}%" if asset_class else None
    df = read_sql(
        """
        SELECT d.isin,
               COALESCE(p.clean_name, d.full_name) AS name,
               d.issuer_name,
               p.fund_family,
               d.index_tracked,
               d.asset_class,
               d.ter_pct,
               d.aum_eur,
               d.sfdr_article,
               d.replication_method,
               p.stock_pct,
               p.bond_pct,
               p.profile_status,
               (SELECT l.exchange_ticker
                FROM dim_etf_listing l
                WHERE l.isin = d.isin AND l.exchange_ticker IS NOT NULL
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic
                LIMIT 1) AS exchange_ticker
        FROM dim_etf d
        LEFT JOIN dim_etf_profile p ON p.isin = d.isin
        WHERE (%(q)s::text = ''
               OR UPPER(d.isin) = %(exact)s
               OR UPPER(COALESCE(p.yf_ticker, '')) = %(exact)s
               OR d.full_name ILIKE %(like)s
               OR p.clean_name ILIKE %(like)s
               OR d.index_tracked ILIKE %(like)s
               OR EXISTS (
                    SELECT 1 FROM dim_etf_listing l
                    WHERE l.isin = d.isin AND UPPER(COALESCE(l.exchange_ticker, '')) = %(exact)s
               ))
          AND (%(asset_class)s::text IS NULL OR d.asset_class ILIKE %(asset_class)s)
        ORDER BY CASE
            WHEN UPPER(d.isin) = %(exact)s THEN 0
            WHEN UPPER(COALESCE(p.yf_ticker, '')) = %(exact)s THEN 1
            WHEN p.clean_name ILIKE %(like)s THEN 2
            ELSE 3
        END,
        COALESCE(d.aum_eur, 0) DESC
        LIMIT %(limit)s
        """,
        params={"q": q, "exact": exact, "like": like, "asset_class": cls_like, "limit": limit},
    )
    return {"query": q, "rows": _df_to_records(df, max_rows=limit), "row_count": int(len(df))}


def _etf_price_stats(isin: str) -> dict[str, Any]:
    df = read_sql(
        """
        SELECT price_date, close
        FROM fact_prices_etf
        WHERE isin = %(isin)s
          AND close IS NOT NULL
          AND price_date >= CURRENT_DATE - INTERVAL '400 days'
        ORDER BY price_date
        """,
        params={"isin": isin},
    )
    if df.empty or len(df) < 2:
        return {"price_points": int(len(df)), "return_1y": None, "volatility_annual": None, "as_of": None}
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 2:
        return {"price_points": int(len(close)), "return_1y": None, "volatility_annual": None, "as_of": None}
    returns = close.pct_change().dropna()
    vol = float(returns.std(ddof=1) * (252 ** 0.5)) if len(returns) >= 30 else None
    ret = float(close.iloc[-1] / close.iloc[0] - 1.0) if close.iloc[0] > 0 else None
    as_of = df["price_date"].iloc[-1]
    return {
        "price_points": int(len(close)),
        "return_1y": ret,
        "volatility_annual": vol,
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
    }


def get_etf_detail(isin: str) -> dict:
    """ETF profile, costs, risk metrics, holdings, sectors, and factor loadings."""
    resolved = _resolve_etf_isin(isin)
    rows = fetchall_dict(
        """
        SELECT d.isin,
               COALESCE(p.clean_name, d.full_name) AS name,
               d.full_name,
               d.issuer_name,
               p.fund_family,
               d.index_tracked,
               d.asset_class,
               d.replication_method,
               d.ter_pct,
               d.aum_eur,
               d.sfdr_article,
               d.fund_currency,
               d.inception_date,
               p.category,
               p.stock_pct,
               p.bond_pct,
               p.cash_pct,
               p.other_pct,
               p.pe_ratio,
               p.pb_ratio,
               p.profile_status
        FROM dim_etf d
        LEFT JOIN dim_etf_profile p ON p.isin = d.isin
        WHERE d.isin = %s
        LIMIT 1
        """,
        (resolved,),
    )
    if not rows:
        return {"error": f"ETF not found: {isin}", "isin": resolved}
    row = rows[0]
    holdings = fetchall_dict(
        """
        SELECT rank, symbol, holding_isin, name, weight
        FROM etf_holding
        WHERE isin = %s
        ORDER BY rank
        LIMIT 15
        """,
        (resolved,),
    )
    sectors = fetchall_dict(
        """
        SELECT sector, weight
        FROM etf_sector_weight
        WHERE isin = %s
        ORDER BY weight DESC
        LIMIT 12
        """,
        (resolved,),
    )
    factors = fetchall_dict(
        """
        SELECT model, ff_region, n_obs, window_start, window_end,
               beta_mkt, beta_smb, beta_hml, beta_rmw, beta_cma, beta_mom,
               t_mkt, t_smb, t_hml, t_rmw, t_cma, t_mom, r2
        FROM fact_etf_factor_loadings
        WHERE isin = %s
        ORDER BY model
        LIMIT 4
        """,
        (resolved,),
    )
    return {
        "profile": row,
        "price_stats": _etf_price_stats(resolved),
        "top_holdings": holdings,
        "sector_weights": sectors,
        "factor_loadings": factors,
    }


def get_etf_holdings_and_exposures(isin: str, limit: int = 25) -> dict:
    """Top holdings, sector weights, industry weights, and credit quality for an ETF."""
    resolved = _resolve_etf_isin(isin)
    limit = max(1, min(int(limit or 25), 100))
    return {
        "isin": resolved,
        "top_holdings": fetchall_dict(
            "SELECT rank, symbol, holding_isin, name, weight FROM etf_holding WHERE isin = %s ORDER BY rank LIMIT %s",
            (resolved, limit),
        ),
        "sectors": fetchall_dict(
            "SELECT sector, weight FROM etf_sector_weight WHERE isin = %s ORDER BY weight DESC LIMIT %s",
            (resolved, limit),
        ),
        "industries": fetchall_dict(
            "SELECT industry, weight FROM etf_industry_weight WHERE isin = %s ORDER BY weight DESC LIMIT %s",
            (resolved, limit),
        ),
        "credit_quality": fetchall_dict(
            "SELECT rating, weight FROM etf_credit_quality_weight WHERE isin = %s ORDER BY weight DESC LIMIT %s",
            (resolved, limit),
        ),
    }


def get_portfolio_etf_snapshot(holdings: list[dict] | None = None) -> dict:
    """Lightweight DB-backed ETF portfolio look-through for chat context."""
    raw = holdings or []
    clean: list[dict[str, Any]] = []
    for item in raw[:20]:
        isin = _resolve_etf_isin(item.get("isin") or item.get("ticker") or item.get("id"))
        if not isin:
            continue
        try:
            weight = float(item.get("weight") if item.get("weight") is not None else 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        clean.append({"isin": isin, "weight": max(0.0, weight)})
    if not clean:
        return {"holdings": [], "warnings": ["No portfolio holdings supplied."]}
    total = sum(item["weight"] for item in clean)
    if total <= 0:
        equal = 1.0 / len(clean)
        for item in clean:
            item["weight"] = equal
    else:
        for item in clean:
            item["weight"] = item["weight"] / total

    rows = fetchall_dict(
        """
        SELECT d.isin,
               COALESCE(p.clean_name, d.full_name) AS name,
               d.asset_class,
               d.ter_pct,
               d.aum_eur,
               p.stock_pct,
               p.bond_pct,
               p.profile_status
        FROM dim_etf d
        LEFT JOIN dim_etf_profile p ON p.isin = d.isin
        WHERE d.isin = ANY(%s)
        """,
        ([item["isin"] for item in clean],),
    )
    by_isin = {row["isin"]: row for row in rows}
    weighted_ter = 0.0
    ter_weight = 0.0
    equity = 0.0
    bond = 0.0
    resolved_holdings = []
    top: dict[str, dict[str, Any]] = {}
    for item in clean:
        row = by_isin.get(item["isin"])
        if not row:
            resolved_holdings.append({"isin": item["isin"], "weight": item["weight"], "warning": "ETF not found"})
            continue
        w = item["weight"]
        if row.get("ter_pct") is not None:
            weighted_ter += w * float(row["ter_pct"])
            ter_weight += w
        stock = row.get("stock_pct")
        bond_pct = row.get("bond_pct")
        if stock is None and bond_pct is None:
            cls = str(row.get("asset_class") or "").lower()
            stock = 0.0 if "fixed" in cls or "bond" in cls else 1.0
            bond_pct = 1.0 if "fixed" in cls or "bond" in cls else 0.0
        equity += w * float(stock or 0.0)
        bond += w * float(bond_pct or 0.0)
        resolved_holdings.append({**row, "weight": w})
        for h in fetchall_dict(
            "SELECT symbol, name, weight FROM etf_holding WHERE isin = %s ORDER BY rank LIMIT 25",
            (item["isin"],),
        ):
            key = (h.get("symbol") or h.get("name") or "").upper()
            if not key:
                continue
            bucket = top.setdefault(key, {"symbol": h.get("symbol"), "name": h.get("name"), "exposure": 0.0, "source_count": 0})
            bucket["exposure"] += w * float(h.get("weight") or 0.0)
            bucket["source_count"] += 1
    top_rows = sorted(top.values(), key=lambda row: row["exposure"], reverse=True)[:15]
    return {
        "holdings": resolved_holdings,
        "blended_ter": weighted_ter / ter_weight if ter_weight > 0 else None,
        "equity_pct": equity,
        "bond_pct": bond,
        "top_lookthrough": top_rows,
    }


def get_macro_snapshot(jurisdiction: str = "US", limit: int = 8) -> dict:
    """Latest macro cycle assessment and headline signals for one jurisdiction."""
    j = str(jurisdiction or "US").strip().upper()
    limit = max(1, min(int(limit or 8), 25))
    cycle_rows = fetchall_dict(
        """
        SELECT jurisdiction, phase, score, recession_probability, confidence, period_end, drivers_json
        FROM fact_macro_cycle_assessment
        WHERE jurisdiction = %s
        ORDER BY period_end DESC
        LIMIT 1
        """,
        (j,),
    )
    cycle = None
    if cycle_rows:
        cycle = dict(cycle_rows[0])
        raw = cycle.pop("drivers_json", None) or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if isinstance(raw, list):
            cycle["drivers"] = raw
            cycle["category_scores"] = {}
            cycle["summary"] = None
        elif isinstance(raw, dict):
            cycle["drivers"] = raw.get("drivers") or []
            cycle["category_scores"] = raw.get("category_scores") or {}
            cycle["summary"] = raw.get("summary")
        else:
            cycle["drivers"] = []
            cycle["category_scores"] = {}
            cycle["summary"] = None
    signals = fetchall_dict(
        """
        SELECT s.series_id, s.name AS label, s.category, s.units, s.frequency,
               latest.date AS as_of, latest.value
        FROM ref_macro_series s
        LEFT JOIN LATERAL (
            SELECT f.date, f.value
            FROM fact_macro f
            WHERE f.series_id = s.series_id AND f.value IS NOT NULL
            ORDER BY f.date DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE s.is_active = TRUE
          AND s.jurisdiction = %s
          AND s.story_tile_slot IS NOT NULL
        ORDER BY s.importance, s.story_tile_slot
        LIMIT %s
        """,
        (j, limit),
    )
    return {"jurisdiction": j, "cycle": cycle, "signals": signals}


def get_macro_calendar(jurisdiction: str | None = None, days: int = 14, limit: int = 30) -> dict:
    """Recent and upcoming macro releases from fact_macro_release."""
    j = str(jurisdiction or "").strip().upper()
    days = max(1, min(int(days or 14), 60))
    limit = max(1, min(int(limit or 30), 100))
    where = ["r.release_at >= now() - INTERVAL '7 days'", "r.release_at <= now() + (%(days)s::text || ' days')::interval"]
    params: dict[str, Any] = {"days": days, "limit": limit, "jurisdiction": j or None}
    if j and j != "GLOBAL":
        where.append("s.jurisdiction = %(jurisdiction)s")
    df = read_sql(
        f"""
        SELECT r.series_id, s.name AS label, s.jurisdiction,
               r.release_at, r.period_end, r.value
        FROM fact_macro_release r
        JOIN ref_macro_series s ON s.series_id = r.series_id
        WHERE {' AND '.join(where)}
        ORDER BY r.release_at ASC
        LIMIT %(limit)s
        """,
        params=params,
    )
    return {"jurisdiction": j or "GLOBAL", "rows": _df_to_records(df, max_rows=limit), "row_count": int(len(df))}


# ── Internals ────────────────────────────────────────────────────────────────

_TICKER_JURIS_CACHE: dict[str, str] = {}


def _juris_for_ticker(ticker: str) -> str:
    if not ticker:
        return ""
    t = ticker.strip().upper()
    if t in _TICKER_JURIS_CACHE:
        return _TICKER_JURIS_CACHE[t]
    rows = fetchall_dict(
        "SELECT jurisdiction FROM v_dim_company WHERE UPPER(ticker) = %s LIMIT 1",
        (t,),
    )
    j = rows[0]["jurisdiction"] if rows else ""
    if j:
        _TICKER_JURIS_CACHE[t] = j
    return j


# ── DeepSeek tool schemas (JSON Schema, function-calling) ───────────────────

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "list_companies",
        "description": "Search the company universe by name fragment, ticker prefix, sector, or jurisdiction. Returns up to 50 rows.",
        "parameters": {"type": "object", "properties": {
            "name_query":     {"type": "string", "description": "Case-insensitive substring of company name."},
            "ticker_prefix":  {"type": "string"},
            "jurisdiction":   {"type": "string", "enum": ["US", "JP"]},
            "sector":         {"type": "string", "description": "GICS sector or mapping_sector substring."},
            "limit":          {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
        }, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_company_overview",
        "description": "Identity, classification and available fiscal-year range for a ticker.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_fundamentals",
        "description": "Standardised annual line items (revenue, EBITDA, FCF, total assets, debt, etc.) for one ticker. Defaults to all 13 core line items and all available years.",
        "parameters": {"type": "object", "properties": {
            "ticker":      {"type": "string"},
            "start_year":  {"type": "integer"},
            "end_year":    {"type": "integer"},
            "line_items":  {"type": "array", "items": {"type": "string"},
                            "description": "Optional subset, e.g. ['revenue','free_cash_flow']."},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_raw_fundamentals",
        "description": "Raw XBRL facts (concept-level) for one ticker. Use only when get_fundamentals doesn't have the line item you need. Defaults to the latest-known restatement of each period. fiscal_year filters by the fact's period year (from period_end), not the filing year.",
        "parameters": {"type": "object", "properties": {
            "ticker":         {"type": "string"},
            "fiscal_year":    {"type": "integer", "description": "Period year (from period_end), not the filing year."},
            "fiscal_period":  {"type": "string", "description": "e.g. 'FY','Q1','Q2','Q3','Annual'."},
            "statement_type": {"type": "string", "enum": ["BalanceSheet", "IncomeStatement", "CashFlow"]},
            "as_of":          {"type": "string", "description": "YYYY-MM-DD; return each value as it was known on this date (point-in-time, no look-ahead from later restatements)."},
            "all_vintages":   {"type": "boolean", "description": "Return every filing's version of each period (original + restatements) with filing_id and filed_date, instead of just the latest."},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_metrics",
        "description": "Derived financial metrics (PE, EV/EBITDA, margins, ROIC, leverage, growth rates) as an annual time series for one ticker.",
        "parameters": {"type": "object", "properties": {
            "ticker":      {"type": "string"},
            "metric_ids":  {"type": "array", "items": {"type": "string"}},
            "start_year":  {"type": "integer"},
            "end_year":    {"type": "integer"},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_modeled_statement_snapshot",
        "description": "DB-backed 5-year modeled statement snapshot with standardized line-item IDs, labels, statement type, metric type, and supporting concept paths.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "years": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_market_metrics",
        "description": "Latest market metrics from fact_market_metrics, including stock price, market capitalization, and market beta where available.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_peer_group",
        "description": "Deterministic 10-peer selector using the same GICS sector, same jurisdiction first, and latest market cap. Includes valuation/growth metrics.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 10},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_factor_exposure",
        "description": "Latest ticker-level FF4/FF6 factor loadings and regression quality diagnostics.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_recon_flags",
        "description": "Recent metric reconciliation and trace-quality rows for the ticker.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_etfs",
        "description": "Search the ETF warehouse by name, ISIN, ticker, index, or asset class. Use for ETF discovery questions.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "asset_class": {"type": "string", "description": "Optional asset class filter, e.g. Equity or Fixed Income."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
        }, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_etf_detail",
        "description": "ETF profile, cost, AUM, risk/price stats, top holdings, sector weights, and Fama-French factor loadings for one ISIN or ETF ticker.",
        "parameters": {"type": "object", "properties": {
            "isin": {"type": "string", "description": "ETF ISIN or ETF exchange/yfinance ticker."},
        }, "required": ["isin"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_etf_holdings_and_exposures",
        "description": "ETF top holdings, sector, industry, and credit-quality exposures for one ISIN.",
        "parameters": {"type": "object", "properties": {
            "isin": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }, "required": ["isin"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_portfolio_etf_snapshot",
        "description": "DB-backed ETF portfolio look-through from local portfolio holdings. Returns blended TER, equity/bond mix, resolved holdings, and top look-through exposures.",
        "parameters": {"type": "object", "properties": {
            "holdings": {"type": "array", "items": {"type": "object"}, "description": "Array of holdings with isin/ticker/id and optional weight."},
        }, "required": ["holdings"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_macro_snapshot",
        "description": "Latest macro cycle assessment and headline macro signals for a jurisdiction.",
        "parameters": {"type": "object", "properties": {
            "jurisdiction": {"type": "string", "enum": ["US", "JP", "EZ"], "default": "US"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
        }, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_macro_calendar",
        "description": "Recent and upcoming macro data releases from the macro release table.",
        "parameters": {"type": "object", "properties": {
            "jurisdiction": {"type": "string", "enum": ["US", "JP", "EZ", "GLOBAL"]},
            "days": {"type": "integer", "minimum": 1, "maximum": 60, "default": 14},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
        }, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_institutional_holders",
        "description": "Top 13F institutional holders for a US ticker, ranked by shares held. Can filter by manager_type classification. Quarter is optional and accepts YYYYQn or YYYY-MM-DD; defaults to the latest available 13F period for that ticker.",
        "parameters": {"type": "object", "properties": {
            "ticker":  {"type": "string", "description": "US issuer ticker, e.g. AAPL."},
            "quarter": {"type": "string", "description": "Optional 13F report period, e.g. 2024Q4 or 2024-12-31."},
            "manager_type": {"type": "string", "enum": ["alternative", "traditional", "wealth_trust", "capital_markets", "insurance"], "description": "Optional Portfolio Analytics manager classification filter."},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_manager_portfolio",
        "description": "13F portfolio for one institutional manager CIK, ordered by market value. Quarter is optional and accepts YYYYQn or YYYY-MM-DD; defaults to the manager's latest available 13F period.",
        "parameters": {"type": "object", "properties": {
            "manager_cik": {"type": "string", "description": "Institutional manager CIK; leading zeros optional."},
            "quarter":     {"type": "string", "description": "Optional 13F report period, e.g. 2024Q4 or 2024-12-31."},
        }, "required": ["manager_cik"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "compare_13f_ownership",
        "description": "Compare institutional ownership metrics for up to 10 US tickers. Can filter to one manager_type classification. Quarter is optional; if omitted, each ticker uses its latest available 13F period.",
        "parameters": {"type": "object", "properties": {
            "tickers": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
            "quarter": {"type": "string", "description": "Optional 13F report period, e.g. 2024Q4 or 2024-12-31."},
            "manager_type": {"type": "string", "enum": ["alternative", "traditional", "wealth_trust", "capital_markets", "insurance"], "description": "Optional Portfolio Analytics manager classification filter."},
        }, "required": ["tickers"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "rank_institutional_activity",
        "description": "Rank US tickers by aggregate 13F net buying, net selling, or holder count in a quarter. Supports manager_type classification filters, GICS sector filters, PE ratio bounds, and stock-performance return windows.",
        "parameters": {"type": "object", "properties": {
            "quarter":   {"type": "string", "description": "Required 13F report period, e.g. 2024Q4 or 2024-12-31."},
            "direction": {"type": "string", "enum": ["buy", "sell", "top_held"], "default": "buy"},
            "sector":    {"type": "string", "description": "Optional GICS sector substring, e.g. Technology."},
            "manager_type": {"type": "string", "enum": ["alternative", "traditional", "wealth_trust", "capital_markets", "insurance"], "description": "Optional Portfolio Analytics manager classification filter. Use alternative for hedge funds/alternative asset managers."},
            "pe_min":    {"type": "number", "description": "Optional lower bound for latest trailing P/E from fact_metrics_us."},
            "pe_max":    {"type": "number", "description": "Optional upper bound for latest trailing P/E from fact_metrics_us."},
            "performance_months": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Optional stock-price return window in months, e.g. 6 for last six months."},
            "limit":     {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }, "required": ["quarter"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_13f_managers",
        "description": "Search 13F managers by Portfolio Analytics manager_type classification and/or manager name. Use this when the user asks which managers are alternative, traditional, wealth/trust, capital markets, or insurance.",
        "parameters": {"type": "object", "properties": {
            "manager_type": {"type": "string", "enum": ["alternative", "traditional", "wealth_trust", "capital_markets", "insurance"], "description": "Optional manager classification filter."},
            "name_query": {"type": "string", "description": "Optional manager-name or CIK substring."},
            "quarter": {"type": "string", "description": "Optional 13F report period, e.g. 2024Q4 or 2024-12-31. Defaults to latest classification period."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "compare_metrics",
        "description": "Same metric for multiple tickers (US and/or JP mixed) over an optional year range. Use this for cross-company and cross-jurisdiction comparisons.",
        "parameters": {"type": "object", "properties": {
            "tickers":     {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
            "metric_id":   {"type": "string"},
            "start_year":  {"type": "integer"},
            "end_year":    {"type": "integer"},
        }, "required": ["tickers", "metric_id"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_prices",
        "description": "Daily closing prices for one ticker, optionally resampled. Capped at 200 rows after resampling.",
        "parameters": {"type": "object", "properties": {
            "ticker":     {"type": "string"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date":   {"type": "string", "description": "YYYY-MM-DD"},
            "resample":   {"type": "string", "enum": ["D", "W", "M", "Q", "Y"]},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "get_filings",
        "description": "Recent regulatory filings (form, filed date, fiscal year) for one ticker.",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "limit":  {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
        }, "required": ["ticker"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "rank_universe",
        "description": "Rank all companies by one metric in one fiscal year. Optional sector filter; optional jurisdiction filter (omit for both US and JP).",
        "parameters": {"type": "object", "properties": {
            "metric_id":    {"type": "string"},
            "fiscal_year":  {"type": "integer"},
            "jurisdiction": {"type": "string", "enum": ["US", "JP"]},
            "sector":       {"type": "string"},
            "ascending":    {"type": "boolean", "default": False},
            "limit":        {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        }, "required": ["metric_id", "fiscal_year"], "additionalProperties": False}}},
]


_DISPATCH = {
    "list_companies":       list_companies,
    "get_company_overview": get_company_overview,
    "get_fundamentals":     get_fundamentals,
    "get_raw_fundamentals": get_raw_fundamentals,
    "get_metrics":          get_metrics,
    "get_modeled_statement_snapshot": get_modeled_statement_snapshot,
    "get_market_metrics":   get_market_metrics,
    "get_peer_group":       get_peer_group,
    "get_factor_exposure":  get_factor_exposure,
    "get_recon_flags":      get_recon_flags,
    "search_etfs":          search_etfs,
    "get_etf_detail":       get_etf_detail,
    "get_etf_holdings_and_exposures": get_etf_holdings_and_exposures,
    "get_portfolio_etf_snapshot": get_portfolio_etf_snapshot,
    "get_macro_snapshot":   get_macro_snapshot,
    "get_macro_calendar":   get_macro_calendar,
    "get_institutional_holders": get_institutional_holders,
    "get_manager_portfolio": get_manager_portfolio,
    "compare_13f_ownership": compare_13f_ownership,
    "rank_institutional_activity": rank_institutional_activity,
    "search_13f_managers": search_13f_managers,
    "compare_metrics":      compare_metrics,
    "get_prices":           get_prices,
    "get_filings":          get_filings,
    "rank_universe":        rank_universe,
}


def execute(name: str, args: dict) -> Any:
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name!r}."}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
