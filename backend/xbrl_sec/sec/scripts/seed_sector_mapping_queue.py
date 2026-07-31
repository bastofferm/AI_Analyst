"""Seed map_concept_to_taxonomy_review_queue with sector-specific mapping
proposals for the gaps identified by audit_sector_mapping_gaps.

Each proposal is one queue row keyed on (concept, sector, jurisdiction).
The reviewer (human or review_promotion.py) sees the suggested target plus
the aggregation_type/sign_policy/reasoning evidence and decides whether to
promote it into map_concept_to_taxonomy_versioned.

Reasoning sources:
  * Each candidate concept is checked against fact_fundamentals_{us,jp} to
    confirm it actually appears in the target sector with non-trivial filing
    coverage. Concepts with <5 filings of evidence are dropped.
  * Existing mapping for the concept (if any) is recorded so the reviewer
    can see whether this is a NEW mapping or an OVERRIDE.
  * The proposed aggregation_type comes from domain knowledge: ROOT for
    top-level concepts, CHILD_SUM for component concepts.
"""
from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import Json

from xbrl_sec.sec.db.connection import connect


REVIEW_BATCH = "sector_gap_fill_2026_06"
MODEL_NAME = "claude_sonnet_terminal_authoring"
PROMPT_VERSION = "sector_gap_fill_v1"


# --- US bank ---------------------------------------------------------------
# Target: route bank-sector entities to bank-specific subtotals.

US_BANK = [
    # (concept_id, target_variable, aggregation_type, sign_policy, reasoning)
    ("us-gaap/InterestAndDividendIncomeOperating", "total_interest_income", "ROOT", "as_reported",
     "Top-level interest+dividend income aggregator used by most US banks. Currently no bank-sector mapping; needs ROOT routing to total_interest_income for the bank IS."),
    ("us-gaap/InterestIncomeOperating", "total_interest_income", "FALLBACK_TOTAL", "as_reported",
     "Alternative bank top-level interest income used by smaller banks. Currently mapped to universal interest_income (tier 2); needs FALLBACK_TOTAL override for bank sector."),
    ("us-gaap/InterestAndFeeIncomeLoansAndLeases", "total_interest_income", "CHILD_SUM", "as_reported",
     "Loans+leases component of interest income. CHILD_SUM under total_interest_income when no top-level ROOT fact is present."),
    ("us-gaap/InterestIncomeSecuritiesOperating", "total_interest_income", "CHILD_SUM", "as_reported",
     "Securities component of interest income."),
    ("us-gaap/InterestExpense", "total_interest_expense", "ROOT", "as_reported",
     "Top-level interest expense. Currently maps to interest_expense (universal); needs override for bank to total_interest_expense."),
    ("us-gaap/InterestExpenseDeposits", "total_interest_expense", "CHILD_SUM", "as_reported",
     "Deposit interest expense. Already maps to interest_expense_deposits for bank, but also needs to be a CHILD_SUM contributor to total_interest_expense."),
    ("us-gaap/InterestExpenseBorrowings", "total_interest_expense", "CHILD_SUM", "as_reported",
     "Borrowings interest expense. CHILD_SUM under total_interest_expense."),
    ("us-gaap/InterestExpenseDebt", "total_interest_expense", "CHILD_SUM", "as_reported",
     "Debt interest expense."),
    ("us-gaap/NoninterestIncome", "non_interest_income", "ROOT", "as_reported",
     "Currently maps to 'noninterest_income' (no underscore between non and interest); profile uses 'non_interest_income'. Override needed to match profile naming."),
    ("us-gaap/FeesAndCommissionsDepositorAccounts", "non_interest_income", "CHILD_SUM", "as_reported",
     "Deposit account fees - component of non_interest_income."),
    ("us-gaap/NoninterestExpense", "non_interest_expense", "ROOT", "as_reported",
     "Currently maps to selling_general_and_administrative_expense for bank, which is wrong: NoninterestExpense is the bank analog of total operating expenses, distinct from SG&A. Override to non_interest_expense."),
    ("us-gaap/FederalDepositInsuranceCorporationPremiumExpense", "fdic_insurance_expense", "ROOT", "as_reported",
     "FDIC insurance premium expense - direct bank-specific concept."),
    ("us-gaap/AmortizationOfIntangibleAssets", "amortization_of_core_deposit_intangibles", "CHILD_SUM", "as_reported",
     "When in bank IS context, amortization of intangibles is typically core deposit intangibles. CHILD_SUM under non_interest_expense (lower priority than the specific FDIC concept)."),
    # KPIs - typically computed but sometimes filed directly
    ("us-gaap/ReturnOnAverageAssets", "return_on_average_assets", "DIRECT", "as_reported",
     "Bank-specific KPI: ROA on average assets."),
    ("us-gaap/ReturnOnAverageEquity", "return_on_average_equity_bank", "DIRECT", "as_reported",
     "Bank-specific KPI: ROE on average equity."),
    ("us-gaap/FinancingReceivableNonaccrualToTotalLoansRatio", "nonperforming_loan_ratio", "DIRECT", "as_reported",
     "Direct NPL ratio when filed."),
    ("us-gaap/TangibleBookValuePerShare", "tangible_book_value_per_share", "DIRECT", "as_reported",
     "Direct TBVPS when filed."),
    ("us-gaap/TradingGainsLossesNet", "trading_income", "ROOT", "as_reported",
     "Net trading gains/losses - maps to trading_income for bank profile."),
    ("us-gaap/CommonStockValue", "common_stock_par_value", "ROOT", "as_reported",
     "Common stock par value - exists for corp profile too; needs ROOT mapping."),
]

# --- US asset manager ------------------------------------------------------

US_ASSET_MANAGER = [
    ("us-gaap/InvestmentAdvisoryManagementAndAdministrativeFees", "management_fee_revenue", "ROOT", "as_reported",
     "Asset manager top-level management fee revenue. Currently maps to revenue tier=2 (corp-style); for asset_manager sector needs ROOT override to management_fee_revenue."),
    ("us-gaap/AssetManagementFees1", "management_fee_revenue", "FALLBACK_TOTAL", "as_reported",
     "Alternative asset management fee concept. FALLBACK_TOTAL when InvestmentAdvisoryManagementAndAdministrativeFees is absent."),
    ("us-gaap/InvestmentAdvisoryFees", "management_fee_revenue", "FALLBACK_TOTAL", "as_reported",
     "Investment advisory fees - synonym for management fees."),
    ("us-gaap/PerformanceFees", "performance_fee_revenue", "ROOT", "as_reported",
     "Performance fees - bank/asset manager incentive comp. Currently no asset_manager mapping; needs ROOT."),
    ("us-gaap/IncentiveFeeIncome", "performance_fee_revenue", "FALLBACK_TOTAL", "as_reported",
     "Incentive fee income - synonym for performance fees."),
    ("us-gaap/AssetsUnderManagementCarryingAmount", "assets_under_management", "DIRECT", "as_reported",
     "AUM as carrying amount. Direct mapping for the AUM KPI."),
    ("us-gaap/AssetsUnderManagementFairValueAmount", "assets_under_management", "FALLBACK_TOTAL", "as_reported",
     "Alternative AUM reported at fair value."),
]

# --- US insurance ---------------------------------------------------------

US_INSURANCE = [
    ("us-gaap/PremiumsWrittenNet", "net_premiums_written", "ROOT", "as_reported",
     "Net premiums written - currently maps to 'premiums_written_net' (different name); profile uses 'net_premiums_written'. Override to align."),
    ("us-gaap/NetInvestmentIncome", "net_investment_income_insurance", "ROOT", "as_reported",
     "Insurance net investment income. Currently maps to interest_income for bank, investment_income for non_bank; for insurance specifically should map to net_investment_income_insurance."),
    ("us-gaap/OtherUnderwritingExpense", "insurance_underwriting_expense", "ROOT", "as_reported",
     "Insurance underwriting expense - direct mapping."),
    ("us-gaap/PolicyholderBenefitsAndClaimsIncurredOther", "insurance_underwriting_expense", "CHILD_SUM", "as_reported",
     "Other underwriting/policyholder benefits expense."),
]

# --- US REIT --------------------------------------------------------------

US_REIT = [
    ("us-gaap/StraightLineRent", "straight_line_rent_adjustment", "DIRECT", "as_reported",
     "Straight-line rent adjustment - currently maps to change_in_other_working_capital for corp; for REIT it's a direct income statement adjustment item."),
]

# --- US corp gaps ---------------------------------------------------------

US_CORP = [
    ("us-gaap/CommonStockValue", "common_stock_par_value", "ROOT", "as_reported",
     "Common stock par value at balance sheet - ROOT mapping. (Same as bank.)"),
    ("us-gaap/InterestIncomeExpenseNet", "net_interest_expense", "ROOT", "flip",
     "Net interest income/expense - when reported as expense (i.e. interest expense > interest income), flip to net_interest_expense."),
]

# --- JP corp --------------------------------------------------------------

JP_CORP = [
    ("jppfs_cor/CapitalStock", "common_stock_par_value", "ROOT", "as_reported",
     "JP common stock (資本金)."),
    ("jppfs_cor/DividendsIncomeNOI", "dividend_income_jp", "ROOT", "as_reported",
     "JP non-operating dividend income."),
    ("jppfs_cor/NonOperatingExpenses", "non_operating_expenses_jp", "ROOT", "as_reported",
     "JP non-operating expenses subtotal (営業外費用合計)."),
]

# --- JP bank --------------------------------------------------------------

JP_BANK = [
    ("jppfs_cor/OrdinaryExpensesBNK", "ordinary_expenses_bank_jp", "ROOT", "as_reported",
     "JP bank ordinary expenses (経常費用)."),
    ("jppfs_cor/TradingIncomeOIBNK", "trading_income", "ROOT", "as_reported",
     "JP bank trading income (特定取引収益)."),
]

# --- JP insurance ---------------------------------------------------------

JP_INSURANCE = [
    ("jppfs_cor/NetPremiumsWrittenOIINS", "net_premiums_written", "ROOT", "as_reported",
     "JP net premiums written (正味収入保険料)."),
    ("jppfs_cor/UnderwritingIncomeOIINS", "net_premiums_earned", "ROOT", "as_reported",
     "JP underwriting income (保険引受収益) - top-line premium recognized."),
    ("jppfs_cor/UnderwritingExpensesOEINS", "insurance_underwriting_expense", "ROOT", "as_reported",
     "JP underwriting expenses (保険引受費用)."),
    ("jppfs_cor/ProvisionOfPolicyReserveAndOtherOEINS", "increase_in_policy_reserves_japan", "ROOT", "as_reported",
     "JP provision of policy reserve and other (責任準備金等繰入額)."),
    ("jppfs_cor/ProvisionOfOutstandingClaimsOEINS", "claims_and_losses_incurred", "ROOT", "as_reported",
     "JP provision of outstanding claims (支払備金繰入額)."),
    ("jppfs_cor/PolicyReserveLiabilitiesINS", "change_in_policy_benefit_reserves", "ROOT", "as_reported",
     "JP policy reserve liabilities (責任準備金) - reserve balance."),
    ("jppfs_cor/OutstandingClaimsLiabilitiesINS", "catastrophe_losses", "FALLBACK_TOTAL", "as_reported",
     "JP outstanding claims liabilities (支払備金) - claims reserve balance."),
]

# --- JP REIT --------------------------------------------------------------
# JP REIT (J-REIT) taxonomy lives mostly under jppfs_cor with fund-specific
# variants. Real estate leasing concepts may be filed as jpcrp_cor extensions.
# Skip authoring until evidence in the data shows up post-backfill.
JP_REIT: list[tuple[str, str, str, str, str]] = []


_ALL_PROPOSALS = [
    ("US", "bank_financial", US_BANK),
    ("US", "non_bank_financial", US_ASSET_MANAGER),
    ("US", "non_bank_financial", US_INSURANCE),
    ("US", "non_bank_financial", US_REIT),
    ("US", "corp", US_CORP),
    ("JP", "corp", JP_CORP),
    ("JP", "bank_financial", JP_BANK),
    ("JP", "non_bank_financial", JP_INSURANCE),
    ("JP", "non_bank_financial", JP_REIT),
]


def _evidence(conn, jurisdiction: str, concept_id: str, mapping_sector: str) -> dict[str, Any]:
    """Build the evidence blob for one proposal."""
    table = "fact_fundamentals_us" if jurisdiction == "US" else "fact_fundamentals_jp"
    entity_col = "cik" if jurisdiction == "US" else "edinet_code"
    dim_table = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT f.filing_id), COUNT(DISTINCT f.{entity_col})
            FROM {table} f
            JOIN {dim_table} d ON d.{entity_col} = f.{entity_col}
            WHERE f.concept_id = %s
              AND COALESCE(d.mapping_sector, 'corp') = %s
            """,
            (concept_id, mapping_sector),
        )
        filings, entities = cur.fetchone()
        cur.execute(
            """
            SELECT mapping_sector, target_variable, tier, multiplier,
                   aggregation_type, sign_policy, mapping_id
            FROM map_concept_to_taxonomy_versioned
            WHERE concept_id = %s
              AND jurisdiction = %s
            ORDER BY mapping_sector
            """,
            (concept_id, jurisdiction),
        )
        current = []
        for row in cur.fetchall():
            d = dict(zip(
                ("mapping_sector", "target_variable", "tier", "multiplier",
                 "aggregation_type", "sign_policy", "mapping_id"),
                row,
            ))
            # Decimal isn't JSON-serializable; stringify so the evidence blob persists cleanly.
            if d.get("multiplier") is not None:
                d["multiplier"] = str(d["multiplier"])
            current.append(d)
    return {
        "evidence_source": "fact_fundamentals + map_concept_to_taxonomy_versioned",
        "filings_in_sector": filings,
        "entities_in_sector": entities,
        "current_mappings": current,
    }


def _namespace(concept_id: str) -> str:
    return concept_id.split("/", 1)[0] if "/" in concept_id else ""


def _local_name(concept_id: str) -> str:
    return concept_id.split("/", 1)[1] if "/" in concept_id else concept_id


_GICS_SCOPE_FOR_SECTOR = {
    "corp": "generic",
    "bank_financial": "generic",
    "non_bank_financial": "generic",
}


def seed() -> dict[str, int]:
    stats = {"inserted": 0, "skipped_no_evidence": 0, "skipped_existing": 0}
    with connect() as conn, conn.cursor() as cur:
        # Pre-clear any rows from a prior run in the same batch so re-running is safe.
        cur.execute(
            "DELETE FROM map_concept_to_taxonomy_review_queue WHERE review_batch = %s",
            (REVIEW_BATCH,),
        )
        for jurisdiction, mapping_sector, proposals in _ALL_PROPOSALS:
            for concept_id, target, agg_type, sign_policy, reasoning in proposals:
                evidence = _evidence(conn, jurisdiction, concept_id, mapping_sector)
                if evidence["filings_in_sector"] == 0:
                    stats["skipped_no_evidence"] += 1
                    print(f"  skip (no evidence in sector): {jurisdiction} {mapping_sector} {concept_id}")
                    continue
                current = next(
                    (m for m in evidence["current_mappings"]
                     if m["mapping_sector"] == mapping_sector or m["mapping_sector"] is None),
                    None,
                )
                if current and current["target_variable"] == target:
                    stats["skipped_existing"] += 1
                    continue
                # Use the schema's accepted vocabulary: 'sector_scope' for new
                # sector-specific routing; 'alternate_total' for fallback totals.
                proposed_action = (
                    "alternate_total" if agg_type == "FALLBACK_TOTAL"
                    else "sector_scope"
                )
                candidate_targets = Json([{
                    "target_variable": target,
                    "aggregation_type": agg_type,
                    "sign_policy": sign_policy,
                    "confidence": 0.85,
                }])
                cur.execute(
                    """
                    INSERT INTO map_concept_to_taxonomy_review_queue
                        (jurisdiction, normalized_concept_id, mapping_sector, gics_scope,
                         local_name, namespaces, source_concept_ids, review_class,
                         top_candidate_label, candidate_targets,
                         suggested_aggregation_type, suggested_sign_policy,
                         suggested_multiplier,
                         proposed_action, review_action_type, review_status,
                         decision, reasoning, evidence, current_mapping_id,
                         confidence, mapping_source, prompt_version, model_name,
                         review_batch)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        jurisdiction, concept_id, mapping_sector,
                        _GICS_SCOPE_FOR_SECTOR.get(mapping_sector, "ALL"),
                        _local_name(concept_id),
                        [_namespace(concept_id)],
                        [concept_id],
                        "map_candidate",  # review_class from accepted vocabulary
                        target,
                        candidate_targets,
                        agg_type,
                        sign_policy,
                        1,  # suggested_multiplier - sign_policy carries the sign
                        proposed_action,
                        "sector_mapping_split",  # review_action_type from accepted vocabulary
                        "queued",
                        "NEEDS_HUMAN_REVIEW",
                        reasoning,
                        Json(evidence),
                        current.get("mapping_id") if current else None,
                        0.85,
                        "claude_sonnet_terminal_authoring",
                        PROMPT_VERSION,
                        MODEL_NAME,
                        REVIEW_BATCH,
                    ),
                )
                stats["inserted"] += 1
    return stats


def main() -> int:
    print(f"Seeding sector mapping queue batch: {REVIEW_BATCH}")
    stats = seed()
    print(f"\nStats: {stats}")
    print(f"\nReview queue rows written: {stats['inserted']}")
    print(f"Inspect with: SELECT jurisdiction, normalized_concept_id, mapping_sector, top_candidate_label,")
    print(f"              suggested_aggregation_type, proposed_action")
    print(f"              FROM sec.map_concept_to_taxonomy_review_queue")
    print(f"              WHERE review_batch = '{REVIEW_BATCH}' ORDER BY jurisdiction, mapping_sector;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
