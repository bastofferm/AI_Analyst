"""Shared M:1 raw concept aggregation resolver.

One resolver, two jurisdiction-specific context strategies. Replaces the
duplicate tier1/tier2 logic in ``us_standardize.py`` and ``jp_standardize.py``
when the ``XBRL_SEC_USE_M1_RESOLVER`` feature flag is enabled.

Algorithm (per (entity, fiscal_year, fiscal_period, target_variable)):

1. Resolve each raw fact's mapping rule using the existing versioned mapping
   selector.
2. Apply ``sign_policy`` then ``multiplier`` to each fact value exactly once.
3. Partition mapped facts by the jurisdiction's compatible-context grouper.
4. If any ROOT/DIRECT/FALLBACK_TOTAL fact is present in the best context
   group, return the best one (priority + jurisdiction-specific rank) and
   record the ignored CHILD_SUM concepts for provenance.
5. Otherwise, sum the CHILD_SUM facts in the best compatible context group.
6. Emit ``AggregationResult`` with provenance and quality flags.

The resolver itself contains zero references to ``cik``, ``edinet_code``,
``context_id``, ``dimension_signature``, presentation parents, or any other
jurisdiction-specific column. Those live in the ``ContextStrategy``
implementations below.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol


_ROOT_TYPES = frozenset({"ROOT", "DIRECT", "FALLBACK_TOTAL"})
_COMPONENT_TYPES = frozenset({"CHILD_SUM"})
_DEFAULT_PRIORITY = 100


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawFact:
    """A single XBRL fact, jurisdiction-agnostic.

    Jurisdiction-specific context fields (``context_id``,
    ``dimension_signature``, ``pre_parent_id``) are optional. The active
    ``ContextStrategy`` decides which fields it cares about.
    """
    entity_id: str
    filing_id: str | None
    concept_id: str
    fiscal_year: int
    fiscal_period: str
    period_start: date | None
    period_end: date | None
    value: Decimal
    unit: str | None
    context_id: str | None = None
    dimension_signature: str | None = None
    statement_type: str | None = None
    concept_path: Any = None
    pre_parent_id: str | None = None
    pre_level: int | None = None
    weight: Decimal | None = None
    filing_type: str | None = None
    filed_date: date | None = None
    taxonomy_version: str | None = None
    accounting_standard: str | None = None
    mapping_sector: str | None = None
    gics_sector: str | None = None
    gics_industry_group: str | None = None
    gics_industry: str | None = None
    gics_sub_industry: str | None = None
    # Raw dict the strategy can inspect for jurisdiction-specific fields not
    # promoted to first-class attributes (e.g. JP's ``context_tier``).
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MappingRule:
    """The resolved mapping decision for one raw fact."""
    mapping_id: int | None
    mapping_exception_id: int | None
    concept_id: str
    target_variable: str
    aggregation_type: str       # ROOT | DIRECT | FALLBACK_TOTAL | CHILD_SUM | EXCLUDE
    aggregation_priority: int   # lower is higher priority within type
    multiplier: Decimal
    sign_policy: str            # as_reported | flip | force_negative | force_positive
    normal_balance: str | None  # debit | credit | None
    tier: int                   # legacy tier (1 or 2) for backwards-compat ranking
    mapping_source: str         # 'versioned' or 'exception'


@dataclass(frozen=True)
class AggregationResult:
    """Resolved value plus provenance for one (entity, period, target)."""
    entity_id: str
    fiscal_year: int
    fiscal_period: str
    period_end: date | None
    target_variable: str
    value: Decimal
    metric_type: str            # RAW | T2_SUM | T2_COMPONENT
    aggregation_type: str       # ROOT | DIRECT | FALLBACK_TOTAL | CHILD_SUM
    unit: str | None
    source_concept_ids: tuple[str, ...]
    mapping_ids: tuple[int | None, ...]
    mapping_exception_ids: tuple[int | None, ...]
    ignored_component_concepts: tuple[str, ...]
    quality_flags: tuple[str, ...]
    filing_id: str | None
    filing_type: str | None
    filed_date: date | None
    concept_path: Any
    context_group_key: tuple


# ---------------------------------------------------------------------------
# Context strategy protocol
# ---------------------------------------------------------------------------


class ContextStrategy(Protocol):
    """Jurisdiction-specific decisions about context compatibility and rank."""

    jurisdiction: str

    def is_eligible(self, fact: RawFact) -> bool:
        """Drop facts that should never enter standardization (e.g. JP segment-only)."""

    def context_group_key(self, fact: RawFact) -> tuple:
        """Stable key partitioning facts into compatible groups for summation."""

    def root_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        """Sort key for picking the best ROOT/DIRECT fact (max wins)."""

    def component_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        """Sort key for picking the best CHILD_SUM context group (max wins)."""

    def quality_flags(self, fact: RawFact) -> tuple[str, ...]:
        """Optional quality flags emitted with each result."""


# ---------------------------------------------------------------------------
# Sign policy application
# ---------------------------------------------------------------------------


def apply_sign(value: Decimal, multiplier: Decimal, sign_policy: str | None) -> Decimal:
    """Apply sign policy and multiplier exactly once per fact.

    Semantics: ``sign_policy`` is the final word on orientation. The
    ``multiplier`` only applies when sign_policy is ``as_reported`` (i.e. the
    multiplier IS the legacy sign signal). This avoids double-flipping when
    the DB carries both ``multiplier=-1`` AND ``sign_policy='flip'``, which is
    how Phase 1 backfill encodes legacy sign evidence.

    - ``force_negative`` / ``force_positive``: canonical override; multiplier ignored.
    - ``flip``: negate the value; multiplier ignored (the flip IS the sign).
    - ``as_reported``: multiplier carries the sign.
    """
    policy = (sign_policy or "as_reported").lower()
    if policy == "force_negative":
        return -abs(value)
    if policy == "force_positive":
        return abs(value)
    if policy == "flip":
        return -value
    # as_reported: multiplier is the sign signal
    return value * (multiplier or Decimal("1"))


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------


def _priority(rule: MappingRule) -> int:
    return rule.aggregation_priority if rule.aggregation_priority is not None else _DEFAULT_PRIORITY


def _is_root(rule: MappingRule) -> bool:
    if rule.aggregation_type in _ROOT_TYPES:
        return True
    # Legacy: tier=1 means root when explicit aggregation_type is absent.
    return rule.aggregation_type is None and rule.tier == 1


def _is_component(rule: MappingRule) -> bool:
    if rule.aggregation_type in _COMPONENT_TYPES:
        return True
    return rule.aggregation_type is None and rule.tier != 1


def _resolve_group(
    target_variable: str,
    facts_and_rules: list[tuple[RawFact, MappingRule]],
    strategy: ContextStrategy,
    unit_type: str | None,
    entity_id: str,
    fiscal_year: int,
    fiscal_period: str,
) -> AggregationResult | None:
    """Resolve one (entity, period, target_variable) group into a single result."""
    if not facts_and_rules:
        return None

    # Drop EXCLUDE rules entirely.
    facts_and_rules = [(f, r) for f, r in facts_and_rules if r.aggregation_type != "EXCLUDE"]
    if not facts_and_rules:
        return None

    # Apply sign policy + multiplier once per fact.
    adjusted: list[tuple[RawFact, MappingRule, Decimal, tuple]] = []
    for fact, rule in facts_and_rules:
        value = apply_sign(fact.value, rule.multiplier, rule.sign_policy)
        ctx_key = strategy.context_group_key(fact)
        adjusted.append((fact, rule, value, ctx_key))

    # 1. Prefer ROOT/DIRECT/FALLBACK_TOTAL — pick the best priority bucket
    #    (lowest priority value wins), then break ties with strategy.root_rank
    #    (highest rank wins).
    roots = [(f, r, v, k) for (f, r, v, k) in adjusted if _is_root(r)]
    if roots:
        best_priority = min(_priority(item[1]) for item in roots)
        same_priority = [item for item in roots if _priority(item[1]) == best_priority]
        same_priority.sort(
            key=lambda item: strategy.root_rank(item[0], target_variable, unit_type),
            reverse=True,
        )
        chosen_fact, chosen_rule, chosen_value, chosen_key = same_priority[0]
        ignored = tuple(sorted({
            f.concept_id for (f, r, _v, _k) in adjusted
            if _is_component(r)
        }))
        flags: list[str] = list(strategy.quality_flags(chosen_fact))
        if ignored:
            flags.append("components_ignored_root_present")
        return AggregationResult(
            entity_id=entity_id,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            period_end=chosen_fact.period_end,
            target_variable=target_variable,
            value=chosen_value,
            metric_type="RAW",
            aggregation_type=chosen_rule.aggregation_type or "ROOT",
            unit=chosen_fact.unit,
            source_concept_ids=(chosen_fact.concept_id,),
            mapping_ids=(chosen_rule.mapping_id,),
            mapping_exception_ids=(chosen_rule.mapping_exception_id,),
            ignored_component_concepts=ignored,
            quality_flags=tuple(flags),
            filing_id=chosen_fact.filing_id,
            filing_type=chosen_fact.filing_type,
            filed_date=chosen_fact.filed_date,
            concept_path=chosen_fact.concept_path,
            context_group_key=chosen_key,
        )

    # 2. Fall back to summing CHILD_SUM components by compatible context group.
    components = [(f, r, v, k) for (f, r, v, k) in adjusted if _is_component(r)]
    if not components:
        return None

    by_group: dict[tuple, list[tuple[RawFact, MappingRule, Decimal]]] = defaultdict(list)
    for fact, rule, value, key in components:
        by_group[key].append((fact, rule, value))

    def _group_rank(group: list[tuple[RawFact, MappingRule, Decimal]]) -> tuple:
        best_rank = max(
            strategy.component_rank(item[0], target_variable, unit_type) for item in group
        )
        size = len(group)
        magnitude = max(abs(item[2]) for item in group)
        return (best_rank, size, magnitude)

    best_key, best_group = max(by_group.items(), key=lambda kv: _group_rank(kv[1]))
    total = sum((item[2] for item in best_group), Decimal("0"))
    anchor = max(
        best_group,
        key=lambda item: strategy.component_rank(item[0], target_variable, unit_type),
    )[0]
    metric_type = "T2_SUM" if len(best_group) > 1 else "T2_COMPONENT"
    source_ids = tuple(dict.fromkeys(item[0].concept_id for item in best_group))
    mapping_ids = tuple(dict.fromkeys(item[1].mapping_id for item in best_group))
    exception_ids = tuple(dict.fromkeys(item[1].mapping_exception_id for item in best_group))
    flags = list(strategy.quality_flags(anchor))
    if len(by_group) > 1:
        flags.append("multiple_context_groups_present")

    return AggregationResult(
        entity_id=entity_id,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_end=anchor.period_end,
        target_variable=target_variable,
        value=total,
        metric_type=metric_type,
        aggregation_type="CHILD_SUM",
        unit=anchor.unit,
        source_concept_ids=source_ids,
        mapping_ids=mapping_ids,
        mapping_exception_ids=exception_ids,
        ignored_component_concepts=(),
        quality_flags=tuple(flags),
        filing_id=anchor.filing_id,
        filing_type=anchor.filing_type,
        filed_date=anchor.filed_date,
        concept_path=anchor.concept_path,
        context_group_key=best_key,
    )


def resolve(
    facts_and_rules: list[tuple[RawFact, MappingRule]],
    strategy: ContextStrategy,
    unit_type_for_target: dict[str, str | None] | None = None,
    *,
    fact_fiscal_year: Any = None,
) -> list[AggregationResult]:
    """Resolve all (entity, period, target) groups.

    ``fact_fiscal_year`` lets callers override how fiscal_year is computed
    from a fact (e.g. US uses ``period_end.year`` for FY/Annual periods).
    Default: use ``fact.fiscal_year`` as-is.
    """
    unit_type_for_target = unit_type_for_target or {}
    eligible = [(f, r) for f, r in facts_and_rules if strategy.is_eligible(f)]

    groups: dict[tuple, list[tuple[RawFact, MappingRule]]] = defaultdict(list)
    for fact, rule in eligible:
        fy = int(fact_fiscal_year(fact)) if fact_fiscal_year else int(fact.fiscal_year)
        groups[(fact.entity_id, fy, fact.fiscal_period, rule.target_variable)].append((fact, rule))

    out: list[AggregationResult] = []
    for (entity_id, fy, fp, target), bucket in groups.items():
        result = _resolve_group(
            target, bucket, strategy,
            unit_type=unit_type_for_target.get(target),
            entity_id=entity_id,
            fiscal_year=fy,
            fiscal_period=fp,
        )
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# Helpers shared by both jurisdiction strategies
# ---------------------------------------------------------------------------


_SHARES_UNITS = frozenset({"shares", "share", "pure"})
_MONETARY_UNIT_TYPES = frozenset({"CCY", "MONETARY", "CURRENCY"})


def _unit_rank(unit_type: str | None, fact_unit: str | None) -> int:
    if str(unit_type or "").upper() not in _MONETARY_UNIT_TYPES:
        return 1
    if not fact_unit or fact_unit.lower() in _SHARES_UNITS:
        return 0
    return 1


def _annual_rank(fact: RawFact) -> int:
    if fact.period_start and fact.period_end:
        return 1 if (fact.period_end - fact.period_start).days >= 300 else 0
    return 0


def _path_tokens(path: Any) -> list[str]:
    if path is None:
        return []
    if isinstance(path, (list, tuple)):
        return [str(item) for item in path if item is not None]
    text = str(path).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip().strip("'\"") for part in text.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# US context strategy
# ---------------------------------------------------------------------------


@dataclass
class USContextStrategy:
    """Prefer real context_id when present; fall back to presentation grouping."""
    jurisdiction: str = "US"

    def is_eligible(self, fact: RawFact) -> bool:
        return True

    def context_group_key(self, fact: RawFact) -> tuple:
        # Prefer real context evidence when available; otherwise use the
        # presentation/calculation parent as a proxy.
        if fact.context_id and fact.dimension_signature is not None:
            return (
                fact.filing_id,
                fact.context_id,
                fact.dimension_signature,
                fact.period_start,
                fact.period_end,
                fact.unit,
            )
        tokens = _path_tokens(fact.concept_path)
        path_parent = tuple(tokens[:-1]) if len(tokens) > 1 else ()
        return (
            fact.filing_id,
            fact.period_start,
            fact.period_end,
            fact.unit,
            fact.statement_type,
            fact.pre_parent_id or path_parent,
        )

    def root_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        pre_level = fact.pre_level if fact.pre_level is not None else 999
        return (
            _unit_rank(unit_type, fact.unit),
            _annual_rank(fact),
            -int(pre_level),
            fact.period_end or date.min,
            fact.filed_date or fact.period_end or date.min,
        )

    def component_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        parent = str(fact.pre_parent_id or "")
        path_text = " ".join(_path_tokens(fact.concept_path))
        income_context = int(
            fact.statement_type == "IncomeStatement"
            or "IncomeStatement" in parent
            or "IncomeStatement" in path_text
            or "NetIncomeLoss" in path_text
            or "IncomeLossFromContinuingOperations" in path_text
        )
        disclosure_penalty = int(
            any(term in parent for term in ("Investments", "Tax", "Lease", "DebtAndEquitySecurities"))
            and line_item in {"non_operating_income", "total_operating_expenses"}
        )
        pre_level = fact.pre_level if fact.pre_level is not None else 999
        return (
            _unit_rank(unit_type, fact.unit),
            _annual_rank(fact),
            income_context,
            -disclosure_penalty,
            -int(pre_level),
            fact.period_end or date.min,
            fact.filed_date or fact.period_end or date.min,
        )

    def quality_flags(self, fact: RawFact) -> tuple[str, ...]:
        flags: list[str] = []
        if not fact.context_id or fact.dimension_signature is None:
            flags.append("context_inferred_from_presentation")
        return tuple(flags)


# ---------------------------------------------------------------------------
# JP context strategy
# ---------------------------------------------------------------------------


def _is_standardizable_dimension(signature: str | None) -> bool:
    if not signature:
        return True
    return "ConsolidatedOrNonConsolidatedAxis" in signature and "Member" in signature


@dataclass
class JPContextStrategy:
    """Hard requirement: context_id + dimension_signature must be present.

    Filters out segment-only dimensional facts via the standardizable-dimension
    check that previously lived inside ``jp_standardize.py``.
    """
    jurisdiction: str = "JP"

    def is_eligible(self, fact: RawFact) -> bool:
        return _is_standardizable_dimension(fact.dimension_signature)

    def context_group_key(self, fact: RawFact) -> tuple:
        tokens = _path_tokens(fact.concept_path)
        path_parent = tuple(tokens[:-1]) if len(tokens) > 1 else ()
        return (
            fact.filing_id,
            fact.context_id,
            fact.dimension_signature,
            fact.period_start,
            fact.period_end,
            fact.unit,
            path_parent,
        )

    def root_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        context_tier = fact.extras.get("context_tier")
        return (
            _unit_rank(unit_type, fact.unit),
            1 if context_tier == 0 else 0,
            _annual_rank(fact),
            fact.period_end or date.min,
            fact.filed_date or fact.period_end or date.min,
        )

    def component_rank(self, fact: RawFact, line_item: str, unit_type: str | None) -> tuple:
        path_text = " ".join(_path_tokens(fact.concept_path))
        income_context = int(
            "IncomeStatement" in path_text
            or "NetIncome" in path_text
            or "ProfitLoss" in path_text
            or "OrdinaryIncome" in path_text
        )
        disclosure_penalty = int(
            any(term in path_text for term in ("Investments", "Tax", "Lease", "DebtAndEquitySecurities"))
            and line_item in {"non_operating_income", "total_operating_expenses"}
        )
        context_tier = fact.extras.get("context_tier")
        return (
            _unit_rank(unit_type, fact.unit),
            1 if context_tier == 0 else 0,
            _annual_rank(fact),
            income_context,
            -disclosure_penalty,
            fact.period_end or date.min,
            fact.filed_date or fact.period_end or date.min,
        )

    def quality_flags(self, fact: RawFact) -> tuple[str, ...]:
        return ()


# ---------------------------------------------------------------------------
# Bridge from existing standardizer inputs to RawFact / MappingRule
# ---------------------------------------------------------------------------


def rule_from_versioned_record(rec: dict[str, Any]) -> MappingRule:
    """Build a MappingRule from the dict returned by versioned_mapping loaders."""
    tier = int(rec.get("tier") or 0)
    aggregation_type = rec.get("aggregation_type")
    if aggregation_type is None:
        aggregation_type = "ROOT" if tier == 1 else "CHILD_SUM"
    priority_raw = rec.get("aggregation_priority")
    priority = int(priority_raw) if priority_raw is not None else _DEFAULT_PRIORITY
    # Sign policy comes from the explicit column when present. Do NOT infer
    # "flip" from a -1 multiplier: that would double-flip (flip + multiplier=-1
    # cancel each other). When sign_policy is unset, leave it as "as_reported"
    # and let the multiplier alone carry the legacy sign semantics.
    sign_policy = rec.get("sign_policy") or "as_reported"
    return MappingRule(
        mapping_id=rec.get("mapping_id"),
        mapping_exception_id=rec.get("mapping_exception_id"),
        concept_id="",  # filled by caller from raw fact
        target_variable=rec["target_variable"],
        aggregation_type=aggregation_type,
        aggregation_priority=priority,
        multiplier=Decimal(str(rec.get("multiplier", 1))),
        sign_policy=sign_policy,
        normal_balance=rec.get("normal_balance"),
        tier=tier,
        mapping_source=rec.get("mapping_source", "versioned"),
    )


def fact_from_us_row(row: dict[str, Any]) -> RawFact:
    """Convert a US raw row from _raw_rows() into a RawFact."""
    return RawFact(
        entity_id=str(row.get("cik") or ""),
        filing_id=row.get("filing_id"),
        concept_id=str(row.get("concept_id") or ""),
        fiscal_year=int(row.get("fiscal_year") or 0),
        fiscal_period=str(row.get("fiscal_period") or ""),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        value=Decimal(str(row.get("value") or 0)),
        unit=row.get("unit"),
        context_id=row.get("context_id"),
        dimension_signature=row.get("dimension_signature"),
        statement_type=row.get("statement_type"),
        concept_path=row.get("concept_path"),
        pre_parent_id=row.get("pre_parent_id"),
        pre_level=row.get("pre_level"),
        filing_type=row.get("filing_type"),
        filed_date=row.get("filed_date"),
        taxonomy_version=row.get("taxonomy_version"),
        accounting_standard=row.get("accounting_standard"),
        mapping_sector=row.get("mapping_sector"),
        gics_sector=row.get("gics_sector"),
        gics_industry_group=row.get("gics_industry_group"),
    )


def fact_from_jp_row(row: dict[str, Any]) -> RawFact:
    """Convert a JP raw row from _raw_rows() into a RawFact."""
    return RawFact(
        entity_id=str(row.get("edinet_code") or ""),
        filing_id=row.get("filing_id"),
        concept_id=str(row.get("concept_id") or ""),
        fiscal_year=int(row.get("fiscal_year") or 0),
        fiscal_period=str(row.get("fiscal_period") or ""),
        period_start=row.get("period_start"),
        period_end=row.get("period_end"),
        value=Decimal(str(row.get("value") or 0)),
        unit=row.get("unit"),
        context_id=row.get("context_id"),
        dimension_signature=row.get("dimension_signature"),
        statement_type=row.get("statement_type"),
        concept_path=row.get("concept_path"),
        filing_type=row.get("filing_type"),
        filed_date=row.get("filed_date"),
        taxonomy_version=row.get("taxonomy_version"),
        accounting_standard=row.get("accounting_standard"),
        mapping_sector=row.get("mapping_sector"),
        gics_sector=row.get("gics_sector"),
        gics_industry_group=row.get("gics_industry_group"),
        extras={"context_tier": row.get("context_tier")},
    )
