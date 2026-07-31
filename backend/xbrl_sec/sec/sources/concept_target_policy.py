"""Build durable concept-target display/source-selection policies.

This module turns concept-health review rows into enforcement rules. It never
changes production mappings or facts.
"""
from __future__ import annotations

from collections import Counter
from typing import Any
import json
import site

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
from psycopg2.extras import Json

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


_MAPPING_SOURCE = "deterministic_concept_target_policy_v1"

_ACTION_META = {
    "allow_main": ("default", 0),
    "prefer_main": ("default", -10),
    "fallback_only": ("default", 50),
    "component_only": ("supplemental", 60),
    "supplemental_only": ("supplemental", 90),
    "audit_only": ("audit_only", 100),
    "deny_main": ("hidden", 1000),
    "mapping_change_candidate": ("supplemental", 120),
    "needs_review": ("default", 25),
}

_SUPPLEMENTAL_ROLES = {"disclosure_only", "table_member_noise"}
_COMPONENT_ROLES = {"component", "contra_component"}
_AUDIT_ROLES = {"audit_only", "table_member_noise"}
_SUPPRESSIVE_ACTIONS = {"component_only", "supplemental_only", "audit_only", "deny_main"}

_ALLOW_MAIN_EXACT = {
    ("us-gaap/AllocatedShareBasedCompensationExpense", "stock_based_compensation"),
    ("us-gaap/AmortizationOfIntangibleAssets", "amortization_of_intangibles"),
    ("us-gaap/FiniteLivedIntangibleAssetsAmortizationExpense", "amortization_of_intangibles"),
    ("us-gaap/ForeignCurrencyTransactionGainLossBeforeTax", "foreign_exchange_gain_loss"),
    ("us-gaap/GoodwillAndIntangibleAssetImpairment", "asset_impairment"),
    ("us-gaap/ImpairmentOfLongLivedAssetsHeldForUse", "asset_impairment"),
    ("us-gaap/IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "earnings_before_taxes"),
    ("us-gaap/IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments", "earnings_before_taxes"),
    ("us-gaap/IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest", "net_income"),
    ("us-gaap/IncomeLossFromEquityMethodInvestments", "equity_in_earnings_of_affiliates"),
    ("us-gaap/InterestAndDebtExpense", "interest_expense"),
    ("us-gaap/InterestAndDividendIncomeOperating", "interest_income"),
    ("us-gaap/InterestExpense", "interest_expense"),
    ("us-gaap/InterestExpenseDebt", "interest_expense"),
    ("us-gaap/InvestmentIncomeInterest", "interest_income"),
    ("us-gaap/InvestmentIncomeInterestAndDividend", "interest_income"),
    ("us-gaap/InvestmentIncomeNet", "interest_income"),
    ("us-gaap/LaborAndRelatedExpense", "labor_and_employee_costs"),
    ("us-gaap/LeaseAndRentalExpense", "rent_and_lease_expense"),
    ("us-gaap/LeaseCost", "rent_and_lease_expense"),
    ("us-gaap/NetIncomeLoss", "net_income"),
    ("us-gaap/NonoperatingIncomeExpense", "non_operating_income"),
    ("us-gaap/OperatingExpenses", "total_operating_expenses"),
    ("us-gaap/OperatingLeasesRentExpenseNet", "rent_and_lease_expense"),
    ("us-gaap/OtherIncome", "non_operating_income"),
    ("us-gaap/OtherNonoperatingIncomeExpense", "non_operating_income"),
    ("us-gaap/ProfitLoss", "net_income"),
    ("us-gaap/ResearchAndDevelopmentExpense", "research_and_development_expense"),
    ("us-gaap/RestructuringCharges", "restructuring_charges"),
    ("us-gaap/SellingGeneralAndAdministrativeExpense", "selling_general_and_administrative_expense"),
}

_FINAL_REVIEW_EXACT = {
    ("us-gaap/AccrualForEnvironmentalLossContingenciesChargesToExpenseForNewLosses", "restructuring_charges"): (
        "component_only",
        "reviewed_environmental_loss_component",
    ),
    ("us-gaap/AdjustmentsToAdditionalPaidInCapitalShareBasedCompensationStockOptionsRequisiteServicePeriodRecognition", "stock_based_compensation"): (
        "component_only",
        "reviewed_equity_adjustment_component",
    ),
    ("us-gaap/AmortizationOfDeferredCharges", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_amortization_component",
    ),
    ("us-gaap/AmortizationOfLeaseIncentives", "depreciation"): (
        "component_only",
        "reviewed_lease_incentive_component",
    ),
    ("us-gaap/AmortizationOfMortgageServicingRightsMSRs", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_msr_amortization_component",
    ),
    ("us-gaap/AmortizationOfPowerContractsEmissionCredits", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_contract_emission_credit_component",
    ),
    ("us-gaap/BusinessCombinationIntegrationRelatedCosts", "restructuring_charges"): (
        "component_only",
        "reviewed_business_combination_cost_component",
    ),
    ("us-gaap/BusinessExitCosts", "restructuring_charges"): (
        "component_only",
        "reviewed_business_exit_cost_component",
    ),
    ("us-gaap/CapitalizedComputerSoftwareAmortization", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_software_amortization_component",
    ),
    ("us-gaap/CapitalizedComputerSoftwareAmortization1", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_software_amortization_component",
    ),
    ("us-gaap/CapitalizedContractCostAmortization", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_contract_cost_amortization_component",
    ),
    ("us-gaap/CapitalLeasesIncomeStatementAmortizationExpense", "depreciation"): (
        "component_only",
        "reviewed_capital_lease_amortization_component",
    ),
    ("us-gaap/CostOfCoalProductsAndServices", "cost_of_goods_sold"): (
        "allow_main",
        "reviewed_sector_specific_cost_of_goods_sold",
    ),
    ("us-gaap/CostOfServicesDepreciationAndAmortization", "depreciation"): (
        "component_only",
        "reviewed_cost_of_services_depreciation_component",
    ),
    ("us-gaap/DebtorReorganizationItemsLegalAndAdvisoryProfessionalFees", "restructuring_charges"): (
        "component_only",
        "reviewed_reorganization_fee_component",
    ),
    ("us-gaap/DeferredSalesInducementsAmortizationExpense", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_deferred_sales_inducement_component",
    ),
    ("us-gaap/Depletion", "depreciation"): (
        "component_only",
        "reviewed_depletion_component",
    ),
    ("us-gaap/HostingArrangementServiceContractImplementationCostExpenseAmortization", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_hosting_contract_amortization_component",
    ),
    ("us-gaap/ImpairedFinancingReceivableInterestIncomeAccrualMethod", "interest_income"): (
        "supplemental_only",
        "reviewed_financing_receivable_interest_disclosure",
    ),
    ("us-gaap/IncomeTaxExaminationPenaltiesAndInterestExpense", "income_tax_provision"): (
        "component_only",
        "reviewed_tax_exam_penalty_interest_component",
    ),
    ("us-gaap/Investments", "equity_method_investments"): (
        "supplemental_only",
        "reviewed_broad_investments_not_equity_method_headline",
    ),
    ("us-gaap/LeaseDepositLiability", "operating_lease_liability"): (
        "supplemental_only",
        "reviewed_lease_deposit_not_operating_lease_liability_headline",
    ),
    ("us-gaap/LossOnContractTermination", "restructuring_charges"): (
        "component_only",
        "reviewed_contract_termination_loss_component",
    ),
    ("us-gaap/OperatingLeasesIncomeStatementDepreciationExpenseOnPropertySubjectToOrHeldForLease", "depreciation"): (
        "component_only",
        "reviewed_operating_lease_depreciation_component",
    ),
    ("us-gaap/OperatingLeasesRentExpenseContingentRentals", "rent_and_lease_expense"): (
        "component_only",
        "reviewed_contingent_rent_component",
    ),
    ("us-gaap/OtherDeferredCostAmortizationExpense", "amortization_of_intangibles"): (
        "component_only",
        "reviewed_other_deferred_cost_amortization_component",
    ),
    ("us-gaap/RecapitalizationCosts", "restructuring_charges"): (
        "component_only",
        "reviewed_recapitalization_cost_component",
    ),
    ("us-gaap/RestructuringAndRelatedCostCostIncurredToDate", "restructuring_charges"): (
        "supplemental_only",
        "reviewed_restructuring_cost_incurred_to_date_disclosure",
    ),
    ("us-gaap/RestructuringAndRelatedCostCostIncurredToDate1", "restructuring_charges"): (
        "supplemental_only",
        "reviewed_restructuring_cost_incurred_to_date_disclosure",
    ),
    ("us-gaap/RestructuringSettlementAndImpairmentProvisions", "restructuring_charges"): (
        "component_only",
        "reviewed_restructuring_settlement_impairment_component",
    ),
    ("us-gaap/ResultsOfOperationsAccretionOfAssetRetirementObligations", "depreciation"): (
        "component_only",
        "reviewed_asset_retirement_obligation_accretion_component",
    ),
    ("us-gaap/StockGrantedDuringPeriodSharesSharebasedCompensation", "stock_based_compensation"): (
        "audit_only",
        "reviewed_share_count_not_currency_target",
    ),
    ("us-gaap/StockIssuedDuringPeriodSharesEmployeeStockOwnershipPlan", "equity_issuance_proceeds"): (
        "audit_only",
        "reviewed_share_count_not_currency_target",
    ),
    ("us-gaap/StockIssuedDuringPeriodSharesIssuedForServices", "stock_based_compensation"): (
        "audit_only",
        "reviewed_share_count_not_currency_target",
    ),
    ("us-gaap/StockIssuedDuringPeriodValueIssuedForServices", "stock_based_compensation"): (
        "component_only",
        "reviewed_stock_issued_for_services_value_component",
    ),
    ("us-gaap/TreasuryStockCommonShares", "treasury_stock"): (
        "audit_only",
        "reviewed_share_count_not_currency_target",
    ),
    ("us-gaap/WriteOffOfDeferredDebtIssuanceCost", "asset_impairment_addback_cashflow"): (
        "component_only",
        "reviewed_debt_issuance_cost_writeoff_component",
    ),
}

_DETAIL_TERMS = (
    "afterfive",
    "afterone",
    "afterten",
    "averagecostpershare",
    "award",
    "awards",
    "continuousunrealized",
    "expected",
    "fairvalueassumptions",
    "fifthyear",
    "forfeited",
    "forfeitures",
    "futureminimum",
    "grantsinperiod",
    "maturities",
    "nexttwelvemonths",
    "ownershippercentage",
    "pershare",
    "sharespurchased",
    "vestedinperiod",
    "weightedaverage",
)

_SHARE_UNITS = {"share", "shares", "sharesitem", "xbrli:shares"}

_COMPONENT_TERMS = (
    "domestic",
    "foreign",
    "servicecost",
    "definedbenefit",
    "definedcontribution",
    "pension",
    "taxbenefit",
    "valuationallowance",
    "leaseback",
    "deferredgain",
    "transactioncosts",
    "indemnificationassets",
)

_MAPPING_CANDIDATE_TERMS = (
    "receivable",
    "nopardvalue",
    "noparvalue",
    "percentage",
    "shares",
    "stockissued",
    "commonstock",
    "option",
    "options",
)


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def _target_from_evidence(row: dict[str, Any]) -> str:
    target = str(row.get("current_target_variable") or "")
    if target:
        return target
    evidence = row.get("evidence") or {}
    if not isinstance(evidence, dict):
        return ""
    current_mapping = evidence.get("current_mapping")
    if isinstance(current_mapping, dict) and current_mapping.get("target_variable"):
        return str(current_mapping["target_variable"])
    mapped_anomaly = evidence.get("mapped_anomaly")
    if isinstance(mapped_anomaly, dict):
        anomaly_mapping = mapped_anomaly.get("current_mapping")
        if isinstance(anomaly_mapping, dict) and anomaly_mapping.get("target_variable"):
            return str(anomaly_mapping["target_variable"])
    return ""


def _squash(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _has_term(value: str, terms: tuple[str, ...]) -> bool:
    return any(term in value for term in terms)


def _normal_unit(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "")


def _units_from_row(row: dict[str, Any]) -> list[str]:
    units = row.get("units")
    if not units:
        return []
    if isinstance(units, str):
        return [units]
    return [str(unit) for unit in units if unit is not None]


def _target_unit_mismatch_policy(row: dict[str, Any]) -> tuple[str, str] | None:
    target_unit = str(row.get("target_unit_type") or "").strip().upper()
    units = [_normal_unit(unit) for unit in _units_from_row(row)]
    if target_unit == "CCY" and units and all(unit in _SHARE_UNITS for unit in units):
        return "audit_only", "reviewed_target_unit_mismatch_shares_vs_ccy"
    return None


def _refine_needs_review_policy(row: dict[str, Any], target: str) -> tuple[str, str]:
    concept = str(row.get("normalized_concept_id") or "")
    local = _squash(concept.split("/", 1)[-1])
    description = _squash(row.get("description") or "")
    text = f"{local} {description}"
    target_s = str(target or "")
    review_action = str(row.get("review_action_type") or "")

    unit_mismatch = _target_unit_mismatch_policy(row)
    if unit_mismatch is not None:
        return unit_mismatch

    final_review = _FINAL_REVIEW_EXACT.get((concept, target_s))
    if final_review is not None:
        return final_review

    if (concept, target_s) in _ALLOW_MAIN_EXACT:
        return "allow_main", "second_pass_exact_primary_allow"

    if _has_term(text, _DETAIL_TERMS):
        return "supplemental_only", "second_pass_detail_or_disclosure"

    if _has_term(text, _COMPONENT_TERMS):
        return "component_only", "second_pass_component_or_segment"

    if _has_term(text, _MAPPING_CANDIDATE_TERMS):
        return "mapping_change_candidate", "second_pass_mapping_candidate_text_mismatch"

    if review_action == "sector_mapping_split":
        return "mapping_change_candidate", "second_pass_sector_split_mapping_candidate"

    if target_s in {
        "net_income",
        "earnings_before_taxes",
        "interest_expense",
        "interest_income",
        "research_and_development_expense",
        "selling_general_and_administrative_expense",
        "total_operating_expenses",
        "non_operating_income",
        "foreign_exchange_gain_loss",
        "asset_impairment",
        "rent_and_lease_expense",
        "labor_and_employee_costs",
        "stock_based_compensation",
        "equity_in_earnings_of_affiliates",
    }:
        return "allow_main", "second_pass_primary_target_allow"

    return "mapping_change_candidate", "second_pass_unresolved_mapping_candidate"


def policy_action_from_queue_row(row: dict[str, Any]) -> tuple[str, str]:
    """Return (policy_action, reason_code) for one review queue row."""
    review_class = str(row.get("review_class") or "")
    review_status = str(row.get("review_status") or "")
    decision = str(row.get("decision") or "")
    proposed = str(row.get("proposed_action") or "")
    action = str(row.get("review_action_type") or "")
    role = str(row.get("concept_role") or "")

    if review_status == "reviewed" and decision.startswith("KEEP_CURRENT_MAPPING"):
        return "allow_main", "reviewed_keep_current_mapping"
    if review_status == "reviewed" and "DISPLAY_SUPPLEMENTAL" in decision:
        return "supplemental_only", "reviewed_display_supplemental"
    if review_status == "reviewed" and "DISPLAY_COMPONENT" in decision:
        return "component_only", "reviewed_display_component"
    if review_status == "reviewed" and "DISPLAY_AUDIT" in decision:
        return "audit_only", "reviewed_display_audit"

    if action == "audit_only" or proposed == "audit_only":
        return "audit_only", "review_action_audit_only"
    if action == "display_supplemental_only" or proposed == "supplemental_only":
        return "supplemental_only", "review_action_supplemental_only"
    if action == "alternate_total_fallback" or proposed == "alternate_total" or role == "alternate_total":
        return "fallback_only", "review_action_alternate_total_fallback"
    if action == "component_only" or proposed == "component_scope":
        return "component_only", "review_action_component_only"
    if action == "keep" or proposed == "keep":
        return "allow_main", "review_action_keep"

    if review_class == "mapped_clean":
        return "allow_main", "mapped_clean_negative_evidence"
    if review_class == "display_suppressed_candidate":
        if role in _AUDIT_ROLES:
            return "audit_only", "display_suppressed_audit_role"
        return "supplemental_only", "display_suppressed_candidate"
    if review_class == "audit_only":
        return "audit_only", "audit_only_lane"

    if action == "sector_mapping_split":
        if role in _SUPPLEMENTAL_ROLES:
            return "supplemental_only", "sector_split_disclosure_role"
        if role in _COMPONENT_ROLES:
            return "component_only", "sector_split_component_role"
        return "needs_review", "sector_split_primary_review_required"

    if role in _AUDIT_ROLES:
        return "audit_only", "concept_role_audit_only"
    if role in _SUPPLEMENTAL_ROLES:
        return "supplemental_only", "concept_role_supplemental"
    if role in _COMPONENT_ROLES:
        return "component_only", "concept_role_component"

    return "needs_review", "unresolved_review_policy"


def _policy_tuple(row: dict[str, Any]) -> tuple | None:
    concept_id = str(row.get("normalized_concept_id") or "")
    if not concept_id:
        return None
    target = _target_from_evidence(row)
    action, reason = policy_action_from_queue_row(row)
    if action == "needs_review":
        action, reason = _refine_needs_review_policy(row, target)
    if not target and action in {"allow_main", "prefer_main", "fallback_only", "needs_review"}:
        return None
    visibility, penalty = _ACTION_META[action]
    evidence = {
        "source": "concept_health_review_queue",
        "queue_id": row.get("queue_id"),
        "review_class": row.get("review_class"),
        "review_status": row.get("review_status"),
        "decision": row.get("decision"),
        "proposed_action": row.get("proposed_action"),
        "review_action_type": row.get("review_action_type"),
        "concept_role": row.get("concept_role"),
        "target_variable": target,
        "reason_code": reason,
    }
    if isinstance(row.get("evidence"), dict):
        evidence["queue_evidence"] = row["evidence"]
    accounting_standard = _first_text(row.get("accounting_standards"))
    taxonomy_version = _first_text(row.get("taxonomies"))
    return (
        row["jurisdiction"],
        concept_id,
        target,
        row.get("mapping_sector") or "",
        row.get("gics_sector"),
        row.get("gics_industry_group"),
        accounting_standard,
        taxonomy_version,
        row.get("fiscal_year_min"),
        row.get("fiscal_year_max"),
        None,
        action,
        visibility,
        penalty,
        reason,
        _json(evidence),
        row.get("queue_id"),
        _MAPPING_SOURCE,
        "reviewed" if str(row.get("review_status") or "") == "reviewed" else "generated",
    )


def _fetch_queue_rows(cur, jurisdiction: str, include_mapped_clean: bool, limit: int | None) -> list[dict[str, Any]]:
    classes = [
        "mapped_anomaly",
        "display_suppressed_candidate",
        "audit_only",
    ]
    if include_mapped_clean:
        classes.append("mapped_clean")
    limit_sql = f"LIMIT {max(1, int(limit))}" if limit is not None else ""
    cur.execute(
        f"""
        SELECT q.queue_id,
               q.jurisdiction,
               q.normalized_concept_id,
               q.mapping_sector,
               q.gics_sector,
               q.gics_industry_group,
               q.accounting_standards,
               q.taxonomies,
               q.fiscal_year_min,
               q.fiscal_year_max,
               q.review_class,
               q.review_status,
               q.decision,
               q.proposed_action,
               q.review_action_type,
               q.concept_role,
               q.description,
               q.units,
               q.evidence,
               v.current_target_variable,
               r.unit_type AS target_unit_type,
               r.statement_type AS target_statement_type
        FROM map_concept_to_taxonomy_review_queue q
        LEFT JOIN vw_concept_universe_health_triage v ON v.queue_id = q.queue_id
        LEFT JOIN ref_standardized_line_items r
               ON r.line_item_id = v.current_target_variable
        WHERE q.jurisdiction = %s
          AND q.review_class = ANY(%s)
          AND q.review_status IN ('queued', 'reviewed')
          AND q.normalized_concept_id IS NOT NULL
        ORDER BY
          CASE WHEN q.review_status = 'reviewed' THEN 0 ELSE 1 END,
          COALESCE(q.triage_priority, 99),
          q.reporter_count DESC,
          q.fact_count DESC,
          q.queue_id
        {limit_sql}
        """,
        (jurisdiction, classes),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def reset_concept_target_display_policy(jurisdiction: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM concept_target_display_policy
            WHERE jurisdiction = %s
              AND mapping_source = %s
            """,
            (jurisdiction, _MAPPING_SOURCE),
        )
        return int(cur.rowcount or 0)


def _write_policies(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO concept_target_display_policy (
                jurisdiction, normalized_concept_id, target_variable,
                mapping_sector, gics_sector, gics_industry_group,
                accounting_standard, taxonomy_version, fiscal_year_from,
                fiscal_year_to, fiscal_period, policy_action,
                default_visibility, source_rank_penalty, reason_code,
                evidence, source_queue_id, mapping_source, review_status
            )
            VALUES %s
            """,
            rows,
            page_size=1000,
        )


def build_concept_target_display_policy(
    jurisdiction: str,
    *,
    include_mapped_clean: bool = True,
    dry_run: bool = False,
    reset_existing: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    jurisdiction = jurisdiction.upper()
    if jurisdiction not in {"US", "JP"}:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")
    ctx = start_run(jurisdiction, "concept_target_display_policy", "validate" if dry_run else "incremental")
    try:
        with connect() as conn, conn.cursor() as cur:
            queue_rows = _fetch_queue_rows(cur, jurisdiction, include_mapped_clean, limit)
        base_policy_rows: list[tuple] = []
        counts: Counter[str] = Counter()
        for row in queue_rows:
            item = _policy_tuple(row)
            if item is None:
                counts["skipped_no_target"] += 1
                continue
            base_policy_rows.append(item)
        non_suppressive_targets = {
            (item[0], item[1], item[2] or "")
            for item in base_policy_rows
            if item[11] not in _SUPPRESSIVE_ACTIONS
        }
        policy_by_key: dict[tuple, tuple] = {}
        expanded_policy_rows: list[tuple] = []
        for item in base_policy_rows:
            expanded_policy_rows.append(item)
            if item[11] in _SUPPRESSIVE_ACTIONS and item[3] and (item[0], item[1], item[2] or "") not in non_suppressive_targets:
                broad = list(item)
                broad[3] = ""
                broad[8] = None
                broad[9] = None
                broad[10] = None
                expanded_policy_rows.append(tuple(broad))
        for item in expanded_policy_rows:
            key = (
                item[0],
                item[1],
                item[2] or "",
                item[3] or "",
                item[4] or "",
                item[5] or "",
                item[6] or "",
                item[7] or "",
                item[8],
                item[9],
                item[10] or "",
                item[17],
            )
            current = policy_by_key.get(key)
            if current is None:
                policy_by_key[key] = item
                continue
            current_reviewed = current[18] == "reviewed"
            item_reviewed = item[18] == "reviewed"
            if item_reviewed and not current_reviewed:
                policy_by_key[key] = item
            elif item_reviewed == current_reviewed and int(item[13] or 0) > int(current[13] or 0):
                policy_by_key[key] = item
        policy_rows = list(policy_by_key.values())
        for item in policy_rows:
            counts[str(item[11])] += 1
        reset_count = 0
        written = 0
        if not dry_run:
            reset_count = reset_concept_target_display_policy(jurisdiction)
            written = _write_policies(policy_rows)
        finish_run(ctx, "succeeded", rows_in=len(queue_rows), rows_out=written)
        out = {
            "selected": len(queue_rows),
            "policies": len(policy_rows),
            "written": written,
            "reset": reset_count,
        }
        out.update({f"action_{key}": value for key, value in sorted(counts.items())})
        return out
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
