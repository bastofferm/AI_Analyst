"""Build filing-native financial statement display projections.

The runtime API reads the projection tables populated here. Local filing HTML
and label linkbases are ingestion inputs only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


_LINK_NS = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_XLINK_HREF = f"{{{_XLINK}}}href"
_XLINK_LABEL = f"{{{_XLINK}}}label"
_XLINK_FROM = f"{{{_XLINK}}}from"
_XLINK_TO = f"{{{_XLINK}}}to"
_XLINK_ROLE = f"{{{_XLINK}}}role"
_IX_NS = "http://www.xbrl.org/2013/inlineXBRL"
_XBRLI_NS = "http://www.xbrl.org/2003/instance"
_XBRLDI_NS = "http://xbrl.org/2006/xbrldi"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_AAR_CIK = "0000001750"
_AAR_FILING_ID = "0001104659-20-108360"
_AAR_STEM = f"CIK{_AAR_CIK}_{_AAR_FILING_ID}"


@dataclass(frozen=True)
class StatementSpec:
    api_statement: str
    statement_type: str
    role_suffix: str
    statement_title: str
    standardized_label: str
    period_kind: str


@dataclass(frozen=True)
class PeriodContext:
    context_id: str
    kind: str
    period_start: date | None
    period_end: date
    has_dimensions: bool


@dataclass(frozen=True)
class DisplayColumn:
    key: str
    label: str
    kind: str
    period_start: date | None
    period_end: date
    order: int


@dataclass
class DisplayNode:
    node_key: str
    parent_node_key: str | None
    source_concept_id: str
    source_parent_concept_id: str | None
    value_binding_concept_id: str | None
    std_line_item_id: str | None
    raw_label: str
    standardized_label: str | None
    display_label: str
    display_role: str
    default_visibility: str
    is_abstract: bool
    presentation_depth: int
    display_depth: int
    display_order: int


@dataclass(frozen=True)
class FactValue:
    value: Decimal
    unit: str | None
    fact_id: str | None
    provenance: str


_AAR_SPECS: tuple[StatementSpec, ...] = (
    StatementSpec(
        api_statement="BS",
        statement_type="balance_sheet",
        role_suffix="StatementCondensedConsolidatedBalanceSheets",
        statement_title="Condensed Consolidated Balance Sheets",
        standardized_label="Balance Sheet",
        period_kind="instant",
    ),
    StatementSpec(
        api_statement="IS",
        statement_type="income_statement",
        role_suffix="StatementCondensedConsolidatedStatementsOfOperations",
        statement_title="Condensed Consolidated Statements of Operations",
        standardized_label="Income Statement",
        period_kind="duration",
    ),
    StatementSpec(
        api_statement="CF",
        statement_type="cash_flow_statement",
        role_suffix="StatementCondensedConsolidatedStatementsOfCashFlows",
        statement_title="Condensed Consolidated Statements of Cash Flows",
        standardized_label="Cash Flow Statement",
        period_kind="duration",
    ),
)

_ROOT_LABELS = {
    "BS": "Balance Sheet",
    "IS": "Income Statement",
    "CF": "Cash Flow Statement",
}

_STD_LABEL_OVERRIDES = {
    "us-gaap/AssetsAbstract": "Assets",
    "us-gaap/LiabilitiesAndStockholdersEquityAbstract": "Liabilities and Equity",
    "us-gaap/RevenuesAbstract": "Revenue",
    "us-gaap/CostsAndExpensesAbstract": "Costs and expenses",
    "air/OperatingIncomeLossIncludingIncomeLossFromEquityMethodInvestments": "Operating income (loss)",
    "air/IncomeLossFromContinuingOperationsBeforeIncomeTaxesAndMinorityInterest": "Income (loss) from continuing operations before income taxes",
    "us-gaap/IncomeLossFromContinuingOperations": "Income (loss) from continuing operations",
    "us-gaap/IncomeLossFromDiscontinuedOperationsNetOfTax": "Income (loss) from discontinued operations, net of tax",
    "us-gaap/NetIncomeLoss": "Net income (loss)",
    "us-gaap/EarningsPerShareBasicAbstract": "Earnings per share, basic",
    "us-gaap/EarningsPerShareDilutedAbstract": "Earnings per share, diluted",
    "us-gaap/OtherComprehensiveIncomeLossNetOfTaxPeriodIncreaseDecreaseAbstract": "Other comprehensive income (loss), net of tax",
    "us-gaap/NetCashProvidedByUsedInOperatingActivitiesAbstract": "Operating activities",
    "us-gaap/NetCashProvidedByUsedInInvestingActivitiesAbstract": "Investing activities",
    "us-gaap/NetCashProvidedByUsedInFinancingActivitiesAbstract": "Financing activities",
    "us-gaap/EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations": "Effect of exchange rate changes on cash",
    "us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect": "Net change in cash and restricted cash",
    "us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations": "Cash and restricted cash, end of period",
}

_DETAIL_VISIBILITY_OVERRIDES = {
    "us-gaap/OtherComprehensiveIncomeLossNetOfTax",
}

_DURATION_END_INSTANT_CONCEPTS = {
    "us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
}

_BINDING_CANDIDATES = {
    "us-gaap/AssetsAbstract": ("us-gaap/Assets",),
    "us-gaap/LiabilitiesAndStockholdersEquityAbstract": ("us-gaap/LiabilitiesAndStockholdersEquity",),
    "us-gaap/AssetsCurrentAbstract": ("us-gaap/AssetsCurrent",),
    "us-gaap/LiabilitiesCurrentAbstract": ("us-gaap/LiabilitiesCurrent",),
    "us-gaap/StockholdersEquityAbstract": ("us-gaap/StockholdersEquity",),
    "us-gaap/RevenuesAbstract": ("us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",),
    "us-gaap/CostsAndExpensesAbstract": ("us-gaap/CostsAndExpenses",),
    "us-gaap/EarningsPerShareBasicAbstract": ("us-gaap/EarningsPerShareBasic",),
    "us-gaap/EarningsPerShareDilutedAbstract": ("us-gaap/EarningsPerShareDiluted",),
    "us-gaap/OtherComprehensiveIncomeLossNetOfTaxPeriodIncreaseDecreaseAbstract": (
        "us-gaap/OtherComprehensiveIncomeLossNetOfTax",
    ),
    "us-gaap/NetCashProvidedByUsedInOperatingActivitiesAbstract": (
        "us-gaap/NetCashProvidedByUsedInOperatingActivities",
        "us-gaap/NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "us-gaap/NetCashProvidedByUsedInInvestingActivitiesAbstract": (
        "us-gaap/NetCashProvidedByUsedInInvestingActivities",
        "us-gaap/NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ),
    "us-gaap/NetCashProvidedByUsedInFinancingActivitiesAbstract": (
        "us-gaap/NetCashProvidedByUsedInFinancingActivities",
        "us-gaap/NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ),
}

_SCAFFOLD_EXACT = {
    "us-gaap/StatementTable",
    "us-gaap/StatementLineItems",
}


def _sec_root() -> Path:
    return Path("D:/market_data/us_sec")


def _concept_from_href(href: str | None) -> str | None:
    if not href or "#" not in href:
        return None
    fragment = href.rsplit("#", 1)[-1]
    if "_" not in fragment:
        return fragment.replace(":", "/", 1)
    parts = fragment.split("_", 2)
    if len(parts) == 3:
        prefix = f"{parts[0]}_{parts[1]}"
        local = parts[2]
    else:
        prefix, local = fragment.split("_", 1)
    return f"{prefix}/{local}"


def _concept_from_name(name: str | None) -> str | None:
    if not name:
        return None
    return name.replace(":", "/", 1)


def _local_name(concept_id: str) -> str:
    return concept_id.split("/", 1)[-1]


def _humanize_concept(concept_id: str) -> str:
    local = _local_name(concept_id)
    local = re.sub(r"(Abstract|Table|Axis|Domain|Member)$", "", local)
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", local).strip()
    return words or concept_id


def _is_scaffold_concept(concept_id: str) -> bool:
    if concept_id in _SCAFFOLD_EXACT:
        return True
    local = _local_name(concept_id)
    return local.endswith(("Table", "Axis", "Domain", "Member", "LineItems"))


def _is_abstract_concept(concept_id: str, taxonomy_is_abstract: bool | None) -> bool:
    if taxonomy_is_abstract is not None:
        return bool(taxonomy_is_abstract)
    return _local_name(concept_id).endswith("Abstract")


def _node_key(order: int, concept_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", concept_id).strip("_").lower()
    return f"n{order:04d}_{slug[:80]}"


def _date_label(value: date) -> str:
    months = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.")
    return f"{months[value.month - 1]} {value.day}, {value.year}"


def _column_key(ctx: PeriodContext) -> str:
    if ctx.kind == "instant":
        return f"instant_{ctx.period_end.isoformat()}"
    assert ctx.period_start is not None
    return f"duration_{ctx.period_start.isoformat()}_{ctx.period_end.isoformat()}"


def _column_label(ctx: PeriodContext) -> str:
    if ctx.kind == "instant":
        return _date_label(ctx.period_end)
    return f"3 months ended {_date_label(ctx.period_end)}"


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    return date.fromisoformat(text.strip())


def _parse_label_linkbase(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    labels: dict[str, dict[str, str]] = {}
    for link in root.iter(f"{{{_LINK_NS}}}labelLink"):
        locs: dict[str, str] = {}
        resources: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for loc in link.iter(f"{{{_LINK_NS}}}loc"):
            label_id = loc.get(_XLINK_LABEL)
            concept = _concept_from_href(loc.get(_XLINK_HREF))
            if label_id and concept:
                locs[label_id] = concept
        for resource in link.iter(f"{{{_LINK_NS}}}label"):
            label_id = resource.get(_XLINK_LABEL)
            text = (resource.text or "").strip()
            if not label_id or not text:
                continue
            resources[label_id].append((resource.get(_XLINK_ROLE, ""), resource.get(_XML_LANG, ""), text))
        for arc in link.iter(f"{{{_LINK_NS}}}labelArc"):
            concept = locs.get(arc.get(_XLINK_FROM, ""))
            if not concept:
                continue
            for role, lang, text in resources.get(arc.get(_XLINK_TO, ""), []):
                entry = labels.setdefault(concept, {})
                role_lower = role.lower()
                lang_lower = lang.lower()
                if lang_lower.startswith("ja"):
                    continue
                if "terse" in role_lower:
                    entry.setdefault("terse", text)
                elif "verbose" in role_lower:
                    entry.setdefault("verbose", text)
                elif "documentation" not in role_lower:
                    entry["label"] = text
    return labels


def _parse_inline_xbrl(path: Path | None) -> tuple[dict[str, PeriodContext], dict[tuple[str, str], FactValue]]:
    if path is None or not path.exists():
        return {}, {}
    root = ET.parse(path).getroot()
    contexts: dict[str, PeriodContext] = {}
    for ctx in root.iter(f"{{{_XBRLI_NS}}}context"):
        context_id = ctx.get("id")
        if not context_id:
            continue
        period = ctx.find(f"{{{_XBRLI_NS}}}period")
        if period is None:
            continue
        instant = period.find(f"{{{_XBRLI_NS}}}instant")
        start = period.find(f"{{{_XBRLI_NS}}}startDate")
        end = period.find(f"{{{_XBRLI_NS}}}endDate")
        if instant is not None and instant.text:
            period_end = _parse_date(instant.text)
            kind = "instant"
            period_start = None
        elif end is not None and end.text:
            period_end = _parse_date(end.text)
            kind = "duration"
            period_start = _parse_date(start.text) if start is not None and start.text else None
        else:
            continue
        if period_end is None:
            continue
        has_dimensions = (
            ctx.find(f".//{{{_XBRLDI_NS}}}explicitMember") is not None
            or ctx.find(f".//{{{_XBRLDI_NS}}}typedMember") is not None
        )
        contexts[context_id] = PeriodContext(
            context_id=context_id,
            kind=kind,
            period_start=period_start,
            period_end=period_end,
            has_dimensions=has_dimensions,
        )

    units = _parse_units(root)
    facts: dict[tuple[str, str], FactValue] = {}
    for fact in root.iter(f"{{{_IX_NS}}}nonFraction"):
        nil_value = str(fact.get(_XSI_NIL) or "").lower()
        if nil_value in {"true", "1"}:
            continue
        concept_id = _concept_from_name(fact.get("name"))
        context_id = fact.get("contextRef")
        if not concept_id or not context_id or context_id not in contexts:
            continue
        value = _parse_fact_decimal("".join(fact.itertext()), fact.get("scale"), fact.get("sign"))
        if value is None:
            continue
        ctx = contexts[context_id]
        if ctx.has_dimensions:
            continue
        facts.setdefault(
            (concept_id, _column_key(ctx)),
            FactValue(value=value, unit=units.get(fact.get("unitRef")), fact_id=fact.get("id"), provenance="inline_xbrl"),
        )
    return contexts, facts


def _parse_units(root: ET.Element) -> dict[str | None, str]:
    out: dict[str | None, str] = {}

    def short(text: str | None) -> str:
        value = (text or "").strip()
        if ":" in value:
            value = value.split(":", 1)[1]
        return "shares" if value.lower() == "shares" else value

    for unit in root.iter(f"{{{_XBRLI_NS}}}unit"):
        unit_id = unit.get("id")
        if not unit_id:
            continue
        divide = unit.find(f"{{{_XBRLI_NS}}}divide")
        if divide is not None:
            num = divide.find(f".//{{{_XBRLI_NS}}}unitNumerator/{{{_XBRLI_NS}}}measure")
            den = divide.find(f".//{{{_XBRLI_NS}}}unitDenominator/{{{_XBRLI_NS}}}measure")
            out[unit_id] = f"{short(num.text if num is not None else None)}/{short(den.text if den is not None else None)}"
            continue
        measure = unit.find(f"{{{_XBRLI_NS}}}measure")
        if measure is not None:
            out[unit_id] = short(measure.text)
    return out


def _parse_fact_decimal(text: str, scale: str | None, sign: str | None) -> Decimal | None:
    cleaned = (
        text.replace(",", "")
        .replace("\u00a0", "")
        .replace("\u200b", "")
        .replace("$", "")
        .strip()
    )
    if cleaned in {"", "-", "\u2014", "\u2013"}:
        return None
    try:
        value = Decimal(cleaned)
        if scale not in (None, ""):
            value *= Decimal(10) ** int(scale)
        if sign == "-":
            value = -value
        return value
    except (InvalidOperation, ValueError):
        return None


def _role_uri_for(spec: StatementSpec) -> str:
    return f"http://www.aarcorp.com/role/{spec.role_suffix}"


def _resolve_role_uri(cur: Any, entity_id: str, filing_id: str, spec: StatementSpec) -> str:
    cur.execute(
        """
        SELECT role_uri, COUNT(*) AS edge_count
        FROM ref_xbrl_relationship_edge
        WHERE jurisdiction = 'US'
          AND entity_id = %s
          AND filing_id = %s
          AND linkbase_type = 'presentation'
          AND role_uri LIKE %s
        GROUP BY role_uri
        ORDER BY edge_count DESC, role_uri
        LIMIT 1
        """,
        (entity_id, filing_id, f"%/{spec.role_suffix}"),
    )
    row = cur.fetchone()
    return str(row[0]) if row else _role_uri_for(spec)


def _statement_display_sector(mapping_sector: str | None, gics_industry_group_code: object = None) -> str:
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


def _policy_sector_keys(sector_scope: str) -> list[str]:
    keys = {"", sector_scope}
    if sector_scope in {"insurance", "reit", "asset_manager_other_financial"}:
        keys.add("non_bank_financial")
    return sorted(keys)


def _load_edges(cur: Any, entity_id: str, filing_id: str, role_uri: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT parent_concept_id, child_concept_id, order_index, preferred_label, role_uri
        FROM ref_xbrl_relationship_edge
        WHERE jurisdiction = 'US'
          AND entity_id = %s
          AND filing_id = %s
          AND linkbase_type = 'presentation'
          AND role_uri = %s
        ORDER BY order_index NULLS LAST, child_concept_id
        """,
        (entity_id, filing_id, role_uri),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_taxonomy_labels(cur: Any, concepts: set[str]) -> dict[str, dict[str, Any]]:
    if not concepts:
        return {}
    cur.execute(
        """
        SELECT DISTINCT ON (concept_id)
               concept_id, label, label_terse, label_verbose, is_abstract
        FROM ref_taxonomy_element
        WHERE concept_id = ANY(%s)
        ORDER BY concept_id, taxonomy_year DESC NULLS LAST
        """,
        (list(concepts),),
    )
    return {
        row[0]: {
            "label": row[1],
            "label_terse": row[2],
            "label_verbose": row[3],
            "is_abstract": row[4],
        }
        for row in cur.fetchall()
    }


def _load_standard_labels(cur: Any, concepts: set[str], sector_scope: str = "corp") -> dict[str, tuple[str, str]]:
    if not concepts:
        return {}
    sector_keys = _policy_sector_keys(sector_scope)
    cur.execute(
        """
        SELECT DISTINCT ON (m.concept_id)
               m.concept_id, m.target_variable, r.label
        FROM map_concept_to_taxonomy_versioned m
        JOIN ref_standardized_line_items r ON r.line_item_id = m.target_variable
        WHERE m.jurisdiction IN ('US', 'BOTH')
          AND m.concept_id = ANY(%s)
          AND m.target_variable IS NOT NULL
          AND m.target_variable <> 'UNMAPPED'
          AND m.tier IS NOT NULL
          AND COALESCE(m.mapping_sector, '') = ANY(%s)
        ORDER BY m.concept_id,
                 CASE WHEN COALESCE(m.mapping_sector, '') = %s THEN 0
                      WHEN COALESCE(m.mapping_sector, '') = 'corp' THEN 1
                      ELSE 2 END,
                 m.tier,
                 m.confidence DESC NULLS LAST,
                 m.mapping_id DESC
        """,
        (list(concepts), sector_keys, sector_scope),
    )
    return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _load_statement_display_profile(
    cur: Any,
    accounting_standard: str,
    sector_scope: str,
    statement_type: str,
) -> dict[str, dict[str, Any]]:
    scopes = [sector_scope]
    if sector_scope != "corp":
        scopes.append("corp")
    cur.execute(
        """
        SELECT dp.sector_scope, dp.line_item_id, COALESCE(r.label, dp.line_item_id) AS label,
               dp.display_role, dp.display_policy, dp.display_order,
               dp.display_parent_id, dp.indent_level, dp.default_visibility,
               dp.priority_rank
        FROM ref_std_statement_display_profile dp
        LEFT JOIN ref_standardized_line_items r ON r.line_item_id = dp.line_item_id
        WHERE dp.accounting_standard = %s
          AND dp.sector_scope = ANY(%s)
          AND dp.statement_type = %s
        ORDER BY dp.line_item_id,
                 CASE WHEN dp.sector_scope = %s THEN 0 ELSE 1 END,
                 dp.display_order NULLS LAST
        """,
        (accounting_standard, scopes, statement_type, sector_scope),
    )
    profile: dict[str, dict[str, Any]] = {}
    cols = [desc[0] for desc in cur.description]
    for row in cur.fetchall():
        item = dict(zip(cols, row))
        profile.setdefault(str(item["line_item_id"]), item)
    return profile


def _load_concept_policies(
    cur: Any,
    concepts: set[str],
    std_items: set[str],
    sector_scope: str,
    fiscal_year: int | None,
    fiscal_period: str | None,
) -> list[dict[str, Any]]:
    if not concepts:
        return []
    target_variables = sorted(item for item in std_items if item)
    target_variables.append("")
    cur.execute(
        """
        SELECT policy_id, normalized_concept_id, target_variable, mapping_sector,
               policy_action, default_visibility, source_rank_penalty,
               reason_code, specificity_rank
        FROM vw_concept_target_display_policy_active p
        WHERE p.jurisdiction = 'US'
          AND p.normalized_concept_id = ANY(%s)
          AND COALESCE(p.target_variable, '') = ANY(%s)
          AND COALESCE(p.mapping_sector, '') = ANY(%s)
          AND (p.fiscal_year_from IS NULL OR %s::int IS NULL OR %s::int >= p.fiscal_year_from)
          AND (p.fiscal_year_to IS NULL OR %s::int IS NULL OR %s::int <= p.fiscal_year_to)
          AND (p.fiscal_period IS NULL OR %s::text IS NULL OR p.fiscal_period = %s::text)
        ORDER BY normalized_concept_id,
                 CASE WHEN COALESCE(target_variable, '') = '' THEN 1 ELSE 0 END,
                 specificity_rank DESC,
                 source_rank_penalty DESC,
                 policy_id DESC
        """,
        (
            list(concepts),
            target_variables,
            _policy_sector_keys(sector_scope),
            fiscal_year,
            fiscal_year,
            fiscal_year,
            fiscal_year,
            fiscal_period,
            fiscal_period,
        ),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_filing_display_overrides(
    cur: Any,
    entity_id: str,
    filing_id: str,
    spec: StatementSpec,
    role_uri: str,
) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT override_id, entity_id, filing_id, api_statement, statement_type,
               role_uri, source_concept_id, std_line_item_id, override_action,
               display_label, display_parent_concept_id,
               display_parent_std_line_item_id, value_binding_concept_id,
               display_depth, display_order, display_role, default_visibility,
               note
        FROM ref_filing_statement_display_override
        WHERE active
          AND jurisdiction = 'US'
          AND (entity_id IS NULL OR entity_id = %s)
          AND (filing_id IS NULL OR filing_id = %s)
          AND (api_statement IS NULL OR api_statement = %s)
          AND (statement_type IS NULL OR statement_type = %s)
          AND (role_uri IS NULL OR role_uri = %s)
        ORDER BY (
          CASE WHEN entity_id IS NULL THEN 0 ELSE 16 END
          + CASE WHEN filing_id IS NULL THEN 0 ELSE 8 END
          + CASE WHEN role_uri IS NULL THEN 0 ELSE 4 END
          + CASE WHEN api_statement IS NULL THEN 0 ELSE 2 END
          + CASE WHEN statement_type IS NULL THEN 0 ELSE 1 END
        ) DESC,
          override_id
        """,
        (entity_id, filing_id, spec.api_statement, spec.statement_type, role_uri),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _raw_label(concept_id: str, taxonomy: dict[str, dict[str, Any]], filing_labels: dict[str, dict[str, str]]) -> str:
    filing = filing_labels.get(concept_id) or {}
    tax = taxonomy.get(concept_id) or {}
    return (
        filing.get("terse")
        or filing.get("label")
        or tax.get("label_terse")
        or tax.get("label")
        or tax.get("label_verbose")
        or _humanize_concept(concept_id)
    )


_VISIBILITY_RANK = {
    "default": 0,
    "detail": 1,
    "supplemental": 1,
    "audit_only": 2,
    "hidden": 3,
}


def _coerce_visibility(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in _VISIBILITY_RANK:
        return text
    return None


def _merge_visibility(current: str, incoming: str | None) -> str:
    if incoming is None:
        return current
    cur = _coerce_visibility(current) or "default"
    inc = _coerce_visibility(incoming) or "default"
    return inc if _VISIBILITY_RANK[inc] > _VISIBILITY_RANK[cur] else cur


def _visibility_from_display_profile(profile: dict[str, Any]) -> str | None:
    visibility = _coerce_visibility(profile.get("default_visibility"))
    if visibility:
        return visibility
    policy = str(profile.get("display_policy") or "").upper()
    if policy == "MAIN":
        return "default"
    if policy in {"SUPPLEMENTAL", "DRILLDOWN_ONLY"}:
        return "detail"
    if policy == "HIDE":
        return "hidden"
    return None


def _visibility_from_concept_policy(policy: dict[str, Any] | None) -> str | None:
    if not policy:
        return None
    visibility = _coerce_visibility(policy.get("default_visibility"))
    action = str(policy.get("policy_action") or "").lower()
    if visibility and visibility != "default":
        return visibility
    if action in {"fallback_only", "component_only", "supplemental_only", "audit_only", "needs_review"}:
        return "detail"
    if action in {"deny_main", "mapping_change_candidate"}:
        return "hidden"
    return visibility


def _best_policy_for_node(policies: list[dict[str, Any]], node: DisplayNode) -> dict[str, Any] | None:
    candidates = [
        policy for policy in policies
        if policy.get("normalized_concept_id") == node.source_concept_id
        and (
            not policy.get("target_variable")
            or (node.std_line_item_id and policy.get("target_variable") == node.std_line_item_id)
        )
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda policy: (
            1 if node.std_line_item_id and policy.get("target_variable") == node.std_line_item_id else 0,
            int(policy.get("specificity_rank") or 0),
            int(policy.get("source_rank_penalty") or 0),
            int(policy.get("policy_id") or 0),
        ),
    )


def _fallback_display_overrides() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for concept_id, display_label in _STD_LABEL_OVERRIDES.items():
        rows.append({
            "source_concept_id": concept_id,
            "std_line_item_id": None,
            "override_action": "rename",
            "display_label": display_label,
            "default_visibility": None,
        })
    for concept_id in _DETAIL_VISIBILITY_OVERRIDES:
        rows.append({
            "source_concept_id": concept_id,
            "std_line_item_id": None,
            "override_action": "visibility",
            "display_label": None,
            "default_visibility": "detail",
        })
    for concept_id, candidates in _BINDING_CANDIDATES.items():
        for candidate in candidates:
            rows.append({
                "source_concept_id": concept_id,
                "std_line_item_id": None,
                "override_action": "bind_value",
                "value_binding_concept_id": candidate,
            })
    return rows


def _override_matches(override: dict[str, Any], node: DisplayNode) -> bool:
    source_concept_id = override.get("source_concept_id")
    std_line_item_id = override.get("std_line_item_id")
    if source_concept_id and source_concept_id != node.source_concept_id:
        return False
    if std_line_item_id and std_line_item_id != node.std_line_item_id:
        return False
    return bool(source_concept_id or std_line_item_id)


def _binding_candidates_from_overrides(overrides: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    candidates: dict[str, list[str]] = defaultdict(list)
    for override in overrides:
        if override.get("override_action") != "bind_value":
            continue
        source_concept_id = override.get("source_concept_id")
        value_binding_concept_id = override.get("value_binding_concept_id")
        if not source_concept_id or not value_binding_concept_id:
            continue
        values = candidates[str(source_concept_id)]
        value = str(value_binding_concept_id)
        if value not in values:
            values.append(value)
    return {key: tuple(values) for key, values in candidates.items()}


def _apply_display_curation(
    nodes: list[DisplayNode],
    spec: StatementSpec,
    display_profile: dict[str, dict[str, Any]],
    concept_policies: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
) -> None:
    node_by_concept: dict[str, DisplayNode] = {}
    node_by_std: dict[str, DisplayNode] = {}
    original_order = {node.node_key: idx for idx, node in enumerate(nodes)}
    for node in nodes:
        node_by_concept.setdefault(node.source_concept_id, node)
        if node.std_line_item_id:
            node_by_std.setdefault(node.std_line_item_id, node)

    for node in nodes:
        if node.display_depth == 0:
            node.display_label = _ROOT_LABELS.get(spec.api_statement, spec.standardized_label)
            node.standardized_label = spec.standardized_label
            node.default_visibility = "default"
            continue

        profile = display_profile.get(node.std_line_item_id or "")
        if profile:
            profile_label = profile.get("label")
            if profile_label:
                node.standardized_label = str(profile_label)
            node.default_visibility = _merge_visibility(node.default_visibility, _visibility_from_display_profile(profile))
            if str(profile.get("display_role") or "").upper() == "HIDDEN":
                node.default_visibility = "hidden"
            if node.display_depth <= 1:
                node.display_label = str(profile_label or node.standardized_label or node.display_label)
                display_role = str(profile.get("display_role") or "").strip()
                if display_role and display_role.upper() != "HIDDEN":
                    node.display_role = display_role
                display_order = profile.get("priority_rank") if profile.get("priority_rank") is not None else profile.get("display_order")
                if display_order is not None:
                    node.display_order = int(display_order)
                if profile.get("indent_level") is not None:
                    node.display_depth = max(1, int(profile["indent_level"]))
        elif node.display_depth <= 1 and node.std_line_item_id and not node.is_abstract:
            node.default_visibility = _merge_visibility(node.default_visibility, "detail")

        policy = _best_policy_for_node(concept_policies, node)
        node.default_visibility = _merge_visibility(node.default_visibility, _visibility_from_concept_policy(policy))

    for node in nodes:
        for override in overrides:
            if not _override_matches(override, node):
                continue
            action = str(override.get("override_action") or "").lower()
            if action == "hide":
                node.default_visibility = "hidden"
            elif action == "promote":
                node.default_visibility = "default"
            elif action == "demote":
                node.default_visibility = "detail"
            elif action == "bind_value":
                pass
            elif action == "reparent":
                parent_key = None
                parent_concept = override.get("display_parent_concept_id")
                parent_std = override.get("display_parent_std_line_item_id")
                if parent_concept and parent_concept in node_by_concept:
                    parent_key = node_by_concept[str(parent_concept)].node_key
                elif parent_std and parent_std in node_by_std:
                    parent_key = node_by_std[str(parent_std)].node_key
                if parent_key and parent_key != node.node_key:
                    node.parent_node_key = parent_key

            if override.get("display_label"):
                node.display_label = str(override["display_label"])
            if override.get("display_role"):
                node.display_role = str(override["display_role"])
            if override.get("display_order") is not None:
                node.display_order = int(override["display_order"])
            if override.get("display_depth") is not None:
                node.display_depth = max(0, int(override["display_depth"]))
            visibility = _coerce_visibility(override.get("default_visibility"))
            if visibility:
                node.default_visibility = visibility

    for node in nodes:
        profile = display_profile.get(node.std_line_item_id or "")
        parent_id = profile.get("display_parent_id") if profile else None
        if not parent_id or node.display_depth > 2:
            continue
        parent = node_by_std.get(str(parent_id))
        if parent and parent.node_key != node.node_key:
            node.parent_node_key = parent.node_key
            node.display_depth = max(node.display_depth, parent.display_depth + 1)

    _resequence_display_order(nodes, original_order)


def _resequence_display_order(nodes: list[DisplayNode], original_order: dict[str, int]) -> None:
    node_by_key = {node.node_key: node for node in nodes}
    children: dict[str | None, list[DisplayNode]] = defaultdict(list)
    for node in nodes:
        parent_key = node.parent_node_key if node.parent_node_key in node_by_key else None
        if parent_key != node.parent_node_key:
            node.parent_node_key = parent_key
        children[parent_key].append(node)
    for siblings in children.values():
        siblings.sort(key=lambda node: (node.display_order, original_order.get(node.node_key, 999999), node.node_key))

    ordered: list[DisplayNode] = []
    visited: set[str] = set()

    def visit(node: DisplayNode) -> None:
        if node.node_key in visited:
            return
        visited.add(node.node_key)
        ordered.append(node)
        for child in children.get(node.node_key, []):
            visit(child)

    for root in children.get(None, []):
        visit(root)
    for node in nodes:
        visit(node)
    for order, node in enumerate(ordered):
        node.display_order = order


def _build_display_nodes(
    spec: StatementSpec,
    edges: list[dict[str, Any]],
    taxonomy: dict[str, dict[str, Any]],
    filing_labels: dict[str, dict[str, str]],
    standard_labels: dict[str, tuple[str, str]],
) -> list[DisplayNode]:
    children: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    child_concepts: set[str] = set()
    parent_concepts: set[str] = set()
    for edge in edges:
        parent = edge.get("parent_concept_id")
        child = edge.get("child_concept_id")
        if not child:
            continue
        children[parent].append(edge)
        child_concepts.add(child)
        if parent:
            parent_concepts.add(parent)
    for items in children.values():
        items.sort(key=lambda row: (row.get("order_index") is None, row.get("order_index") or 0, row.get("child_concept_id") or ""))

    roots = sorted(parent_concepts - child_concepts)
    if not roots:
        roots = sorted({edge["child_concept_id"] for edge in edges if edge.get("child_concept_id")})

    nodes: list[DisplayNode] = []
    concept_to_first_node: dict[str, str] = {}

    def add_subtree(
        concept_id: str,
        raw_parent_concept: str | None,
        parent_node_key: str | None,
        display_depth: int,
        presentation_depth: int,
    ) -> None:
        if _is_scaffold_concept(concept_id):
            for child_edge in children.get(concept_id, []):
                add_subtree(
                    child_edge["child_concept_id"],
                    concept_id,
                    parent_node_key,
                    display_depth,
                    presentation_depth + 1,
                )
            return

        order = len(nodes)
        node_key = _node_key(order, concept_id)
        raw = _raw_label(concept_id, taxonomy, filing_labels)
        std = standard_labels.get(concept_id)
        std_line_item_id = std[0] if std else None
        std_label = std[1] if std else None
        if display_depth == 0:
            display_label = _ROOT_LABELS[spec.api_statement]
        elif display_depth <= 1:
            display_label = std_label or raw
        else:
            display_label = raw
        is_abstract = _is_abstract_concept(concept_id, (taxonomy.get(concept_id) or {}).get("is_abstract"))
        display_role = "ROOT" if display_depth == 0 else "GROUP" if is_abstract else "LINE"
        nodes.append(DisplayNode(
            node_key=node_key,
            parent_node_key=parent_node_key,
            source_concept_id=concept_id,
            source_parent_concept_id=raw_parent_concept,
            value_binding_concept_id=None,
            std_line_item_id=std_line_item_id,
            raw_label=raw,
            standardized_label=std_label,
            display_label=display_label,
            display_role=display_role,
            default_visibility="default" if display_depth <= 1 else "detail",
            is_abstract=is_abstract,
            presentation_depth=presentation_depth,
            display_depth=display_depth,
            display_order=order,
        ))
        concept_to_first_node.setdefault(concept_id, node_key)
        for child_edge in children.get(concept_id, []):
            add_subtree(
                child_edge["child_concept_id"],
                concept_id,
                node_key,
                display_depth + 1,
                presentation_depth + 1,
            )

    for root in roots:
        add_subtree(root, None, None, 0, 0)

    if len([node for node in nodes if node.display_depth == 0]) > 1:
        # A malformed or unexpected role should still render under one root.
        synthetic_key = _node_key(0, f"synthetic/{spec.statement_type}")
        for idx, node in enumerate(nodes):
            node.display_order = idx + 1
            if node.parent_node_key is None:
                node.parent_node_key = synthetic_key
                node.display_depth += 1
        nodes.insert(0, DisplayNode(
            node_key=synthetic_key,
            parent_node_key=None,
            source_concept_id=f"synthetic/{spec.statement_type}",
            source_parent_concept_id=None,
            value_binding_concept_id=None,
            std_line_item_id=None,
            raw_label=spec.statement_title,
            standardized_label=spec.standardized_label,
            display_label=spec.standardized_label,
            display_role="ROOT",
            default_visibility="default",
            is_abstract=True,
            presentation_depth=0,
            display_depth=0,
            display_order=0,
        ))
    return nodes


def _choose_columns(
    spec: StatementSpec,
    contexts: dict[str, PeriodContext],
    facts: dict[tuple[str, str], FactValue],
    raw_rows: list[dict[str, Any]],
    wanted_concepts: set[str],
) -> list[DisplayColumn]:
    counts: Counter[str] = Counter()
    context_by_key: dict[str, PeriodContext] = {}
    for concept_id, column_key in facts:
        if concept_id not in wanted_concepts:
            continue
        for ctx in contexts.values():
            if _column_key(ctx) == column_key and ctx.kind == spec.period_kind and not ctx.has_dimensions:
                counts[column_key] += 1
                context_by_key[column_key] = ctx
                break
    for row in raw_rows:
        if row["concept_id"] not in wanted_concepts:
            continue
        kind = "instant" if row["period_start"] is None else "duration"
        if kind != spec.period_kind or row["period_end"] is None:
            continue
        ctx = PeriodContext(
            context_id=f"raw:{row['concept_id']}:{row['period_end']}",
            kind=kind,
            period_start=row["period_start"],
            period_end=row["period_end"],
            has_dimensions=False,
        )
        key = _column_key(ctx)
        counts[key] += 1
        context_by_key.setdefault(key, ctx)

    selected = sorted(context_by_key.values(), key=lambda ctx: ctx.period_end, reverse=True)[:2]
    return [
        DisplayColumn(
            key=_column_key(ctx),
            label=_column_label(ctx),
            kind=ctx.kind,
            period_start=ctx.period_start,
            period_end=ctx.period_end,
            order=i,
        )
        for i, ctx in enumerate(selected)
    ]


def _load_raw_rows(cur: Any, entity_id: str, filing_id: str, concepts: set[str]) -> list[dict[str, Any]]:
    if not concepts:
        return []
    cur.execute(
        """
        SELECT concept_id, period_start, period_end, value, unit
        FROM fact_fundamentals_us
        WHERE cik = %s
          AND filing_id = %s
          AND concept_id = ANY(%s)
        """,
        (entity_id, filing_id, list(concepts)),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _values_by_column(
    columns: list[DisplayColumn],
    inline_facts: dict[tuple[str, str], FactValue],
    raw_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], FactValue]:
    out = dict(inline_facts)
    column_by_period = {(col.kind, col.period_start, col.period_end): col.key for col in columns}
    for row in raw_rows:
        kind = "instant" if row["period_start"] is None else "duration"
        key = column_by_period.get((kind, row["period_start"], row["period_end"]))
        if not key or (row["concept_id"], key) in out or row["value"] is None:
            continue
        out[(row["concept_id"], key)] = FactValue(
            value=Decimal(row["value"]),
            unit=row["unit"],
            fact_id=f"raw:{row['concept_id']}:{key}",
            provenance="raw_fact",
        )
    for col in columns:
        if col.kind != "duration":
            continue
        instant_key = f"instant_{col.period_end.isoformat()}"
        for concept_id in _DURATION_END_INSTANT_CONCEPTS:
            fact = out.get((concept_id, instant_key))
            if fact is not None and (concept_id, col.key) not in out:
                out[(concept_id, col.key)] = fact
    return out


def _apply_value_bindings(
    nodes: list[DisplayNode],
    columns: list[DisplayColumn],
    values: dict[tuple[str, str], FactValue],
    binding_candidates: dict[str, tuple[str, ...]],
) -> None:
    column_keys = [col.key for col in columns]
    for node in nodes:
        if node.value_binding_concept_id:
            continue
        candidates = binding_candidates.get(node.source_concept_id)
        if not candidates:
            continue
        for candidate in candidates:
            if any((candidate, key) in values for key in column_keys):
                node.value_binding_concept_id = candidate
                break
        if node.value_binding_concept_id is None:
            node.value_binding_concept_id = candidates[0]


def _entity_metadata(cur: Any, entity_id: str, filing_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT d.primary_ticker, f.filing_type, MAX(f.filed_date), MAX(f.fiscal_year),
               MAX(f.fiscal_period), MAX(f.period_end),
               COALESCE(d.mapping_sector, 'corp'), COALESCE(d.gics_industry_group_code, '')
        FROM fact_fundamentals_us f
        JOIN dim_company_us d ON d.cik = f.cik
        WHERE f.cik = %s
          AND f.filing_id = %s
        GROUP BY d.primary_ticker, f.filing_type, d.mapping_sector, d.gics_industry_group_code
        ORDER BY MAX(f.period_end) DESC
        LIMIT 1
        """,
        (entity_id, filing_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No raw filing facts found for {entity_id} {filing_id}")
    return {
        "ticker": row[0],
        "filing_form": row[1],
        "filed_date": row[2],
        "fiscal_year": row[3],
        "fiscal_period": row[4],
        "period_end": row[5],
        "mapping_sector": row[6],
        "gics_industry_group_code": row[7],
    }


def _remove_stale_statement_displays(cur: Any, entity_id: str, filing_id: str) -> None:
    desired_roles = [_role_uri_for(spec) for spec in _AAR_SPECS]
    cur.execute(
        """
        DELETE FROM fact_filing_statement_display
        WHERE jurisdiction = 'US'
          AND entity_id = %s
          AND filing_id = %s
          AND api_statement IN ('BS', 'IS', 'CF')
          AND NOT (role_uri = ANY(%s))
        """,
        (entity_id, filing_id, desired_roles),
    )


def _upsert_statement(
    cur: Any,
    entity_id: str,
    filing_id: str,
    spec: StatementSpec,
    meta: dict[str, Any],
    role_uri: str,
    source_path: Path | None,
) -> int:
    cur.execute(
        """
        INSERT INTO fact_filing_statement_display
            (jurisdiction, entity_id, ticker, filing_id, filing_form, filed_date,
             fiscal_year, fiscal_period, period_end, accounting_standard,
             api_statement, statement_type, statement_title,
             standardized_statement_label, role_uri, source_path)
        VALUES
            ('US', %s, %s, %s, %s, %s, %s, %s, %s, 'US_GAAP',
             %s, %s, %s, %s, %s, %s)
        ON CONFLICT (jurisdiction, entity_id, filing_id, statement_type, role_uri)
        DO UPDATE SET
            ticker = EXCLUDED.ticker,
            filing_form = EXCLUDED.filing_form,
            filed_date = EXCLUDED.filed_date,
            fiscal_year = EXCLUDED.fiscal_year,
            fiscal_period = EXCLUDED.fiscal_period,
            period_end = EXCLUDED.period_end,
            api_statement = EXCLUDED.api_statement,
            statement_title = EXCLUDED.statement_title,
            standardized_statement_label = EXCLUDED.standardized_statement_label,
            source_path = EXCLUDED.source_path,
            updated_at = now()
        RETURNING statement_display_id
        """,
        (
            entity_id,
            meta["ticker"],
            filing_id,
            meta["filing_form"],
            meta["filed_date"],
            meta["fiscal_year"],
            meta["fiscal_period"],
            meta["period_end"],
            spec.api_statement,
            spec.statement_type,
            spec.statement_title,
            spec.standardized_label,
            role_uri,
            str(source_path) if source_path else None,
        ),
    )
    return int(cur.fetchone()[0])


def _replace_statement_rows(
    cur: Any,
    statement_display_id: int,
    role_uri: str,
    fiscal_year: int | None,
    fiscal_period: str | None,
    columns: list[DisplayColumn],
    nodes: list[DisplayNode],
    values: dict[tuple[str, str], FactValue],
) -> tuple[int, int, int]:
    cur.execute("DELETE FROM fact_filing_statement_display_node WHERE statement_display_id = %s", (statement_display_id,))
    cur.execute("DELETE FROM fact_filing_statement_display_column WHERE statement_display_id = %s", (statement_display_id,))
    column_rows = [
        (
            statement_display_id,
            col.key,
            col.label,
            col.kind,
            col.period_start,
            col.period_end,
            fiscal_year,
            fiscal_period,
            col.order,
        )
        for col in columns
    ]
    columns_written = execute_values(
        cur,
        """
        INSERT INTO fact_filing_statement_display_column
            (statement_display_id, column_key, label, column_kind, period_start,
             period_end, fiscal_year, fiscal_period, column_order)
        VALUES %s
        """,
        column_rows,
    )
    node_rows = [
        (
            statement_display_id,
            node.node_key,
            node.parent_node_key,
            node.source_concept_id,
            node.source_parent_concept_id,
            node.value_binding_concept_id,
            node.std_line_item_id,
            node.raw_label,
            node.standardized_label,
            node.display_label,
            node.display_role,
            node.default_visibility,
            node.is_abstract,
            node.presentation_depth,
            node.display_depth,
            node.display_order,
            role_uri,
        )
        for node in nodes
    ]
    nodes_written = execute_values(
        cur,
        """
        INSERT INTO fact_filing_statement_display_node
            (statement_display_id, node_key, parent_node_key, source_concept_id,
             source_parent_concept_id, value_binding_concept_id, std_line_item_id,
             raw_label, standardized_label, display_label, display_role,
             default_visibility, is_abstract, presentation_depth, display_depth,
             display_order, source_role_uri)
        VALUES %s
        """,
        node_rows,
    )
    cur.execute(
        """
        SELECT node_key, node_id
        FROM fact_filing_statement_display_node
        WHERE statement_display_id = %s
        """,
        (statement_display_id,),
    )
    node_ids = {row[0]: int(row[1]) for row in cur.fetchall()}
    value_rows = []
    for node in nodes:
        value_concept = node.value_binding_concept_id or node.source_concept_id
        for col in columns:
            fact = values.get((value_concept, col.key))
            if fact is None:
                continue
            value_rows.append((
                node_ids[node.node_key],
                col.key,
                fact.value,
                fact.unit,
                value_concept,
                fact.fact_id,
                "bound_total" if node.value_binding_concept_id else fact.provenance,
            ))
    values_written = execute_values(
        cur,
        """
        INSERT INTO fact_filing_statement_display_value
            (node_id, column_key, value, unit, source_concept_id, source_fact_id, provenance)
        VALUES %s
        """,
        value_rows,
    )
    return columns_written, nodes_written, values_written


def build_filing_statement_display(
    entity_id: str = _AAR_CIK,
    filing_id: str = _AAR_FILING_ID,
    force: bool = False,
    specs: tuple[StatementSpec, ...] = _AAR_SPECS,
) -> dict[str, int]:
    """Populate filing-native display projections from DB curation rules."""
    root = _sec_root()
    stem = f"CIK{entity_id}_{filing_id}"
    html_path = root / "xbrl_html" / f"{stem}.htm"
    lab_path = root / "xbrl_lab" / f"{stem}_lab.xml"
    filing_labels = _parse_label_linkbase(lab_path)
    contexts, inline_facts = _parse_inline_xbrl(html_path)

    counts = {"statements": 0, "columns": 0, "nodes": 0, "values": 0}
    with connect() as conn, conn.cursor() as cur:
        meta = _entity_metadata(cur, entity_id, filing_id)
        sector_scope = _statement_display_sector(meta.get("mapping_sector"), meta.get("gics_industry_group_code"))
        _remove_stale_statement_displays(cur, entity_id, filing_id)
        for spec in specs:
            role_uri = _resolve_role_uri(cur, entity_id, filing_id, spec)
            edges = _load_edges(cur, entity_id, filing_id, role_uri)
            if not edges:
                continue
            concepts = {
                concept
                for edge in edges
                for concept in (edge.get("parent_concept_id"), edge.get("child_concept_id"))
                if concept
            }
            overrides = _load_filing_display_overrides(cur, entity_id, filing_id, spec, role_uri)
            if not overrides and entity_id == _AAR_CIK and filing_id == _AAR_FILING_ID:
                overrides = _fallback_display_overrides()
            binding_candidates = _binding_candidates_from_overrides(overrides)
            concepts.update(candidate for candidates in binding_candidates.values() for candidate in candidates)
            taxonomy = _load_taxonomy_labels(cur, concepts)
            standard_labels = _load_standard_labels(cur, concepts, sector_scope)
            display_profile = _load_statement_display_profile(cur, "US_GAAP", sector_scope, spec.statement_type)
            std_items = {target for target, _ in standard_labels.values() if target}
            concept_policies = _load_concept_policies(
                cur,
                concepts,
                std_items,
                sector_scope,
                meta.get("fiscal_year"),
                meta.get("fiscal_period"),
            )
            nodes = _build_display_nodes(spec, edges, taxonomy, filing_labels, standard_labels)
            _apply_display_curation(nodes, spec, display_profile, concept_policies, overrides)
            wanted = {node.source_concept_id for node in nodes}
            wanted.update(candidate for node in nodes for candidate in binding_candidates.get(node.source_concept_id, ()))
            raw_rows = _load_raw_rows(cur, entity_id, filing_id, wanted)
            columns = _choose_columns(spec, contexts, inline_facts, raw_rows, wanted)
            values = _values_by_column(columns, inline_facts, raw_rows)
            _apply_value_bindings(nodes, columns, values, binding_candidates)
            statement_id = _upsert_statement(cur, entity_id, filing_id, spec, meta, role_uri, html_path if html_path.exists() else None)
            columns_written, nodes_written, values_written = _replace_statement_rows(
                cur,
                statement_id,
                role_uri,
                meta["fiscal_year"],
                meta["fiscal_period"],
                columns,
                nodes,
                values,
            )
            counts["statements"] += 1
            counts["columns"] += columns_written
            counts["nodes"] += nodes_written
            counts["values"] += values_written
    return counts


def build_aar_filing_statement_display(entity_id: str = _AAR_CIK, filing_id: str = _AAR_FILING_ID, force: bool = False) -> dict[str, int]:
    """Populate the AAR filing-native display projection for the pilot filing."""
    return build_filing_statement_display(entity_id=entity_id, filing_id=filing_id, force=force, specs=_AAR_SPECS)


__all__ = [
    "build_filing_statement_display",
    "build_aar_filing_statement_display",
    "_apply_display_curation",
    "_build_display_nodes",
    "_is_scaffold_concept",
]
