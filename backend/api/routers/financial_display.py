from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query

from ..db import acquire
from ..models.financials import (
    Direction,
    FinancialDisplayProvenance,
    FinancialDisplayResponse,
    FinancialDisplayRow,
    FinancialDisplaySection,
    FinancialDisplaySource,
    FinancialDisplayVisibility,
    Period,
)


router = APIRouter()


@dataclass(frozen=True)
class DisplaySpec:
    source_id: str
    source_type: FinancialDisplaySource
    section: str
    priority: int
    display_role: str
    label: str | None = None
    unit_type: str | None = None
    visibility: FinancialDisplayVisibility = "default"
    tooltip: str = ""


@dataclass(frozen=True)
class ConceptDisplayPolicy:
    action_type: str
    proposed_action: str
    visibility: FinancialDisplayVisibility | None
    source_rank_penalty: int
    flags: tuple[str, ...]


SECTION_META: dict[str, tuple[str, str, int]] = {
    "key_metrics": ("Key Metrics", "Analytics first: the fastest read on the company.", 10),
    "growth_profitability": ("Growth And Profitability", "Margins, returns, and growth rates.", 12),
    "cash_generation": ("Cash Generation", "Operating cash flow, reinvestment, and free cash flow.", 10),
    "balance_sheet_strength": ("Balance Sheet Strength", "Liquidity, leverage, capital, and asset scale.", 12),
    "capital_structure": ("Capital Structure", "Debt, equity, and shareholder capital support rows.", 10),
    "statement_summary": ("Curated Statement Summary", "High-level standardized line items only.", 14),
    "diagnostics": ("Diagnostics", "Mapping gaps and audit support rows.", 8),
}


_SOURCE_POLICY_RANK = {
    "RAW": 0,
    "MARKET": 0,
    "T2_SUM": 1,
    "T2_COMPONENT": 1,
    "DERIVED_BOTTOM_UP": 2,
    "DERIVED_PARTIAL": 3,
    "RESIDUAL": 4,
}


def _concept_key(source_concept_id: object) -> str:
    return str(source_concept_id or "").split(",", 1)[0].strip()


def _visibility_rank(visibility: FinancialDisplayVisibility | None) -> int:
    if visibility == "audit_only":
        return 3
    if visibility == "supplemental":
        return 2
    if visibility == "default":
        return 1
    return 0


def _merge_visibility(
    current: FinancialDisplayVisibility,
    candidate: FinancialDisplayVisibility | None,
) -> FinancialDisplayVisibility:
    if candidate is None:
        return current
    return candidate if _visibility_rank(candidate) > _visibility_rank(current) else current


def _policy_from_action(
    action_type: str | None,
    proposed_action: str | None = None,
    concept_role: str | None = None,
) -> ConceptDisplayPolicy | None:
    action = str(action_type or "")
    proposed = str(proposed_action or "")
    role = str(concept_role or "")
    if action == "alternate_total_fallback":
        return ConceptDisplayPolicy(
            action_type=action,
            proposed_action=proposed or "alternate_total",
            visibility=None,
            source_rank_penalty=50,
            flags=("source_policy_alternate_total_fallback",),
        )
    if action == "display_supplemental_only":
        return ConceptDisplayPolicy(
            action_type=action,
            proposed_action=proposed or "supplemental_only",
            visibility="supplemental",
            source_rank_penalty=80,
            flags=("source_policy_supplemental_only",),
        )
    if action == "component_only":
        return ConceptDisplayPolicy(
            action_type=action,
            proposed_action=proposed or "component_scope",
            visibility="supplemental",
            source_rank_penalty=25,
            flags=("source_policy_component_only",),
        )
    if action == "sector_mapping_split":
        if role == "disclosure_only":
            return ConceptDisplayPolicy(
                action_type=action,
                proposed_action=proposed or "sector_scope",
                visibility="supplemental",
                source_rank_penalty=80,
                flags=("source_policy_sector_disclosure_supplemental",),
            )
        return ConceptDisplayPolicy(
            action_type=action,
            proposed_action=proposed or "sector_scope",
            visibility=None,
            source_rank_penalty=20,
            flags=("source_policy_sector_review",),
        )
    return None


def _policy_from_policy_row(
    policy_action: str | None,
    default_visibility: str | None,
    source_rank_penalty: object,
    reason_code: str | None,
) -> ConceptDisplayPolicy | None:
    action = str(policy_action or "")
    if not action:
        return None
    visibility_raw = str(default_visibility or "default")
    visibility: FinancialDisplayVisibility | None
    if visibility_raw == "default":
        visibility = None
    elif visibility_raw in {"supplemental", "audit_only"}:
        visibility = visibility_raw  # type: ignore[assignment]
    elif visibility_raw == "hidden":
        visibility = "audit_only"
    else:
        visibility = None
    try:
        penalty = int(source_rank_penalty or 0)
    except Exception:
        penalty = 0
    flags = [f"concept_policy_{action}"]
    if reason_code:
        flags.append(f"concept_policy_reason_{reason_code}")
    return ConceptDisplayPolicy(
        action_type=action,
        proposed_action=action,
        visibility=visibility,
        source_rank_penalty=penalty,
        flags=tuple(flags),
    )


def _select_display_line_rows(
    rows: list,
    policies: dict[tuple[str, str], ConceptDisplayPolicy],
    *,
    full: bool,
) -> tuple[list, dict[str, set[str]], dict[str, FinancialDisplayVisibility]]:
    """Select one fact per line item/year, using concept-health as display policy."""
    candidates: dict[tuple[str, int], list[tuple[tuple, object, ConceptDisplayPolicy | None]]] = {}
    for row in rows:
        lid = str(row["line_item_id"])
        year = int(row["fiscal_year"])
        concept = _concept_key(row.get("source_concept_id"))
        policy = policies.get((lid, concept)) or policies.get(("", concept))
        if policy and policy.visibility in {"supplemental", "audit_only"} and not full:
            continue
        metric_rank = _SOURCE_POLICY_RANK.get(str(row.get("metric_type") or "").upper(), 5)
        period_end = row.get("period_end")
        filed_date = row.get("filed_date")
        sort_key = (
            policy.source_rank_penalty if policy else 0,
            metric_rank,
            -(period_end.toordinal() if hasattr(period_end, "toordinal") else 0),
            -(filed_date.toordinal() if hasattr(filed_date, "toordinal") else 0),
            str(row.get("source_concept_id") or ""),
        )
        candidates.setdefault((lid, year), []).append((sort_key, row, policy))

    selected: list = []
    quality_flags: dict[str, set[str]] = {}
    visibility_by_line: dict[str, FinancialDisplayVisibility] = {}
    for (_lid, _year), row_candidates in sorted(candidates.items()):
        row_candidates.sort(key=lambda item: item[0])
        _, row, policy = row_candidates[0]
        selected.append(row)
        lid = str(row["line_item_id"])
        if policy:
            quality_flags.setdefault(lid, set()).update(policy.flags)
            if policy.visibility:
                current = visibility_by_line.get(lid, "default")
                visibility_by_line[lid] = _merge_visibility(current, policy.visibility)
    return selected, quality_flags, visibility_by_line



COMMON_METRIC_SPECS = [
    DisplaySpec("revenue_growth_year_over_year", "metric", "key_metrics", 100, "metric"),
    DisplaySpec("revenue_compound_annual_growth_rate_5_year", "metric", "key_metrics", 110, "metric"),
    DisplaySpec("gross_margin", "metric", "key_metrics", 120, "metric"),
    DisplaySpec("operating_margin", "metric", "key_metrics", 130, "metric"),
    DisplaySpec("net_profit_margin", "metric", "key_metrics", 140, "metric"),
    DisplaySpec("return_on_equity", "metric", "key_metrics", 150, "metric"),
    DisplaySpec("return_on_assets", "metric", "key_metrics", 160, "metric"),
    DisplaySpec(
        "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
        "metric",
        "key_metrics",
        170,
        "metric",
        label="EV / EBITDA",
    ),
    DisplaySpec("revenue_growth_year_over_year", "metric", "growth_profitability", 200, "metric"),
    DisplaySpec("gross_profit_growth_year_over_year", "metric", "growth_profitability", 210, "metric"),
    DisplaySpec("free_cash_flow_growth_year_over_year", "metric", "growth_profitability", 220, "metric"),
    DisplaySpec("earnings_before_interest_taxes_depreciation_amortization_margin", "metric", "growth_profitability", 230, "metric"),
    DisplaySpec("gross_margin", "metric", "growth_profitability", 240, "metric"),
    DisplaySpec("operating_margin", "metric", "growth_profitability", 250, "metric"),
    DisplaySpec("net_profit_margin", "metric", "growth_profitability", 260, "metric"),
    DisplaySpec("free_cash_flow_yield", "metric", "cash_generation", 300, "metric"),
    DisplaySpec("cash_flow_from_operations_yield", "metric", "cash_generation", 310, "metric"),
    DisplaySpec("net_debt_to_earnings_before_interest_taxes_depreciation_amortization", "metric", "balance_sheet_strength", 400, "metric"),
    DisplaySpec("total_financial_debt_to_equity", "metric", "balance_sheet_strength", 410, "metric"),
    DisplaySpec("cash_ratio", "metric", "balance_sheet_strength", 420, "metric"),
]

BANK_METRIC_SPECS = [
    DisplaySpec("return_on_equity", "metric", "key_metrics", 100, "metric"),
    DisplaySpec("return_on_assets", "metric", "key_metrics", 110, "metric"),
    DisplaySpec("net_interest_margin", "metric", "key_metrics", 120, "metric"),
    DisplaySpec("loan_to_deposit_ratio", "metric", "key_metrics", 130, "metric"),
    DisplaySpec("nonperforming_loan_ratio", "metric", "key_metrics", 140, "metric"),
    DisplaySpec("common_equity_tier1_ratio_metric", "metric", "key_metrics", 150, "metric", label="CET1 Ratio"),
]

INSURANCE_METRIC_SPECS = [
    DisplaySpec("premium_growth_rate", "metric", "key_metrics", 100, "metric"),
    DisplaySpec("combined_ratio", "metric", "key_metrics", 110, "metric"),
    DisplaySpec("underwriting_profit_margin", "metric", "key_metrics", 120, "metric"),
    DisplaySpec("return_on_equity", "metric", "key_metrics", 130, "metric"),
    DisplaySpec("return_on_assets", "metric", "key_metrics", 140, "metric"),
]

REAL_ESTATE_METRIC_SPECS = [
    DisplaySpec("funds_from_operations_to_debt", "metric", "key_metrics", 100, "metric"),
    DisplaySpec(
        "net_debt_to_earnings_before_interest_taxes_depreciation_amortization_real_estate",
        "metric",
        "key_metrics",
        110,
        "metric",
        label="Net Debt / EBITDA",
    ),
    DisplaySpec("free_cash_flow_yield", "metric", "key_metrics", 120, "metric"),
    DisplaySpec("return_on_equity", "metric", "key_metrics", 130, "metric"),
]

ASSET_MANAGER_METRIC_SPECS = [
    DisplaySpec("return_on_equity", "metric", "key_metrics", 100, "metric"),
    DisplaySpec("operating_margin", "metric", "key_metrics", 110, "metric"),
    DisplaySpec("net_profit_margin", "metric", "key_metrics", 120, "metric"),
    DisplaySpec("free_cash_flow_yield", "metric", "key_metrics", 130, "metric"),
]

COMMON_LINE_ITEM_SPECS = [
    DisplaySpec("revenue", "line_item", "statement_summary", 1000, "high_level_line_item"),
    DisplaySpec("gross_profit", "line_item", "statement_summary", 1010, "high_level_line_item"),
    DisplaySpec("earnings_before_interest_taxes", "line_item", "statement_summary", 1020, "high_level_line_item", label="EBIT / Operating Income"),
    DisplaySpec("net_income", "line_item", "statement_summary", 1030, "high_level_line_item"),
    DisplaySpec("cash_flow_from_operations", "line_item", "cash_generation", 1100, "high_level_line_item"),
    DisplaySpec("capital_expenditures", "line_item", "cash_generation", 1110, "supporting_line_item"),
    DisplaySpec("free_cash_flow", "derived", "cash_generation", 1120, "derived_line_item"),
    DisplaySpec("cash_and_cash_equivalents", "line_item", "balance_sheet_strength", 1200, "high_level_line_item"),
    DisplaySpec("total_assets", "line_item", "balance_sheet_strength", 1210, "high_level_line_item"),
    DisplaySpec("total_liabilities", "line_item", "balance_sheet_strength", 1220, "high_level_line_item"),
    DisplaySpec("total_equity", "line_item", "balance_sheet_strength", 1230, "high_level_line_item"),
    DisplaySpec("total_financial_debt", "derived", "capital_structure", 1300, "derived_line_item"),
    DisplaySpec("net_debt", "derived", "capital_structure", 1310, "derived_line_item"),
    DisplaySpec("long_term_debt", "line_item", "capital_structure", 1320, "supporting_line_item"),
    DisplaySpec("short_term_debt", "line_item", "capital_structure", 1330, "supporting_line_item"),
]

BANK_LINE_ITEM_SPECS = [
    DisplaySpec("total_net_revenue_bank", "line_item", "statement_summary", 1000, "high_level_line_item", label="Total Net Revenue"),
    DisplaySpec("gross_banking_profit", "line_item", "statement_summary", 1010, "high_level_line_item"),
    DisplaySpec("pre_provision_net_revenue", "line_item", "statement_summary", 1020, "high_level_line_item"),
    DisplaySpec("provision_for_loan_losses", "line_item", "statement_summary", 1030, "supporting_line_item"),
    DisplaySpec("net_income", "line_item", "statement_summary", 1040, "high_level_line_item"),
    DisplaySpec("total_loans_net", "line_item", "balance_sheet_strength", 1100, "high_level_line_item"),
    DisplaySpec("total_deposits", "line_item", "balance_sheet_strength", 1110, "high_level_line_item"),
    DisplaySpec("risk_weighted_assets", "line_item", "balance_sheet_strength", 1120, "supporting_line_item"),
    DisplaySpec("common_equity_tier1_ratio", "line_item", "balance_sheet_strength", 1130, "supporting_line_item", label="CET1 Ratio"),
    DisplaySpec("total_assets", "line_item", "balance_sheet_strength", 1140, "high_level_line_item"),
    DisplaySpec("total_equity", "line_item", "capital_structure", 1200, "high_level_line_item"),
]

INSURANCE_LINE_ITEM_SPECS = [
    DisplaySpec("net_premiums_earned", "line_item", "statement_summary", 1000, "high_level_line_item"),
    DisplaySpec("net_premiums_written", "line_item", "statement_summary", 1010, "supporting_line_item"),
    DisplaySpec("gross_premiums_written", "line_item", "statement_summary", 1020, "supporting_line_item"),
    DisplaySpec("investment_income", "line_item", "statement_summary", 1030, "supporting_line_item"),
    DisplaySpec("net_income", "line_item", "statement_summary", 1040, "high_level_line_item"),
    DisplaySpec("float_invested_assets", "line_item", "balance_sheet_strength", 1100, "high_level_line_item"),
    DisplaySpec("total_assets", "line_item", "balance_sheet_strength", 1110, "high_level_line_item"),
    DisplaySpec("total_equity", "line_item", "capital_structure", 1200, "high_level_line_item"),
]

REAL_ESTATE_LINE_ITEM_SPECS = [
    DisplaySpec("rental_revenue", "line_item", "statement_summary", 1000, "high_level_line_item"),
    DisplaySpec("revenue", "line_item", "statement_summary", 1010, "high_level_line_item"),
    DisplaySpec("cash_flow_from_operations", "line_item", "cash_generation", 1100, "high_level_line_item"),
    DisplaySpec("free_cash_flow", "derived", "cash_generation", 1110, "derived_line_item"),
    DisplaySpec("net_debt", "derived", "balance_sheet_strength", 1200, "derived_line_item"),
    DisplaySpec("total_assets", "line_item", "balance_sheet_strength", 1210, "high_level_line_item"),
    DisplaySpec("total_equity", "line_item", "capital_structure", 1300, "high_level_line_item"),
]

ASSET_MANAGER_LINE_ITEM_SPECS = [
    DisplaySpec("assets_under_management", "line_item", "key_metrics", 100, "operating_metric"),
    DisplaySpec("fee_earning_assets_under_management", "line_item", "key_metrics", 110, "operating_metric"),
    DisplaySpec("management_fee_revenue", "line_item", "statement_summary", 1000, "high_level_line_item"),
    DisplaySpec("performance_fee_revenue", "line_item", "statement_summary", 1010, "supporting_line_item"),
    DisplaySpec("revenue", "line_item", "statement_summary", 1020, "high_level_line_item"),
    DisplaySpec("earnings_before_interest_taxes", "line_item", "statement_summary", 1030, "high_level_line_item", label="EBIT / Operating Income"),
    DisplaySpec("net_income", "line_item", "statement_summary", 1040, "high_level_line_item"),
    DisplaySpec("cash_flow_from_operations", "line_item", "cash_generation", 1100, "high_level_line_item"),
    DisplaySpec("free_cash_flow", "derived", "cash_generation", 1110, "derived_line_item"),
    DisplaySpec("total_assets", "line_item", "balance_sheet_strength", 1200, "high_level_line_item"),
    DisplaySpec("total_equity", "line_item", "capital_structure", 1300, "high_level_line_item"),
]


def _period_filter(alias: str, period: Period) -> str:
    if period == "FY":
        return f"{alias}.fiscal_period IN ('FY','Annual')"
    if period == "H1":
        return f"{alias}.fiscal_period IN ('H1','SemiAnnual','Q')"
    if period == "Q":
        return f"{alias}.fiscal_period IN ('Q1','Q2','Q3','Q4')"
    return f"{alias}.fiscal_period = '{period}'"


def _display_sector(mapping_sector: str | None, gics_industry_group_code: object = None) -> str:
    sector = str(mapping_sector or "corp")
    gics = str(gics_industry_group_code or "")
    if sector == "bank_financial":
        return "bank_financial"
    if sector == "non_bank_financial":
        if gics == "4030":
            return "insurance"
        if gics == "6010":
            return "reit"
        return "asset_manager_other_financial"
    return "corp"


def _metric_specs(sector_scope: str) -> list[DisplaySpec]:
    if sector_scope == "bank_financial":
        return BANK_METRIC_SPECS
    if sector_scope == "insurance":
        return INSURANCE_METRIC_SPECS
    if sector_scope == "reit":
        return REAL_ESTATE_METRIC_SPECS
    if sector_scope == "asset_manager_other_financial":
        return ASSET_MANAGER_METRIC_SPECS
    return COMMON_METRIC_SPECS


def _line_item_specs(sector_scope: str) -> list[DisplaySpec]:
    if sector_scope == "bank_financial":
        return BANK_LINE_ITEM_SPECS
    if sector_scope == "insurance":
        return INSURANCE_LINE_ITEM_SPECS
    if sector_scope == "reit":
        return REAL_ESTATE_LINE_ITEM_SPECS
    if sector_scope == "asset_manager_other_financial":
        return ASSET_MANAGER_LINE_ITEM_SPECS
    return COMMON_LINE_ITEM_SPECS


def _unique_specs(specs: list[DisplaySpec]) -> list[DisplaySpec]:
    seen: set[tuple[str, str, str]] = set()
    out: list[DisplaySpec] = []
    for spec in specs:
        key = (spec.source_type, spec.source_id, spec.section)
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


async def _fetch_profile_specs(
    conn,
    accounting_standard: str,
    sector_scope: str,
) -> list[DisplaySpec]:
    try:
        rows = await conn.fetch(
            """
            SELECT source_type, source_id, display_section, display_role,
                   priority_rank, default_visibility, label_override,
                   unit_type_override, note
            FROM ref_financial_display_profile
            WHERE accounting_standard = $1
              AND sector_scope = $2
              AND default_visibility <> 'hidden'
            ORDER BY display_section, priority_rank, source_type, source_id
            """,
            accounting_standard,
            sector_scope,
        )
    except Exception:
        return []
    specs: list[DisplaySpec] = []
    for row in rows:
        source_type = str(row["source_type"])
        if source_type not in {"metric", "line_item", "derived"}:
            continue
        visibility = str(row["default_visibility"] or "default")
        if visibility not in {"default", "supplemental", "audit_only"}:
            visibility = "default"
        specs.append(
            DisplaySpec(
                source_id=str(row["source_id"]),
                source_type=source_type,  # type: ignore[arg-type]
                section=str(row["display_section"]),
                priority=int(row["priority_rank"] or 9999),
                display_role=str(row["display_role"] or source_type),
                label=str(row["label_override"]) if row["label_override"] else None,
                unit_type=str(row["unit_type_override"]) if row["unit_type_override"] else None,
                visibility=visibility,  # type: ignore[arg-type]
                tooltip=str(row["note"] or ""),
            )
        )
    return specs


def _policy_sector_keys(sector_scope: str) -> set[str]:
    keys = {"", sector_scope}
    if sector_scope in {"insurance", "reit", "asset_manager_other_financial"}:
        keys.add("non_bank_financial")
    return keys


async def _fetch_concept_display_policies(
    conn,
    jurisdiction: Literal["US", "JP"],
    sector_scope: str,
) -> dict[tuple[str, str], ConceptDisplayPolicy]:
    sector_keys = _policy_sector_keys(sector_scope)
    try:
        rows = await conn.fetch(
            """
            SELECT normalized_concept_id,
                   target_variable AS current_target_variable,
                   mapping_sector,
                   policy_action,
                   default_visibility,
                   source_rank_penalty,
                   reason_code,
                   specificity_rank
            FROM vw_concept_target_display_policy_active
            WHERE jurisdiction = $1
            ORDER BY specificity_rank DESC, source_rank_penalty DESC, policy_id DESC
            """,
            jurisdiction,
        )
    except Exception:
        rows = []
    if rows:
        policies: dict[tuple[str, str], ConceptDisplayPolicy] = {}
        for row in rows:
            mapping_sector = str(row["mapping_sector"] or "")
            if mapping_sector not in sector_keys:
                continue
            concept_id = str(row["normalized_concept_id"] or "")
            target = str(row["current_target_variable"] or "")
            if not concept_id:
                continue
            policy = _policy_from_policy_row(
                row["policy_action"],
                row["default_visibility"],
                row["source_rank_penalty"],
                row["reason_code"],
            )
            if not policy:
                continue
            key = (target, concept_id)
            existing = policies.get(key)
            if existing is None or policy.source_rank_penalty > existing.source_rank_penalty:
                policies[key] = policy
            policies.setdefault(("", concept_id), policy)
        return policies

    try:
        rows = await conn.fetch(
            """
            SELECT normalized_concept_id, current_target_variable, mapping_sector,
                   review_action_type, proposed_action, triage_priority,
                   evidence->'concept'->>'concept_role' AS concept_role
            FROM vw_concept_universe_health_triage
            WHERE jurisdiction = $1
              AND health_lane = 'mapped_anomaly'
              AND review_action_type IN (
                  'alternate_total_fallback',
                  'display_supplemental_only',
                  'component_only',
                  'sector_mapping_split'
              )
            """,
            jurisdiction,
        )
    except Exception:
        return {}
    policies: dict[tuple[str, str], ConceptDisplayPolicy] = {}
    for row in rows:
        mapping_sector = str(row["mapping_sector"] or "")
        if mapping_sector not in sector_keys:
            continue
        concept_id = str(row["normalized_concept_id"] or "")
        target = str(row["current_target_variable"] or "")
        if not concept_id:
            continue
        policy = _policy_from_action(
            row["review_action_type"],
            row["proposed_action"],
            row["concept_role"],
        )
        if not policy:
            continue
        key = (target, concept_id)
        existing = policies.get(key)
        if existing is None or policy.source_rank_penalty > existing.source_rank_penalty:
            policies[key] = policy
        policies.setdefault(("", concept_id), policy)
    return policies


def _unit_family(source_id: str, unit_type: str | None) -> str:
    sid = source_id.lower()
    unit = str(unit_type or "").upper()
    if unit in {"PCT", "PERCENT", "PERCENTAGE", "DEC", "DECIMAL"}:
        return "DEC"
    if unit in {"CCY", "CURRENCY", "MONEY", "USD", "JPY", "EUR", "GBP"}:
        return "CCY"
    if unit in {"PER_SHARE", "PERSHARE"}:
        return "PER_SHARE"
    if unit in {"DAYS"}:
        return "DAYS"
    if unit in {"COUNT", "SHARES", "SHARE"}:
        return "COUNT"
    if any(token in sid for token in ("margin", "growth", "yield", "return_on", "tax_rate", "combined_ratio")):
        return "DEC"
    if any(token in sid for token in ("loan_to_deposit", "nonperforming_loan", "common_equity_tier1")):
        return "DEC"
    if any(token in sid for token in ("enterprise_value_to", "price_to", "debt_to", "to_debt")):
        return "RATIO"
    return unit or "CCY"


def _direction(value: float | None) -> Direction:
    if value is None or abs(value) < 1e-9:
        return "neu"
    return "up" if value > 0 else "down"


def _cagr(values: dict[int, Optional[float]], unit_type: str | None = None, source_id: str = "") -> Optional[float]:
    if _unit_family(source_id, unit_type) in {"DEC", "RATIO", "DAYS"}:
        return None
    years = sorted(year for year, value in values.items() if value is not None)
    if len(years) < 2:
        return None
    first = values.get(years[0])
    last = values.get(years[-1])
    if first is None or last is None or first == 0:
        return None
    if (first < 0 < last) or (first > 0 > last):
        return None
    n = years[-1] - years[0]
    if n <= 0:
        return None
    try:
        ratio = abs(last) / abs(first)
        if ratio <= 0:
            return None
        result = ratio ** (1.0 / n) - 1.0
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    return -result if first < 0 and last < 0 else result


def _pct_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _latest_stats(values: dict[int, Optional[float]], unit_type: str, source_id: str) -> tuple[float | None, int | None, float | None, float | None]:
    years = sorted(year for year, value in values.items() if value is not None)
    if not years:
        return None, None, None, None
    latest_year = years[-1]
    latest = values.get(latest_year)
    prior = values.get(years[-2]) if len(years) >= 2 else None
    latest_change = None if latest is None or prior is None else latest - prior
    growth = None
    unit = _unit_family(source_id, unit_type)
    if unit in {"CCY", "COUNT", "PER_SHARE"}:
        growth = _pct_change(latest, prior)
    return latest, latest_year, latest_change, growth


def _series(values_by_id: dict[str, dict[int, Optional[float]]], source_id: str) -> dict[int, Optional[float]]:
    return values_by_id.get(source_id, {})


def _derive_pct_change(source: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    out: dict[int, Optional[float]] = {}
    years = sorted(source)
    for year in years:
        out[year] = _pct_change(source.get(year), source.get(year - 1))
    return out


def _derive_binary(
    left: dict[int, Optional[float]],
    right: dict[int, Optional[float]],
    op: str,
) -> dict[int, Optional[float]]:
    years = sorted(set(left) | set(right))
    out: dict[int, Optional[float]] = {}
    for year in years:
        lhs = left.get(year)
        rhs = right.get(year)
        if lhs is None or rhs is None:
            out[year] = None
        elif op == "add":
            out[year] = lhs + rhs
        elif op == "sub":
            out[year] = lhs - rhs
        else:
            out[year] = _ratio(lhs, rhs)
    return out


def _derive_total_debt(lines: dict[str, dict[int, Optional[float]]]) -> dict[int, Optional[float]]:
    components = [
        lines.get("short_term_debt", {}),
        lines.get("long_term_debt_current_portion", {}),
        lines.get("long_term_debt", {}),
    ]
    years = sorted({year for item in components for year in item})
    out: dict[int, Optional[float]] = {}
    for year in years:
        seen = False
        total = 0.0
        for item in components:
            value = item.get(year)
            if value is None:
                continue
            total += abs(value)
            seen = True
        out[year] = total if seen else None
    return out


def _derived_series(
    source_id: str,
    metrics: dict[str, dict[int, Optional[float]]],
    lines: dict[str, dict[int, Optional[float]]],
) -> tuple[dict[int, Optional[float]], str, list[str]]:
    flags = ["computed_from_standardized_line_items"]
    if source_id == "revenue_growth_year_over_year":
        return _derive_pct_change(_series(lines, "revenue")), "DEC", flags
    if source_id == "gross_profit_growth_year_over_year":
        return _derive_pct_change(_series(lines, "gross_profit")), "DEC", flags
    if source_id == "gross_margin":
        return _derive_binary(_series(lines, "gross_profit"), _series(lines, "revenue"), "div"), "DEC", flags
    if source_id == "operating_margin":
        return _derive_binary(_series(lines, "earnings_before_interest_taxes"), _series(lines, "revenue"), "div"), "DEC", flags
    if source_id == "net_profit_margin":
        return _derive_binary(_series(lines, "net_income"), _series(lines, "revenue"), "div"), "DEC", flags
    if source_id == "return_on_equity":
        return _derive_binary(_series(lines, "net_income"), _series(lines, "total_equity"), "div"), "DEC", flags + ["uses_period_end_balance_proxy"]
    if source_id == "return_on_assets":
        return _derive_binary(_series(lines, "net_income"), _series(lines, "total_assets"), "div"), "DEC", flags + ["uses_period_end_balance_proxy"]
    if source_id == "free_cash_flow":
        return _derive_binary(_series(lines, "cash_flow_from_operations"), _series(lines, "capital_expenditures"), "add"), "CCY", flags
    if source_id == "free_cash_flow_growth_year_over_year":
        fcf = metrics.get("free_cash_flow") or _derived_series("free_cash_flow", metrics, lines)[0]
        return _derive_pct_change(fcf), "DEC", flags
    if source_id == "total_financial_debt":
        existing = _series(lines, "total_financial_debt")
        if any(value is not None for value in existing.values()):
            return existing, "CCY", flags
        return _derive_total_debt(lines), "CCY", flags
    if source_id == "net_debt":
        debt = metrics.get("total_financial_debt") or _derived_series("total_financial_debt", metrics, lines)[0]
        return _derive_binary(debt, _series(lines, "cash_and_cash_equivalents"), "sub"), "CCY", flags
    if source_id == "total_financial_debt_to_equity":
        debt = metrics.get("total_financial_debt") or _derived_series("total_financial_debt", metrics, lines)[0]
        return _derive_binary(debt, _series(lines, "total_equity"), "div"), "RATIO", flags
    if source_id == "loan_to_deposit_ratio":
        return _derive_binary(_series(lines, "total_loans_net"), _series(lines, "total_deposits"), "div"), "DEC", flags
    if source_id == "nonperforming_loan_ratio":
        return _derive_binary(_series(lines, "nonperforming_loans"), _series(lines, "total_loans_net"), "div"), "DEC", flags
    if source_id == "premium_growth_rate":
        base = _series(lines, "net_premiums_earned") or _series(lines, "net_premiums_written")
        return _derive_pct_change(base), "DEC", flags
    return {}, "", []


def _has_values(values: dict[int, Optional[float]]) -> bool:
    return any(value is not None for value in values.values())


def _row(
    *,
    spec: DisplaySpec,
    label: str,
    unit_type: str,
    values: dict[int, Optional[float]],
    provenance: FinancialDisplayProvenance,
    quality_flags: list[str] | None = None,
    tooltip: str = "",
) -> FinancialDisplayRow:
    unit = _unit_family(spec.source_id, spec.unit_type or unit_type)
    latest, latest_year, latest_change, growth = _latest_stats(values, unit, spec.source_id)
    return FinancialDisplayRow(
        row_id=f"{spec.source_type}:{spec.source_id}:{spec.section}",
        source_id=spec.source_id,
        source_type=spec.source_type,
        label=spec.label or label or spec.source_id.replace("_", " ").title(),
        section=spec.section,
        unit_type=unit,
        display_role=spec.display_role,
        priority_rank=spec.priority,
        default_visibility=spec.visibility,
        values={int(year): value for year, value in sorted(values.items())},
        latest_value=latest,
        latest_year=latest_year,
        latest_change=latest_change,
        growth=growth,
        cagr=_cagr(values, unit, spec.source_id),
        trend_direction=_direction(growth if growth is not None else latest_change),
        provenance=provenance,
        quality_flags=quality_flags or [],
        tooltip=tooltip or spec.tooltip,
    )


async def _resolve_context(conn, ticker: str, jurisdiction: Literal["US", "JP"]) -> dict:
    if jurisdiction == "US":
        row = await conn.fetchrow(
            """
            SELECT cik::text AS entity_id,
                   primary_ticker,
                   COALESCE(mapping_sector, 'corp') AS mapping_sector,
                   COALESCE(gics_sector_code, '') AS gics_sector_code,
                   COALESCE(gics_industry_group_code, '') AS gics_industry_group_code
            FROM dim_company_us
            WHERE primary_ticker = $1
            LIMIT 1
            """,
            ticker,
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT edinet_code AS entity_id,
                   primary_ticker,
                   COALESCE(mapping_sector, 'corp') AS mapping_sector,
                   COALESCE(gics_sector_code, '') AS gics_sector_code,
                   COALESCE(gics_industry_group_code, '') AS gics_industry_group_code
            FROM dim_company_jp
            WHERE primary_ticker = $1
            LIMIT 1
            """,
            ticker,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
    return dict(row)


async def _fetch_metric_rows(
    conn,
    ticker: str,
    jurisdiction: Literal["US", "JP"],
    metric_ids: list[str],
    period: Period,
    year_min: int,
    year_max: int,
) -> tuple[dict[str, dict], dict[str, dict[int, Optional[float]]], dict[str, dict[int, str]]]:
    if not metric_ids:
        return {}, {}, {}
    table = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    recon = "fact_metrics_recon_us" if jurisdiction == "US" else "fact_metrics_recon_jp"
    eid_col = "cik" if jurisdiction == "US" else "edinet_code"
    period_filter = _period_filter("m", period)
    rows = await conn.fetch(
        f"""
        SELECT m.metric_id, m.fiscal_year, m.value,
               d.name, d.category, d.unit_type, d.formula_symbolic, d.formula, d.note,
               r.formula_with_values
        FROM {table} m
        LEFT JOIN ref_metric_definitions d ON d.metric_id = m.metric_id
        LEFT JOIN {recon} r
               ON r.ticker = m.ticker
              AND r.{eid_col} = m.{eid_col}
              AND r.fiscal_year = m.fiscal_year
              AND r.fiscal_period = m.fiscal_period
              AND r.metric_id = m.metric_id
        WHERE m.ticker = $1
          AND m.metric_id = ANY($2::text[])
          AND m.fiscal_year BETWEEN $3 AND $4
          AND {period_filter}
        ORDER BY m.metric_id, m.fiscal_year
        """,
        ticker,
        metric_ids,
        year_min,
        year_max,
    )
    if jurisdiction == "US":
        try:
            supplemental = await conn.fetch(
                f"""
                SELECT m.metric_id, m.fiscal_year, m.value,
                       d.name, d.category, d.unit_type, d.formula_symbolic, d.formula, d.note,
                       m.formula_with_values
                FROM fact_metrics_supplemental_us m
                LEFT JOIN ref_metric_definitions d ON d.metric_id = m.metric_id
                WHERE m.ticker = $1
                  AND m.metric_id = ANY($2::text[])
                  AND m.fiscal_year BETWEEN $3 AND $4
                  AND {period_filter}
                ORDER BY m.metric_id, m.fiscal_year
                """,
                ticker,
                metric_ids,
                year_min,
                year_max,
            )
            rows = list(rows) + list(supplemental)
        except Exception:
            pass

    defs: dict[str, dict] = {}
    values: dict[str, dict[int, Optional[float]]] = {}
    formulas: dict[str, dict[int, str]] = {}
    for row in rows:
        mid = str(row["metric_id"])
        defs.setdefault(
            mid,
            {
                "name": row["name"],
                "category": row["category"],
                "unit_type": row["unit_type"],
                "formula": row["formula_symbolic"] or row["formula"],
                "note": row["note"],
            },
        )
        values.setdefault(mid, {})[int(row["fiscal_year"])] = (
            float(row["value"]) if row["value"] is not None else None
        )
        if row["formula_with_values"]:
            formulas.setdefault(mid, {})[int(row["fiscal_year"])] = str(row["formula_with_values"])
    if len(defs) < len(metric_ids):
        def_rows = await conn.fetch(
            """
            SELECT metric_id, name, category, unit_type, formula_symbolic, formula, note
            FROM ref_metric_definitions
            WHERE metric_id = ANY($1::text[])
            """,
            metric_ids,
        )
        for row in def_rows:
            defs.setdefault(
                str(row["metric_id"]),
                {
                    "name": row["name"],
                    "category": row["category"],
                    "unit_type": row["unit_type"],
                    "formula": row["formula_symbolic"] or row["formula"],
                    "note": row["note"],
                },
            )
    return defs, values, formulas


async def _fetch_line_rows(
    conn,
    entity_id: str,
    jurisdiction: Literal["US", "JP"],
    sector_scope: str,
    line_item_ids: list[str],
    period: Period,
    year_min: int,
    year_max: int,
    full: bool,
) -> tuple[
    dict[str, dict],
    dict[str, dict[int, Optional[float]]],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, FinancialDisplayVisibility],
    str,
]:
    if not line_item_ids:
        return {}, {}, {}, {}, {}, "USD" if jurisdiction == "US" else "JPY"
    table = "fact_fundamentals_std_us" if jurisdiction == "US" else "fact_fundamentals_std_jp"
    eid_col = "cik" if jurisdiction == "US" else "edinet_code"
    period_filter = _period_filter("s", period)
    rows = await conn.fetch(
        f"""
        SELECT s.line_item_id, s.fiscal_year, s.value, s.currency, s.metric_type,
               s.period_end, s.filed_date, s.source_concept_id,
               COALESCE(r.label, s.line_item_id) AS label,
               COALESCE(r.unit_type, 'CCY') AS unit_type,
               r.category, r.statement_type
        FROM {table} s
        LEFT JOIN ref_standardized_line_items r ON r.line_item_id = s.line_item_id
        WHERE s.{eid_col} = $1
          AND s.line_item_id = ANY($2::text[])
          AND s.fiscal_year BETWEEN $3 AND $4
          AND {period_filter}
        ORDER BY line_item_id, fiscal_year
        """,
        entity_id,
        line_item_ids,
        year_min,
        year_max,
    )
    policies = await _fetch_concept_display_policies(
        conn,
        jurisdiction,
        sector_scope,
    )
    selected_rows, policy_flags, policy_visibility = _select_display_line_rows(
        [dict(row) for row in rows],
        policies,
        full=full,
    )
    defs: dict[str, dict] = {}
    values: dict[str, dict[int, Optional[float]]] = {}
    metric_types: dict[str, set[str]] = {}
    currency = "USD" if jurisdiction == "US" else "JPY"
    for row in selected_rows:
        lid = str(row["line_item_id"])
        defs.setdefault(
            lid,
            {
                "label": row["label"],
                "unit_type": row["unit_type"],
                "category": row["category"],
                "statement_type": row["statement_type"],
            },
        )
        values.setdefault(lid, {})[int(row["fiscal_year"])] = (
            float(row["value"]) if row["value"] is not None else None
        )
        if row["metric_type"]:
            metric_types.setdefault(lid, set()).add(str(row["metric_type"]))
        if row["currency"]:
            currency = str(row["currency"]).upper()
    if len(defs) < len(line_item_ids):
        def_rows = await conn.fetch(
            """
            SELECT line_item_id, label, unit_type, category, statement_type
            FROM ref_standardized_line_items
            WHERE line_item_id = ANY($1::text[])
            """,
            line_item_ids,
        )
        for row in def_rows:
            defs.setdefault(
                str(row["line_item_id"]),
                {
                    "label": row["label"],
                    "unit_type": row["unit_type"],
                    "category": row["category"],
                    "statement_type": row["statement_type"],
                },
            )
    return defs, values, metric_types, policy_flags, policy_visibility, currency


def _line_provenance(metric_types: set[str] | None) -> tuple[FinancialDisplayProvenance, list[str]]:
    if not metric_types:
        return "reported", []
    upper = {value.upper() for value in metric_types}
    flags: list[str] = []
    if any(value.startswith("DERIVED") for value in upper):
        flags.append("standardized_derived")
    if "RESIDUAL" in upper:
        flags.append("standardized_residual")
    if any(value.startswith("T2") for value in upper):
        flags.append("tier2_mapping")
    if "RAW" in upper and len(upper) == 1:
        return "reported", flags
    if any(value.startswith("DERIVED") for value in upper):
        return "derived", flags
    if "RESIDUAL" in upper:
        return "residual", flags
    return "mixed", flags


@router.get("/{ticker}", response_model=FinancialDisplayResponse)
async def get_financial_display(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    period: Period = Query("FY"),
    year_min: int = Query(...),
    year_max: int = Query(...),
    full: bool = Query(False),
) -> FinancialDisplayResponse:
    if year_min > year_max:
        year_min, year_max = year_max, year_min

    async with acquire() as conn:
        ctx = await _resolve_context(conn, ticker, jurisdiction)
        sector_scope = _display_sector(ctx.get("mapping_sector"), ctx.get("gics_industry_group_code"))
        accounting_standard = "US_GAAP" if jurisdiction == "US" else "JP_GAAP"
        profile_specs = await _fetch_profile_specs(conn, accounting_standard, sector_scope)
        metric_specs = _unique_specs(
            [spec for spec in profile_specs if spec.source_type == "metric"] + _metric_specs(sector_scope)
        )
        line_specs = _unique_specs(
            [spec for spec in profile_specs if spec.source_type != "metric"] + _line_item_specs(sector_scope)
        )
        metric_ids = sorted({spec.source_id for spec in metric_specs})
        line_ids = sorted({spec.source_id for spec in line_specs} | {
            "revenue",
            "gross_profit",
            "earnings_before_interest_taxes",
            "net_income",
            "cash_flow_from_operations",
            "capital_expenditures",
            "cash_and_cash_equivalents",
            "short_term_debt",
            "long_term_debt_current_portion",
            "long_term_debt",
            "total_financial_debt",
            "total_assets",
            "total_equity",
            "total_loans_net",
            "total_deposits",
            "nonperforming_loans",
            "net_premiums_earned",
            "net_premiums_written",
        })
        metric_defs, metric_values, metric_formulas = await _fetch_metric_rows(
            conn, ticker, jurisdiction, metric_ids, period, year_min, year_max
        )
        (
            line_defs,
            line_values,
            line_metric_types,
            line_policy_flags,
            line_policy_visibility,
            currency,
        ) = await _fetch_line_rows(
            conn,
            str(ctx["entity_id"]),
            jurisdiction,
            sector_scope,
            line_ids,
            period,
            year_min,
            year_max,
            full,
        )

    rows: list[FinancialDisplayRow] = []
    diagnostics: list[str] = []
    derived_cache: dict[str, dict[int, Optional[float]]] = {}

    for spec in metric_specs:
        values = metric_values.get(spec.source_id, {})
        provenance: FinancialDisplayProvenance = "computed_metric"
        flags: list[str] = []
        unit_type = str(metric_defs.get(spec.source_id, {}).get("unit_type") or spec.unit_type or "")
        tooltip_parts = []
        if metric_defs.get(spec.source_id, {}).get("formula"):
            tooltip_parts.append(f"Formula: {metric_defs[spec.source_id]['formula']}")
        formulas = metric_formulas.get(spec.source_id)
        if formulas:
            latest_formula_year = max(formulas)
            tooltip_parts.append(f"Latest values: {formulas[latest_formula_year]}")
        if not _has_values(values):
            derived, derived_unit, derived_flags = _derived_series(spec.source_id, derived_cache, line_values)
            if _has_values(derived):
                values = derived
                derived_cache[spec.source_id] = derived
                unit_type = derived_unit or unit_type
                provenance = "derived"
                flags.extend(derived_flags)
        if not _has_values(values):
            continue
        rows.append(
            _row(
                spec=spec,
                label=str(metric_defs.get(spec.source_id, {}).get("name") or spec.label or spec.source_id),
                unit_type=unit_type or "RATIO",
                values=values,
                provenance=provenance,
                quality_flags=flags,
                tooltip="\n".join(tooltip_parts),
            )
        )

    for spec in line_specs:
        if spec.source_type == "derived":
            values = line_values.get(spec.source_id, {})
            provenance: FinancialDisplayProvenance = "reported"
            flags: list[str] = []
            unit_type = str(line_defs.get(spec.source_id, {}).get("unit_type") or spec.unit_type or "CCY")
            if not _has_values(values):
                values, unit_type, flags = _derived_series(spec.source_id, derived_cache, line_values)
                provenance = "derived"
                if _has_values(values):
                    derived_cache[spec.source_id] = values
            else:
                provenance, flags = _line_provenance(line_metric_types.get(spec.source_id))
        else:
            values = line_values.get(spec.source_id, {})
            unit_type = str(line_defs.get(spec.source_id, {}).get("unit_type") or spec.unit_type or "CCY")
            provenance, flags = _line_provenance(line_metric_types.get(spec.source_id))
        if not _has_values(values):
            continue
        flags = list(dict.fromkeys(flags + sorted(line_policy_flags.get(spec.source_id, set()))))
        effective_spec = spec
        visibility = line_policy_visibility.get(spec.source_id)
        if visibility is not None:
            effective_spec = replace(
                spec,
                visibility=_merge_visibility(spec.visibility, visibility),
            )
        rows.append(
            _row(
                spec=effective_spec,
                label=str(line_defs.get(spec.source_id, {}).get("label") or spec.label or spec.source_id),
                unit_type=unit_type,
                values=values,
                provenance=provenance,
                quality_flags=flags,
                tooltip=f"Standardized line item: {spec.source_id}",
            )
        )

    if not rows:
        diagnostics.append("No analytics-led display rows were available for the selected period.")

    sections: list[FinancialDisplaySection] = []
    for section_id, (title, subtitle, max_rows) in SECTION_META.items():
        section_rows = sorted(
            [row for row in rows if row.section == section_id],
            key=lambda row: (row.priority_rank, row.source_type, row.source_id),
        )
        if not full:
            section_rows = [row for row in section_rows if row.default_visibility == "default"][:max_rows]
        if section_rows:
            sections.append(
                FinancialDisplaySection(
                    section_id=section_id,
                    title=title,
                    subtitle=subtitle,
                    max_default_rows=max_rows,
                    rows=section_rows,
                )
            )

    return FinancialDisplayResponse(
        ticker=ticker,
        jurisdiction=jurisdiction,
        period=period,
        currency=currency,
        accounting_standard="US_GAAP" if jurisdiction == "US" else "JP_GAAP",
        sector_scope=sector_scope,
        year_min=year_min,
        year_max=year_max,
        sections=sections,
        diagnostics=diagnostics,
    )
