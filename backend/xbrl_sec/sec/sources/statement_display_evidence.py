"""Build statement display evidence from raw XBRL hierarchy fields.

This module never changes governed concept mappings. It records filing-level
evidence that lets display surfaces decide whether a mapped concept is a clean
waterfall subtotal, an additive-looking component, a supplemental disclosure, or
ambiguous.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


_OPERATING_TOTAL_LINE_ITEMS = frozenset({
    "total_operating_expenses",
})

_OPERATING_COMPONENT_LINE_ITEMS = frozenset({
    "research_and_development_expense",
    "selling_general_and_administrative_expense",
    "depreciation",
    "amortization_of_intangibles",
    "total_depreciation_and_amortization",
    "labor_and_employee_costs",
    "rent_and_lease_expense",
    "restructuring_charges",
    "asset_impairment",
    "stock_based_compensation",
    "other_operating_income_expense_net",
})

_NON_OPERATING_LINE_ITEMS = frozenset({
    "interest_income",
    "interest_expense",
    "net_interest_expense",
    "equity_in_earnings_of_affiliates",
    "foreign_exchange_gain_loss",
    "non_operating_income",
    "total_non_operating_income_expense",
    "earnings_before_taxes",
    "income_tax_provision",
    "net_income",
})

_AUDIT_LINE_ITEMS = (
    sorted(_OPERATING_TOTAL_LINE_ITEMS | _OPERATING_COMPONENT_LINE_ITEMS | _NON_OPERATING_LINE_ITEMS)
)

_OPERATING_MARKERS = (
    "operatingexpense",
    "operatingexpenses",
    "operatingcost",
    "operatingcosts",
    "operatingincome",
    "operatingprofit",
)

_NON_OPERATING_MARKERS = (
    "nonoperating",
    "othernonoperatingincomeexpense",
    "nonoperatingincomeexpense",
    "interestincome",
    "interestexpense",
)

_NATURE_DISCLOSURE_HINTS = (
    "depreciation",
    "amortization",
    "stockbasedcompensation",
    "sharebasedcompensation",
    "labor",
    "employee",
    "rent",
    "lease",
)


def _squash(value: Any) -> str:
    return str(value or "").replace("/", "").replace("_", "").replace(" ", "").lower()


def _text_blob(row: dict[str, Any]) -> str:
    return " ".join(
        _squash(row.get(key))
        for key in (
            "line_item_id",
            "source_concept_id",
            "presentation_parent_id",
            "calculation_parent_id",
            "calculation_root_id",
            "concept_path",
        )
    )


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _classify(row: dict[str, Any]) -> tuple[str, str, str]:
    line_item_id = str(row.get("line_item_id") or "")
    text = _text_blob(row)
    has_presentation = bool(row.get("presentation_parent_id") or row.get("concept_path"))
    has_operating_context = _has_any(text, _OPERATING_MARKERS)
    has_non_operating_context = _has_any(text, _NON_OPERATING_MARKERS)
    local_concept = _squash(str(row.get("source_concept_id") or "").split("/")[-1])

    if line_item_id in _OPERATING_TOTAL_LINE_ITEMS or local_concept in {"operatingexpenses", "operatingexpense"}:
        quality = "STRONG" if has_presentation else "MODERATE"
        return (
            "OPERATING_EXPENSE_TOTAL",
            quality,
            "Mapped/identified as explicit operating expense subtotal.",
        )

    if line_item_id in _OPERATING_COMPONENT_LINE_ITEMS:
        if has_operating_context:
            return (
                "OPERATING_EXPENSE_COMPONENT",
                "STRONG" if has_presentation else "MODERATE",
                "Operating-expense component supported by presentation/concept path.",
            )
        if _has_any(text, _NATURE_DISCLOSURE_HINTS):
            return (
                "NATURE_DISCLOSURE",
                "MODERATE" if has_presentation else "WEAK",
                "Cost nature disclosure; often embedded in COGS/SG&A and not additive by default.",
            )
        return (
            "AMBIGUOUS",
            "WEAK",
            "Operating-cost mapping without clear operating-expense hierarchy support.",
        )

    if line_item_id in _NON_OPERATING_LINE_ITEMS or has_non_operating_context:
        quality = "MODERATE" if has_presentation else "WEAK"
        return (
            "NON_OPERATING_OR_OTHER",
            quality,
            "Below-operating or tax/non-operating line item.",
        )

    return (
        "AMBIGUOUS",
        "WEAK",
        "No display role rule matched.",
    )


def _reconciliation_status(delta: Decimal | None) -> str:
    if delta is None:
        return "MISSING"
    return "PASS" if abs(delta) <= Decimal("1") else "FAIL"


def _load_period_reconciliation(entity_ids: list[str] | None = None) -> dict[tuple[str, int, str], Decimal | None]:
    params: list[Any] = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND s.cik = ANY(%s)"
        params.append(entity_ids)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.cik, s.fiscal_year, s.fiscal_period,
                   MAX(s.value) FILTER (WHERE s.line_item_id = 'gross_profit') AS gross_profit,
                   MAX(s.value) FILTER (WHERE s.line_item_id = 'total_operating_expenses') AS total_operating_expenses,
                   MAX(s.value) FILTER (WHERE s.line_item_id = 'earnings_before_interest_taxes') AS ebit
            FROM fact_fundamentals_std_us s
            JOIN dim_company_us d ON d.cik = s.cik
            WHERE COALESCE(d.mapping_sector, 'corp') = 'corp'
              {entity_filter}
            GROUP BY s.cik, s.fiscal_year, s.fiscal_period
            """,
            params,
        )
        out: dict[tuple[str, int, str], Decimal | None] = {}
        for cik, fiscal_year, fiscal_period, gross_profit, total_opex, ebit in cur.fetchall():
            key = (str(cik), int(fiscal_year), str(fiscal_period))
            if gross_profit is None or total_opex is None or ebit is None:
                out[key] = None
            else:
                out[key] = Decimal(gross_profit) + Decimal(total_opex) - Decimal(ebit)
        return out


def _load_evidence_rows(entity_ids: list[str] | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [_AUDIT_LINE_ITEMS]
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND s.cik = ANY(%s)"
        params.append(entity_ids)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.cik, s.fiscal_year, s.fiscal_period, s.period_end, s.filing_id,
                   s.line_item_id, split_part(s.source_concept_id, ',', 1) AS source_concept_id,
                   COALESCE(te.label, te.label_terse, te.label_verbose, f.concept_id) AS source_concept_label,
                   COALESCE(rli.statement_type, rli.category, f.statement_type, 'income_statement') AS statement_type,
                   COALESCE(d.mapping_sector, 'corp') AS sector_scope,
                   f.pre_parent_id AS presentation_parent_id,
                   f.pre_level AS presentation_level,
                   f.pre_order AS presentation_order,
                   f.pre_position AS presentation_position,
                   f.parent_id AS calculation_parent_id,
                   f.root_id AS calculation_root_id,
                   COALESCE(f.concept_path, s.concept_path) AS concept_path,
                   f.weight,
                   f.effective_weight,
                   s.value,
                   s.currency,
                   s.mapping_id
            FROM fact_fundamentals_std_us s
            JOIN dim_company_us d ON d.cik = s.cik
            JOIN ref_standardized_line_items rli ON rli.line_item_id = s.line_item_id
            LEFT JOIN v_fact_fundamentals_us_latest f
              ON f.cik = s.cik
             AND f.fiscal_period = s.fiscal_period
             AND f.period_end = s.period_end
             AND f.concept_id = split_part(s.source_concept_id, ',', 1)
             -- Do NOT add f.fiscal_year = s.fiscal_year: std fiscal_year is
             -- period-aligned (period_end.year) but the raw column is the filing
             -- year, so for early-FYE filers' comparative periods the equality
             -- would null out the raw presentation/calculation enrichment below.
             -- (cik, concept, fiscal_period, period_end) already identifies the row.
            LEFT JOIN LATERAL (
                SELECT label, label_terse, label_verbose
                FROM ref_taxonomy_element rte
                WHERE rte.concept_id = split_part(s.source_concept_id, ',', 1)
                ORDER BY rte.taxonomy_year DESC NULLS LAST
                LIMIT 1
            ) te ON TRUE
            WHERE COALESCE(d.mapping_sector, 'corp') = 'corp'
              AND COALESCE(rli.statement_type, rli.category) = 'income_statement'
              AND s.source_concept_id IS NOT NULL
              AND s.line_item_id = ANY(%s)
              {entity_filter}
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_us_operating_cost_evidence(entity_ids: list[str] | None = None, full: bool = False) -> dict[str, int]:
    """Populate US corp operating-cost display evidence."""
    ctx = start_run("US", "statement_display_evidence", "full_refresh" if full else "incremental")
    try:
        rows = _load_evidence_rows(entity_ids)
        deltas = _load_period_reconciliation(entity_ids)
        out_rows = []
        for row in rows:
            display_role, evidence_quality, role_reason = _classify(row)
            key = (str(row["cik"]), int(row["fiscal_year"]), str(row["fiscal_period"]))
            delta = deltas.get(key)
            out_rows.append((
                row["cik"],
                "US",
                row["fiscal_year"],
                row["fiscal_period"],
                row["period_end"],
                row["filing_id"] or "",
                row["line_item_id"],
                row["source_concept_id"],
                row["source_concept_label"],
                "income_statement",
                "US_GAAP",
                "corp",
                display_role,
                evidence_quality,
                role_reason,
                row["presentation_parent_id"],
                row["presentation_level"],
                row["presentation_order"],
                row["presentation_position"],
                row["calculation_parent_id"],
                row["calculation_root_id"],
                row["concept_path"],
                row["weight"],
                row["effective_weight"],
                row["value"],
                row["currency"],
                row["mapping_id"],
                delta,
                _reconciliation_status(delta),
            ))

        with connect() as conn, conn.cursor() as cur:
            if full and not entity_ids:
                cur.execute("DELETE FROM fact_statement_display_evidence_us WHERE sector_scope = 'corp'")
            elif entity_ids:
                cur.execute(
                    "DELETE FROM fact_statement_display_evidence_us WHERE sector_scope = 'corp' AND cik = ANY(%s)",
                    (entity_ids,),
                )
            written = execute_values(
                cur,
                """
                INSERT INTO fact_statement_display_evidence_us
                    (cik, jurisdiction, fiscal_year, fiscal_period, period_end, filing_id,
                     line_item_id, source_concept_id, source_concept_label, statement_type,
                     accounting_standard, sector_scope, display_role, evidence_quality,
                     role_reason, presentation_parent_id, presentation_level, presentation_order,
                     presentation_position, calculation_parent_id, calculation_root_id,
                     concept_path, weight, effective_weight, value, currency, mapping_id,
                     operating_reconciliation_delta, operating_reconciliation_status)
                VALUES %s
                ON CONFLICT (cik, fiscal_year, fiscal_period, filing_id, line_item_id, source_concept_id)
                DO UPDATE SET
                    period_end = EXCLUDED.period_end,
                    filing_id = EXCLUDED.filing_id,
                    source_concept_label = EXCLUDED.source_concept_label,
                    statement_type = EXCLUDED.statement_type,
                    accounting_standard = EXCLUDED.accounting_standard,
                    sector_scope = EXCLUDED.sector_scope,
                    display_role = EXCLUDED.display_role,
                    evidence_quality = EXCLUDED.evidence_quality,
                    role_reason = EXCLUDED.role_reason,
                    presentation_parent_id = EXCLUDED.presentation_parent_id,
                    presentation_level = EXCLUDED.presentation_level,
                    presentation_order = EXCLUDED.presentation_order,
                    presentation_position = EXCLUDED.presentation_position,
                    calculation_parent_id = EXCLUDED.calculation_parent_id,
                    calculation_root_id = EXCLUDED.calculation_root_id,
                    concept_path = EXCLUDED.concept_path,
                    weight = EXCLUDED.weight,
                    effective_weight = EXCLUDED.effective_weight,
                    value = EXCLUDED.value,
                    currency = EXCLUDED.currency,
                    mapping_id = EXCLUDED.mapping_id,
                    operating_reconciliation_delta = EXCLUDED.operating_reconciliation_delta,
                    operating_reconciliation_status = EXCLUDED.operating_reconciliation_status,
                    updated_at = now()
                """,
                out_rows,
                page_size=5000,
            )
        counts = {
            "rows_in": len(rows),
            "rows_out": written,
            "operating_expense_total": sum(1 for r in out_rows if r[12] == "OPERATING_EXPENSE_TOTAL"),
            "operating_expense_component": sum(1 for r in out_rows if r[12] == "OPERATING_EXPENSE_COMPONENT"),
            "nature_disclosure": sum(1 for r in out_rows if r[12] == "NATURE_DISCLOSURE"),
            "non_operating_or_other": sum(1 for r in out_rows if r[12] == "NON_OPERATING_OR_OTHER"),
            "ambiguous": sum(1 for r in out_rows if r[12] == "AMBIGUOUS"),
            "reconciliation_fail": sum(1 for r in out_rows if r[28] == "FAIL"),
        }
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=written)
        return counts
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def operating_cost_audit_summary_json(limit: int = 50) -> str:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_concept_id, line_item_id, display_role, evidence_quality,
                   evidence_rows, entity_count, filing_count,
                   reconciliation_pass_rows, reconciliation_fail_rows,
                   common_presentation_parents, sample_concept_paths, role_reasons
            FROM vw_us_corp_operating_cost_audit
            ORDER BY evidence_rows DESC, source_concept_id, line_item_id
            LIMIT %s
            """,
            (limit,),
        )
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return json.dumps(rows, default=str, ensure_ascii=False, indent=2)
