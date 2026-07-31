"""Per-ticker mapping evidence pack + DeepSeek-triage plumbing for the committee.

This module is the data + governance layer behind ``data_quality_agent_node``:

* **Evidence pack** (read-only, via ``ai_analyst._db.read_sql``): unmapped raw
  concepts for one entity ranked by value, an entity-scoped sector-compatibility
  check (the per-ticker analogue of ``audit_sector_mapping_gaps.py``), and suspect
  mappings behind broken recon traces.
* **Prompt builder** for the LLM triage (the LLM call itself lives in the node so it
  can reuse the committee's ``_invoke_structured``/``_reason`` helpers — this module
  must not import ``committee.nodes``).
* **Queue writer**: mapping proposals are written to the governed *review queue*
  ``map_concept_to_taxonomy_review_queue`` tagged ``committee_dq_agent_v1``. It NEVER
  writes ``map_concept_to_taxonomy_versioned`` — promotion stays a deliberate step in
  the existing approval flow. ``ai_analyst._db`` is forced read-only, so the write goes
  through ``xbrl_sec.sec.db.connection.connect()``.
* **Finding persistence** (phase 2): upserts findings into ``dq_finding_state`` keyed by
  the deterministic ``stable_dq_id`` and reports new/resolved deltas across runs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import Json

from . import data_quality_agent as dqa
from ._db import read_sql
from .committee.schemas import DqProposal, DqTriage, DqTriageItem

logger = logging.getLogger(__name__)

_MAPPING_SOURCE = "committee_dq_agent_v1"
_PROMPT_VERSION = "committee_dq_agent_v1"
_REVIEW_BATCH = "committee_dq_agent"
_MODEL_NAME = "committee_dq_agent"

# jurisdiction -> (raw read relation, entity column). US reads the latest-vintage
# view so one row per period; JP reads the base table (no JP "latest" view).
_RAW_READ = {
    "US": ("v_fact_fundamentals_us_latest", "cik"),
    "JP": ("fact_fundamentals_jp", "edinet_code"),
}
_ACCOUNTING_STANDARD = {"US": "US_GAAP", "JP": "JP_GAAP"}

# entity sector_scope -> compatible mapping_sector values (mirrors
# audit_sector_mapping_gaps._MAPPING_SECTOR_FOR_SCOPE).
_MAPPING_SECTOR_FOR_SCOPE = {
    "corp": ("BOTH", "corp"),
    "bank_financial": ("BOTH", "bank_financial"),
    "insurance": ("BOTH", "non_bank_financial"),
    "reit": ("BOTH", "non_bank_financial"),
    "asset_manager_other_financial": ("BOTH", "non_bank_financial"),
}
_JURISDICTION_FOR_STANDARD = {"US_GAAP": ("US", "BOTH"), "JP_GAAP": ("JP", "BOTH")}

# Concept-id fragments that are not raw numeric facts (abstracts, dimensions, text).
_CONCEPT_EXCLUDE = ("TextBlock", "Abstract", "Axis", "Domain", "Member", "Table", "LineItems", "RollForward")

_MAPPING_KINDS = {"mapping_add", "mapping_retarget", "mapping_sector_override"}
_ALLOWED_ACTIONS = {"global_mapping", "sector_scope", "sign_fix", "unmap"}

# The LLM speaks in sector *scopes* (insurance/reit/...), but the governed mapping table
# and its resolver key on mapping_sector *codes* (non_bank_financial/bank_financial/corp/BOTH).
# A mapping stored under 'insurance' would never be selected at standardization time, so we
# normalize before writing to the queue (and defensively before promotion).
_GOVERNED_MAPPING_SECTOR = {
    "insurance": "non_bank_financial",
    "reit": "non_bank_financial",
    "asset_manager_other_financial": "non_bank_financial",
    "non_bank_financial": "non_bank_financial",
    "bank_financial": "bank_financial",
    "corp": "corp",
    "both": "BOTH",
    "": "",
}


def governed_mapping_sector(value: Any) -> str:
    """Map a sector scope (or an already-governed code) to the governed mapping_sector."""
    text = _clean_str(value)
    return _GOVERNED_MAPPING_SECTOR.get(text.lower(), text)
# Degraded trace_quality values for a FACT-derived recon metric. Price-derived metrics
# (yields, volatility) carry 'computed_only' with no source concepts and are excluded
# up front, so they never reach this set.
_BAD_TRACE = {"broken", "bad", "fail", "failed", "red", "partial", "missing", "fallback", "incomplete"}


# --------------------------------------------------------------------------- pack

def build_mapping_pack(
    *,
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    sector_scope: str,
) -> dict[str, Any]:
    """Assemble the per-ticker mapping evidence pack (read-only)."""
    pack: dict[str, Any] = {
        "sector_scope": sector_scope,
        "accounting_standard": _ACCOUNTING_STANDARD.get(jurisdiction),
        "unmapped_concepts": unmapped_concepts(jurisdiction, entity_id, sector_scope),
        "sector_compatibility_gaps": sector_compatibility_gaps(jurisdiction, sector_scope),
        "suspect_mappings": suspect_mappings(ticker, jurisdiction, entity_id),
    }
    pack["is_empty"] = not (
        pack["unmapped_concepts"] or pack["sector_compatibility_gaps"] or pack["suspect_mappings"]
    )
    return pack


def unmapped_concepts(
    jurisdiction: str,
    entity_id: str | None,
    sector_scope: str,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Raw facts for the entity with no compatible governed mapping, ranked by |value|."""
    if not entity_id:
        return []
    relation, entity_col = _RAW_READ.get(jurisdiction, (None, None))
    if not relation:
        return []
    patterns = [f"%{frag}%" for frag in _CONCEPT_EXCLUDE]
    sql = f"""
        SELECT f.concept_id,
               MAX(ABS(f.value))::double precision AS max_abs_value,
               COUNT(*)::bigint AS fact_count,
               MAX(f.fiscal_year)::int AS latest_fiscal_year,
               MAX(f.statement_type) AS statement_type,
               MAX(f.concept_path) AS concept_path
        FROM {relation} f
        WHERE f.{entity_col} = %(entity_id)s
          AND f.value_type = 'ORIG'
          AND f.value IS NOT NULL
          AND f.fiscal_year IS NOT NULL
          AND NOT (f.concept_id ILIKE ANY(%(patterns)s))
          AND NOT EXISTS (
              SELECT 1
              FROM map_concept_to_taxonomy_versioned m
              WHERE m.concept_id = f.concept_id
                AND m.jurisdiction IN (%(juris)s, 'BOTH')
                AND COALESCE(m.mapping_sector, 'BOTH') IN ('BOTH', %(sector)s)
                AND m.target_variable IS NOT NULL
                AND m.target_variable <> 'UNMAPPED'
          )
        GROUP BY f.concept_id
        ORDER BY max_abs_value DESC NULLS LAST
        LIMIT %(limit)s
    """
    try:
        df = read_sql(
            sql,
            {
                "entity_id": entity_id,
                "juris": jurisdiction,
                "sector": sector_scope,
                "patterns": patterns,
                "limit": int(limit),
            },
        )
    except Exception as exc:  # noqa: BLE001 - evidence pack is advisory
        logger.warning("unmapped_concepts query failed: %s", exc)
        return []
    return dqa._records(df)


def sector_compatibility_gaps(jurisdiction: str, sector_scope: str) -> list[dict[str, Any]]:
    """Expected line items for the entity's sector whose mappings only exist under an
    incompatible mapping_sector — the per-ticker analogue of audit_sector_mapping_gaps."""
    standard = _ACCOUNTING_STANDARD.get(jurisdiction)
    scopes = _MAPPING_SECTOR_FOR_SCOPE.get(sector_scope)
    jurs = _JURISDICTION_FOR_STANDARD.get(standard or "")
    if not (standard and scopes and jurs):
        return []
    try:
        profile = dqa._records(
            read_sql(
                """
                SELECT DISTINCT statement_type, line_item_id
                FROM ref_std_statement_display_profile
                WHERE accounting_standard = %(std)s
                  AND sector_scope = %(scope)s
                  AND display_policy <> 'HIDE'
                  AND display_role <> 'CALCULATED'
                """,
                {"std": standard, "scope": sector_scope},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("display-profile query failed: %s", exc)
        return []
    if not profile:
        return []
    try:
        have_rows = dqa._records(
            read_sql(
                """
                SELECT DISTINCT target_variable
                FROM map_concept_to_taxonomy_versioned
                WHERE jurisdiction = ANY(%(jurs)s)
                  AND COALESCE(mapping_sector, 'BOTH') = ANY(%(scopes)s)
                  AND target_variable IS NOT NULL
                  AND target_variable <> 'UNMAPPED'
                """,
                {"jurs": list(jurs), "scopes": list(scopes)},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("compatible-mapping query failed: %s", exc)
        return []
    have = {str(row.get("target_variable")) for row in have_rows}
    gaps: list[dict[str, Any]] = []
    for row in profile:
        line_item = row.get("line_item_id")
        if line_item and str(line_item) not in have:
            gaps.append(
                {
                    "line_item_id": line_item,
                    "statement_type": row.get("statement_type"),
                    "sector_scope": sector_scope,
                    "accounting_standard": standard,
                }
            )
    return gaps


def suspect_mappings(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fact-derived recon rows whose trace is degraded OR whose input concepts don't resolve
    to a compatible governed mapping (the insurance-style bug), with those current mappings.

    Price-derived metrics (yields, volatility) carry no source_concept_ids and are excluded —
    they legitimately have no XBRL mapping trace, so flagging them would be noise.
    """
    try:
        rows, _warnings = dqa._detailed_recon_rows(ticker, jurisdiction, entity_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("suspect_mappings recon read failed: %s", exc)
        return []
    candidates: list[tuple[dict[str, Any], list[str], str]] = []
    concept_ids: set[str] = set()
    for row in rows:
        cids = dqa._listish(row.get("source_concept_ids"))
        if not cids:
            continue  # price-derived / non-fact metric — not a mapping concern
        trace_quality = str(row.get("trace_quality") or "").strip().lower()
        candidates.append((row, cids, trace_quality))
        concept_ids.update(cids)
    mappings = _current_mappings(list(concept_ids), jurisdiction) if concept_ids else {}
    suspect: list[dict[str, Any]] = []
    for row, cids, trace_quality in candidates:
        resolved = [mappings[cid] for cid in cids if cid in mappings]
        bad_trace = trace_quality in _BAD_TRACE
        # High-precision: only flag when NONE of the fact-derived inputs resolve to a
        # governed mapping. Partial resolution is normal (formulas reference raw concepts
        # that are intentionally unmapped) and would flood the pack with false positives.
        unmapped_inputs = bool(cids) and not resolved
        if not (bad_trace or unmapped_inputs):
            continue
        suspect.append(
            {
                "metric_id": row.get("metric_id"),
                "fiscal_year": dqa._int(row.get("fiscal_year")),
                "trace_quality": row.get("trace_quality"),
                "formula": dqa._clean(row.get("formula")),
                "source_concept_ids": cids,
                "current_mappings": resolved,
                "reason": "broken_trace" if bad_trace else "unmapped_inputs",
            }
        )
        if len(suspect) >= limit:
            break
    return suspect


def _current_mappings(concept_ids: list[str], jurisdiction: str) -> dict[str, dict[str, Any]]:
    if not concept_ids:
        return {}
    try:
        rows = dqa._records(
            read_sql(
                """
                SELECT concept_id, target_variable, mapping_sector,
                       sign_policy, multiplier::double precision AS multiplier,
                       tier, aggregation_type
                FROM map_concept_to_taxonomy_versioned
                WHERE concept_id = ANY(%(cids)s)
                  AND jurisdiction IN (%(juris)s, 'BOTH')
                """,
                {"cids": list(concept_ids), "juris": jurisdiction},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("current-mapping lookup failed: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out.setdefault(str(row.get("concept_id")), row)
    return out


# ------------------------------------------------------------------------- prompt

def build_triage_prompt(
    *,
    ticker: str,
    jurisdiction: str,
    sector_scope: str,
    report_compact: dict[str, Any],
    mapping_pack: dict[str, Any],
) -> str:
    payload = {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "sector_scope": sector_scope,
        "data_quality_report": report_compact,
        "mapping_evidence_pack": mapping_pack,
    }
    return (
        "You are the data-quality and mapping-check analyst for a single equity. You are given a "
        "deterministic data-quality report (raw/standardized/metrics/recon/Yahoo layers) and a "
        "per-ticker mapping evidence pack (unmapped raw concepts by value, sector-compatibility "
        "gaps, and suspect mappings behind broken recon traces).\n\n"
        "TASKS:\n"
        "1) triage: for each material finding (id starts with 'dq-'), classify the most likely root "
        "cause and a 1-5 priority.\n"
        "2) proposals: propose concrete, typed fixes. For mapping issues set kind to "
        "mapping_add/mapping_retarget/mapping_sector_override and fill concept_id, target_variable, "
        "mapping_sector, and proposed_action (global_mapping|sector_scope|sign_fix|unmap). For "
        "pipeline issues use reparse_filing/restandardize_entity/recompute_metrics/refresh_yahoo. "
        "Only propose a mapping when the evidence pack supports it (the concept must appear in "
        "unmapped_concepts or suspect_mappings). Proposals are advisory review-queue entries; never "
        "propose writing production mappings directly.\n"
        "3) way_forward: an ordered remediation checklist, most impactful first.\n"
        "4) narrative: 2-3 sentences for the investment committee.\n\n"
        "DOMAIN GUIDANCE: an insurer's investment-portfolio concepts (AvailableForSale / "
        "HeldToMaturity / FixedMaturity / SummaryOfInvestments) belong in investment_securities under "
        "mapping_sector='insurance', not corporate short_term_investments. Prefer "
        "benign_definition_difference when a Yahoo delta is only a definition or period difference.\n\n"
        "EVIDENCE (JSON):\n" + json.dumps(payload, default=str)[:12000]
    )


# DeepSeek JSON-mode system prompt. We use json_object mode (not tool-calling) because
# deepseek-chat's with_structured_output returns empty tool args for this nested schema,
# whereas free-form JSON generation is reliable. The shape mirrors committee.schemas.DqTriage.
TRIAGE_SYSTEM_PROMPT = (
    "You are a data-quality and XBRL-mapping analyst. Respond ONLY with a single JSON object of "
    "exactly this shape (no prose outside the JSON):\n"
    '{"triage":[{"finding_id":str,"root_cause":str,"explanation":str,"priority":int}],'
    '"proposals":[{"kind":str,"concept_id":str,"target_variable":str,"mapping_sector":str,'
    '"proposed_action":str,"confidence":number,"reasoning":str,"evidence_finding_ids":[str],'
    '"next_step":str}],"way_forward":[str],"narrative":str}\n'
    "kind is one of mapping_add, mapping_retarget, mapping_sector_override, reparse_filing, "
    "restandardize_entity, recompute_metrics, refresh_yahoo, no_action. "
    "proposed_action (mapping kinds only) is one of global_mapping, sector_scope, sign_fix, unmap. "
    "confidence is 0..1. Only propose a mapping for a concept that appears in the evidence pack. "
    "Return at most 8 triage items and at most 8 proposals; keep each explanation and reasoning "
    "under 200 characters so the JSON stays complete."
)


def parse_triage(raw: Any) -> DqTriage:
    """Build a DqTriage from a raw JSON dict, dropping malformed items (never raises)."""
    if not isinstance(raw, dict):
        return DqTriage()

    def _items(key: str, model: Any) -> list:
        out = []
        for item in raw.get(key) or []:
            if not isinstance(item, dict):
                continue
            try:
                out.append(model.model_validate(item))
            except Exception:  # noqa: BLE001 - skip one bad item, keep the rest
                continue
        return out

    return DqTriage(
        triage=_items("triage", DqTriageItem),
        proposals=_items("proposals", DqProposal),
        way_forward=[str(x) for x in (raw.get("way_forward") or []) if x][:12],
        narrative=str(raw.get("narrative") or "")[:800],
    )


# -------------------------------------------------------------------- queue write

def queue_proposals(
    proposals: list[dict[str, Any]],
    *,
    jurisdiction: str,
    ticker: str,
    entity_id: str | None,
) -> list[str]:
    """Write mapping proposals to the governed review queue (queue-only, idempotent).

    Returns the normalized_concept_ids written. Uses xbrl_sec's read-write connection
    because ai_analyst._db is forced read-only. NEVER writes the versioned mapping table.
    """
    rows = _proposal_rows(proposals, jurisdiction=jurisdiction, ticker=ticker, entity_id=entity_id)
    if not rows:
        return []
    from xbrl_sec.sec.db.connection import connect
    from xbrl_sec.sec.db.bulk import execute_values

    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, _QUEUE_INSERT_SQL, rows, page_size=200)
    return [row[1] for row in rows]


def _proposal_rows(
    proposals: list[dict[str, Any]],
    *,
    jurisdiction: str,
    ticker: str,
    entity_id: str | None,
) -> list[tuple]:
    juris = (jurisdiction or "US").upper()
    if juris not in ("US", "JP"):
        return []
    seen: set[tuple[str, str]] = set()
    rows: list[tuple] = []
    for proposal in proposals:
        kind = _clean_str(proposal.get("kind"))
        concept_id = _clean_str(proposal.get("concept_id"))
        if kind not in _MAPPING_KINDS or not concept_id:
            continue
        mapping_sector = governed_mapping_sector(proposal.get("mapping_sector"))
        key = (concept_id, mapping_sector)
        if key in seen:
            continue
        seen.add(key)
        target = _clean_str(proposal.get("target_variable")) or None
        action = _clean_str(proposal.get("proposed_action"))
        if action not in _ALLOWED_ACTIONS:
            action = "sector_scope" if kind == "mapping_sector_override" else "global_mapping"
        review_class = "special_case_review" if kind == "mapping_retarget" else "map_candidate"
        evidence = {
            "ticker": ticker,
            "entity_id": entity_id,
            "proposal_kind": kind,
            "evidence_finding_ids": [str(x) for x in (proposal.get("evidence_finding_ids") or [])],
            "next_step": _clean_str(proposal.get("next_step")),
        }
        rows.append(
            (
                juris,                                  # jurisdiction
                concept_id,                             # normalized_concept_id
                mapping_sector,                         # mapping_sector
                "generic",                              # gics_scope
                review_class,                           # review_class
                target,                                 # suggested_target_variable
                target,                                 # top_candidate_label (promote reads this)
                _clamp01(proposal.get("confidence")),   # confidence
                action,                                 # proposed_action
                [concept_id],                           # source_concept_ids
                _clean_str(proposal.get("reasoning")),  # reasoning
                Json(evidence),                         # evidence (jsonb)
                "queued",                               # review_status
                "NEEDS_CODEX_REVIEW",                   # decision
                _REVIEW_BATCH,                          # review_batch
                _PROMPT_VERSION,                        # prompt_version
                _MODEL_NAME,                            # model_name
                _MAPPING_SOURCE,                        # mapping_source
            )
        )
    return rows


_QUEUE_INSERT_SQL = """
    INSERT INTO map_concept_to_taxonomy_review_queue (
        jurisdiction, normalized_concept_id, mapping_sector, gics_scope, review_class,
        suggested_target_variable, top_candidate_label, confidence, proposed_action,
        source_concept_ids, reasoning, evidence, review_status, decision, review_batch,
        prompt_version, model_name, mapping_source
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
        review_class = EXCLUDED.review_class,
        suggested_target_variable = EXCLUDED.suggested_target_variable,
        top_candidate_label = EXCLUDED.top_candidate_label,
        confidence = EXCLUDED.confidence,
        proposed_action = EXCLUDED.proposed_action,
        source_concept_ids = EXCLUDED.source_concept_ids,
        reasoning = EXCLUDED.reasoning,
        evidence = EXCLUDED.evidence,
        review_batch = EXCLUDED.review_batch,
        model_name = EXCLUDED.model_name,
        decision = EXCLUDED.decision,
        updated_at = now()
    WHERE map_concept_to_taxonomy_review_queue.review_status = 'queued'
"""


# --------------------------------------------------------------- phase 2 persistence

def record_findings(
    report: Any,
    *,
    ticker: str,
    jurisdiction: str | None,
    entity_id: str | None,
    explained_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Upsert current findings into dq_finding_state and report run-over-run deltas.

    Degrades to empty deltas if the table is absent or the write fails (never gates a run).
    """
    findings = _report_findings(report)
    ticker_u = (ticker or "").upper()
    explained = {str(x) for x in (explained_ids or [])}
    deltas: dict[str, Any] = {"new": [], "resolved": [], "explained": [], "still_open": 0}
    if not ticker_u:
        return deltas
    try:
        from xbrl_sec.sec.db.connection import connect
        from xbrl_sec.sec.db.bulk import execute_values

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT finding_id, status FROM dq_finding_state WHERE ticker = %s", (ticker_u,))
            existing = {str(row[0]): str(row[1]) for row in cur.fetchall()}
            current_ids: set[str] = set()
            rows: list[tuple] = []
            for finding in findings:
                fid = _clean_str(finding.get("finding_id"))
                if not fid:
                    continue
                current_ids.add(fid)
                status = "explained" if fid in explained else "open"
                rows.append(
                    (
                        fid,
                        ticker_u,
                        jurisdiction,
                        entity_id,
                        _clean_str(finding.get("layer")) or None,
                        _clean_str(finding.get("severity")) or None,
                        (_clean_str(finding.get("title")))[:300] or None,
                        status,
                    )
                )
                if fid not in existing:
                    deltas["new"].append(fid)
                if fid in explained:
                    deltas["explained"].append(fid)
            if rows:
                execute_values(cur, _FINDING_UPSERT_SQL, rows, page_size=200)
            stale = [fid for fid, status in existing.items() if status == "open" and fid not in current_ids]
            if stale:
                cur.execute(
                    "UPDATE dq_finding_state SET status='resolved', resolved_at=now(), last_run_at=now() "
                    "WHERE ticker = %s AND finding_id = ANY(%s)",
                    (ticker_u, stale),
                )
                deltas["resolved"] = stale
            deltas["still_open"] = len(current_ids)
    except Exception as exc:  # noqa: BLE001 - persistence is advisory
        logger.warning("record_findings failed: %s", exc)
        deltas["note"] = type(exc).__name__
    return deltas


_FINDING_UPSERT_SQL = """
    INSERT INTO dq_finding_state
        (finding_id, ticker, jurisdiction, entity_id, layer, severity, title, status)
    VALUES %s
    ON CONFLICT (finding_id) DO UPDATE SET
        severity = EXCLUDED.severity,
        title = EXCLUDED.title,
        status = CASE
            WHEN EXCLUDED.status = 'explained' THEN 'explained'
            WHEN dq_finding_state.status = 'resolved' THEN 'open'
            ELSE dq_finding_state.status
        END,
        last_seen_at = now(),
        last_run_at = now(),
        resolved_at = NULL
"""


# ------------------------------------------------------------------------- compact

def compact_triage(agent: Any, *, max_items: int = 6) -> dict[str, Any]:
    """Compact the node's data_quality_agent output for tribunal/memo prompts."""
    if not isinstance(agent, dict) or not agent.get("available"):
        return {}
    triage = agent.get("triage") if isinstance(agent.get("triage"), dict) else {}
    proposals = triage.get("proposals") or []
    return {
        "narrative": triage.get("narrative"),
        "way_forward": (triage.get("way_forward") or [])[:max_items],
        "top_proposals": [
            {
                "kind": proposal.get("kind"),
                "concept_id": proposal.get("concept_id"),
                "target_variable": proposal.get("target_variable"),
                "mapping_sector": proposal.get("mapping_sector"),
                "confidence": proposal.get("confidence"),
                "next_step": proposal.get("next_step"),
            }
            for proposal in proposals[:max_items]
        ],
        "queued_proposal_ids": (agent.get("queued_proposal_ids") or [])[:max_items],
        "triage_skipped_reason": agent.get("triage_skipped_reason"),
        "finding_deltas": agent.get("finding_deltas"),
    }


# ------------------------------------------------------------------------- helpers

def _report_findings(report: Any) -> list[dict[str, Any]]:
    if report is None:
        return []
    if hasattr(report, "model_dump"):
        report = report.model_dump(mode="json")
    if isinstance(report, dict):
        return [finding for finding in (report.get("findings") or []) if isinstance(finding, dict)]
    return []


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clamp01(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))
