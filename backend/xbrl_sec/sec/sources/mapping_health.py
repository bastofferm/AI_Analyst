"""Mapped-concept anomaly review queue builder.

This module reviews mappings that are already in production use. It never
changes the governed versioned mapping table; it only writes review packets
with evidence and advisory proposed_action values.
"""
from __future__ import annotations

import json
import re
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


_PROMPT_VERSION = "mapped_anomaly_review_v3"
_SOURCE_PREFIX = "mapped_anomaly_health_v3"
_OLD_SOURCE_PREFIXES = ("mapped_anomaly_health_v1", "mapped_anomaly_health_v2")

_DISCLOSURE_TERMS = (
    "disclosure",
    "fairvalue",
    "fair value",
    "schedule",
    "textblock",
    "policy",
    "commitment",
    "derivative",
    "maturity",
    "concentration",
)
_ALTERNATE_TOTAL_TERMS = (
    "liabilitiesandstockholdersequity",
    "liabilitiesandshareholdersequity",
    "liabilitiesandequity",
    "liabilitiesandpartnerscapital",
    "liabilitiesandredeemablenoncontrollinginterest",
)
_CONTRA_TERMS = (
    "allowance",
    "reserve",
    "accumulateddepreciation",
    "accumulatedamortization",
    "valuationallowance",
)
_COMPONENT_TERMS = (
    "rawmaterials",
    "workinprocess",
    "finishedgoods",
    "supplies",
    "current",
    "noncurrent",
    "gross",
    "netofreserves",
)
_PRIMARY_TOTAL_TARGETS = {
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_current_assets",
    "total_noncurrent_assets",
    "total_current_liabilities",
    "total_noncurrent_liabilities",
    "inventory_total",
    "property_plant_equipment_net",
    "accounts_receivable_net",
}
_PRIMARY_CONCEPT_LOCALS = {
    "assets",
    "liabilities",
    "liabilitiescurrent",
    "inventorynet",
    "propertyplantandequipmentnet",
    "accountsreceivablenetcurrent",
    "receivablesnetcurrent",
}

_JURISDICTION = {
    "US": {
        "std_table": "fact_fundamentals_std_us",
        # latest-vintage view: one row per period so the std->raw enrichment
        # joins don't fan out across filing vintages (see migration 113).
        "raw_table": "v_fact_fundamentals_us_latest",
        "dim_table": "dim_company_us",
        "entity_col": "cik",
        "standard": "US_GAAP",
        "dimension_expr": "NULL::text",
    },
    "JP": {
        "std_table": "fact_fundamentals_std_jp",
        "raw_table": "fact_fundamentals_jp",
        "dim_table": "dim_company_jp",
        "entity_col": "edinet_code",
        "standard": "JP_GAAP",
        "dimension_expr": "f.dimension_signature",
    },
}


def _concept_namespace(concept_id: str) -> str | None:
    if "/" in concept_id:
        return concept_id.split("/", 1)[0]
    if ":" in concept_id:
        return concept_id.split(":", 1)[0]
    return None


def _concept_local_name(concept_id: str) -> str:
    if "/" in concept_id:
        return concept_id.rsplit("/", 1)[-1]
    if ":" in concept_id:
        return concept_id.rsplit(":", 1)[-1]
    return concept_id


def _limited(value: Any, limit: int = 20) -> Any:
    if isinstance(value, list):
        return value[:limit]
    return value


def _squash(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _text_blob(row: dict[str, Any]) -> str:
    values: list[Any] = [
        row.get("concept_id"),
        row.get("label_en"),
        row.get("label_ja"),
        row.get("description"),
        row.get("target_variable"),
    ]
    for key in ("concept_paths", "root_ids", "parent_ids", "line_items"):
        value = row.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    return " ".join(str(value or "") for value in values).lower()


def _concept_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("concept_id", "label_en", "label_ja", "description", "target_variable")
    ).lower()


def _has_squashed_term(text: str, terms: tuple[str, ...]) -> bool:
    squashed = _squash(text)
    return any(_squash(term) in squashed for term in terms)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _role_from_evidence(row: dict[str, Any]) -> tuple[str, float]:
    concept_text = _concept_text(row)
    local = _squash(_concept_local_name(str(row.get("concept_id") or "")))
    target = str(row.get("target_variable") or "")
    check_ids = {str(item) for item in row.get("failed_check_ids") or []}
    line_items = {str(item) for item in row.get("line_items") or []}

    if _has_squashed_term(concept_text, ("axis", "member", "domain", "table")) and not row.get("fact_count"):
        return "table_member_noise", 0.95
    if target == "total_assets" and _has_squashed_term(concept_text, _ALTERNATE_TOTAL_TERMS):
        return "alternate_total", 0.9
    if local in _PRIMARY_CONCEPT_LOCALS or (target in _PRIMARY_TOTAL_TARGETS and local == _squash(target)):
        return "primary_total", 0.85
    if _has_squashed_term(concept_text, _DISCLOSURE_TERMS):
        return "disclosure_only", 0.95
    if _has_squashed_term(concept_text, _CONTRA_TERMS):
        return "contra_component", 0.85
    if any(str(check_id).startswith("rollup:") for check_id in check_ids):
        if target in _PRIMARY_TOTAL_TARGETS and len(line_items) == 1 and next(iter(line_items), "") == target:
            return "primary_total", 0.75
        return "component", 0.8
    if _has_squashed_term(concept_text, _COMPONENT_TERMS) and target not in _PRIMARY_TOTAL_TARGETS:
        return "component", 0.75
    if target in _PRIMARY_TOTAL_TARGETS:
        return "primary_total", 0.8
    return "primary_line_item", 0.7


def _context_distribution(row: dict[str, Any]) -> dict[str, int]:
    raw = row.get("context_role_distribution") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("primary_statement", "note_disclosure", "segment_or_schedule", "cash_flow_addback", "dimension_heavy", "unknown"):
        try:
            out[key] = int(raw.get(key) or 0)
        except Exception:
            out[key] = 0
    return out


def _noisy_context_count(row: dict[str, Any]) -> int:
    dist = _context_distribution(row)
    return sum(dist.get(key, 0) for key in ("note_disclosure", "segment_or_schedule", "dimension_heavy", "cash_flow_addback"))


def _action_from_role(row: dict[str, Any]) -> str:
    anomaly = str(row.get("anomaly_type") or "")
    role = str(row.get("concept_role") or "")
    improvement = _safe_float(row.get("residual_improvement_pct")) or 0.0
    best_action = str(row.get("counterfactual_best_action") or "")
    dist = _context_distribution(row)
    primary_contexts = dist.get("primary_statement", 0)
    noisy_contexts = _noisy_context_count(row)

    if anomaly == "sector_mismatch":
        return "sector_scope"
    if anomaly in {"accounting_standard_mismatch", "taxonomy_year_drift"}:
        return "year_scope"
    if role in {"disclosure_only", "audit_only", "table_member_noise"}:
        return "supplemental_only" if role != "table_member_noise" else "unmap"
    if role == "alternate_total":
        return "alternate_total"
    if role in {"component", "contra_component"}:
        if best_action == "sign_flip" and improvement >= 0.8 and int(row.get("identity_fact_count") or 0) >= 10:
            return "sign_fix"
        return "component_scope"
    if role == "primary_line_item" and anomaly == "display_layer_incoherence":
        if primary_contexts > 0 and noisy_contexts > 0:
            return "needs_review"
        if noisy_contexts > 0 and primary_contexts == 0:
            return "supplemental_only"
    if anomaly == "identity_failure":
        return "keep"
    return "needs_review"


def _review_action_type(row: dict[str, Any]) -> str:
    proposed = str(row.get("proposed_action") or "")
    role = str(row.get("concept_role") or "")
    if proposed == "sign_fix":
        return "sign_fix_candidate"
    if proposed == "alternate_total" or role == "alternate_total":
        return "alternate_total_fallback"
    if proposed == "component_scope":
        return "component_only"
    if proposed == "sector_scope":
        return "sector_mapping_split"
    if proposed == "supplemental_only":
        return "display_supplemental_only"
    if proposed == "keep":
        return "keep"
    return "needs_review"


def _triage_priority(row: dict[str, Any]) -> int:
    action = str(row.get("review_action_type") or "")
    role = str(row.get("concept_role") or "")
    improvement = _safe_float(row.get("residual_improvement_pct")) or 0.0
    if action == "sign_fix_candidate" and improvement >= 0.8:
        return 1
    if action == "alternate_total_fallback":
        return 2
    if role == "disclosure_only" and action in {"display_supplemental_only", "sector_mapping_split"}:
        return 3
    if action == "sector_mapping_split":
        return 4
    if action == "component_only":
        return 5
    if action == "needs_review":
        return 6
    if action == "keep":
        return 90
    return 50


def _review_batch(row: dict[str, Any]) -> str:
    action = str(row.get("review_action_type") or "")
    role = str(row.get("concept_role") or "")
    proposed = str(row.get("proposed_action") or "")
    if action in {"sign_fix_candidate", "alternate_total_fallback"} or (role == "disclosure_only" and proposed == "sector_scope"):
        return "A_high_signal"
    if role == "disclosure_only" and proposed == "supplemental_only":
        return "B_disclosure"
    if role in {"component", "contra_component"}:
        return "C_component"
    if role == "primary_total" and proposed == "keep":
        return "D_primary_total_keep"
    return "backlog"


def _context_role_case_sql(dimension_expr: str) -> str:
    """Return the SQL CASE expression used to classify raw fact contexts."""
    context_text = """
        lower(concat_ws(
            ' ',
            COALESCE(f.statement_type, ''),
            COALESCE(f.root_id, ''),
            COALESCE(f.parent_id, ''),
            COALESCE(f.pre_parent_id, ''),
            COALESCE(f.concept_path, s.concept_path, ''),
            COALESCE(f.concept_id, split_part(s.source_concept_id, ',', 1), '')
        ))
    """
    dimension_text = f"lower(COALESCE({dimension_expr}, ''))"
    return f"""
        CASE
            WHEN {dimension_text} <> ''
                 AND {dimension_text} NOT LIKE '%%consolidatedornonconsolidatedaxis%%'
                 AND {dimension_text} NOT LIKE '%%nonconsolidatedmember%%'
                 AND {dimension_text} NOT LIKE '%%consolidatedmember%%'
                THEN 'dimension_heavy'
            WHEN {context_text} ~ '(disclosure|fairvalue|fair value|maturity|policy|commitment|derivative|concentration|textblock)'
                THEN 'note_disclosure'
            WHEN {context_text} ~ '(segment|schedule|businesssegment|geographic|geographical|bybusiness|bygeographic|breakdown)'
                THEN 'segment_or_schedule'
            WHEN {context_text} ~ '(cashflow|cash flow|operatingactivities|investingactivities|financingactivities)'
                THEN 'cash_flow_addback'
            WHEN lower(COALESCE(f.statement_type, '')) IN (
                    'incomestatement',
                    'balancesheet',
                    'cashflowstatement',
                    'income_statement',
                    'balance_sheet',
                    'cash_flow_statement'
                 )
                 OR {context_text} ~ '(statementofincome|statementofoperations|statementoffinancialposition|balancesheet|cashflowstatement)'
                THEN 'primary_statement'
            ELSE 'unknown'
        END
    """


def _entity_sector_expr(alias: str = "d") -> str:
    return f"""
        CASE
            WHEN COALESCE({alias}.mapping_sector, 'corp') <> 'non_bank_financial'
                THEN COALESCE({alias}.mapping_sector, 'corp')
            WHEN {alias}.gics_sector_code = '60'
                THEN 'reit'
            WHEN {alias}.gics_industry_group_code = '4030'
                THEN 'insurance'
            WHEN {alias}.gics_industry_group_code = '4020' OR {alias}.gics_sector_code = '40'
                THEN 'asset_manager_other_financial'
            ELSE 'asset_manager_other_financial'
        END
    """


def _base_select_sql(
    cfg: dict[str, str],
    anomaly_type: str,
    proposed_action: str,
    where_sql: str,
    extra_join: str = "",
    row_limit: int | None = None,
) -> str:
    std_table = cfg["std_table"]
    raw_table = cfg["raw_table"]
    dim_table = cfg["dim_table"]
    entity_col = cfg["entity_col"]
    standard = cfg["standard"]
    entity_sector = _entity_sector_expr("d")
    row_limit_sql = f"LIMIT {max(1, int(row_limit))}" if row_limit else ""
    return f"""
        WITH anomaly_rows AS (
            SELECT
                %s::text AS anomaly_type,
                %s::text AS proposed_action,
                split_part(s.source_concept_id, ',', 1) AS concept_id,
                s.mapping_id,
                COALESCE(m.mapping_sector, '') AS mapping_sector,
                m.target_variable,
                m.tier,
                m.multiplier,
                m.accounting_standard AS mapping_accounting_standard,
                m.taxonomy_version AS mapping_taxonomy_version,
                d.gics_sector_code AS gics_sector,
                d.gics_industry_group_code AS gics_industry_group,
                {entity_sector} AS entity_sector,
                s.{entity_col} AS entity_id,
                s.fiscal_year,
                s.fiscal_period,
                s.period_end,
                s.filing_id,
                s.line_item_id,
                s.currency,
                f.unit,
                f.taxonomy,
                f.statement_type,
                f.root_id,
                f.parent_id,
                f.pre_parent_id,
                COALESCE(f.concept_path, s.concept_path) AS concept_path,
                te.label AS label_en,
                NULL::text AS label_ja,
                te.documentation AS description
            FROM {std_table} s
            JOIN map_concept_to_taxonomy_versioned m ON m.mapping_id = s.mapping_id
            JOIN {dim_table} d ON d.{entity_col} = s.{entity_col}
            LEFT JOIN {raw_table} f
              ON f.{entity_col} = s.{entity_col}
             AND f.concept_id = split_part(s.source_concept_id, ',', 1)
             AND f.fiscal_period = s.fiscal_period
             AND f.period_end = s.period_end
            -- NB: do NOT join on f.fiscal_year = s.fiscal_year. The std table's
            -- fiscal_year is period-aligned (period_end.year) while the raw
            -- column is the filing year; for early-FYE filers' comparative
            -- periods they differ and the equality would silently null the raw
            -- enrichment. (entity, concept, fiscal_period, period_end) already
            -- pins the raw row.
            LEFT JOIN LATERAL (
                SELECT label, documentation
                FROM ref_taxonomy_element rte
                WHERE rte.concept_id = split_part(s.source_concept_id, ',', 1)
                ORDER BY rte.taxonomy_year DESC NULLS LAST
                LIMIT 1
            ) te ON TRUE
            {extra_join}
            WHERE s.source_concept_id IS NOT NULL
              AND s.mapping_id IS NOT NULL
              AND {where_sql}
            {row_limit_sql}
        )
        SELECT
            anomaly_type,
            proposed_action,
            concept_id,
            mapping_id,
            mapping_sector,
            MAX(target_variable) AS target_variable,
            MAX(tier) AS tier,
            MAX(multiplier) AS multiplier,
            MAX(mapping_accounting_standard) AS mapping_accounting_standard,
            MAX(mapping_taxonomy_version) AS mapping_taxonomy_version,
            (array_agg(DISTINCT gics_sector) FILTER (WHERE gics_sector IS NOT NULL))[1] AS gics_sector,
            (array_agg(DISTINCT gics_industry_group) FILTER (WHERE gics_industry_group IS NOT NULL))[1] AS gics_industry_group,
            (array_agg(DISTINCT entity_sector) FILTER (WHERE entity_sector IS NOT NULL))[1:8] AS entity_sectors,
            MIN(fiscal_year) AS fiscal_year_min,
            MAX(fiscal_year) AS fiscal_year_max,
            (array_agg(DISTINCT statement_type) FILTER (WHERE statement_type IS NOT NULL))[1:12] AS statement_types,
            (array_agg(DISTINCT taxonomy) FILTER (WHERE taxonomy IS NOT NULL))[1:12] AS taxonomies,
            ARRAY[%s::text] AS accounting_standards,
            (array_agg(DISTINCT COALESCE(currency, unit)) FILTER (WHERE COALESCE(currency, unit) IS NOT NULL))[1:12] AS units,
            (array_agg(DISTINCT root_id) FILTER (WHERE root_id IS NOT NULL))[1:12] AS root_ids,
            (array_agg(DISTINCT COALESCE(parent_id, pre_parent_id)) FILTER (WHERE COALESCE(parent_id, pre_parent_id) IS NOT NULL))[1:12] AS parent_ids,
            (array_agg(DISTINCT concept_path) FILTER (WHERE concept_path IS NOT NULL))[1:12] AS concept_paths,
            COUNT(*)::bigint AS fact_count,
            COUNT(DISTINCT filing_id)::bigint AS filing_count,
            COUNT(DISTINCT entity_id)::integer AS reporter_count,
            MIN(period_end) AS first_period_end,
            MAX(period_end) AS last_period_end,
            (array_agg(DISTINCT entity_id) FILTER (WHERE entity_id IS NOT NULL))[1:12] AS sample_entities,
            (array_agg(DISTINCT filing_id) FILTER (WHERE filing_id IS NOT NULL))[1:12] AS sample_filings,
            MAX(label_en) AS label_en,
            MAX(label_ja) AS label_ja,
            MAX(description) AS description,
            (array_agg(DISTINCT line_item_id) FILTER (WHERE line_item_id IS NOT NULL))[1:12] AS line_items,
            (array_agg(DISTINCT fiscal_period) FILTER (WHERE fiscal_period IS NOT NULL))[1:8] AS fiscal_periods
        FROM anomaly_rows
        GROUP BY anomaly_type, proposed_action, concept_id, mapping_id, mapping_sector
        ORDER BY COUNT(*) DESC, COUNT(DISTINCT entity_id) DESC, concept_id
    """


def _identity_sql(cfg: dict[str, str], row_limit: int | None = None) -> str:
    std_table = cfg["std_table"]
    entity_col = cfg["entity_col"]
    extra_join = f"""
        JOIN (
            SELECT v.entity_id, v.jurisdiction, v.fiscal_year, v.fiscal_period, ci.line_item_id
            FROM ref_std_identity_violation v
            JOIN (
                SELECT check_id, lhs_item_id AS line_item_id
                FROM ref_std_identity_check
                UNION ALL
                SELECT check_id, unnest(rhs_item_ids) AS line_item_id
                FROM ref_std_identity_check
            ) ci ON ci.check_id = v.check_id
            WHERE v.jurisdiction = %s
        ) iv
          ON iv.entity_id = s.{entity_col}
         AND iv.fiscal_year = s.fiscal_year
         AND iv.fiscal_period = s.fiscal_period
         AND iv.line_item_id = s.line_item_id
    """
    return _base_select_sql(
        cfg,
        "identity_failure",
        "sign_fix",
        "TRUE",
        extra_join=extra_join,
        row_limit=row_limit,
    )


def _sector_mismatch_sql(cfg: dict[str, str], row_limit: int | None = None) -> str:
    entity_sector = _entity_sector_expr("d")
    where = f"""
        (
            ({entity_sector}) = 'bank_financial'
            AND COALESCE(m.mapping_sector, '') NOT IN ('bank_financial', 'bank', '')
        )
        OR (
            ({entity_sector}) IN ('insurance', 'reit', 'asset_manager_other_financial', 'non_bank_financial')
            AND COALESCE(m.mapping_sector, '') = 'corp'
        )
    """
    return _base_select_sql(cfg, "sector_mismatch", "sector_scope", where, row_limit=row_limit)


def _accounting_mismatch_sql(cfg: dict[str, str], row_limit: int | None = None) -> str:
    standard = cfg["standard"]
    where = f"m.accounting_standard IS NOT NULL AND m.accounting_standard <> '{standard}'"
    return _base_select_sql(cfg, "accounting_standard_mismatch", "year_scope", where, row_limit=row_limit)


def _taxonomy_drift_sql(cfg: dict[str, str], row_limit: int | None = None) -> str:
    where = """
        m.taxonomy_version IS NOT NULL
        AND f.taxonomy IS NOT NULL
        AND lower(m.taxonomy_version) <> lower(f.taxonomy)
        AND (
            substring(m.taxonomy_version from '(19[0-9]{2}|20[0-9]{2})') IS NULL
            OR substring(f.taxonomy from '(19[0-9]{2}|20[0-9]{2})') IS NULL
            OR substring(m.taxonomy_version from '(19[0-9]{2}|20[0-9]{2})')
               <> substring(f.taxonomy from '(19[0-9]{2}|20[0-9]{2})')
        )
    """
    return _base_select_sql(cfg, "taxonomy_year_drift", "year_scope", where, row_limit=row_limit)


def _display_incoherence_sql(cfg: dict[str, str], row_limit: int | None = None) -> str:
    if cfg["entity_col"] != "cik":
        return ""
    extra_join = """
        JOIN fact_statement_display_evidence_us de
          ON de.cik = s.cik
         AND de.fiscal_year = s.fiscal_year
         AND de.fiscal_period = s.fiscal_period
         AND de.line_item_id = s.line_item_id
         AND de.mapping_id = s.mapping_id
    """
    where = "de.evidence_quality = 'WEAK' OR de.operating_reconciliation_status = 'FAIL'"
    return _base_select_sql(
        cfg,
        "display_layer_incoherence",
        "supplemental_only",
        where,
        extra_join=extra_join,
        row_limit=row_limit,
    )


def _fetch_anomalies(cur, jurisdiction: str, limit: int | None = None) -> list[dict[str, Any]]:
    cfg = _JURISDICTION[jurisdiction]
    row_limit = max(limit * 500, limit) if limit else None
    queries = [
        (_identity_sql(cfg, row_limit=row_limit), ["identity_failure", "sign_fix", jurisdiction, cfg["standard"]]),
        (_sector_mismatch_sql(cfg, row_limit=row_limit), ["sector_mismatch", "sector_scope", cfg["standard"]]),
        (_accounting_mismatch_sql(cfg, row_limit=row_limit), ["accounting_standard_mismatch", "year_scope", cfg["standard"]]),
        (_taxonomy_drift_sql(cfg, row_limit=row_limit), ["taxonomy_year_drift", "year_scope", cfg["standard"]]),
    ]
    display_sql = _display_incoherence_sql(cfg, row_limit=row_limit)
    if display_sql:
        queries.append((display_sql, ["display_layer_incoherence", "supplemental_only", cfg["standard"]]))

    rows: list[dict[str, Any]] = []
    for sql, params in queries:
        limit_sql = " LIMIT %s" if limit else ""
        query_params = list(params)
        if limit:
            query_params.append(limit)
        cur.execute(sql + limit_sql, query_params)
        cols = [desc[0] for desc in cur.description]
        rows.extend(dict(zip(cols, row)) for row in cur.fetchall())
        if limit and len(rows) >= limit:
            break
    rows.sort(key=lambda row: (int(row.get("fact_count") or 0), int(row.get("reporter_count") or 0)), reverse=True)
    rows = rows[:limit] if limit else rows
    _enrich_identity_attribution(cur, jurisdiction, rows)
    _enrich_context_distribution(cur, jurisdiction, rows)
    for row in rows:
        role, confidence = _role_from_evidence(row)
        row["concept_role"] = role
        row["role_confidence"] = confidence
        row["proposed_action"] = _action_from_role(row)
        row["review_action_type"] = _review_action_type(row)
        row["triage_priority"] = _triage_priority(row)
        row["review_batch"] = _review_batch(row)
    return rows


def _enrich_identity_attribution(cur, jurisdiction: str, rows: list[dict[str, Any]]) -> None:
    identity_rows = [
        row for row in rows
        if row.get("anomaly_type") == "identity_failure" and row.get("mapping_id") is not None
    ]
    if not identity_rows:
        return
    cfg = _JURISDICTION[jurisdiction]
    std_table = cfg["std_table"]
    entity_col = cfg["entity_col"]
    mapping_ids = sorted({int(row["mapping_id"]) for row in identity_rows})
    cur.execute(
        f"""
        WITH check_items AS (
            SELECT check_id, lhs_item_id AS line_item_id, 'lhs'::text AS identity_side, NULL::smallint AS rhs_sign
            FROM ref_std_identity_check
            UNION ALL
            SELECT c.check_id, c.rhs_item_ids[i] AS line_item_id, 'rhs'::text AS identity_side, c.rhs_signs[i] AS rhs_sign
            FROM ref_std_identity_check c, generate_subscripts(c.rhs_item_ids, 1) AS g(i)
        ),
        joined AS (
            SELECT split_part(s.source_concept_id, ',', 1) AS concept_id,
                   s.mapping_id,
                   ci.check_id,
                   ci.identity_side,
                   ci.rhs_sign,
                   s.value AS participant_value,
                   v.delta,
                   CASE
                       WHEN ci.identity_side = 'lhs' THEN v.delta - (2 * s.value)
                       ELSE v.delta + (2 * ci.rhs_sign * s.value)
                   END AS sign_flip_delta,
                   CASE
                       WHEN ci.identity_side = 'rhs' THEN v.delta + (ci.rhs_sign * s.value)
                       ELSE NULL
                   END AS exclude_delta
            FROM {std_table} s
            JOIN check_items ci ON ci.line_item_id = s.line_item_id
            JOIN ref_std_identity_violation v
              ON v.jurisdiction = %s
             AND v.entity_id = s.{entity_col}
             AND v.fiscal_year = s.fiscal_year
             AND v.fiscal_period = s.fiscal_period
             AND v.check_id = ci.check_id
            WHERE s.mapping_id = ANY(%s)
              AND s.source_concept_id IS NOT NULL
        )
        SELECT concept_id,
               mapping_id,
               (array_agg(DISTINCT check_id) FILTER (WHERE check_id IS NOT NULL))[1:12] AS failed_check_ids,
               (array_agg(DISTINCT identity_side) FILTER (WHERE identity_side IS NOT NULL))[1:3] AS identity_sides,
               COUNT(*)::bigint AS identity_fact_count,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY abs(delta)) AS median_abs_delta,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY abs(sign_flip_delta)) AS median_abs_sign_flip_delta,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY abs(exclude_delta))
                   FILTER (WHERE exclude_delta IS NOT NULL) AS median_abs_exclude_delta,
               MAX(abs(delta)) AS max_abs_delta
        FROM joined
        GROUP BY concept_id, mapping_id
        """,
        (jurisdiction, mapping_ids),
    )
    attrs = {(row[0], int(row[1])): row[2:] for row in cur.fetchall()}
    for row in identity_rows:
        key = (row.get("concept_id"), int(row["mapping_id"]))
        value = attrs.get(key)
        if not value:
            continue
        (
            failed_check_ids,
            identity_sides,
            identity_fact_count,
            median_abs_delta,
            median_abs_sign_flip_delta,
            median_abs_exclude_delta,
            max_abs_delta,
        ) = value
        current = _safe_float(median_abs_delta) or 0.0
        candidates = [
            ("sign_flip", _safe_float(median_abs_sign_flip_delta)),
            ("exclude", _safe_float(median_abs_exclude_delta)),
        ]
        candidates = [(name, score) for name, score in candidates if score is not None]
        best_action, best_score = min(candidates, key=lambda item: item[1]) if candidates else ("none", None)
        improvement = ((current - best_score) / current) if current and best_score is not None and best_score < current else 0.0
        row.update({
            "failed_check_ids": failed_check_ids or [],
            "identity_sides": identity_sides or [],
            "identity_fact_count": int(identity_fact_count or 0),
            "median_abs_delta": median_abs_delta,
            "median_abs_sign_flip_delta": median_abs_sign_flip_delta,
            "median_abs_exclude_delta": median_abs_exclude_delta,
            "max_abs_delta": max_abs_delta,
            "counterfactual_best_action": best_action,
            "residual_improvement_pct": improvement,
        })


def _enrich_context_distribution(cur, jurisdiction: str, rows: list[dict[str, Any]]) -> None:
    """Attach context-role counts for the selected mapped concept rows."""
    keyed_rows = [
        row for row in rows
        if row.get("concept_id") and row.get("mapping_id") is not None
    ]
    if not keyed_rows:
        return
    cfg = _JURISDICTION[jurisdiction]
    std_table = cfg["std_table"]
    raw_table = cfg["raw_table"]
    entity_col = cfg["entity_col"]
    dimension_expr = cfg["dimension_expr"]
    mapping_ids = sorted({int(row["mapping_id"]) for row in keyed_rows})
    concept_ids = sorted({str(row["concept_id"]) for row in keyed_rows})
    raw_context_join = ""
    if jurisdiction == "JP":
        raw_context_join = "AND f.context_id = COALESCE(NULLIF(s.context_id, ''), f.context_id)"
    context_role_case = _context_role_case_sql(dimension_expr)
    cur.execute(
        f"""
        WITH context_rows AS (
            SELECT split_part(s.source_concept_id, ',', 1) AS concept_id,
                   s.mapping_id,
                   {context_role_case} AS context_role,
                   COUNT(*)::bigint AS role_fact_count
            FROM {std_table} s
            LEFT JOIN {raw_table} f
              ON f.{entity_col} = s.{entity_col}
             AND f.concept_id = split_part(s.source_concept_id, ',', 1)
             AND f.fiscal_period = s.fiscal_period
             AND f.period_end = s.period_end
             -- period_end pins the raw row; joining on the filing-year
             -- fiscal_year would drop comparatives for early-FYE filers.
             {raw_context_join}
            WHERE s.source_concept_id IS NOT NULL
              AND s.mapping_id = ANY(%s)
              AND split_part(s.source_concept_id, ',', 1) = ANY(%s)
            GROUP BY split_part(s.source_concept_id, ',', 1), s.mapping_id, context_role
        )
        SELECT concept_id,
               mapping_id,
               jsonb_object_agg(context_role, role_fact_count ORDER BY context_role) AS context_role_distribution
        FROM context_rows
        GROUP BY concept_id, mapping_id
        """,
        (mapping_ids, concept_ids),
    )
    distributions = {
        (str(concept_id), int(mapping_id)): dict(context_role_distribution or {})
        for concept_id, mapping_id, context_role_distribution in cur.fetchall()
    }
    empty_distribution = {
        "primary_statement": 0,
        "note_disclosure": 0,
        "segment_or_schedule": 0,
        "cash_flow_addback": 0,
        "dimension_heavy": 0,
        "unknown": 0,
    }
    for row in keyed_rows:
        key = (str(row.get("concept_id")), int(row["mapping_id"]))
        distribution = dict(empty_distribution)
        distribution.update(distributions.get(key) or {})
        row["context_role_distribution"] = distribution


def _evidence(row: dict[str, Any], jurisdiction: str) -> dict[str, Any]:
    return {
        "review_class": "mapped_anomaly",
        "anomaly_type": row.get("anomaly_type"),
        "proposed_action": row.get("proposed_action"),
        "review_action_type": row.get("review_action_type"),
        "triage_priority": row.get("triage_priority"),
        "review_batch": row.get("review_batch"),
        "concept_role": row.get("concept_role"),
        "role_confidence": row.get("role_confidence"),
        "context_role_distribution": row.get("context_role_distribution") or {},
        "current_mapping": {
            "mapping_id": row.get("mapping_id"),
            "target_variable": row.get("target_variable"),
            "tier": row.get("tier"),
            "multiplier": str(row.get("multiplier") or ""),
            "mapping_sector": row.get("mapping_sector"),
            "accounting_standard": row.get("mapping_accounting_standard"),
            "taxonomy_version": row.get("mapping_taxonomy_version"),
        },
        "scope": {
            "jurisdiction": jurisdiction,
            "entity_sectors": _limited(row.get("entity_sectors")),
            "gics_sector": row.get("gics_sector"),
            "gics_industry_group": row.get("gics_industry_group"),
            "fiscal_year_min": row.get("fiscal_year_min"),
            "fiscal_year_max": row.get("fiscal_year_max"),
            "fiscal_periods": _limited(row.get("fiscal_periods")),
        },
        "xbrl_evidence": {
            "statement_types": _limited(row.get("statement_types")),
            "taxonomies": _limited(row.get("taxonomies")),
            "accounting_standards": _limited(row.get("accounting_standards")),
            "units": _limited(row.get("units")),
            "root_ids": _limited(row.get("root_ids")),
            "parent_ids": _limited(row.get("parent_ids")),
            "concept_paths": _limited(row.get("concept_paths"), 8),
        },
        "impact": {
            "fact_count": row.get("fact_count"),
            "filing_count": row.get("filing_count"),
            "reporter_count": row.get("reporter_count"),
            "line_items": _limited(row.get("line_items")),
            "sample_entities": _limited(row.get("sample_entities")),
            "sample_filings": _limited(row.get("sample_filings")),
        },
        "identity_attribution": {
            "failed_check_ids": _limited(row.get("failed_check_ids")),
            "identity_sides": _limited(row.get("identity_sides")),
            "identity_fact_count": row.get("identity_fact_count"),
            "median_abs_delta": str(row.get("median_abs_delta") or ""),
            "median_abs_sign_flip_delta": str(row.get("median_abs_sign_flip_delta") or ""),
            "median_abs_exclude_delta": str(row.get("median_abs_exclude_delta") or ""),
            "max_abs_delta": str(row.get("max_abs_delta") or ""),
            "counterfactual_best_action": row.get("counterfactual_best_action"),
            "residual_improvement_pct": row.get("residual_improvement_pct"),
        },
    }


def _queue_tuple(row: dict[str, Any], jurisdiction: str) -> tuple:
    concept_id = str(row["concept_id"])
    namespace = _concept_namespace(concept_id)
    evidence = _evidence(row, jurisdiction)
    anomaly_type = str(row.get("anomaly_type") or "mapped_anomaly")
    return (
        jurisdiction,
        concept_id,
        row.get("mapping_sector") or "",
        "generic",
        row.get("gics_sector"),
        row.get("gics_industry_group"),
        _concept_local_name(concept_id),
        row.get("label_en"),
        row.get("label_ja"),
        row.get("description"),
        [concept_id],
        [namespace] if namespace else [],
        row.get("fiscal_year_min"),
        row.get("fiscal_year_max"),
        row.get("statement_types") or [],
        row.get("taxonomies") or [],
        row.get("accounting_standards") or [],
        row.get("units") or [],
        row.get("root_ids") or [],
        row.get("parent_ids") or [],
        row.get("concept_paths") or [],
        row.get("fact_count") or 0,
        row.get("filing_count") or 0,
        row.get("reporter_count") or 0,
        row.get("first_period_end"),
        row.get("last_period_end"),
        row.get("sample_entities") or [],
        row.get("sample_filings") or [],
        "mapped_anomaly",
        None,
        None,
        None,
        None,
        None,
        None,
        row.get("multiplier") or 1,
        None,
        "queued",
        "NEEDS_MAPPING_REVIEW",
        f"Mapped concept flagged by {anomaly_type}. Review evidence before promoting any mapping change.",
        json.dumps(evidence, ensure_ascii=False, default=str),
        json.dumps([], ensure_ascii=False),
        _PROMPT_VERSION,
        None,
        f"{_SOURCE_PREFIX}:{anomaly_type}",
        row.get("mapping_id"),
        row.get("proposed_action"),
        row.get("concept_role"),
        row.get("role_confidence"),
        row.get("failed_check_ids") or [],
        row.get("identity_sides") or [],
        row.get("residual_improvement_pct"),
        row.get("counterfactual_best_action"),
        json.dumps(row.get("context_role_distribution") or {}, ensure_ascii=False, default=str),
        row.get("review_action_type"),
        row.get("triage_priority"),
        row.get("review_batch"),
    )


def _write_queue(rows: list[dict[str, Any]], jurisdiction: str) -> int:
    data = [_queue_tuple(row, jurisdiction) for row in rows if row.get("concept_id")]
    if not data:
        return 0
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO map_concept_to_taxonomy_review_queue (
                jurisdiction, normalized_concept_id, mapping_sector, gics_scope,
                gics_sector, gics_industry_group, local_name, label_en, label_ja,
                description, source_concept_ids, namespaces, fiscal_year_min,
                fiscal_year_max, statement_types, taxonomies, accounting_standards,
                units, root_ids, parent_ids, concept_paths, fact_count, filing_count,
                reporter_count, first_period_end, last_period_end, sample_entities,
                sample_filings, review_class, suggested_target_variable,
                top_candidate_label, top_candidate_description,
                top_candidate_category, top_candidate_unit_type, suggested_tier,
                suggested_multiplier, confidence, review_status, decision,
                reasoning, evidence, candidate_targets, prompt_version, model_name,
                mapping_source, current_mapping_id, proposed_action,
                concept_role, role_confidence, failed_check_ids, identity_sides,
                residual_improvement_pct, counterfactual_best_action,
                context_role_distribution, review_action_type, triage_priority,
                review_batch
            )
            VALUES %s
            ON CONFLICT (
                jurisdiction,
                normalized_concept_id,
                mapping_sector,
                gics_scope,
                COALESCE(gics_sector, ''),
                COALESCE(gics_industry_group, ''),
                COALESCE(mapping_source, ''),
                COALESCE(prompt_version, '')
            )
            DO UPDATE SET
                local_name = EXCLUDED.local_name,
                label_en = EXCLUDED.label_en,
                label_ja = EXCLUDED.label_ja,
                description = EXCLUDED.description,
                source_concept_ids = EXCLUDED.source_concept_ids,
                namespaces = EXCLUDED.namespaces,
                fiscal_year_min = EXCLUDED.fiscal_year_min,
                fiscal_year_max = EXCLUDED.fiscal_year_max,
                statement_types = EXCLUDED.statement_types,
                taxonomies = EXCLUDED.taxonomies,
                accounting_standards = EXCLUDED.accounting_standards,
                units = EXCLUDED.units,
                root_ids = EXCLUDED.root_ids,
                parent_ids = EXCLUDED.parent_ids,
                concept_paths = EXCLUDED.concept_paths,
                fact_count = EXCLUDED.fact_count,
                filing_count = EXCLUDED.filing_count,
                reporter_count = EXCLUDED.reporter_count,
                first_period_end = EXCLUDED.first_period_end,
                last_period_end = EXCLUDED.last_period_end,
                sample_entities = EXCLUDED.sample_entities,
                sample_filings = EXCLUDED.sample_filings,
                review_class = EXCLUDED.review_class,
                suggested_target_variable = EXCLUDED.suggested_target_variable,
                suggested_tier = EXCLUDED.suggested_tier,
                suggested_multiplier = EXCLUDED.suggested_multiplier,
                confidence = EXCLUDED.confidence,
                review_status = EXCLUDED.review_status,
                decision = EXCLUDED.decision,
                reasoning = EXCLUDED.reasoning,
                evidence = EXCLUDED.evidence,
                candidate_targets = EXCLUDED.candidate_targets,
                model_name = EXCLUDED.model_name,
                current_mapping_id = EXCLUDED.current_mapping_id,
                proposed_action = EXCLUDED.proposed_action,
                concept_role = EXCLUDED.concept_role,
                role_confidence = EXCLUDED.role_confidence,
                failed_check_ids = EXCLUDED.failed_check_ids,
                identity_sides = EXCLUDED.identity_sides,
                residual_improvement_pct = EXCLUDED.residual_improvement_pct,
                counterfactual_best_action = EXCLUDED.counterfactual_best_action,
                context_role_distribution = EXCLUDED.context_role_distribution,
                review_action_type = EXCLUDED.review_action_type,
                triage_priority = EXCLUDED.triage_priority,
                review_batch = EXCLUDED.review_batch,
                updated_at = now()
            WHERE map_concept_to_taxonomy_review_queue.review_status = 'queued'
            """,
            data,
            page_size=1000,
        )


def reset_mapping_health_review_queue(jurisdiction: str, include_v2: bool = True) -> int:
    """Delete unreviewed generated mapping-health rows for a clean rebuild."""
    prefixes = list(_OLD_SOURCE_PREFIXES)
    if include_v2:
        prefixes.append(_SOURCE_PREFIX)
    patterns = [f"{prefix}:%" for prefix in prefixes]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM map_concept_to_taxonomy_review_queue
            WHERE jurisdiction = %s
              AND review_class = 'mapped_anomaly'
              AND review_status = 'queued'
              AND mapping_source LIKE ANY(%s)
            """,
            (jurisdiction, patterns),
        )
        return cur.rowcount


def build_mapping_health_review_queue(
    jurisdiction: str,
    limit: int | None = None,
    dry_run: bool = False,
    reset_existing: bool = False,
) -> dict[str, int]:
    """Queue mapped-concept anomaly review packets for one jurisdiction."""
    jurisdiction = jurisdiction.upper()
    if jurisdiction not in _JURISDICTION:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")

    ctx = start_run(jurisdiction, "mapping_health", "validate" if dry_run else "incremental")
    try:
        reset_count = 0
        if reset_existing and not dry_run:
            reset_count = reset_mapping_health_review_queue(jurisdiction)
        with connect() as conn, conn.cursor() as cur:
            rows = _fetch_anomalies(cur, jurisdiction, limit=limit)
        by_type: dict[str, int] = {}
        for row in rows:
            key = str(row.get("anomaly_type") or "unknown")
            by_type[key] = by_type.get(key, 0) + 1
        written = 0 if dry_run else _write_queue(rows, jurisdiction)
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=written)
        out = {"selected": len(rows), "queued": written, "reset": reset_count}
        out.update({f"type_{key}": value for key, value in sorted(by_type.items())})
        return out
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
