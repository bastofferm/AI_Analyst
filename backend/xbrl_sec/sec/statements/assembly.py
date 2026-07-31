"""Side-by-side financial statement assembly.

This module is intentionally independent from the Dash renderer.  It turns
standardized facts plus display-profile metadata into a stable row contract that
can be compared against the existing dashboard output before any UI switch.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


PROVENANCE_REPORTED = "reported"
PROVENANCE_DERIVED = "derived"
PROVENANCE_RESIDUAL = "residual"
PROVENANCE_DISPLAY_ONLY = "display_only"
PROVENANCE_SUPPLEMENTAL = "supplemental"
PROVENANCE_MAPPING_GAP = "mapping_gap"
PROVENANCE_HIDDEN = "hidden"

_STATEMENT_ALIASES = {
    "BalanceSheet": "balance_sheet",
    "IncomeStatement": "income_statement",
    "CashFlow": "cash_flow_statement",
    "balance_sheet": "balance_sheet",
    "income_statement": "income_statement",
    "cash_flow": "cash_flow_statement",
    "cash_flow_statement": "cash_flow_statement",
}


def normalize_statement_type(statement_type: str) -> str:
    return _STATEMENT_ALIASES.get(str(statement_type), str(statement_type))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _edge_contribution(value: Decimal, sign: int) -> Decimal:
    return -abs(value) if int(sign or 1) < 0 else value


def _metric_provenance(metric_type: str | None, display_policy: str | None = None) -> str:
    mt = str(metric_type or "").upper()
    if mt == "RESIDUAL":
        return PROVENANCE_RESIDUAL
    if mt.startswith("DERIVED") or mt in {"T2_SUM", "T2_COMPONENT"}:
        return PROVENANCE_DERIVED
    if str(display_policy or "").upper() == "SUPPLEMENTAL":
        return PROVENANCE_SUPPLEMENTAL
    if str(display_policy or "").upper() == "HIDE":
        return PROVENANCE_HIDDEN
    return PROVENANCE_REPORTED


def _cagr_eligible(unit: str | None, line_item_id: str | None = None) -> bool:
    unit_s = str(unit or "").strip().upper()
    if unit_s in {"%", "PCT", "PERCENT", "PERCENTAGE", "DEC", "DECIMAL"}:
        return False
    lid = str(line_item_id or "").lower()
    if "growth" in lid or "margin" in lid or "rate" in lid:
        return False
    return True


def _blank_row(profile: dict[str, Any], periods: list[int], provenance: str) -> dict[str, Any]:
    line_item_id = str(profile["line_item_id"])
    return {
        "line_item_id": line_item_id,
        "label": profile.get("label") or line_item_id,
        "unit": profile.get("unit") or profile.get("unit_type") or "",
        "display_role": profile.get("display_role") or "DISCLOSURE",
        "display_policy": profile.get("display_policy") or "MAIN",
        "display_order": profile.get("display_order"),
        "display_parent_id": profile.get("display_parent_id"),
        "indent_level": int(profile.get("indent_level") or 1),
        "item_class": profile.get("item_class"),
        "derivation_policy": profile.get("derivation_policy"),
        "values": {int(y): None for y in periods},
        "_all_values": {int(y): None for y in periods},
        "metric_type_by_year": {},
        "provenance_by_year": {},
        "warnings_by_year": {},
        "source_line_item_ids": [],
        "cagr_eligible": _cagr_eligible(profile.get("unit") or profile.get("unit_type"), line_item_id),
        "row_provenance": provenance,
    }


def _profile_for_fact(row: dict[str, Any]) -> dict[str, Any]:
    line_item_id = str(row.get("line_item_id") or row.get("concept_id") or "")
    return {
        "line_item_id": line_item_id,
        "label": row.get("label") or line_item_id,
        "unit": row.get("unit") or row.get("currency") or "",
        "unit_type": row.get("unit_type"),
        "item_class": row.get("item_class"),
        "derivation_policy": row.get("derivation_policy"),
        "display_role": "DISCLOSURE",
        "display_policy": "SUPPLEMENTAL",
        "display_order": row.get("display_order"),
        "display_parent_id": None,
        "indent_level": 1,
    }


def _row_from_fact(row: dict[str, Any], profile: dict[str, Any], periods: list[int]) -> dict[str, Any]:
    out = _blank_row(profile, periods, _metric_provenance(row.get("metric_type"), profile.get("display_policy")))
    out["label"] = profile.get("label") or row.get("label") or out["line_item_id"]
    out["unit"] = row.get("unit") or row.get("currency") or profile.get("unit") or ""
    out["unit_type"] = row.get("unit_type") or profile.get("unit_type") or profile.get("unit")
    out["source_line_item_ids"] = [out["line_item_id"]]
    values = row.get("values") or {}
    metric_types = row.get("metric_type_by_year") or {}
    for year_key, raw_value in values.items():
        try:
            year_int = int(year_key)
        except Exception:
            continue
        out["_all_values"][year_int] = _decimal_or_none(raw_value)
    for year in periods:
        value = _decimal_or_none(values.get(year) if year in values else values.get(str(year)))
        out["values"][int(year)] = value
        metric_type = metric_types.get(year) if year in metric_types else metric_types.get(str(year), row.get("metric_type"))
        if value is not None:
            out["metric_type_by_year"][int(year)] = metric_type or row.get("metric_type") or "RAW"
            out["provenance_by_year"][int(year)] = _metric_provenance(metric_type or row.get("metric_type"), profile.get("display_policy"))
    out["cagr_eligible"] = _cagr_eligible(out.get("unit"), out["line_item_id"])
    return out


def _value_for_year(row: dict[str, Any] | None, year: int) -> Decimal | None:
    if not row:
        return None
    values = row.get("values") or {}
    if year in values and values.get(year) is not None:
        return values.get(year)
    all_values = row.get("_all_values") or {}
    return all_values.get(year)


def _set_computed_value(row: dict[str, Any], year: int, value: Decimal, metric_type: str, provenance: str) -> None:
    row.setdefault("_all_values", {})[int(year)] = value
    if int(year) in row.get("values", {}):
        row["values"][int(year)] = value
        row["metric_type_by_year"][int(year)] = metric_type
        row["provenance_by_year"][int(year)] = provenance
    row["row_provenance"] = provenance


def _ensure_profile_row(
    line_item_id: str,
    row_by_id: dict[str, dict[str, Any]],
    output_rows: list[dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> dict[str, Any] | None:
    row = row_by_id.get(line_item_id)
    if row:
        return row
    profile = profile_by_id.get(line_item_id)
    if not profile:
        return None
    row = _blank_row(profile, periods, PROVENANCE_MAPPING_GAP)
    row_by_id[line_item_id] = row
    output_rows.append(row)
    return row


def _pct_change(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


_REQUIRED_BY_STATEMENT = {
    "balance_sheet": {
        "cash_and_cash_equivalents",
        "total_assets",
        "total_liabilities",
        "total_equity",
    },
    "income_statement": {
        "revenue",
        "gross_profit",
        "earnings_before_interest_taxes",
        "earnings_before_taxes",
        "net_income",
    },
    "cash_flow_statement": {
        "cash_flow_from_operations",
        "capital_expenditures",
        "free_cash_flow",
    },
}

_UNSTABLE_NEGATIVE_RESIDUAL_ITEMS = {
    "other_current_assets",
    "other_non_current_assets",
    "other_current_liabilities",
    "other_non_current_liabilities",
}


def _is_required_gap(row: dict[str, Any], statement_type: str) -> bool:
    if str(row.get("display_policy") or "").upper() != "MAIN":
        return False
    line_item_id = str(row.get("line_item_id") or "")
    return line_item_id in _REQUIRED_BY_STATEMENT.get(statement_type, set())


def _is_unstable_negative_residual(row: dict[str, Any]) -> bool:
    line_item_id = str(row.get("line_item_id") or "")
    if line_item_id not in _UNSTABLE_NEGATIVE_RESIDUAL_ITEMS:
        return False
    if str(row.get("row_provenance") or "") != PROVENANCE_RESIDUAL:
        metric_types = {str(value or "").upper() for value in (row.get("metric_type_by_year") or {}).values()}
        if "RESIDUAL" not in metric_types:
            return False
    for value in (row.get("values") or {}).values():
        if value is not None and value < 0:
            return True
    return False


def _apply_display_readiness(row: dict[str, Any]) -> None:
    policy = str(row.get("display_policy") or "MAIN").upper()
    has_values = any(value is not None for value in (row.get("values") or {}).values())
    has_warnings = any(row.get("warnings_by_year") or {})
    unstable_negative_residual = _is_unstable_negative_residual(row)
    if unstable_negative_residual and policy == "MAIN":
        policy = "SUPPLEMENTAL"
        row["display_policy"] = "SUPPLEMENTAL"
    row["display_ready"] = policy != "HIDE" and has_values
    row["diagnostic_only"] = not row["display_ready"]
    reasons: list[str] = []
    if policy == "HIDE":
        reasons.append("hidden_policy")
    if not has_values:
        reasons.append("empty_profile_row")
    if has_warnings:
        reasons.append("has_warnings")
    if unstable_negative_residual:
        reasons.append("unstable_negative_residual")
        quality_flags = list(row.get("quality_flags") or [])
        if "unstable_negative_residual" not in quality_flags:
            quality_flags.append("unstable_negative_residual")
        row["quality_flags"] = quality_flags
    row["diagnostic_reasons"] = reasons


def _years_to_compute(row_by_id: dict[str, dict[str, Any]], periods: list[int]) -> list[int]:
    all_years = sorted({
        int(year)
        for row in row_by_id.values()
        for year, value in (row.get("_all_values") or {}).items()
        if value is not None
    }, reverse=True)
    return sorted(set(periods) | {year - 1 for year in periods if year - 1 in all_years}, reverse=True)


def _apply_bank_income_statement_calculations(
    output_rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> None:
    """Bridge calculations for the bank income statement structure.

    Computes only when the target row is empty; never overwrites reported facts.
    All expense inputs are treated as positive magnitudes via abs() so the
    sign convention is: revenue +, expenses subtracted.
    """
    years = _years_to_compute(row_by_id, periods)

    total_ii = row_by_id.get("total_interest_income")
    total_ie = row_by_id.get("total_interest_expense")
    nii = _ensure_profile_row("net_interest_income", row_by_id, output_rows, profile_by_id, periods)
    if nii and total_ii and total_ie:
        for year in years:
            if _value_for_year(nii, year) is not None:
                continue
            ii = _value_for_year(total_ii, year)
            ie = _value_for_year(total_ie, year)
            if ii is None or ie is None:
                continue
            _set_computed_value(nii, year, ii - abs(ie), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    provision = row_by_id.get("provision_for_loan_losses")
    nii_after = _ensure_profile_row(
        "net_interest_income_after_provision", row_by_id, output_rows, profile_by_id, periods
    )
    if nii_after and nii and provision:
        for year in years:
            if _value_for_year(nii_after, year) is not None:
                continue
            n = _value_for_year(nii, year)
            p = _value_for_year(provision, year)
            if n is None or p is None:
                continue
            _set_computed_value(nii_after, year, n - abs(p), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    non_int_income = row_by_id.get("non_interest_income")
    non_int_expense = row_by_id.get("non_interest_expense")
    ppnr = _ensure_profile_row(
        "pre_provision_net_revenue", row_by_id, output_rows, profile_by_id, periods
    )
    if ppnr and nii and non_int_income and non_int_expense:
        for year in years:
            if _value_for_year(ppnr, year) is not None:
                continue
            n = _value_for_year(nii, year)
            ni = _value_for_year(non_int_income, year)
            ne = _value_for_year(non_int_expense, year)
            if n is None or ni is None or ne is None:
                continue
            _set_computed_value(ppnr, year, n + ni - abs(ne), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    _apply_common_bottom_line(row_by_id, output_rows, profile_by_id, periods, years)


def _apply_insurance_income_statement_calculations(
    output_rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> None:
    """Bridge calculations for the insurance underwriting structure."""
    years = _years_to_compute(row_by_id, periods)

    premiums = row_by_id.get("net_premiums_earned")
    claims = row_by_id.get("claims_and_losses_incurred")
    underwriting_expense = row_by_id.get("insurance_underwriting_expense")
    underwriting_income = _ensure_profile_row(
        "underwriting_income_loss", row_by_id, output_rows, profile_by_id, periods
    )
    if underwriting_income and premiums and claims and underwriting_expense:
        for year in years:
            if _value_for_year(underwriting_income, year) is not None:
                continue
            p = _value_for_year(premiums, year)
            c = _value_for_year(claims, year)
            e = _value_for_year(underwriting_expense, year)
            if p is None or c is None or e is None:
                continue
            _set_computed_value(
                underwriting_income, year, p - abs(c) - abs(e),
                "DERIVED_BRIDGE", PROVENANCE_DERIVED,
            )

    loss_ratio = _ensure_profile_row("loss_ratio", row_by_id, output_rows, profile_by_id, periods)
    if loss_ratio and premiums and claims:
        loss_ratio["unit"] = "%"
        loss_ratio["unit_type"] = "DEC"
        loss_ratio["cagr_eligible"] = False
        for year in years:
            if _value_for_year(loss_ratio, year) is not None:
                continue
            p = _value_for_year(premiums, year)
            c = _value_for_year(claims, year)
            if p is None or c is None or p == 0:
                continue
            _set_computed_value(loss_ratio, year, abs(c) / p, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)

    expense_ratio = _ensure_profile_row(
        "expense_ratio_insurance_underwriting", row_by_id, output_rows, profile_by_id, periods
    )
    if expense_ratio and premiums and underwriting_expense:
        expense_ratio["unit"] = "%"
        expense_ratio["unit_type"] = "DEC"
        expense_ratio["cagr_eligible"] = False
        for year in years:
            if _value_for_year(expense_ratio, year) is not None:
                continue
            p = _value_for_year(premiums, year)
            e = _value_for_year(underwriting_expense, year)
            if p is None or e is None or p == 0:
                continue
            _set_computed_value(expense_ratio, year, abs(e) / p, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)

    combined_ratio = _ensure_profile_row(
        "combined_ratio", row_by_id, output_rows, profile_by_id, periods
    )
    if combined_ratio and loss_ratio and expense_ratio:
        combined_ratio["unit"] = "%"
        combined_ratio["unit_type"] = "DEC"
        combined_ratio["cagr_eligible"] = False
        for year in years:
            if _value_for_year(combined_ratio, year) is not None:
                continue
            lr = _value_for_year(loss_ratio, year)
            er = _value_for_year(expense_ratio, year)
            if lr is None or er is None:
                continue
            _set_computed_value(combined_ratio, year, lr + er, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)

    _apply_common_bottom_line(row_by_id, output_rows, profile_by_id, periods, years)


def _apply_reit_income_statement_calculations(
    output_rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> None:
    """REIT-specific bridges: NOI, FFO, AFFO and their per-share variants."""
    years = _years_to_compute(row_by_id, periods)

    rental = row_by_id.get("rental_revenue")
    prop_opex = row_by_id.get("property_operating_expenses")
    noi = _ensure_profile_row("net_operating_income", row_by_id, output_rows, profile_by_id, periods)
    if noi and rental and prop_opex:
        for year in years:
            if _value_for_year(noi, year) is not None:
                continue
            r = _value_for_year(rental, year)
            e = _value_for_year(prop_opex, year)
            if r is None or e is None:
                continue
            _set_computed_value(noi, year, r - abs(e), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    net_income = row_by_id.get("net_income")
    da = row_by_id.get("total_depreciation_and_amortization")
    ffo = _ensure_profile_row("funds_from_operations", row_by_id, output_rows, profile_by_id, periods)
    if ffo and net_income and da:
        for year in years:
            if _value_for_year(ffo, year) is not None:
                continue
            ni = _value_for_year(net_income, year)
            d = _value_for_year(da, year)
            if ni is None or d is None:
                continue
            # Simplified Nareit FFO: net income + D&A. Real estate gains/losses
            # adjustment intentionally omitted — would need a separate fact.
            _set_computed_value(ffo, year, ni + abs(d), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    capex = row_by_id.get("capital_expenditures")
    affo = _ensure_profile_row(
        "adjusted_funds_from_operations", row_by_id, output_rows, profile_by_id, periods
    )
    if affo and ffo and capex:
        for year in years:
            if _value_for_year(affo, year) is not None:
                continue
            f = _value_for_year(ffo, year)
            c = _value_for_year(capex, year)
            if f is None or c is None:
                continue
            # Simplified AFFO: FFO - capex. Recurring capex only is the
            # textbook definition; we use total capex as a conservative proxy.
            _set_computed_value(affo, year, f - abs(c), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    shares = row_by_id.get("shares_outstanding_basic")
    ffo_ps = _ensure_profile_row(
        "funds_from_operations_per_share", row_by_id, output_rows, profile_by_id, periods
    )
    if ffo_ps and ffo and shares:
        for year in years:
            if _value_for_year(ffo_ps, year) is not None:
                continue
            f = _value_for_year(ffo, year)
            s = _value_for_year(shares, year)
            if f is None or s is None or s == 0:
                continue
            _set_computed_value(ffo_ps, year, f / s, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)

    affo_ps = _ensure_profile_row(
        "adjusted_funds_from_operations_per_share", row_by_id, output_rows, profile_by_id, periods
    )
    if affo_ps and affo and shares:
        for year in years:
            if _value_for_year(affo_ps, year) is not None:
                continue
            a = _value_for_year(affo, year)
            s = _value_for_year(shares, year)
            if a is None or s is None or s == 0:
                continue
            _set_computed_value(affo_ps, year, a / s, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)

    _apply_common_bottom_line(row_by_id, output_rows, profile_by_id, periods, years)


def _apply_asset_manager_income_statement_calculations(
    output_rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> None:
    """Asset manager: fee-related earnings and synthetic revenue from fees."""
    years = _years_to_compute(row_by_id, periods)

    mgmt_fee = row_by_id.get("management_fee_revenue")
    perf_fee = row_by_id.get("performance_fee_revenue")
    revenue = _ensure_profile_row("revenue", row_by_id, output_rows, profile_by_id, periods)
    if revenue and (mgmt_fee or perf_fee):
        for year in years:
            if _value_for_year(revenue, year) is not None:
                continue
            m = _value_for_year(mgmt_fee, year) if mgmt_fee else None
            p = _value_for_year(perf_fee, year) if perf_fee else None
            if m is None and p is None:
                continue
            total = (m or Decimal("0")) + (p or Decimal("0"))
            _set_computed_value(revenue, year, total, "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    opex = row_by_id.get("total_operating_expenses")
    fre = _ensure_profile_row("fee_related_earnings", row_by_id, output_rows, profile_by_id, periods)
    if fre and revenue and opex:
        for year in years:
            if _value_for_year(fre, year) is not None:
                continue
            r = _value_for_year(revenue, year)
            e = _value_for_year(opex, year)
            if r is None or e is None:
                continue
            _set_computed_value(fre, year, r - abs(e), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    _apply_common_bottom_line(row_by_id, output_rows, profile_by_id, periods, years)


def _apply_common_bottom_line(
    row_by_id: dict[str, dict[str, Any]],
    output_rows: list[dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
    years: list[int],
) -> None:
    """Common bottom-line bridges every sector reuses: NI-to-common fallback,
    EPS placeholders, growth rates.
    """
    net_income = row_by_id.get("net_income")
    net_income_common = _ensure_profile_row(
        "net_income_attributable_to_common", row_by_id, output_rows, profile_by_id, periods
    )
    if net_income and net_income_common:
        for year in years:
            if _value_for_year(net_income_common, year) is not None:
                continue
            value = _value_for_year(net_income, year)
            if value is None:
                continue
            _set_computed_value(net_income_common, year, value, "DERIVED_FALLBACK", PROVENANCE_DERIVED)

    growth_sources = {
        "revenue_growth_year_over_year": "revenue",
        "net_income_growth_year_over_year": "net_income_attributable_to_common",
    }
    for growth_id, source_id in growth_sources.items():
        growth_row = _ensure_profile_row(growth_id, row_by_id, output_rows, profile_by_id, periods)
        source_row = row_by_id.get(source_id)
        if source_row is None and growth_id == "net_income_growth_year_over_year":
            source_row = row_by_id.get("net_income")
        if not growth_row or not source_row:
            continue
        growth_row["unit"] = "%"
        growth_row["unit_type"] = "DEC"
        growth_row["cagr_eligible"] = False
        for year in periods:
            if _value_for_year(growth_row, year) is not None:
                continue
            current = _value_for_year(source_row, year)
            prior = _value_for_year(source_row, year - 1)
            if growth_id == "net_income_growth_year_over_year":
                fallback_row = row_by_id.get("net_income")
                current = current if current is not None else _value_for_year(fallback_row, year)
                prior = prior if prior is not None else _value_for_year(fallback_row, year - 1)
            value = _pct_change(current, prior)
            if value is None:
                continue
            _set_computed_value(growth_row, year, value, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)


def _apply_income_statement_display_calculations(
    output_rows: list[dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    profile_by_id: dict[str, dict[str, Any]],
    periods: list[int],
) -> None:
    all_years = sorted({
        int(year)
        for row in row_by_id.values()
        for year, value in (row.get("_all_values") or {}).items()
        if value is not None
    }, reverse=True)
    years_to_compute = sorted(set(periods) | {year - 1 for year in periods if year - 1 in all_years}, reverse=True)

    revenue = row_by_id.get("revenue")
    cost = row_by_id.get("cost_of_goods_sold")
    gross = _ensure_profile_row("gross_profit", row_by_id, output_rows, profile_by_id, periods)
    if gross and revenue and cost:
        for year in years_to_compute:
            rev = _value_for_year(revenue, year)
            cogs = _value_for_year(cost, year)
            if rev is None or cogs is None:
                continue
            _set_computed_value(gross, year, rev - abs(cogs), "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    ebit = row_by_id.get("earnings_before_interest_taxes")
    total_opex = _ensure_profile_row("total_operating_expenses", row_by_id, output_rows, profile_by_id, periods)
    if total_opex and gross and ebit:
        for year in years_to_compute:
            gp = _value_for_year(gross, year)
            op = _value_for_year(ebit, year)
            if gp is None or op is None:
                continue
            _set_computed_value(total_opex, year, op - gp, "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    ebt = row_by_id.get("earnings_before_taxes")
    total_nonop = _ensure_profile_row(
        "total_non_operating_income_expense",
        row_by_id,
        output_rows,
        profile_by_id,
        periods,
    )
    if total_nonop and ebit and ebt:
        for year in years_to_compute:
            if _value_for_year(total_nonop, year) is not None:
                continue
            op = _value_for_year(ebit, year)
            pretax = _value_for_year(ebt, year)
            if op is None or pretax is None:
                continue
            _set_computed_value(total_nonop, year, pretax - op, "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    total_da = _ensure_profile_row(
        "total_depreciation_and_amortization",
        row_by_id,
        output_rows,
        profile_by_id,
        periods,
    )
    da_rows = [
        row_by_id.get("depreciation"),
        row_by_id.get("amortization_of_intangibles"),
    ]
    if total_da:
        for year in years_to_compute:
            if _value_for_year(total_da, year) is not None:
                continue
            da_total = Decimal("0")
            seen_da = False
            for da_row in da_rows:
                da_value = _value_for_year(da_row, year)
                if da_value is None:
                    continue
                da_total += abs(da_value)
                seen_da = True
            if not seen_da:
                continue
            _set_computed_value(total_da, year, da_total, "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    ebitda = _ensure_profile_row(
        "earnings_before_interest_taxes_depreciation_amortization",
        row_by_id,
        output_rows,
        profile_by_id,
        periods,
    )
    da_rows_for_ebitda = [
        row_by_id.get("total_depreciation_and_amortization"),
        row_by_id.get("depreciation"),
        row_by_id.get("amortization_of_intangibles"),
    ]
    if ebitda and ebit:
        for year in years_to_compute:
            if _value_for_year(ebitda, year) is not None:
                continue
            op = _value_for_year(ebit, year)
            if op is None:
                continue
            da_total = Decimal("0")
            seen_da = False
            if da_rows_for_ebitda[0] is not None and _value_for_year(da_rows_for_ebitda[0], year) is not None:
                da_total = abs(_value_for_year(da_rows_for_ebitda[0], year))
                seen_da = True
            else:
                for da_row in da_rows_for_ebitda[1:]:
                    da_value = _value_for_year(da_row, year)
                    if da_value is None:
                        continue
                    da_total += abs(da_value)
                    seen_da = True
            if not seen_da:
                continue
            _set_computed_value(ebitda, year, op + da_total, "DERIVED_BRIDGE", PROVENANCE_DERIVED)

    net_income = row_by_id.get("net_income")
    net_income_common = _ensure_profile_row(
        "net_income_attributable_to_common",
        row_by_id,
        output_rows,
        profile_by_id,
        periods,
    )
    if net_income and net_income_common:
        for year in years_to_compute:
            if _value_for_year(net_income_common, year) is not None:
                continue
            value = _value_for_year(net_income, year)
            if value is None:
                continue
            _set_computed_value(net_income_common, year, value, "DERIVED_FALLBACK", PROVENANCE_DERIVED)

    growth_sources = {
        "revenue_growth_year_over_year": "revenue",
        "net_income_growth_year_over_year": "net_income_attributable_to_common",
        "earnings_before_interest_taxes_growth_year_over_year": "earnings_before_interest_taxes",
        "earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year": (
            "earnings_before_interest_taxes_depreciation_amortization"
        ),
    }
    for growth_id, source_id in growth_sources.items():
        growth_row = _ensure_profile_row(growth_id, row_by_id, output_rows, profile_by_id, periods)
        source_row = row_by_id.get(source_id)
        if source_row is None and growth_id == "net_income_growth_year_over_year":
            source_row = row_by_id.get("net_income")
        if not growth_row or not source_row:
            continue
        growth_row["unit"] = "%"
        growth_row["unit_type"] = "DEC"
        growth_row["cagr_eligible"] = False
        for year in periods:
            if growth_id == "earnings_before_interest_taxes_growth_year_over_year" and year - 1 not in periods:
                continue
            if _value_for_year(growth_row, year) is not None:
                continue
            current = _value_for_year(source_row, year)
            prior = _value_for_year(source_row, year - 1)
            if growth_id == "net_income_growth_year_over_year":
                fallback_row = row_by_id.get("net_income")
                current = current if current is not None else _value_for_year(fallback_row, year)
                prior = prior if prior is not None else _value_for_year(fallback_row, year - 1)
            value = _pct_change(current, prior)
            if value is None:
                continue
            _set_computed_value(growth_row, year, value, "DISPLAY_CALC", PROVENANCE_DISPLAY_ONLY)


_SECTOR_IS_CALCULATIONS: dict[str, Any] = {
    "corp": _apply_income_statement_display_calculations,
    "bank_financial": _apply_bank_income_statement_calculations,
    "insurance": _apply_insurance_income_statement_calculations,
    "reit": _apply_reit_income_statement_calculations,
    "asset_manager_other_financial": _apply_asset_manager_income_statement_calculations,
}


def _children_by_parent(edge_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edge_rows:
        children[str(edge["parent_id"])].append(edge)
    for edges in children.values():
        edges.sort(key=lambda item: (item.get("sibling_rank") or 999999, str(item.get("child_id") or "")))
    return children


def _derive_bottom_up(
    line_item_id: str,
    year: int,
    row_by_id: dict[str, dict[str, Any]],
    children: dict[str, list[dict[str, Any]]],
    visiting: set[str],
) -> Decimal | None:
    existing = row_by_id.get(line_item_id)
    if existing:
        value = existing["values"].get(year)
        if value is not None:
            return value
    if line_item_id in visiting:
        return None
    child_edges = children.get(line_item_id) or []
    if not child_edges:
        return None
    visiting.add(line_item_id)
    known_sum = Decimal("0")
    known_count = 0
    for edge in child_edges:
        child_id = str(edge["child_id"])
        child_value = _derive_bottom_up(child_id, year, row_by_id, children, visiting)
        if child_value is None:
            continue
        known_sum += _edge_contribution(child_value, int(edge.get("sign") or 1))
        known_count += 1
    visiting.discard(line_item_id)
    if known_count == len(child_edges) and known_count > 0:
        return known_sum
    return None


def _derive_residual(
    line_item_id: str,
    year: int,
    row_by_id: dict[str, dict[str, Any]],
    edge_rows: list[dict[str, Any]],
) -> Decimal | None:
    for edge in edge_rows:
        if str(edge.get("child_id")) != line_item_id:
            continue
        parent_id = str(edge.get("parent_id"))
        parent = row_by_id.get(parent_id)
        if not parent:
            continue
        parent_value = parent["values"].get(year)
        if parent_value is None:
            continue
        sibling_sum = Decimal("0")
        for sibling in edge_rows:
            if str(sibling.get("parent_id")) != parent_id or str(sibling.get("child_id")) == line_item_id:
                continue
            sibling_row = row_by_id.get(str(sibling.get("child_id")))
            if not sibling_row:
                continue
            sibling_value = sibling_row["values"].get(year)
            if sibling_value is None:
                continue
            sibling_sum += _edge_contribution(sibling_value, int(sibling.get("sign") or 1))
        sign = int(edge.get("sign") or 1)
        if sign == 0:
            return None
        residual = parent_value - sibling_sum
        return -abs(residual) if sign < 0 else residual
    return None


def assemble_statement(
    *,
    jurisdiction: str,
    accounting_standard: str,
    sector_scope: str,
    statement_type: str,
    periods: list[int],
    period_ends: dict[int, str] | None,
    std_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]] | None = None,
    display_evidence: list[dict[str, Any]] | None = None,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """Assemble a normalized statement row contract without touching the UI."""
    stmt = normalize_statement_type(statement_type)
    ordered_periods = sorted({int(y) for y in periods}, reverse=True)
    edge_rows = edge_rows or []
    display_evidence = display_evidence or []
    warnings: list[dict[str, Any]] = []

    fact_by_id: dict[str, dict[str, Any]] = {}
    for row in std_rows:
        line_item_id = str(row.get("line_item_id") or row.get("concept_id") or "")
        if not line_item_id:
            continue
        fact_by_id[line_item_id] = row

    profile_by_id = {str(row["line_item_id"]): row for row in profile_rows if row.get("line_item_id")}
    ordered_profiles = sorted(
        profile_rows,
        key=lambda row: (
            row.get("display_order") if row.get("display_order") is not None else 999999,
            str(row.get("line_item_id") or ""),
        ),
    )
    children = _children_by_parent(edge_rows)

    output_rows: list[dict[str, Any]] = []
    row_by_id: dict[str, dict[str, Any]] = {}
    emitted: set[str] = set()

    for profile in ordered_profiles:
        line_item_id = str(profile["line_item_id"])
        policy = str(profile.get("display_policy") or "MAIN").upper()
        if policy == "HIDE" and not include_hidden:
            continue
        fact = fact_by_id.get(line_item_id)
        if fact:
            row = _row_from_fact(fact, profile, ordered_periods)
        else:
            row = _blank_row(profile, ordered_periods, PROVENANCE_MAPPING_GAP)
        output_rows.append(row)
        row_by_id[line_item_id] = row
        emitted.add(line_item_id)

    if stmt == "income_statement":
        sector_calc = _SECTOR_IS_CALCULATIONS.get(sector_scope)
        if sector_calc is not None:
            sector_calc(output_rows, row_by_id, profile_by_id, ordered_periods)

    # Derive missing subtotal values from displayed children when possible.
    for row in output_rows:
        if any(value is not None for value in row["values"].values()):
            continue
        line_item_id = row["line_item_id"]
        for year in ordered_periods:
            derived = _derive_bottom_up(line_item_id, year, row_by_id, children, set())
            if derived is None:
                continue
            row["values"][year] = derived
            row["metric_type_by_year"][year] = "DERIVED_BOTTOM_UP"
            row["provenance_by_year"][year] = PROVENANCE_DERIVED
            row["row_provenance"] = PROVENANCE_DERIVED

    # Derive explicit profile residuals from parent minus known siblings.
    for row in output_rows:
        if str(row.get("display_role") or "").upper() != "RESIDUAL":
            continue
        line_item_id = row["line_item_id"]
        for year in ordered_periods:
            if row["values"].get(year) is not None:
                continue
            residual = _derive_residual(line_item_id, year, row_by_id, edge_rows)
            if residual is None:
                continue
            row["values"][year] = residual
            row["metric_type_by_year"][year] = "RESIDUAL"
            row["provenance_by_year"][year] = PROVENANCE_RESIDUAL
            row["row_provenance"] = PROVENANCE_RESIDUAL

    for row in output_rows:
        if any(value is not None for value in row["values"].values()):
            continue
        if _is_required_gap(row, stmt):
            warning = {
                "line_item_id": row["line_item_id"],
                "severity": "WARN",
                "type": "required_mapping_gap",
                "message": "Required statement row has no reported or derived values.",
            }
            warnings.append(warning)
            for year in ordered_periods:
                row["warnings_by_year"][year] = [warning["type"]]

    # Keep non-profile facts as supplemental diagnostics. This is deliberately
    # visible in the side-by-side output so profile gaps cannot hide facts.
    for line_item_id, fact in sorted(fact_by_id.items()):
        if line_item_id in emitted:
            continue
        profile = _profile_for_fact(fact)
        row = _row_from_fact(fact, profile, ordered_periods)
        row["row_provenance"] = PROVENANCE_SUPPLEMENTAL
        output_rows.append(row)

    evidence_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in display_evidence:
        evidence_by_item[str(evidence.get("line_item_id") or "")].append(evidence)
    for row in output_rows:
        evidence = evidence_by_item.get(row["line_item_id"])
        if evidence:
            row["display_evidence"] = deepcopy(evidence[:5])
        _apply_display_readiness(row)

    return {
        "jurisdiction": jurisdiction,
        "accounting_standard": accounting_standard,
        "sector_scope": sector_scope,
        "statement_type": stmt,
        "profile_key": f"{accounting_standard}/{sector_scope}/{stmt}",
        "periods": ordered_periods,
        "period_ends": {int(k): v for k, v in (period_ends or {}).items()},
        "rows": output_rows,
        "warnings": warnings,
    }


def row_value_as_float(row: dict[str, Any], year: int) -> float | None:
    value = (row.get("values") or {}).get(year)
    if value is None:
        return None
    return float(value)
