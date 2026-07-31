"""Unit tests for the shared M:1 aggregation resolver.

Covers every scenario listed in the Phase 4 section of the refactor spec.
No database access — all fixtures are in-memory.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from xbrl_sec.sec.std.m1_aggregation import (
    AggregationResult,
    JPContextStrategy,
    MappingRule,
    RawFact,
    USContextStrategy,
    apply_sign,
    resolve,
    rule_from_versioned_record,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _us_fact(
    *,
    concept_id: str,
    value: Decimal,
    period_start: date = date(2024, 1, 1),
    period_end: date = date(2024, 12, 31),
    unit: str = "USD",
    pre_parent_id: str | None = None,
    concept_path: str | None = None,
    filing_id: str = "0000000000-24-000001",
    statement_type: str = "IncomeStatement",
    context_id: str | None = None,
    dimension_signature: str | None = None,
) -> RawFact:
    return RawFact(
        entity_id="0000320193",
        filing_id=filing_id,
        concept_id=concept_id,
        fiscal_year=2024,
        fiscal_period="FY",
        period_start=period_start,
        period_end=period_end,
        value=value,
        unit=unit,
        statement_type=statement_type,
        concept_path=concept_path,
        pre_parent_id=pre_parent_id,
        pre_level=2,
        filing_type="10-K",
        filed_date=date(2025, 1, 30),
        context_id=context_id,
        dimension_signature=dimension_signature,
        taxonomy_version="us-gaap-2024",
        accounting_standard="US_GAAP",
    )


def _jp_fact(
    *,
    concept_id: str,
    value: Decimal,
    context_id: str = "CurrentYearDuration",
    dimension_signature: str = "ConsolidatedOrNonConsolidatedAxis:ConsolidatedMember",
    period_start: date = date(2024, 4, 1),
    period_end: date = date(2025, 3, 31),
    unit: str = "JPY",
    filing_id: str = "S100ABCD",
    context_tier: int = 0,
    concept_path: str | None = None,
) -> RawFact:
    return RawFact(
        entity_id="E00001",
        filing_id=filing_id,
        concept_id=concept_id,
        fiscal_year=2024,
        fiscal_period="FY",
        period_start=period_start,
        period_end=period_end,
        value=value,
        unit=unit,
        context_id=context_id,
        dimension_signature=dimension_signature,
        statement_type="IncomeStatement",
        concept_path=concept_path,
        filing_type="ASR",
        filed_date=date(2025, 6, 25),
        taxonomy_version="jp-2024",
        accounting_standard="JP_GAAP",
        extras={"context_tier": context_tier},
    )


def _rule(
    concept_id: str,
    target: str,
    *,
    aggregation_type: str = "ROOT",
    aggregation_priority: int = 100,
    multiplier: Decimal = Decimal("1"),
    sign_policy: str = "as_reported",
    mapping_id: int = 1,
    tier: int = 1,
) -> MappingRule:
    return MappingRule(
        mapping_id=mapping_id,
        mapping_exception_id=None,
        concept_id=concept_id,
        target_variable=target,
        aggregation_type=aggregation_type,
        aggregation_priority=aggregation_priority,
        multiplier=multiplier,
        sign_policy=sign_policy,
        normal_balance=None,
        tier=tier,
        mapping_source="versioned",
    )


# ---------------------------------------------------------------------------
# 1. Root and components both present → root wins, components ignored
# ---------------------------------------------------------------------------


def test_root_wins_when_components_also_present_us():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/Revenues", value=Decimal("1000")),
            _rule("us-gaap/Revenues", "revenue", aggregation_type="ROOT", tier=1),
        ),
        (
            _us_fact(concept_id="us-gaap/ProductRevenue", value=Decimal("700")),
            _rule("us-gaap/ProductRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2,
                  mapping_id=2),
        ),
        (
            _us_fact(concept_id="us-gaap/ServiceRevenue", value=Decimal("300")),
            _rule("us-gaap/ServiceRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2,
                  mapping_id=3),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    res = results[0]
    assert res.value == Decimal("1000")
    assert res.metric_type == "RAW"
    assert res.aggregation_type == "ROOT"
    assert res.source_concept_ids == ("us-gaap/Revenues",)
    assert set(res.ignored_component_concepts) == {"us-gaap/ProductRevenue", "us-gaap/ServiceRevenue"}
    assert "components_ignored_root_present" in res.quality_flags


# ---------------------------------------------------------------------------
# 2. Root absent → compatible components sum
# ---------------------------------------------------------------------------


def test_components_sum_when_root_absent_us():
    facts_and_rules = [
        (
            _us_fact(
                concept_id="us-gaap/ProductRevenue", value=Decimal("700"),
                pre_parent_id="RevenueAbstract",
            ),
            _rule("us-gaap/ProductRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2,
                  mapping_id=2),
        ),
        (
            _us_fact(
                concept_id="us-gaap/ServiceRevenue", value=Decimal("300"),
                pre_parent_id="RevenueAbstract",
            ),
            _rule("us-gaap/ServiceRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2,
                  mapping_id=3),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    res = results[0]
    assert res.value == Decimal("1000")
    assert res.metric_type == "T2_SUM"
    assert res.aggregation_type == "CHILD_SUM"
    assert set(res.source_concept_ids) == {"us-gaap/ProductRevenue", "us-gaap/ServiceRevenue"}
    assert res.ignored_component_concepts == ()


# ---------------------------------------------------------------------------
# 3. Components in incompatible contexts → no cross-context sum
# ---------------------------------------------------------------------------


def test_components_in_different_contexts_do_not_cross_sum_us():
    facts_and_rules = [
        (
            _us_fact(
                concept_id="us-gaap/ProductRevenue", value=Decimal("700"),
                pre_parent_id="RevenueAbstract", filing_id="A",
            ),
            _rule("us-gaap/ProductRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2),
        ),
        (
            _us_fact(
                concept_id="us-gaap/ServiceRevenue", value=Decimal("300"),
                pre_parent_id="OtherRevenueAbstract", filing_id="A",
            ),
            _rule("us-gaap/ServiceRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    res = results[0]
    # Only one of the two single-fact groups wins; the other is dropped.
    assert res.value in {Decimal("700"), Decimal("300")}
    assert res.metric_type == "T2_COMPONENT"
    assert "multiple_context_groups_present" in res.quality_flags


def test_jp_segment_only_dimensions_dropped():
    """JP facts with non-consolidated dimension signature must be excluded."""
    facts_and_rules = [
        (
            _jp_fact(
                concept_id="jppfs/OperatingRevenue",
                value=Decimal("1000"),
                dimension_signature="SegmentInformationAxis:RetailSegmentMember",
            ),
            _rule("jppfs/OperatingRevenue", "revenue", aggregation_type="ROOT", tier=1),
        ),
        (
            _jp_fact(
                concept_id="jppfs/OperatingRevenue",
                value=Decimal("5000"),
                dimension_signature="ConsolidatedOrNonConsolidatedAxis:ConsolidatedMember",
            ),
            _rule("jppfs/OperatingRevenue", "revenue", aggregation_type="ROOT", tier=1),
        ),
    ]
    results = resolve(facts_and_rules, JPContextStrategy())
    assert len(results) == 1
    assert results[0].value == Decimal("5000")


# ---------------------------------------------------------------------------
# 4. Sign handling: multiplier and policy apply once
# ---------------------------------------------------------------------------


def test_sign_flip_policy_applied_once():
    """flip is the final-word sign signal; multiplier is ignored when flip is set.

    This prevents the double-flip bug where Phase 1 backfill sets
    sign_policy='flip' AND multiplier=-1 on the same row to encode legacy
    sign evidence twice.
    """
    assert apply_sign(Decimal("100"), Decimal("1"), "flip") == Decimal("-100")
    # multiplier=-1 is ignored when sign_policy='flip'; result is just -100.
    assert apply_sign(Decimal("100"), Decimal("-1"), "flip") == Decimal("-100")


def test_force_negative_policy():
    assert apply_sign(Decimal("100"), Decimal("1"), "force_negative") == Decimal("-100")
    assert apply_sign(Decimal("-100"), Decimal("1"), "force_negative") == Decimal("-100")


def test_force_positive_policy():
    assert apply_sign(Decimal("-100"), Decimal("1"), "force_positive") == Decimal("100")
    assert apply_sign(Decimal("100"), Decimal("1"), "force_positive") == Decimal("100")


def test_as_reported_default():
    assert apply_sign(Decimal("100"), Decimal("1"), None) == Decimal("100")
    assert apply_sign(Decimal("100"), Decimal("1"), "as_reported") == Decimal("100")


def test_multiplier_only_applied_once_in_resolver():
    """Resolver must apply sign + multiplier exactly once per fact."""
    facts_and_rules = [
        (
            _us_fact(
                concept_id="us-gaap/Expense", value=Decimal("500"),
                pre_parent_id="ExpensesAbstract",
            ),
            _rule(
                "us-gaap/Expense", "some_expense",
                aggregation_type="CHILD_SUM", tier=2,
                multiplier=Decimal("-1"), sign_policy="as_reported",
            ),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    assert results[0].value == Decimal("-500")  # multiplier applied exactly once


# ---------------------------------------------------------------------------
# 5. EXCLUDE rules never produce results
# ---------------------------------------------------------------------------


def test_exclude_rules_dropped():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/AuditOnly", value=Decimal("99")),
            _rule("us-gaap/AuditOnly", "revenue", aggregation_type="EXCLUDE"),
        ),
    ]
    assert resolve(facts_and_rules, USContextStrategy()) == []


# ---------------------------------------------------------------------------
# 6. Aggregation priority: lower priority wins among ROOTs
# ---------------------------------------------------------------------------


def test_aggregation_priority_picks_best_root():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/RevenueAlt", value=Decimal("999")),
            _rule("us-gaap/RevenueAlt", "revenue",
                  aggregation_type="FALLBACK_TOTAL", aggregation_priority=200, mapping_id=1),
        ),
        (
            _us_fact(concept_id="us-gaap/Revenues", value=Decimal("1000")),
            _rule("us-gaap/Revenues", "revenue",
                  aggregation_type="ROOT", aggregation_priority=10, mapping_id=2),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    assert results[0].value == Decimal("1000")
    assert results[0].source_concept_ids == ("us-gaap/Revenues",)


# ---------------------------------------------------------------------------
# 7. Fallback total used when only it is present
# ---------------------------------------------------------------------------


def test_fallback_total_used_when_no_root():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/RevenueAlt", value=Decimal("999")),
            _rule("us-gaap/RevenueAlt", "revenue",
                  aggregation_type="FALLBACK_TOTAL", aggregation_priority=200, mapping_id=1),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert len(results) == 1
    assert results[0].value == Decimal("999")
    assert results[0].aggregation_type == "FALLBACK_TOTAL"


# ---------------------------------------------------------------------------
# 8. Legacy fallback: missing aggregation_type behaves like tier
# ---------------------------------------------------------------------------


def test_legacy_tier_fallback_when_aggregation_type_missing():
    """rule_from_versioned_record must derive aggregation_type from tier.

    Sign behavior: when sign_policy is not explicitly set on the mapping row,
    we leave it as 'as_reported' and let the multiplier carry the sign.
    Inferring 'flip' from a -1 multiplier would double-flip and is wrong.
    """
    rec_tier1 = {"target_variable": "revenue", "tier": 1, "multiplier": Decimal("1"), "mapping_id": 1}
    rule = rule_from_versioned_record(rec_tier1)
    assert rule.aggregation_type == "ROOT"
    assert rule.sign_policy == "as_reported"

    rec_tier2 = {"target_variable": "revenue", "tier": 2, "multiplier": Decimal("-1"), "mapping_id": 2}
    rule = rule_from_versioned_record(rec_tier2)
    assert rule.aggregation_type == "CHILD_SUM"
    assert rule.sign_policy == "as_reported"
    assert rule.multiplier == Decimal("-1")


# ---------------------------------------------------------------------------
# 9. US context-inferred flag emitted when context_id missing
# ---------------------------------------------------------------------------


def test_us_emits_context_inferred_flag_when_no_context_id():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/Revenues", value=Decimal("1000")),
            _rule("us-gaap/Revenues", "revenue", aggregation_type="ROOT", tier=1),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert "context_inferred_from_presentation" in results[0].quality_flags


def test_us_no_context_inferred_flag_when_context_id_present():
    facts_and_rules = [
        (
            _us_fact(
                concept_id="us-gaap/Revenues", value=Decimal("1000"),
                context_id="d_2024", dimension_signature="",
            ),
            _rule("us-gaap/Revenues", "revenue", aggregation_type="ROOT", tier=1),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    assert "context_inferred_from_presentation" not in results[0].quality_flags


# ---------------------------------------------------------------------------
# 10. Cross-target independence: different targets are resolved separately
# ---------------------------------------------------------------------------


def test_different_targets_resolved_independently():
    facts_and_rules = [
        (
            _us_fact(concept_id="us-gaap/Revenues", value=Decimal("1000")),
            _rule("us-gaap/Revenues", "revenue", aggregation_type="ROOT", tier=1),
        ),
        (
            _us_fact(concept_id="us-gaap/NetIncomeLoss", value=Decimal("100")),
            _rule("us-gaap/NetIncomeLoss", "net_income", aggregation_type="ROOT", tier=1,
                  mapping_id=2),
        ),
    ]
    results = resolve(facts_and_rules, USContextStrategy())
    by_target = {r.target_variable: r for r in results}
    assert by_target["revenue"].value == Decimal("1000")
    assert by_target["net_income"].value == Decimal("100")


# ---------------------------------------------------------------------------
# 11. Determinism: identical inputs yield identical outputs across runs
# ---------------------------------------------------------------------------


def test_determinism():
    facts_and_rules = [
        (
            _us_fact(
                concept_id="us-gaap/ProductRevenue", value=Decimal("700"),
                pre_parent_id="RevenueAbstract",
            ),
            _rule("us-gaap/ProductRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2),
        ),
        (
            _us_fact(
                concept_id="us-gaap/ServiceRevenue", value=Decimal("300"),
                pre_parent_id="RevenueAbstract",
            ),
            _rule("us-gaap/ServiceRevenue", "revenue", aggregation_type="CHILD_SUM", tier=2),
        ),
    ]
    strategy = USContextStrategy()
    first = resolve(facts_and_rules, strategy)
    second = resolve(facts_and_rules, strategy)
    assert first[0].value == second[0].value
    assert first[0].source_concept_ids == second[0].source_concept_ids
