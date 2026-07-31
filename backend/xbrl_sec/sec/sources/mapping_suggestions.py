"""Generate safe concept-to-standard-item mapping suggestions."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from psycopg2.extras import Json

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


_QUEUE_PROMPT_VERSION = "concept_mapping_review_queue_v1"
_QUEUE_SOURCE = "llm_ready_evidence_builder"
_QUEUE_MODEL = "llm_ready_guardrails_v1"
_TOKEN_RE = re.compile(r"[^a-zA-Z0-9]+")
_JP_EXTENSION_NAMESPACE_RE = re.compile(r"^jpcrp\d{6}-[^_]+_E\d+-\d+$", re.IGNORECASE)
_US_STANDARD_NAMESPACES = {
    "country",
    "currency",
    "dei",
    "exch",
    "invest",
    "naics",
    "srt",
    "stpr",
    "us-gaap",
}
_STATEMENT_CATEGORY = {
    "BalanceSheet": "balance_sheet",
    "IncomeStatement": "income_statement",
    "CashFlow": "cash_flow",
}
_CORE_STATEMENTS = {"BalanceSheet", "IncomeStatement", "CashFlow"}
_TABLE_NOISE_PATTERNS = (
    "details of",
    "specified investment",
    "major shareholder",
    "shareholding ratio",
    "number of shares held",
    "member",
    "axis",
    "table",
    "breakdown",
    "schedule",
)
_NONFUNDAMENTAL_PATTERNS = (
    "address",
    "company name",
    "description",
    "directors and other officers",
    "explanation",
    "name of",
    "note",
    "remuneration",
    "text block",
    "title",
)
_ROLLFORWARD_PATTERNS = (
    "balance at beginning",
    "balance at end",
    "changes of items during the period",
    "cumulative effects",
    "increase decrease",
    "reclassification",
    "transfer",
)
_RATE_PATTERNS = (
    "average interest rate",
    "interest rate",
    "margin",
    "percentage",
    "ratio",
    "rate",
)
_SUPPLEMENTAL_NUMERIC_PATTERNS = (
    "authorized",
    "derivative fair value",
    "number of consolidated subsidiaries",
    "number of reportable segments",
    "options outstanding",
    "par or stated value",
    "public float",
    "shares issued",
    "shares outstanding",
    "stock shares",
    "treasury stock shares",
    "unrecognized tax benefits",
    "weighted average exercise price",
)
_TREASURY_SHARE_PATTERNS = (
    "treasury stock",
    "treasury shares",
    "shares held in own name treasury shares",
    "total number of shares held treasury shares",
)
_AREA_METRIC_PATTERNS = (
    "area of ",
    "gross leasable area",
    "gross rentable area",
    "square feet",
    "square meters",
    "sq ft",
    "sqm",
)
_TAX_COMPONENT_PATTERNS = (
    "tax benefit",
    "income tax",
    "tax effect",
    "tax reconciliation",
    "effective income tax rate",
    "unrecognized tax benefits",
)
_ESG_NOISE_PATTERNS = (
    "greenhouse gas",
    "scope 1",
    "scope 2",
    "scope 3",
    "ghg emissions",
)


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _words(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_words(item) for item in value if item)
    text = str(value)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return _TOKEN_RE.sub(" ", text).lower().strip()


def _unit_bucket(unit: str | None) -> str | None:
    if not unit:
        return None
    raw = unit.upper()
    if raw in {"JPY", "USD", "EUR", "GBP", "CAD", "AUD", "CNY"} or "ISO4217" in raw:
        return "CCY"
    if "SHARE" in raw or raw in {"SHARES", "株"}:
        return "COUNT"
    if raw in {"PURE", "PERCENT", "PERCENTAGE"} or "PERCENT" in raw:
        return "PCT"
    if "PER_SHARE" in raw or "/SHARE" in raw:
        return "PER_SHARE"
    return None


def _load_targets() -> list[dict[str, Any]]:
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
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_unmapped_observations(jurisdiction: str) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                o.jurisdiction,
                o.concept_id,
                o.namespace,
                o.local_name,
                o.fiscal_year,
                o.taxonomy,
                o.accounting_standard,
                o.mapping_sector,
                o.gics_sector_code,
                o.gics_sector_name,
                o.gics_industry_group_code,
                o.gics_industry_group_name,
                o.statement_type,
                o.root_id,
                o.parent_id,
                o.concept_path,
                o.unit,
                o.label_en,
                o.label_ja,
                o.description,
                o.reporter_count,
                o.filing_count,
                o.fact_count,
                o.first_period_end,
                o.last_period_end,
                o.sample_entities,
                o.sample_filings,
                o.sample_units,
                o.sample_concept_paths
            FROM ref_concept_universe_observation o
            WHERE o.jurisdiction = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM map_concept_to_taxonomy_versioned m
                  WHERE m.concept_id IN (o.concept_id, replace(o.concept_id, ':', '/'))
                    AND m.jurisdiction IN (o.jurisdiction, 'BOTH')
                    AND m.mapping_sector IN (o.mapping_sector, 'corp', '')
                    AND COALESCE(m.effective_to_year, 9999) >= o.fiscal_year
                    AND m.effective_from_year <= o.fiscal_year
                    AND (m.gics_sector IS NULL OR m.gics_sector = o.gics_sector_code)
                    AND (m.gics_industry_group IS NULL OR m.gics_industry_group = o.gics_industry_group_code)
              )
            ORDER BY o.fact_count DESC, o.concept_id
            """,
            (jurisdiction,),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _normalized_concept_id(row: dict[str, Any]) -> str:
    concept_id = (row.get("concept_id") or "").replace(":", "/")
    namespace = row.get("namespace") or ""
    local_name = row.get("local_name") or concept_id.rsplit("/", 1)[-1]
    jurisdiction = row.get("jurisdiction")
    if jurisdiction == "JP" and local_name and _JP_EXTENSION_NAMESPACE_RE.match(namespace):
        return f"jp_extension/{local_name}"
    if jurisdiction == "US" and local_name and namespace and namespace.lower() not in _US_STANDARD_NAMESPACES:
        return f"us_extension/{local_name}"
    if namespace and local_name:
        return f"{namespace}/{local_name}"
    return concept_id


def _append_unique(group: dict[str, Any], key: str, value: Any, limit: int = 40) -> None:
    if value in (None, ""):
        return
    bucket = group.setdefault(key, [])
    if value not in bucket and len(bucket) < limit:
        bucket.append(value)


def _append_many(group: dict[str, Any], key: str, values: Any, limit: int = 40) -> None:
    if not values:
        return
    if isinstance(values, str):
        values = [values]
    for value in values:
        _append_unique(group, key, value, limit)


def _prefer_text(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current or len(candidate) > len(current):
        return candidate
    return current


def _new_group(row: dict[str, Any], normalized: str, include_gics: bool) -> dict[str, Any]:
    return {
        "jurisdiction": row.get("jurisdiction"),
        "normalized_concept_id": normalized,
        "mapping_sector": row.get("mapping_sector") or "",
        "gics_scope": "gics_conflict" if include_gics else "generic",
        "gics_sector": row.get("gics_sector_code") if include_gics else None,
        "gics_industry_group": row.get("gics_industry_group_code") if include_gics else None,
        "local_name": row.get("local_name"),
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
        "filing_count": 0,
        "reporter_count": 0,
        "fiscal_year_min": None,
        "fiscal_year_max": None,
        "first_period_end": None,
        "last_period_end": None,
        "gics_sector_name": row.get("gics_sector_name") if include_gics else None,
        "gics_industry_group_name": row.get("gics_industry_group_name") if include_gics else None,
    }


def _merge_observation(group: dict[str, Any], row: dict[str, Any]) -> None:
    group["local_name"] = _prefer_text(group.get("local_name"), row.get("local_name"))
    group["label_en"] = _prefer_text(group.get("label_en"), row.get("label_en"))
    group["label_ja"] = _prefer_text(group.get("label_ja"), row.get("label_ja"))
    group["description"] = _prefer_text(group.get("description"), row.get("description"))
    group["fact_count"] += int(row.get("fact_count") or 0)
    group["filing_count"] += int(row.get("filing_count") or 0)
    group["reporter_count"] = max(group["reporter_count"], int(row.get("reporter_count") or 0))
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
    _append_unique(group, "source_concept_ids", row.get("concept_id"), limit=80)
    _append_unique(group, "namespaces", row.get("namespace"), limit=40)
    _append_unique(group, "statement_types", row.get("statement_type"), limit=20)
    _append_unique(group, "taxonomies", row.get("taxonomy"), limit=20)
    _append_unique(group, "accounting_standards", row.get("accounting_standard"), limit=20)
    _append_unique(group, "units", row.get("unit"), limit=20)
    _append_unique(group, "root_ids", row.get("root_id"), limit=20)
    _append_unique(group, "parent_ids", row.get("parent_id"), limit=20)
    _append_unique(group, "concept_paths", row.get("concept_path"), limit=20)
    _append_many(group, "sample_entities", row.get("sample_entities"), limit=25)
    _append_many(group, "sample_filings", row.get("sample_filings"), limit=25)
    _append_many(group, "concept_paths", row.get("sample_concept_paths"), limit=30)


def _aggregate_review_groups(rows: list[dict[str, Any]], include_gics: bool) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str | None, str | None], dict[str, Any]] = {}
    for row in rows:
        normalized = _normalized_concept_id(row)
        gics_sector = row.get("gics_sector_code") if include_gics else None
        gics_group = row.get("gics_industry_group_code") if include_gics else None
        key = (
            row.get("jurisdiction") or "",
            normalized,
            row.get("mapping_sector") or "",
            gics_sector,
            gics_group,
        )
        group = grouped.get(key)
        if group is None:
            group = _new_group(row, normalized, include_gics)
            grouped[key] = group
        _merge_observation(group, row)
    groups = list(grouped.values())
    for group in groups:
        group["review_class"] = _review_class(group)
    return groups


def _review_class(group: dict[str, Any]) -> str:
    text = _words(
        [
            group.get("normalized_concept_id"),
            group.get("local_name"),
            group.get("label_en"),
            group.get("label_ja"),
            group.get("description"),
            group.get("root_ids"),
            group.get("parent_ids"),
            group.get("concept_paths"),
        ]
    )
    if any(pattern in text for pattern in _ESG_NOISE_PATTERNS):
        return "likely_exclude"
    if any(pattern in text for pattern in _TABLE_NOISE_PATTERNS) and not any(
        pattern in text for pattern in _TREASURY_SHARE_PATTERNS
    ):
        return "likely_exclude"
    if any(pattern in text for pattern in _NONFUNDAMENTAL_PATTERNS):
        return "likely_exclude"
    unit_buckets = {_unit_bucket(unit) for unit in group.get("units") or []}
    if any(pattern in text for pattern in _TREASURY_SHARE_PATTERNS):
        return "special_case_review"
    if any(pattern in text for pattern in _AREA_METRIC_PATTERNS):
        return "special_case_review"
    if any(pattern in text for pattern in _ROLLFORWARD_PATTERNS):
        return "special_case_review"
    if any(pattern in text for pattern in _SUPPLEMENTAL_NUMERIC_PATTERNS):
        return "special_case_review"
    if any(pattern in text for pattern in _TAX_COMPONENT_PATTERNS):
        return "special_case_review"
    if "PCT" in unit_buckets or any(pattern in text for pattern in _RATE_PATTERNS):
        return "special_case_review"
    statement_types = set(group.get("statement_types") or [])
    if statement_types.intersection(_CORE_STATEMENTS) and unit_buckets.intersection({"CCY", "COUNT", "PER_SHARE"}):
        return "map_candidate"
    if unit_buckets.intersection({"CCY", "COUNT", "PER_SHARE", "PCT"}):
        return "special_case_review"
    return "likely_exclude"


def _target_allowed(concept: dict[str, Any], target: dict[str, Any]) -> bool:
    mapping_sector = concept.get("mapping_sector") or ""
    non_bank_sectors = {
        "non_bank_financial",
        "insurance",
        "reit",
        "asset_manager",
        "asset_manager_other_financial",
        "other_financial",
    }
    sector_scope = target.get("sector_scope") or "universal"
    gics_sector = target.get("gics_sector") or ""
    is_bank_target = sector_scope == "gics_40_banks" or gics_sector == "40_banks"
    is_non_bank_financial_target = sector_scope in {"gics_40_insurance", "gics_40_financial_services"} or gics_sector in {
        "40_insurance",
        "40_financial_services",
    }
    if is_bank_target and mapping_sector != "bank_financial":
        return False
    if is_non_bank_financial_target and mapping_sector not in non_bank_sectors:
        return False
    return True


def _queue_decision(review_class: str, has_candidates: bool, llm_scored: bool = False) -> str:
    if review_class == "map_candidate":
        return "READY_FOR_REVIEW" if llm_scored else "NEEDS_LLM_REVIEW"
    if review_class == "special_case_review":
        return "REVIEW_SPECIAL_CASE" if llm_scored else "NEEDS_LLM_REVIEW"
    if has_candidates:
        return "LIKELY_EXCLUDE"
    return "LIKELY_EXCLUDE"


def _queue_evidence(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "concept": {
            "normalized_concept_id": group.get("normalized_concept_id"),
            "local_name": group.get("local_name"),
            "label_en": group.get("label_en"),
            "label_ja": group.get("label_ja"),
            "description": group.get("description"),
            "source_concept_ids": group.get("source_concept_ids") or [],
            "namespaces": group.get("namespaces") or [],
        },
        "scope": {
            "jurisdiction": group.get("jurisdiction"),
            "mapping_sector": group.get("mapping_sector"),
            "gics_scope": group.get("gics_scope"),
            "gics_sector": group.get("gics_sector"),
            "gics_sector_name": group.get("gics_sector_name"),
            "gics_industry_group": group.get("gics_industry_group"),
            "gics_industry_group_name": group.get("gics_industry_group_name"),
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
        },
        "usage": {
            "review_class": group.get("review_class"),
            "fact_count": int(group.get("fact_count") or 0),
            "filing_count": int(group.get("filing_count") or 0),
            "reporter_count": int(group.get("reporter_count") or 0),
            "first_period_end": group.get("first_period_end"),
            "last_period_end": group.get("last_period_end"),
            "sample_entities": group.get("sample_entities") or [],
            "sample_filings": group.get("sample_filings") or [],
        },
        "guardrails": {
            "allowed_target_count": int(group.get("allowed_target_count") or 0),
            "candidate_storage": "Targets are loaded from ref_standardized_line_items at LLM runtime; deterministic ranked candidates are not persisted.",
        },
    }


def _queue_reasoning(group: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    target_count = int(group.get("allowed_target_count") or len(candidates))
    target_text = f" {target_count} canonical targets pass hard guardrails at LLM runtime." if target_count else " No canonical target passed hard guardrails."
    return (
        f"Grouped review queue item classified as {group.get('review_class')} from normalized concept identity, "
        "statement context, units, paths, and disclosure-noise rules. "
        "No deterministic mapping has been selected."
        f"{target_text}"
    )


def _attach_candidates(groups: list[dict[str, Any]], targets: list[dict[str, Any]]) -> None:
    for group in groups:
        allowed_count = 0
        if group["review_class"] != "likely_exclude":
            allowed_count = sum(1 for target in targets if _target_allowed(group, target))
        candidates: list[dict[str, Any]] = []
        group["allowed_target_count"] = allowed_count
        group["candidate_targets"] = candidates
        group["suggested_target_variable"] = None
        group["suggested_tier"] = None
        group["suggested_multiplier"] = None
        group["top_candidate_label"] = None
        group["top_candidate_description"] = None
        group["top_candidate_category"] = None
        group["top_candidate_unit_type"] = None
        group["confidence"] = None
        group["decision"] = _queue_decision(group["review_class"], allowed_count > 0)
        group["reasoning"] = _queue_reasoning(group, candidates)


def _select_review_queue_groups(
    generic_groups: list[dict[str, Any]],
    gics_groups: list[dict[str, Any]],
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = list(generic_groups)
    selected_keys = {
        (
            group.get("jurisdiction") or "",
            group.get("normalized_concept_id") or "",
            group.get("mapping_sector") or "",
            group.get("gics_scope") or "",
            group.get("gics_sector") or "",
            group.get("gics_industry_group") or "",
        )
        for group in selected
    }
    gics_by_base: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for group in gics_groups:
        key = (
            group.get("jurisdiction") or "",
            group.get("normalized_concept_id") or "",
            group.get("mapping_sector") or "",
        )
        gics_by_base[key].append(group)
    for groups in gics_by_base.values():
        core_groups = [
            group
            for group in groups
            if group.get("review_class") in {"map_candidate", "special_case_review"}
            and group.get("gics_sector")
        ]
        sectors = {group.get("gics_sector") for group in core_groups}
        classes = {group.get("review_class") for group in core_groups}
        allowed_counts = {int(group.get("allowed_target_count") or 0) for group in core_groups}
        needs_sector_review = len(sectors) > 1 and (len(classes) > 1 or len(allowed_counts) > 1)
        if needs_sector_review:
            for group in core_groups:
                key = (
                    group.get("jurisdiction") or "",
                    group.get("normalized_concept_id") or "",
                    group.get("mapping_sector") or "",
                    group.get("gics_scope") or "",
                    group.get("gics_sector") or "",
                    group.get("gics_industry_group") or "",
                )
                if key not in selected_keys:
                    selected.append(group)
                    selected_keys.add(key)
    selected.sort(key=lambda item: (int(item.get("fact_count") or 0), int(item.get("reporter_count") or 0)), reverse=True)
    if limit is not None:
        selected = selected[:limit]
    return selected


def _write_review_queue(groups: list[dict[str, Any]]) -> int:
    rows = []
    for group in groups:
        rows.append(
            (
                group["jurisdiction"],
                group["normalized_concept_id"],
                group["mapping_sector"],
                group["gics_scope"],
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
                group.get("suggested_target_variable"),
                group.get("top_candidate_label"),
                group.get("top_candidate_description"),
                group.get("top_candidate_category"),
                group.get("top_candidate_unit_type"),
                group.get("suggested_tier"),
                1,
                group.get("confidence"),
                "queued",
                group.get("decision") or "NEEDS_CODEX_REVIEW",
                group.get("reasoning"),
                _json(_queue_evidence(group)),
                _json(group.get("candidate_targets") or []),
                _QUEUE_PROMPT_VERSION,
                _QUEUE_MODEL,
                _QUEUE_SOURCE,
            )
        )
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
                mapping_source
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
                top_candidate_label = EXCLUDED.top_candidate_label,
                top_candidate_description = EXCLUDED.top_candidate_description,
                top_candidate_category = EXCLUDED.top_candidate_category,
                top_candidate_unit_type = EXCLUDED.top_candidate_unit_type,
                suggested_tier = EXCLUDED.suggested_tier,
                suggested_multiplier = EXCLUDED.suggested_multiplier,
                confidence = EXCLUDED.confidence,
                review_status = EXCLUDED.review_status,
                decision = EXCLUDED.decision,
                reasoning = EXCLUDED.reasoning,
                evidence = EXCLUDED.evidence,
                candidate_targets = EXCLUDED.candidate_targets,
                model_name = EXCLUDED.model_name,
                updated_at = now()
            WHERE map_concept_to_taxonomy_review_queue.review_status = 'queued'
            """,
            rows,
            page_size=1000,
        )


def _reset_unreviewed_queue(jurisdiction: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM map_concept_to_taxonomy_review_queue
            WHERE jurisdiction = %s
              AND prompt_version = %s
              AND review_status = 'queued'
              AND mapping_source IN (%s, 'deterministic_review_queue_builder')
            """,
            (jurisdiction, _QUEUE_PROMPT_VERSION, _QUEUE_SOURCE),
        )
        return cur.rowcount


def build_mapping_review_queue(
    jurisdiction: str,
    limit: int | None = None,
    min_fact_count: int = 1,
    top_n: int | None = None,
) -> dict[str, int]:
    """Build LLM-ready review packets.

    ``top_n`` is retained only for CLI/API compatibility. It is intentionally
    ignored because deterministic shortlists must not bias the LLM reasoner.
    """
    ctx = start_run(jurisdiction, "mapping_review_queue", "incremental")
    try:
        observations = _load_unmapped_observations(jurisdiction)
        generic_groups = [
            group
            for group in _aggregate_review_groups(observations, include_gics=False)
            if int(group.get("fact_count") or 0) >= min_fact_count
        ]
        gics_groups = [
            group
            for group in _aggregate_review_groups(observations, include_gics=True)
            if int(group.get("fact_count") or 0) >= min_fact_count
        ]
        targets = _load_targets()
        _attach_candidates(generic_groups, targets)
        _attach_candidates(gics_groups, targets)
        selected = _select_review_queue_groups(generic_groups, gics_groups, limit)
        deleted = _reset_unreviewed_queue(jurisdiction)
        written = _write_review_queue(selected)
        finish_run(
            ctx,
            "succeeded",
            rows_in=len(observations),
            rows_out=written,
            error=(
                f"generic_groups={len(generic_groups)} "
                f"gics_groups={len(gics_groups)} selected={len(selected)} deleted={deleted}"
            ),
        )
        return {
            "observations": len(observations),
            "generic_groups": len(generic_groups),
            "gics_groups": len(gics_groups),
            "selected": len(selected),
            "deleted": deleted,
            "written": written,
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def review_queue_summary(jurisdiction: str) -> dict[str, Any]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT review_class, decision, gics_scope, COUNT(*), SUM(fact_count)
            FROM map_concept_to_taxonomy_review_queue
            WHERE jurisdiction = %s
            GROUP BY review_class, decision, gics_scope
            ORDER BY review_class, decision, gics_scope
            """,
            (jurisdiction,),
        )
        class_counts = cur.fetchall()
        cur.execute(
            """
            SELECT normalized_concept_id, mapping_sector, gics_scope, gics_sector,
                   review_class, suggested_target_variable, top_candidate_description,
                   confidence, fact_count
            FROM map_concept_to_taxonomy_review_queue
            WHERE jurisdiction = %s
            ORDER BY fact_count DESC, confidence DESC NULLS LAST
            LIMIT 25
            """,
            (jurisdiction,),
        )
        cols = [desc[0] for desc in cur.description]
        samples = [dict(zip(cols, row)) for row in cur.fetchall()]
    return {
        "jurisdiction": jurisdiction,
        "class_counts": [
            {
                "review_class": row[0],
                "decision": row[1],
                "gics_scope": row[2],
                "count": row[3],
                "fact_count": row[4],
            }
            for row in class_counts
        ],
        "top_samples": samples,
    }


def review_queue_summary_json(jurisdiction: str) -> str:
    return json.dumps(review_queue_summary(jurisdiction), default=str, ensure_ascii=False, indent=2)
