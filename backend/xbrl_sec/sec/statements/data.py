"""Database access for side-by-side statement assembly."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
import psycopg2.extras

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.statements.assembly import assemble_statement, normalize_statement_type


_JURISDICTION_CONFIG = {
    "US": {
        "std_table": "fact_fundamentals_std_us",
        "company_table": "dim_company_us",
        "entity_col": "cik",
        "ticker_col": "primary_ticker",
        "accounting_standard": "US_GAAP",
    },
    "JP": {
        "std_table": "fact_fundamentals_std_jp",
        "company_table": "dim_company_jp",
        "entity_col": "edinet_code",
        "ticker_col": "primary_ticker",
        "accounting_standard": "JP_GAAP",
    },
}

_CATEGORY_BY_STATEMENT = {
    "balance_sheet": ("balance_sheet",),
    "income_statement": ("income_statement",),
    "cash_flow_statement": ("cash_flow_statement", "cash_flow"),
}


def _config(jurisdiction: str) -> dict[str, str]:
    jur = str(jurisdiction).upper()
    if jur not in _JURISDICTION_CONFIG:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")
    return _JURISDICTION_CONFIG[jur]


def _entity_context(jurisdiction: str, ticker: str) -> dict[str, Any]:
    cfg = _config(jurisdiction)
    with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {cfg['entity_col']} AS entity_id, {cfg['ticker_col']} AS ticker,
                   COALESCE(mapping_sector, 'corp') AS mapping_sector,
                   gics_sector_code, gics_industry_group_code
            FROM {cfg['company_table']}
            WHERE {cfg['ticker_col']} = %s
            LIMIT 1
            """,
            (ticker,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"{jurisdiction} ticker not found: {ticker}")
    return dict(row)


def _display_sector(mapping_sector: str | None, gics_industry_group_code: Any = None) -> str:
    sector = str(mapping_sector or "corp")
    if sector == "bank_financial":
        return "bank_financial"
    if sector == "non_bank_financial":
        gics = str(gics_industry_group_code or "")
        if gics == "4030":
            return "insurance"
        if gics == "4020" or gics.startswith("40"):
            return "asset_manager_other_financial"
        if gics == "6010":
            return "reit"
        return "asset_manager_other_financial"
    return "corp"


def _policy_sector_keys(sector_scope: str) -> list[str]:
    keys = {"", sector_scope}
    if sector_scope in {"insurance", "reit", "asset_manager_other_financial"}:
        keys.add("non_bank_financial")
    return sorted(keys)


def _fetch_std_rows(
    jurisdiction: str,
    entity_id: str,
    sector_scope: str,
    statement_type: str,
    fiscal_period: str,
    n_periods: int,
    year_from: int | None = None,
    year_to: int | None = None,
    include_suppressed_sources: bool = False,
) -> tuple[list[int], dict[int, str], list[dict[str, Any]]]:
    cfg = _config(jurisdiction)
    stmt = normalize_statement_type(statement_type)
    categories = list(_CATEGORY_BY_STATEMENT[stmt])
    year_filter = ""
    params: list[Any] = [jurisdiction.upper(), _policy_sector_keys(sector_scope), entity_id, fiscal_period, stmt, categories]
    fetch_n_periods = int(n_periods) + (1 if stmt == "income_statement" else 0)
    selected_year_from = int(year_from) if year_from is not None else None
    selected_year_to = int(year_to) if year_to is not None else None
    if year_from is not None and year_to is not None:
        year_filter = "AND EXTRACT(YEAR FROM s.period_end)::int BETWEEN %s AND %s"
        params.extend([int(year_from) - (1 if stmt == "income_statement" else 0), int(year_to)])
    else:
        year_filter = f"""
          AND EXTRACT(YEAR FROM s.period_end)::int = ANY(ARRAY(
              SELECT DISTINCT EXTRACT(YEAR FROM s2.period_end)::int
              FROM {cfg['std_table']} s2
              JOIN ref_standardized_line_items rli2 ON rli2.line_item_id = s2.line_item_id
              WHERE s2.{cfg['entity_col']} = %s
                AND s2.fiscal_period = %s
                AND (COALESCE(rli2.statement_type, rli2.category) = %s OR rli2.category = ANY(%s::text[]))
                AND s2.period_end IS NOT NULL
              ORDER BY EXTRACT(YEAR FROM s2.period_end)::int DESC
              LIMIT %s
          ))
        """
        params.extend([entity_id, fiscal_period, stmt, categories, fetch_n_periods])
    source_policy_filter = ""
    if not include_suppressed_sources:
        source_policy_filter = """
                  AND COALESCE(p.default_visibility, 'default') = 'default'
        """
    with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT s.line_item_id, COALESCE(rli.label, s.line_item_id) AS label,
                       rli.unit_type, rli.statement_type, rli.category,
                       EXTRACT(YEAR FROM s.period_end)::int AS display_year,
                       s.period_end, s.value, s.currency, s.metric_type,
                       s.source_concept_id, s.filing_form, s.filed_date, s.filing_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.line_item_id, EXTRACT(YEAR FROM s.period_end)::int
                           ORDER BY
                               COALESCE(p.source_penalty, 0),
                               CASE s.metric_type
                                   WHEN 'RAW' THEN 0
                                   WHEN 'MARKET' THEN 0
                                   WHEN 'T2_SUM' THEN 1
                                   WHEN 'T2_COMPONENT' THEN 1
                                   WHEN 'DERIVED_BOTTOM_UP' THEN 2
                                   WHEN 'DERIVED_PARTIAL' THEN 3
                                   WHEN 'RESIDUAL' THEN 4
                                   ELSE 5
                               END,
                               s.period_end DESC NULLS LAST,
                               s.filed_date DESC NULLS LAST
                       ) AS rn
                FROM {cfg['std_table']} s
                JOIN ref_standardized_line_items rli ON rli.line_item_id = s.line_item_id
                LEFT JOIN LATERAL (
                    SELECT policy_action,
                           default_visibility,
                           source_rank_penalty AS source_penalty,
                           reason_code
                    FROM vw_concept_target_display_policy_active p
                    WHERE p.jurisdiction = %s
                      AND p.normalized_concept_id = split_part(s.source_concept_id, ',', 1)
                      AND (p.target_variable = s.line_item_id OR p.target_variable = '')
                      AND COALESCE(p.mapping_sector, '') = ANY(%s::text[])
                      AND (p.fiscal_year_from IS NULL OR s.fiscal_year >= p.fiscal_year_from)
                      AND (p.fiscal_year_to IS NULL OR s.fiscal_year <= p.fiscal_year_to)
                      AND (p.fiscal_period IS NULL OR p.fiscal_period = s.fiscal_period)
                    ORDER BY
                      CASE WHEN p.target_variable = s.line_item_id THEN 0 ELSE 1 END,
                      p.specificity_rank DESC,
                      p.source_rank_penalty DESC,
                      p.policy_id DESC
                    LIMIT 1
                ) p ON TRUE
                WHERE s.{cfg['entity_col']} = %s
                  AND s.fiscal_period = %s
                  AND (COALESCE(rli.statement_type, rli.category) = %s OR rli.category = ANY(%s::text[]))
                  AND s.period_end IS NOT NULL
                  {source_policy_filter}
                  {year_filter}
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY display_year DESC, line_item_id
            """,
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]

    rows_by_item: dict[str, dict[str, Any]] = {}
    period_ends: dict[int, str] = {}
    for row in rows:
        year = int(row["display_year"])
        if row.get("period_end") and year not in period_ends:
            period_ends[year] = str(row["period_end"])[:10]
        line_item_id = str(row["line_item_id"])
        out = rows_by_item.setdefault(
            line_item_id,
            {
                "line_item_id": line_item_id,
                "label": row.get("label") or line_item_id,
                "unit": row.get("currency") or row.get("unit_type") or "",
                "unit_type": row.get("unit_type"),
                "metric_type": row.get("metric_type"),
                "source_concept_id": row.get("source_concept_id"),
                "filing_form": row.get("filing_form"),
                "filing_id": row.get("filing_id"),
                "values": {},
                "metric_type_by_year": {},
            },
        )
        value = row.get("value")
        out["values"][year] = Decimal(str(value)) if value is not None else None
        out["metric_type_by_year"][year] = row.get("metric_type")

    all_periods = sorted(period_ends.keys(), reverse=True)
    if selected_year_from is not None and selected_year_to is not None:
        periods = [
            year for year in all_periods
            if selected_year_from <= year <= selected_year_to
        ]
    else:
        periods = all_periods[:int(n_periods)]
    selected_period_ends = {year: period_ends[year] for year in periods if year in period_ends}
    return periods, selected_period_ends, list(rows_by_item.values())


def _fetch_profile_rows(accounting_standard: str, sector_scope: str, statement_type: str) -> list[dict[str, Any]]:
    stmt = normalize_statement_type(statement_type)
    with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT dp.line_item_id, COALESCE(r.label, dp.line_item_id) AS label,
                   r.unit_type AS unit, r.unit_type,
                   r.item_class, r.derivation_policy,
                   dp.display_role, dp.display_policy, dp.display_order,
                   dp.display_parent_id, dp.indent_level
            FROM ref_std_statement_display_profile dp
            JOIN ref_standardized_line_items r ON r.line_item_id = dp.line_item_id
            WHERE dp.accounting_standard = %s
              AND dp.sector_scope = %s
              AND dp.statement_type = %s
            ORDER BY dp.display_order NULLS LAST, dp.line_item_id
            """,
            (accounting_standard, sector_scope, stmt),
        )
        profile = [dict(row) for row in cur.fetchall()]
        if profile:
            return profile
        cur.execute(
            """
            SELECT r.line_item_id, COALESCE(r.label, r.line_item_id) AS label,
                   r.unit_type AS unit, r.unit_type,
                   r.item_class, r.derivation_policy,
                   CASE
                       WHEN r.item_class IN ('intermediate', 'cross_statement_ref', 'catch_all') THEN 'SUBTOTAL'
                       WHEN r.item_class IN ('subtotal') THEN 'SUBTOTAL'
                       ELSE 'DISCLOSURE'
                   END AS display_role,
                   CASE
                       WHEN r.item_class IN ('intermediate', 'cross_statement_ref', 'catch_all', 'subtotal') THEN 'MAIN'
                       ELSE 'SUPPLEMENTAL'
                   END AS display_policy,
                   COALESCE(
                       CASE WHEN %s = 'JP_GAAP' THEN r.display_order_jp_gaap ELSE r.display_order_us_gaap END,
                       r.display_order,
                       r.importance,
                       999999
                   ) AS display_order,
                   NULL::text AS display_parent_id,
                   1::smallint AS indent_level
            FROM ref_standardized_line_items r
            WHERE r.statement_type = %s
              AND r.sector_scope IN ('universal', %s)
            ORDER BY display_order, r.line_item_id
            """,
            (accounting_standard, stmt, sector_scope),
        )
        return [dict(row) for row in cur.fetchall()]


def _fetch_edge_rows(accounting_standard: str, sector_scope: str, statement_type: str) -> list[dict[str, Any]]:
    stmt = normalize_statement_type(statement_type)
    with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT parent_id, child_id, sign, sibling_rank
            FROM ref_std_item_edge
            WHERE edge_type = 'rollup'
              AND statement_type = %s
              AND sector_scope = %s
              AND (accounting_standard = %s OR accounting_standard IS NULL)
            ORDER BY parent_id, sibling_rank, child_id
            """,
            (stmt, sector_scope, accounting_standard),
        )
        return [dict(row) for row in cur.fetchall()]


def _fetch_display_evidence(jurisdiction: str, entity_id: str, statement_type: str, periods: list[int]) -> list[dict[str, Any]]:
    if jurisdiction.upper() != "US" or not periods:
        return []
    stmt = normalize_statement_type(statement_type)
    with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT fiscal_year, fiscal_period, line_item_id, source_concept_id,
                   display_role, evidence_quality, role_reason,
                   operating_reconciliation_status, operating_reconciliation_delta
            FROM fact_statement_display_evidence_us
            WHERE cik = %s
              AND statement_type = %s
              AND fiscal_year = ANY(%s)
            ORDER BY fiscal_year DESC, line_item_id, source_concept_id
            """,
            (entity_id, stmt, periods),
        )
        return [dict(row) for row in cur.fetchall()]


def assemble_statement_for_ticker(
    jurisdiction: str,
    ticker: str,
    statement_type: str,
    fiscal_period: str = "FY",
    n_periods: int = 5,
    year_from: int | None = None,
    year_to: int | None = None,
    include_hidden: bool = False,
) -> dict[str, Any]:
    jur = jurisdiction.upper()
    cfg = _config(jur)
    ctx = _entity_context(jur, ticker)
    sector_scope = _display_sector(ctx.get("mapping_sector"), ctx.get("gics_industry_group_code"))
    if sector_scope != "corp":
        # v1 is intentionally corp-first. We still assemble with the resolved
        # profile when available so comparison can expose what is missing.
        pass
    periods, period_ends, std_rows = _fetch_std_rows(
        jur,
        str(ctx["entity_id"]),
        sector_scope,
        statement_type,
        fiscal_period,
        n_periods,
        year_from,
        year_to,
        include_suppressed_sources=include_hidden,
    )
    profile_rows = _fetch_profile_rows(cfg["accounting_standard"], sector_scope, statement_type)
    edge_rows = _fetch_edge_rows(cfg["accounting_standard"], sector_scope, statement_type)
    evidence = _fetch_display_evidence(jur, str(ctx["entity_id"]), statement_type, periods)
    out = assemble_statement(
        jurisdiction=jur,
        accounting_standard=cfg["accounting_standard"],
        sector_scope=sector_scope,
        statement_type=statement_type,
        periods=periods,
        period_ends=period_ends,
        std_rows=std_rows,
        profile_rows=profile_rows,
        edge_rows=edge_rows,
        display_evidence=evidence,
        include_hidden=include_hidden,
    )
    out["ticker"] = ticker
    out["entity_id"] = str(ctx["entity_id"])
    return out
