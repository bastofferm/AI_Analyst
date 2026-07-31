"""Whole concept-universe deterministic health review.

This pass classifies every observed concept group into one review lane. It
does not promote mappings or change facts; it only writes queue evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import json
import re
import site
from typing import Any

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
from psycopg2.extras import Json

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.mapping_health import (
    _fetch_anomalies as _fetch_mapping_anomalies,
    _review_action_type as _mapped_anomaly_review_action_type,
    _role_from_evidence,
)
from xbrl_sec.sec.sources.mapping_suggestions import (
    _append_many,
    _append_unique,
    _normalized_concept_id,
    _review_class as _unmapped_review_class,
    _target_allowed,
    _words,
)
from xbrl_sec.sec.std.versioned_mapping import (
    load_mapping_exceptions,
    load_versioned_mapping,
    select_versioned_mapping,
)
from xbrl_sec.sec.state.store import finish_run, start_run


_PROMPT_VERSION = "concept_universe_health_v1"
_SOURCE_PREFIX = "concept_universe_health_v1"
_MODEL_NAME = "deterministic_concept_health_v1"

_LANES = {
    "mapped_clean",
    "mapped_anomaly",
    "unmapped_candidate",
    "audit_only",
    "display_suppressed_candidate",
}

_CONTEXT_KEYS = (
    "primary_statement",
    "note_disclosure",
    "segment_or_schedule",
    "cash_flow_addback",
    "dimension_heavy",
    "unknown",
)
_NOISY_CONTEXT_KEYS = (
    "note_disclosure",
    "segment_or_schedule",
    "cash_flow_addback",
    "dimension_heavy",
)
_DISPLAY_SUPPRESSED_ROLES = {
    "alternate_total",
    "component",
    "contra_component",
    "disclosure_only",
    "table_member_noise",
}


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _concept_parts(concept_id: str | None) -> tuple[str, str]:
    concept = str(concept_id or "")
    if "/" in concept:
        namespace, local = concept.split("/", 1)
    elif ":" in concept:
        namespace, local = concept.split(":", 1)
    else:
        namespace, local = "", concept
    return namespace, local


def _normalized_from_concept_id(jurisdiction: str, concept_id: str) -> str:
    namespace, local = _concept_parts(concept_id)
    return _normalized_concept_id(
        {
            "jurisdiction": jurisdiction,
            "concept_id": concept_id,
            "namespace": namespace,
            "local_name": local,
        }
    )


def _empty_context_distribution() -> dict[str, int]:
    return {key: 0 for key in _CONTEXT_KEYS}


def _context_role_from_observation(row: dict[str, Any]) -> str:
    text = _words(
        [
            row.get("statement_type"),
            row.get("root_id"),
            row.get("parent_id"),
            row.get("concept_path"),
            row.get("concept_id"),
            row.get("label_en"),
            row.get("label_ja"),
            row.get("description"),
        ]
    )
    statement = str(row.get("statement_type") or "").lower()
    if re.search(r"\b(disclosure|fair value|fairvalue|maturity|policy|commitment|derivative|concentration|text block|textblock)\b", text):
        return "note_disclosure"
    if re.search(r"\b(segment|schedule|business segment|geographic|geographical|by business|by geographic|breakdown)\b", text):
        return "segment_or_schedule"
    if re.search(r"\b(cash flow|cashflow|operating activities|investing activities|financing activities)\b", text):
        return "cash_flow_addback"
    if statement in {"balancesheet", "balance_sheet", "incomestatement", "income_statement", "cashflow", "cashflowstatement", "cash_flow_statement"}:
        return "primary_statement"
    if re.search(r"\b(statement of income|statement of operations|statement of financial position|balance sheet|cash flow statement)\b", text):
        return "primary_statement"
    return "unknown"


def _add_context(group: dict[str, Any], row: dict[str, Any]) -> None:
    dist = group.setdefault("context_role_distribution", _empty_context_distribution())
    role = _context_role_from_observation(row)
    dist[role] = int(dist.get(role) or 0) + int(row.get("fact_count") or 0)


def _new_group(row: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_concept_id(row)
    namespace, local = _concept_parts(row.get("concept_id"))
    return {
        "jurisdiction": row.get("jurisdiction"),
        "normalized_concept_id": normalized,
        "mapping_sector": row.get("mapping_sector") or "",
        "gics_scope": "generic",
        "gics_sector": None,
        "gics_industry_group": None,
        "local_name": row.get("local_name") or local,
        "label_en": row.get("label_en"),
        "label_ja": row.get("label_ja"),
        "description": row.get("description"),
        "source_concept_ids": [],
        "namespaces": [],
        "statement_types": [],
        "taxonomies": [],
        "accounting_standards": [],
        "units": [],
        "root_ids": [],
        "parent_ids": [],
        "concept_paths": [],
        "sample_entities": [],
        "sample_filings": [],
        "fact_count": 0,
        "mapped_fact_count": 0,
        "unmapped_fact_count": 0,
        "filing_count": 0,
        "reporter_count": 0,
        "fiscal_year_min": None,
        "fiscal_year_max": None,
        "first_period_end": None,
        "last_period_end": None,
        "context_role_distribution": _empty_context_distribution(),
        "target_counts": Counter(),
        "mapping_id_counts": Counter(),
        "mapping_records": {},
        "mapping_sources": Counter(),
    }


def _merge_text(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current or len(candidate) > len(current):
        return candidate
    return current


def _merge_observation(group: dict[str, Any], row: dict[str, Any], mapping: dict[str, Any] | None) -> None:
    fact_count = int(row.get("fact_count") or 0)
    group["local_name"] = _merge_text(group.get("local_name"), row.get("local_name"))
    group["label_en"] = _merge_text(group.get("label_en"), row.get("label_en"))
    group["label_ja"] = _merge_text(group.get("label_ja"), row.get("label_ja"))
    group["description"] = _merge_text(group.get("description"), row.get("description"))
    group["fact_count"] += fact_count
    group["filing_count"] += int(row.get("filing_count") or 0)
    group["reporter_count"] = max(int(group.get("reporter_count") or 0), int(row.get("reporter_count") or 0))
    fiscal_year = row.get("fiscal_year")
    if fiscal_year is not None:
        group["fiscal_year_min"] = fiscal_year if group["fiscal_year_min"] is None else min(group["fiscal_year_min"], fiscal_year)
        group["fiscal_year_max"] = fiscal_year if group["fiscal_year_max"] is None else max(group["fiscal_year_max"], fiscal_year)
    first_period = row.get("first_period_end")
    if first_period is not None:
        group["first_period_end"] = first_period if group["first_period_end"] is None else min(group["first_period_end"], first_period)
    last_period = row.get("last_period_end")
    if last_period is not None:
        group["last_period_end"] = last_period if group["last_period_end"] is None else max(group["last_period_end"], last_period)

    namespace, _local = _concept_parts(row.get("concept_id"))
    _append_unique(group, "source_concept_ids", row.get("concept_id"), limit=100)
    _append_unique(group, "namespaces", row.get("namespace") or namespace, limit=50)
    _append_unique(group, "statement_types", row.get("statement_type"), limit=30)
    _append_unique(group, "taxonomies", row.get("taxonomy"), limit=30)
    _append_unique(group, "accounting_standards", row.get("accounting_standard"), limit=30)
    _append_unique(group, "units", row.get("unit"), limit=30)
    _append_unique(group, "root_ids", row.get("root_id"), limit=30)
    _append_unique(group, "parent_ids", row.get("parent_id"), limit=30)
    _append_unique(group, "concept_paths", row.get("concept_path"), limit=40)
    _append_many(group, "sample_entities", row.get("sample_entities"), limit=30)
    _append_many(group, "sample_filings", row.get("sample_filings"), limit=30)
    _append_many(group, "concept_paths", row.get("sample_concept_paths"), limit=40)
    _add_context(group, row)

    if mapping is None:
        group["unmapped_fact_count"] += fact_count
        return

    target = str(mapping.get("target_variable") or "")
    mapping_id = mapping.get("mapping_id")
    group["mapped_fact_count"] += fact_count
    if target:
        group["target_counts"][target] += fact_count
    if mapping_id is not None:
        group["mapping_id_counts"][int(mapping_id)] += fact_count
        group["mapping_records"][int(mapping_id)] = dict(mapping)
    group["mapping_sources"][str(mapping.get("mapping_source") or "versioned")] += fact_count


def _load_observations(cur, jurisdiction: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT jurisdiction, concept_id, namespace, local_name, fiscal_year,
               taxonomy, accounting_standard, mapping_sector, gics_sector_code,
               gics_sector_name, gics_industry_group_code, gics_industry_group_name,
               statement_type, root_id, parent_id, concept_path, unit, label_en,
               label_ja, description, reporter_count, filing_count, fact_count,
               first_period_end, last_period_end, sample_entities, sample_filings,
               sample_units, sample_concept_paths
        FROM ref_concept_universe_observation
        WHERE jurisdiction = %s
        ORDER BY fact_count DESC, concept_id
        """,
        (jurisdiction,),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _raw_mapping_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fiscal_year": row.get("fiscal_year"),
        "fiscal_period": None,
        "taxonomy": row.get("taxonomy"),
        "taxonomy_version": row.get("taxonomy"),
        "accounting_standard": row.get("accounting_standard"),
        "gics_sector": row.get("gics_sector_code"),
        "gics_industry_group": row.get("gics_industry_group_code"),
        "gics_industry": None,
        "gics_sub_industry": None,
    }


def _aggregate_groups(
    jurisdiction: str,
    observations: list[dict[str, Any]],
    mappings: dict[str, list[dict[str, Any]]],
    exceptions: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in observations:
        if row.get("fiscal_year") is None:
            continue
        normalized = _normalized_concept_id(row)
        key = (str(row.get("jurisdiction") or ""), normalized, str(row.get("mapping_sector") or ""))
        group = groups.get(key)
        if group is None:
            group = _new_group(row)
            groups[key] = group
        concept_id = str(row.get("concept_id") or "")
        mapping = select_versioned_mapping(
            jurisdiction,
            row.get("mapping_sector"),
            _raw_mapping_row(row),
            mappings.get(concept_id) or [],
            exceptions.get(concept_id) or [],
        )
        _merge_observation(group, row, mapping)
    return list(groups.values())


def _display_suppressed_targets(cur) -> set[str]:
    targets: set[str] = set()
    cur.execute(
        """
        SELECT DISTINCT line_item_id
        FROM ref_std_statement_display_profile
        WHERE display_policy IN ('SUPPLEMENTAL', 'DRILLDOWN_ONLY', 'HIDE')
           OR COALESCE(default_visibility, 'default') IN ('supplemental', 'audit_only', 'hidden')
        """
    )
    targets.update(str(row[0]) for row in cur.fetchall() if row[0])
    cur.execute(
        """
        SELECT DISTINCT source_id
        FROM ref_financial_display_profile
        WHERE source_type = 'line_item'
          AND COALESCE(default_visibility, 'default') IN ('supplemental', 'audit_only', 'hidden')
        """
    )
    targets.update(str(row[0]) for row in cur.fetchall() if row[0])
    return targets


def _best_target(group: dict[str, Any]) -> str | None:
    counts: Counter = group.get("target_counts") or Counter()
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _best_mapping_id(group: dict[str, Any]) -> int | None:
    counts: Counter = group.get("mapping_id_counts") or Counter()
    if not counts:
        return None
    return int(counts.most_common(1)[0][0])


def _best_mapping_record(group: dict[str, Any]) -> dict[str, Any] | None:
    mapping_id = _best_mapping_id(group)
    if mapping_id is None:
        return None
    return (group.get("mapping_records") or {}).get(mapping_id)


def _mapping_coverage_ratio(group: dict[str, Any]) -> float:
    total = int(group.get("fact_count") or 0)
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, int(group.get("mapped_fact_count") or 0) / total))


def _concept_role(group: dict[str, Any], target: str | None) -> tuple[str, float]:
    source_ids = group.get("source_concept_ids") or []
    row = {
        "concept_id": source_ids[0] if source_ids else group.get("normalized_concept_id"),
        "target_variable": target or "",
        "label_en": group.get("label_en"),
        "label_ja": group.get("label_ja"),
        "description": group.get("description"),
        "concept_paths": group.get("concept_paths") or [],
        "root_ids": group.get("root_ids") or [],
        "parent_ids": group.get("parent_ids") or [],
        "line_items": [target] if target else [],
        "fact_count": group.get("fact_count") or 0,
    }
    return _role_from_evidence(row)


def _is_display_suppressed(group: dict[str, Any], suppressed_targets: set[str]) -> bool:
    target = _best_target(group)
    if not target:
        return False
    role, confidence = _concept_role(group, target)
    group["concept_role"] = role
    group["role_confidence"] = confidence
    if target in suppressed_targets:
        group["display_suppression_reason"] = "target_display_profile"
        return True
    if role in _DISPLAY_SUPPRESSED_ROLES:
        group["display_suppression_reason"] = f"concept_role:{role}"
        return True
    dist = group.get("context_role_distribution") or {}
    primary = int(dist.get("primary_statement") or 0)
    noisy = sum(int(dist.get(key) or 0) for key in _NOISY_CONTEXT_KEYS)
    if primary == 0 and noisy > 0:
        group["display_suppression_reason"] = "no_primary_context"
        return True
    if role not in {"primary_total", "primary_line_item"} and noisy > primary * 2 and noisy >= 10:
        group["display_suppression_reason"] = "mostly_noisy_contexts"
        return True
    group["display_suppression_reason"] = None
    return False


def _unmapped_lane(group: dict[str, Any]) -> str:
    review_class = _unmapped_review_class(group)
    group["unmapped_review_class"] = review_class
    if review_class in {"map_candidate", "special_case_review"}:
        return "unmapped_candidate"
    return "audit_only"


def _classify_lane(
    group: dict[str, Any],
    anomaly: dict[str, Any] | None = None,
    suppressed_targets: set[str] | None = None,
) -> str:
    if anomaly is not None:
        return "mapped_anomaly"
    suppressed_targets = suppressed_targets or set()
    coverage = _mapping_coverage_ratio(group)
    group["mapping_coverage_ratio"] = coverage
    if coverage >= 0.999:
        if _is_display_suppressed(group, suppressed_targets):
            return "display_suppressed_candidate"
        target = _best_target(group)
        role, confidence = _concept_role(group, target)
        group["concept_role"] = role
        group["role_confidence"] = confidence
        return "mapped_clean"
    if coverage > 0:
        group["unmapped_review_class"] = "partial_mapping_gap"
        return "unmapped_candidate"
    return _unmapped_lane(group)


def _load_existing_v3_anomalies(cur, jurisdiction: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT normalized_concept_id, mapping_sector, gics_sector, gics_industry_group,
               current_mapping_id, proposed_action, concept_role, role_confidence,
               review_action_type, triage_priority, review_batch, failed_check_ids,
               identity_sides, residual_improvement_pct, counterfactual_best_action,
               fact_count, reporter_count, evidence, context_role_distribution
        FROM map_concept_to_taxonomy_review_queue
        WHERE jurisdiction = %s
          AND review_class = 'mapped_anomaly'
          AND mapping_source LIKE 'mapped_anomaly_health_v3:%%'
        """,
        (jurisdiction,),
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_fallback_v3_anomalies(cur, jurisdiction: str) -> list[dict[str, Any]]:
    out = []
    for row in _fetch_mapping_anomalies(cur, jurisdiction):
        out.append(
            {
                "normalized_concept_id": row.get("concept_id"),
                "mapping_sector": row.get("mapping_sector") or "",
                "gics_sector": row.get("gics_sector"),
                "gics_industry_group": row.get("gics_industry_group"),
                "current_mapping_id": row.get("mapping_id"),
                "proposed_action": row.get("proposed_action"),
                "concept_role": row.get("concept_role"),
                "role_confidence": row.get("role_confidence"),
                "review_action_type": row.get("review_action_type"),
                "triage_priority": row.get("triage_priority"),
                "review_batch": row.get("review_batch"),
                "failed_check_ids": row.get("failed_check_ids") or [],
                "identity_sides": row.get("identity_sides") or [],
                "residual_improvement_pct": row.get("residual_improvement_pct"),
                "counterfactual_best_action": row.get("counterfactual_best_action"),
                "fact_count": row.get("fact_count"),
                "reporter_count": row.get("reporter_count"),
                "evidence": {
                    "anomaly_type": row.get("anomaly_type"),
                    "current_mapping": {
                        "mapping_id": row.get("mapping_id"),
                        "target_variable": row.get("target_variable"),
                    },
                },
                "context_role_distribution": row.get("context_role_distribution") or {},
            }
        )
    return out


def _anomaly_key(jurisdiction: str, row: dict[str, Any]) -> tuple[str, str]:
    normalized = _normalized_from_concept_id(jurisdiction, str(row.get("normalized_concept_id") or ""))
    return normalized, str(row.get("mapping_sector") or "")


def _anomaly_source_key(jurisdiction: str, row: dict[str, Any] | None) -> tuple[str, str] | None:
    if not row:
        return None
    return _anomaly_key(jurisdiction, row)


def _load_anomaly_lookup(cur, jurisdiction: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows = _load_existing_v3_anomalies(cur, jurisdiction)
    if not rows:
        rows = _fetch_fallback_v3_anomalies(cur, jurisdiction)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = _anomaly_key(jurisdiction, row)
        current = grouped.get(key)
        if current is None or int(row.get("triage_priority") or 99) < int(current.get("triage_priority") or 99):
            grouped[key] = row
    return grouped


def _anomaly_json(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") or {}
    return evidence if isinstance(evidence, dict) else {}


def _anomaly_xbrl_evidence(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _anomaly_json(row)
    xbrl = evidence.get("xbrl_evidence") if isinstance(evidence.get("xbrl_evidence"), dict) else {}
    return xbrl if isinstance(xbrl, dict) else {}


def _anomaly_current_mapping(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _anomaly_json(row)
    mapping = evidence.get("current_mapping") if isinstance(evidence.get("current_mapping"), dict) else {}
    return mapping if isinstance(mapping, dict) else {}


def _anomaly_for_group(group: dict[str, Any], anomaly_lookup: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    normalized = str(group.get("normalized_concept_id") or "")
    sector = str(group.get("mapping_sector") or "")
    exact = anomaly_lookup.get((normalized, sector))
    if exact is not None:
        return exact
    universal = anomaly_lookup.get((normalized, ""))
    if universal is not None:
        return universal
    candidates = [row for (concept, _sector), row in anomaly_lookup.items() if concept == normalized]
    if not candidates:
        return None
    return min(candidates, key=lambda row: int(row.get("triage_priority") or 99))


def _group_from_unrepresented_anomaly(jurisdiction: str, key: tuple[str, str], anomaly: dict[str, Any]) -> dict[str, Any]:
    normalized, sector = key
    namespace, local = _concept_parts(normalized)
    xbrl = _anomaly_xbrl_evidence(anomaly)
    mapping = _anomaly_current_mapping(anomaly)
    target = mapping.get("target_variable")
    fact_count = int(anomaly.get("fact_count") or 0)
    return {
        "jurisdiction": jurisdiction,
        "normalized_concept_id": normalized,
        "mapping_sector": sector,
        "gics_scope": "generic",
        "gics_sector": anomaly.get("gics_sector"),
        "gics_industry_group": anomaly.get("gics_industry_group"),
        "local_name": local,
        "label_en": None,
        "label_ja": None,
        "description": None,
        "source_concept_ids": [normalized],
        "namespaces": [namespace] if namespace else [],
        "statement_types": xbrl.get("statement_types") or [],
        "taxonomies": xbrl.get("taxonomies") or [],
        "accounting_standards": xbrl.get("accounting_standards") or [],
        "units": xbrl.get("units") or [],
        "root_ids": xbrl.get("root_ids") or [],
        "parent_ids": xbrl.get("parent_ids") or [],
        "concept_paths": xbrl.get("concept_paths") or [],
        "sample_entities": (_anomaly_json(anomaly).get("impact") or {}).get("sample_entities") if isinstance((_anomaly_json(anomaly).get("impact") or {}), dict) else [],
        "sample_filings": (_anomaly_json(anomaly).get("impact") or {}).get("sample_filings") if isinstance((_anomaly_json(anomaly).get("impact") or {}), dict) else [],
        "fact_count": fact_count,
        "mapped_fact_count": fact_count,
        "unmapped_fact_count": 0,
        "filing_count": 0,
        "reporter_count": int(anomaly.get("reporter_count") or 0),
        "fiscal_year_min": None,
        "fiscal_year_max": None,
        "first_period_end": None,
        "last_period_end": None,
        "context_role_distribution": dict(anomaly.get("context_role_distribution") or _empty_context_distribution()),
        "target_counts": Counter({str(target): fact_count}) if target else Counter(),
        "mapping_id_counts": Counter({int(anomaly["current_mapping_id"]): fact_count}) if anomaly.get("current_mapping_id") is not None else Counter(),
        "mapping_records": {},
        "mapping_sources": Counter({"mapped_anomaly_only": fact_count}),
        "review_class": "mapped_anomaly",
        "concept_role": anomaly.get("concept_role"),
        "role_confidence": anomaly.get("role_confidence"),
        "mapping_coverage_ratio": 1.0,
        "universe_gap": True,
    }


def _append_unrepresented_anomalies(
    jurisdiction: str,
    groups: list[dict[str, Any]],
    anomaly_lookup: dict[tuple[str, str], dict[str, Any]],
) -> None:
    represented = {
        key
        for key in (_anomaly_source_key(jurisdiction, _anomaly_for_group(group, anomaly_lookup)) for group in groups)
        if key is not None
    }
    for key, anomaly in anomaly_lookup.items():
        if key not in represented:
            groups.append(_group_from_unrepresented_anomaly(jurisdiction, key, anomaly))


def _allowed_target_count(group: dict[str, Any], targets: list[dict[str, Any]]) -> int:
    if group.get("review_class") not in {"unmapped_candidate"}:
        return 0
    return sum(1 for target in targets if _target_allowed(group, target))


def _review_metadata(group: dict[str, Any], anomaly: dict[str, Any] | None) -> dict[str, Any]:
    lane = group["review_class"]
    if lane == "mapped_anomaly":
        proposed = str(anomaly.get("proposed_action") if anomaly else "needs_review")
        action_type = str(anomaly.get("review_action_type") if anomaly else "needs_review")
        priority = int(anomaly.get("triage_priority") if anomaly and anomaly.get("triage_priority") is not None else 10)
        batch = "batch_1_mapped_anomaly"
        decision = "NEEDS_MAPPING_REVIEW"
    elif lane == "unmapped_candidate":
        proposed = "global_mapping"
        action_type = "needs_review"
        priority = 20 if int(group.get("reporter_count") or 0) >= 10 else 30
        batch = "batch_2_unmapped_candidate"
        decision = "NEEDS_LLM_REVIEW"
    elif lane == "display_suppressed_candidate":
        proposed = "supplemental_only"
        action_type = "display_supplemental_only"
        priority = 40
        batch = "batch_3_display_suppressed"
        decision = "NEEDS_DISPLAY_REVIEW"
    elif lane == "audit_only":
        proposed = "unmap"
        action_type = "needs_review"
        priority = 80
        batch = "batch_4_audit_only"
        decision = "AUDIT_ONLY"
    else:
        proposed = "keep"
        action_type = "keep"
        priority = 90
        batch = "batch_5_mapped_clean"
        decision = "KEEP_NEGATIVE_EVIDENCE"
    return {
        "proposed_action": proposed,
        "review_action_type": action_type,
        "triage_priority": priority,
        "review_batch": batch,
        "decision": decision,
    }


def _current_mapping(group: dict[str, Any], anomaly: dict[str, Any] | None) -> dict[str, Any]:
    rec = _best_mapping_record(group) or {}
    anomaly_evidence = (anomaly or {}).get("evidence") or {}
    if not isinstance(anomaly_evidence, dict):
        anomaly_evidence = {}
    anomaly_mapping = anomaly_evidence.get("current_mapping") if isinstance(anomaly_evidence.get("current_mapping"), dict) else {}
    return {
        "mapping_id": _best_mapping_id(group) or (anomaly or {}).get("current_mapping_id"),
        "target_variable": _best_target(group) or anomaly_mapping.get("target_variable"),
        "tier": rec.get("tier"),
        "multiplier": str(rec.get("multiplier") or ""),
        "mapping_sector": rec.get("mapping_sector"),
        "accounting_standard": rec.get("accounting_standard"),
        "taxonomy_version": rec.get("taxonomy_version"),
        "mapping_source": rec.get("mapping_source"),
    }


def _coverage_evidence(group: dict[str, Any]) -> dict[str, Any]:
    total = int(group.get("fact_count") or 0)
    mapped = int(group.get("mapped_fact_count") or 0)
    unmapped = int(group.get("unmapped_fact_count") or 0)
    return {
        "mapped_fact_count": mapped,
        "unmapped_fact_count": unmapped,
        "total_fact_count": total,
        "coverage_ratio": _mapping_coverage_ratio(group),
    }


def _evidence(group: dict[str, Any], anomaly: dict[str, Any] | None, allowed_target_count: int) -> dict[str, Any]:
    lane = group["review_class"]
    health_reason = {
        "mapped_anomaly": "Current production mapping is present but V3 mapped-anomaly evidence exists.",
        "mapped_clean": "Current production mapping covers observed scope and no anomaly or display-suppression signal was detected.",
        "unmapped_candidate": "No complete active mapping covers observed scope and deterministic heuristics consider this concept reviewable.",
        "audit_only": "No active mapping covers observed scope and deterministic heuristics classify it as audit/noise/disclosure only.",
        "display_suppressed_candidate": "Concept is mapped but deterministic display evidence suggests it should not compete in the default UI.",
    }[lane]
    return {
        "review_class": lane,
        "health_reason": health_reason,
        "concept": {
            "normalized_concept_id": group.get("normalized_concept_id"),
            "local_name": group.get("local_name"),
            "label_en": group.get("label_en"),
            "label_ja": group.get("label_ja"),
            "description": group.get("description"),
            "source_concept_ids": group.get("source_concept_ids") or [],
            "namespaces": group.get("namespaces") or [],
            "concept_role": group.get("concept_role"),
            "role_confidence": group.get("role_confidence"),
        },
        "scope": {
            "jurisdiction": group.get("jurisdiction"),
            "mapping_sector": group.get("mapping_sector"),
            "gics_scope": group.get("gics_scope"),
            "fiscal_year_min": group.get("fiscal_year_min"),
            "fiscal_year_max": group.get("fiscal_year_max"),
        },
        "xbrl_metadata": {
            "statement_types": group.get("statement_types") or [],
            "taxonomies": group.get("taxonomies") or [],
            "accounting_standards": group.get("accounting_standards") or [],
            "units": group.get("units") or [],
            "root_ids": group.get("root_ids") or [],
            "parent_ids": group.get("parent_ids") or [],
            "concept_paths": group.get("concept_paths") or [],
            "context_role_distribution": group.get("context_role_distribution") or {},
        },
        "usage": {
            "fact_count": int(group.get("fact_count") or 0),
            "filing_count": int(group.get("filing_count") or 0),
            "reporter_count": int(group.get("reporter_count") or 0),
            "first_period_end": group.get("first_period_end"),
            "last_period_end": group.get("last_period_end"),
            "sample_entities": group.get("sample_entities") or [],
            "sample_filings": group.get("sample_filings") or [],
        },
        "mapping_coverage": _coverage_evidence(group),
        "current_mapping": _current_mapping(group, anomaly),
        "display": {
            "suppression_reason": group.get("display_suppression_reason"),
        },
        "unmapped": {
            "deterministic_review_class": group.get("unmapped_review_class"),
            "allowed_target_count": allowed_target_count,
        },
        "mapped_anomaly": anomaly or {},
    }


def _queue_reasoning(group: dict[str, Any]) -> str:
    lane = group["review_class"]
    if lane == "mapped_anomaly":
        return "Whole-universe pass classified this scoped concept as mapped_anomaly because V3 anomaly evidence exists."
    if lane == "display_suppressed_candidate":
        return f"Mapped concept is reviewable for display suppression: {group.get('display_suppression_reason') or 'display evidence'}."
    if lane == "unmapped_candidate":
        return f"Unmapped or partially mapped concept is reviewable: {group.get('unmapped_review_class') or 'coverage gap'}."
    if lane == "audit_only":
        return "Concept appears to be disclosure, table/member, text, or other audit-only noise."
    return "Mapped concept appears clean; queued as low-priority negative evidence."


def _queue_tuple(group: dict[str, Any], anomaly: dict[str, Any] | None, targets: list[dict[str, Any]]) -> tuple:
    metadata = _review_metadata(group, anomaly)
    allowed_count = _allowed_target_count(group, targets)
    evidence = _evidence(group, anomaly, allowed_count)
    mapping = evidence["current_mapping"]
    return (
        group["jurisdiction"],
        group["normalized_concept_id"],
        group.get("mapping_sector") or "",
        group.get("gics_scope") or "generic",
        group.get("gics_sector"),
        group.get("gics_industry_group"),
        group.get("local_name"),
        group.get("label_en"),
        group.get("label_ja"),
        group.get("description"),
        group.get("source_concept_ids") or [],
        group.get("namespaces") or [],
        group.get("fiscal_year_min"),
        group.get("fiscal_year_max"),
        group.get("statement_types") or [],
        group.get("taxonomies") or [],
        group.get("accounting_standards") or [],
        group.get("units") or [],
        group.get("root_ids") or [],
        group.get("parent_ids") or [],
        group.get("concept_paths") or [],
        group.get("fact_count") or 0,
        group.get("filing_count") or 0,
        group.get("reporter_count") or 0,
        group.get("first_period_end"),
        group.get("last_period_end"),
        group.get("sample_entities") or [],
        group.get("sample_filings") or [],
        group["review_class"],
        None,
        None,
        None,
        None,
        None,
        None,
        Decimal("1"),
        None,
        "queued",
        metadata["decision"],
        _queue_reasoning(group),
        _json(evidence),
        _json([]),
        _PROMPT_VERSION,
        _MODEL_NAME,
        f"{_SOURCE_PREFIX}:{group['review_class']}",
        mapping.get("mapping_id"),
        metadata["proposed_action"],
        group.get("concept_role"),
        group.get("role_confidence"),
        (anomaly or {}).get("failed_check_ids") or [],
        (anomaly or {}).get("identity_sides") or [],
        (anomaly or {}).get("residual_improvement_pct"),
        (anomaly or {}).get("counterfactual_best_action"),
        _json(group.get("context_role_distribution") or {}),
        metadata["review_action_type"],
        metadata["triage_priority"],
        metadata["review_batch"],
    )


def _write_queue(groups: list[dict[str, Any]], anomaly_lookup: dict[tuple[str, str], dict[str, Any]], targets: list[dict[str, Any]]) -> int:
    rows = []
    for group in groups:
        rows.append(_queue_tuple(group, _anomaly_for_group(group, anomaly_lookup), targets))
    if not rows:
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
            rows,
            page_size=1000,
        )


def reset_concept_health_review_queue(jurisdiction: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM map_concept_to_taxonomy_review_queue
            WHERE jurisdiction = %s
              AND review_status = 'queued'
              AND mapping_source LIKE %s
            """,
            (jurisdiction, f"{_SOURCE_PREFIX}:%"),
        )
        return cur.rowcount


def _classify_groups(
    groups: list[dict[str, Any]],
    anomaly_lookup: dict[tuple[str, str], dict[str, Any]],
    suppressed_targets: set[str],
) -> None:
    for group in groups:
        anomaly = _anomaly_for_group(group, anomaly_lookup)
        lane = _classify_lane(group, anomaly=anomaly, suppressed_targets=suppressed_targets)
        group["review_class"] = lane
        if lane == "mapped_anomaly" and anomaly:
            group["concept_role"] = anomaly.get("concept_role") or group.get("concept_role")
            group["role_confidence"] = anomaly.get("role_confidence") or group.get("role_confidence")


def _lane_counts(groups: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {lane: 0 for lane in sorted(_LANES)}
    for group in groups:
        lane = str(group.get("review_class") or "")
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def build_concept_health_review_queue(
    jurisdiction: str,
    limit: int | None = None,
    min_fact_count: int = 1,
    dry_run: bool = False,
    reset_existing: bool = False,
) -> dict[str, int]:
    """Build deterministic whole-universe concept health queue rows."""
    jurisdiction = jurisdiction.upper()
    if jurisdiction not in {"US", "JP"}:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")

    ctx = start_run(jurisdiction, "concept_health", "validate" if dry_run else "incremental")
    try:
        mappings = load_versioned_mapping(jurisdiction)
        exceptions = load_mapping_exceptions(jurisdiction)
        with connect() as conn, conn.cursor() as cur:
            observations = _load_observations(cur, jurisdiction)
            if not observations:
                raise ValueError(f"No concept-universe observations for {jurisdiction}; run concept-universe first.")
            anomaly_lookup = _load_anomaly_lookup(cur, jurisdiction)
            suppressed_targets = _display_suppressed_targets(cur)

        groups = _aggregate_groups(jurisdiction, observations, mappings, exceptions)
        groups = [group for group in groups if int(group.get("fact_count") or 0) >= min_fact_count]
        _classify_groups(groups, anomaly_lookup, suppressed_targets)
        _append_unrepresented_anomalies(jurisdiction, groups, anomaly_lookup)
        groups.sort(
            key=lambda item: (
                int(item.get("triage_priority") or 99),
                int(item.get("fact_count") or 0),
                int(item.get("reporter_count") or 0),
            ),
            reverse=True,
        )
        if limit is not None:
            groups = groups[:limit]
        counts = _lane_counts(groups)
        reset_count = 0
        written = 0
        if not dry_run:
            if reset_existing:
                reset_count = reset_concept_health_review_queue(jurisdiction)
            targets = []
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT line_item_id, category, label, description, unit_type,
                           std_concept_path, sector_scope, gics_sector, statement_type
                    FROM ref_standardized_line_items
                    WHERE line_item_id IS NOT NULL
                      AND COALESCE(category, '') <> 'market'
                    ORDER BY line_item_id
                    """
                )
                cols = [desc[0] for desc in cur.description]
                targets = [dict(zip(cols, row)) for row in cur.fetchall()]
            written = _write_queue(groups, anomaly_lookup, targets)
        finish_run(ctx, "succeeded", rows_in=len(observations), rows_out=written)
        out = {
            "observations": len(observations),
            "groups": len(groups),
            "queued": written,
            "reset": reset_count,
            "mapped_anomaly_source_groups": len(anomaly_lookup),
        }
        out.update({f"lane_{key}": value for key, value in sorted(counts.items())})
        return out
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
