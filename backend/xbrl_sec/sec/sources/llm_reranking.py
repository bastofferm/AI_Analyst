"""LLM semantic mapping for concept-to-line-item review queue rows.

The deterministic queue builder only packages evidence and hard guardrails.
This module is the first layer that may choose ``suggested_target_variable``.
It is explicit, review-queue only, and never writes to the protected
``map_concept_to_taxonomy_versioned`` table.
"""
from __future__ import annotations

import json
import re
import site
from typing import Any

site.addsitedir(site.getusersitepackages())
import psycopg2
import psycopg2.extensions

from xbrl_sec.sec.sources.llm_client import get_llm_client, get_llm_model


_SYSTEM_PROMPT = (
    "You are a senior financial taxonomy expert. Map raw XBRL concepts to a "
    "fixed canonical line-item universe. Use labels, descriptions, taxonomy "
    "metadata, statement hierarchy, units, period/balance type, jurisdiction, "
    "mapping sector, GICS context, fact frequency, and sample filing evidence. "
    "Deterministic ordering is not evidence. If no target clearly matches, "
    "return UNMAPPED."
)

_TAX_SPECIAL_PATTERNS = (
    "tax reconciliation",
    "effective income tax rate",
    "effective tax rate reconciliation",
    "tax benefit from compensation expense",
    "nondeductible expense",
    "unrecognized tax benefits",
    "income tax effect",
)


def _compact(value: Any, limit: int = 20) -> Any:
    if isinstance(value, list):
        return value[:limit]
    return value


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def _text_blob(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, list):
            parts.extend(_text_blob(item) for item in value if item is not None)
            continue
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(value))
        parts.append(text.replace("_", " ").replace("/", " ").lower())
    return " ".join(part for part in parts if part)


def _is_tax_special_concept(group: dict[str, Any]) -> bool:
    haystack = _text_blob(
        group.get("normalized_concept_id"),
        group.get("local_name"),
        group.get("label_en"),
        group.get("label_ja"),
        group.get("description"),
    )
    return any(pattern in haystack for pattern in _TAX_SPECIAL_PATTERNS)


def _is_tax_target(target: dict[str, Any]) -> bool:
    haystack = _text_blob(
        target.get("line_item_id"),
        target.get("label"),
        target.get("description"),
        target.get("std_concept_path"),
    )
    return "tax" in haystack


def _target_lines(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for item in candidates:
        lines.append(
            " | ".join(
                str(part or "")
                for part in (
                    item.get("line_item_id"),
                    item.get("label"),
                    item.get("description"),
                    f"statement={item.get('statement_type')}",
                    f"unit={item.get('unit_type')}",
                    f"sector_scope={item.get('sector_scope')}",
                    f"gics={item.get('gics_sector')}",
                )
            )
        )
    return "\n".join(f"- {line}" for line in lines)


def _load_targets(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT line_item_id, category, label, description, unit_type,
               std_concept_path, sector_scope, gics_sector, statement_type
        FROM sec.ref_standardized_line_items
        WHERE line_item_id IS NOT NULL
          AND COALESCE(category, '') <> 'market'
        ORDER BY line_item_id
        """
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _target_allowed(group: dict[str, Any], target: dict[str, Any]) -> bool:
    mapping_sector = group.get("mapping_sector") or ""
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
    if _is_tax_special_concept(group) and not _is_tax_target(target):
        return False
    return True


def _target_payload(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_item_id": target["line_item_id"],
        "category": target.get("category"),
        "label": target.get("label"),
        "description": target.get("description"),
        "unit_type": target.get("unit_type"),
        "std_concept_path": target.get("std_concept_path"),
        "sector_scope": target.get("sector_scope"),
        "gics_sector": target.get("gics_sector"),
        "statement_type": target.get("statement_type"),
    }


def _allowed_targets(group: dict[str, Any], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_target_payload(target) for target in targets if _target_allowed(group, target)]


def build_mapping_prompt(group: dict[str, Any]) -> list[dict[str, str]]:
    """Build the LLM message list from a full review-queue row."""
    candidates = group.get("candidate_targets") or []
    evidence = group.get("evidence") or {}
    concept_evidence = {
        "queue_id": group.get("queue_id"),
        "jurisdiction": group.get("jurisdiction"),
        "normalized_concept_id": group.get("normalized_concept_id"),
        "mapping_sector": group.get("mapping_sector"),
        "gics_scope": group.get("gics_scope"),
        "gics_sector": group.get("gics_sector"),
        "gics_industry_group": group.get("gics_industry_group"),
        "local_name": group.get("local_name"),
        "label_en": group.get("label_en"),
        "label_ja": group.get("label_ja"),
        "description": group.get("description"),
        "source_concept_ids": _compact(group.get("source_concept_ids")),
        "namespaces": _compact(group.get("namespaces")),
        "fiscal_year_min": group.get("fiscal_year_min"),
        "fiscal_year_max": group.get("fiscal_year_max"),
        "statement_types": _compact(group.get("statement_types")),
        "taxonomies": _compact(group.get("taxonomies")),
        "accounting_standards": _compact(group.get("accounting_standards")),
        "units": _compact(group.get("units")),
        "root_ids": _compact(group.get("root_ids")),
        "parent_ids": _compact(group.get("parent_ids")),
        "concept_paths": _compact(group.get("concept_paths"), limit=10),
        "fact_count": group.get("fact_count"),
        "filing_count": group.get("filing_count"),
        "reporter_count": group.get("reporter_count"),
        "sample_entities": _compact(group.get("sample_entities"), limit=10),
        "sample_filings": _compact(group.get("sample_filings"), limit=10),
        "review_class": group.get("review_class"),
        "current_mapping_id": group.get("current_mapping_id"),
        "proposed_action": group.get("proposed_action"),
        "review_action_type": group.get("review_action_type"),
        "triage_priority": group.get("triage_priority"),
        "review_batch": group.get("review_batch"),
        "concept_role": group.get("concept_role"),
        "role_confidence": group.get("role_confidence"),
        "context_role_distribution": group.get("context_role_distribution"),
        "failed_check_ids": _compact(group.get("failed_check_ids")),
        "identity_sides": _compact(group.get("identity_sides")),
        "residual_improvement_pct": group.get("residual_improvement_pct"),
        "counterfactual_best_action": group.get("counterfactual_best_action"),
        "evidence_json": evidence,
    }
    user_prompt = (
        "Map this raw XBRL concept to one canonical target.\n\n"
        "RAW CONCEPT EVIDENCE:\n"
        f"{_json_text(concept_evidence)}\n\n"
        "ALLOWED CANONICAL TARGETS AFTER HARD GUARDRAILS:\n"
        f"{_target_lines(candidates)}\n\n"
        "HARD RULES:\n"
        "- Choose only a line_item_id listed above, or UNMAPPED.\n"
        "- corp and non_bank_financial concepts must not map to bank-only targets.\n"
        "- Bank concepts may map to bank-specific or generic/universal targets.\n"
        "- Text blocks, filing metadata, table/member disclosures, and nonfundamental disclosures should be UNMAPPED.\n"
        "- mapped_anomaly rows reassess an existing mapping; propose the target only if the evidence supports a reviewed fix.\n"
        "- Tier 1 means direct equivalent. Tier 2 means component/subtotal used to build a target.\n"
        "- multiplier must be 1 or -1 only.\n\n"
        "Return valid JSON only with keys: "
        "line_item_id, tier, multiplier, confidence, rationale, caveats."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _extract_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}


def _bounded_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, out))


def _clean_result(result: dict[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    line_item_id = str(result.get("line_item_id") or "UNMAPPED")
    if line_item_id not in allowed_ids:
        line_item_id = "UNMAPPED"
    try:
        tier = int(result.get("tier") or 2)
    except Exception:
        tier = 2
    if tier not in {1, 2}:
        tier = 2
    try:
        multiplier = int(result.get("multiplier") or 1)
    except Exception:
        multiplier = 1
    if multiplier not in {-1, 1}:
        multiplier = 1
    return {
        "line_item_id": line_item_id,
        "tier": tier if line_item_id != "UNMAPPED" else None,
        "multiplier": multiplier if line_item_id != "UNMAPPED" else 1,
        "confidence": _bounded_float(result.get("confidence")),
        "rationale": str(result.get("rationale") or "LLM returned no rationale."),
        "caveats": str(result.get("caveats") or ""),
    }


def _sync_candidate_audit(group: dict[str, Any], result: dict[str, Any]) -> None:
    selected = result["line_item_id"]
    audit: list[dict[str, Any]] = []
    for candidate in group.get("candidate_targets") or []:
        is_selected = candidate.get("line_item_id") == selected
        if is_selected:
            candidate = dict(candidate)
            candidate["llm_selected"] = True
            candidate["llm_confidence"] = result["confidence"]
            candidate["llm_rationale"] = result["rationale"]
            candidate["llm_caveats"] = result["caveats"]
            audit.append(candidate)
            group["top_candidate_label"] = candidate.get("label")
            group["top_candidate_description"] = candidate.get("description")
            group["top_candidate_category"] = candidate.get("category")
            group["top_candidate_unit_type"] = candidate.get("unit_type")
    group["candidate_targets_audit"] = audit


def score_candidates(
    conn: psycopg2.extensions.connection,
    group: dict[str, Any],
    client: Any | None = None,
    targets: list[dict[str, Any]] | None = None,
) -> None:
    """Run the LLM mapper for a single review-queue group in place."""
    if targets is None:
        with conn.cursor() as cur:
            targets = _load_targets(cur)
    candidates = _allowed_targets(group, targets)
    group["candidate_targets"] = candidates
    if not candidates:
        group["suggested_target_variable"] = None
        group["confidence"] = 0.0
        group["reasoning"] = "No allowed target variables after hard guardrails."
        return
    allowed_ids = {str(item.get("line_item_id")) for item in candidates if item.get("line_item_id")}
    client = client or get_llm_client()
    model = get_llm_model()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=build_mapping_prompt(group),
            temperature=0.0,
            max_tokens=600,
        )
        raw = response.choices[0].message.content or "{}"
        result = _clean_result(_extract_json(raw), allowed_ids)
    except Exception as exc:
        result = {
            "line_item_id": "UNMAPPED",
            "tier": None,
            "multiplier": 1,
            "confidence": 0.0,
            "rationale": f"LLM scoring failed: {exc}",
            "caveats": "",
        }

    group["suggested_target_variable"] = None if result["line_item_id"] == "UNMAPPED" else result["line_item_id"]
    group["suggested_tier"] = result["tier"]
    group["suggested_multiplier"] = result["multiplier"]
    group["confidence"] = result["confidence"]
    group["reasoning"] = result["rationale"] + (f" Caveats: {result['caveats']}" if result["caveats"] else "")
    _sync_candidate_audit(group, result)


def _load_queue_rows(
    cur,
    jurisdiction: str,
    limit: int | None,
    namespace_prefix: str | None = None,
    review_class: str | None = None,
    min_fact_count: int | None = None,
    queue_modulus: int | None = None,
    queue_remainder: int | None = None,
) -> list[dict[str, Any]]:
    where = [
        "jurisdiction = %s",
        "review_status = 'queued'",
        "confidence IS NULL",
    ]
    params: list[Any] = [jurisdiction]
    if namespace_prefix:
        where.append("normalized_concept_id LIKE %s")
        params.append(f"{namespace_prefix}%")
    if review_class:
        where.append("review_class = %s")
        params.append(review_class)
    if min_fact_count is not None:
        where.append("fact_count >= %s")
        params.append(min_fact_count)
    if queue_modulus is not None and queue_remainder is not None:
        where.append("MOD(queue_id, %s) = %s")
        params.extend([queue_modulus, queue_remainder])
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    cur.execute(
        f"""
        SELECT queue_id, jurisdiction, normalized_concept_id, mapping_sector,
               gics_scope, gics_sector, gics_industry_group, local_name,
               label_en, label_ja, description, source_concept_ids, namespaces,
               fiscal_year_min, fiscal_year_max, statement_types, taxonomies,
               accounting_standards, units, root_ids, parent_ids, concept_paths,
               fact_count, filing_count, reporter_count, first_period_end,
               last_period_end, sample_entities, sample_filings, review_class,
               evidence, candidate_targets, current_mapping_id, proposed_action,
               concept_role, role_confidence, failed_check_ids, identity_sides,
               residual_improvement_pct, counterfactual_best_action,
               context_role_distribution, review_action_type, triage_priority,
               review_batch
        FROM sec.map_concept_to_taxonomy_review_queue
        WHERE {" AND ".join(where)}
        ORDER BY fact_count DESC, reporter_count DESC, queue_id
        {limit_sql}
        """,
        params,
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _llm_decision(group: dict[str, Any]) -> str:
    if not group.get("suggested_target_variable"):
        return "LLM_UNMAPPED"
    review_class = group.get("review_class")
    if review_class == "mapped_anomaly":
        return "REVIEW_MAPPED_ANOMALY"
    if review_class == "special_case_review":
        return "REVIEW_SPECIAL_CASE"
    return "READY_FOR_REVIEW"


def run_one_time_reranking(
    conn: psycopg2.extensions.connection,
    jurisdiction: str,
    limit: int | None = None,
    dry_run: bool = False,
    namespace_prefix: str | None = None,
    review_class: str | None = None,
    min_fact_count: int | None = None,
    queue_modulus: int | None = None,
    queue_remainder: int | None = None,
) -> int:
    """Run an explicit LLM pass over queued review rows.

    ``dry_run`` only counts eligible rows. It does not call the LLM and does
    not update the database.
    """
    with conn.cursor() as cur:
        rows = _load_queue_rows(
            cur,
            jurisdiction,
            limit,
            namespace_prefix=namespace_prefix,
            review_class=review_class,
            min_fact_count=min_fact_count,
            queue_modulus=queue_modulus,
            queue_remainder=queue_remainder,
        )
        targets = _load_targets(cur)
    if dry_run:
        return len(rows)

    client = get_llm_client()
    scored = 0
    for group in rows:
        score_candidates(conn, group, client=client, targets=targets)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sec.map_concept_to_taxonomy_review_queue
                   SET suggested_target_variable = %s,
                       suggested_tier = %s,
                       suggested_multiplier = %s,
                       confidence = %s,
                       reasoning = %s,
                       candidate_targets = %s::jsonb,
                       top_candidate_label = %s,
                       top_candidate_description = %s,
                       top_candidate_category = %s,
                       top_candidate_unit_type = %s,
                       review_status = 'llm_scored',
                       decision = %s,
                       model_name = %s,
                       updated_at = now()
                 WHERE queue_id = %s
                """,
                (
                    group.get("suggested_target_variable"),
                    group.get("suggested_tier"),
                    group.get("suggested_multiplier"),
                    group.get("confidence"),
                    group.get("reasoning"),
                    json.dumps(group.get("candidate_targets_audit") or [], ensure_ascii=False, default=str),
                    group.get("top_candidate_label"),
                    group.get("top_candidate_description"),
                    group.get("top_candidate_category"),
                    group.get("top_candidate_unit_type"),
                    _llm_decision(group),
                    get_llm_model(),
                    group["queue_id"],
                ),
            )
        scored += 1
    return scored
