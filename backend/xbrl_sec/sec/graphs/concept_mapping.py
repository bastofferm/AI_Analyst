"""SEC concept-mapping agentic loop.

Wird vom SEC-Daily-Graph als Sub-Loop aufgerufen: für jede unbekannte
Extension-Konzept-Hülle stellt der Agent über DeepSeek + Tools einen
Mapping-Vorschlag (`ConceptMappingProposal`). Vorschläge mit
`confidence >= 0.85` werden direkt in die Mapping-Tabelle promotet, alle
anderen landen im Review-Queue.

Tools sind dünne Wrapper um die Concept-Universe-Tabellen aus Migration 017
und 088, sowie um die Mapping-Suggestions-Tabellen.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import ConceptMappingProposal
from xbrl_sec.sec.db.connection import connect


_AUTO_PROMOTE_THRESHOLD = 0.85
_REVIEW_THRESHOLD = 0.5
_MAX_LOOP_HOPS = 6
_MAX_CRITIC_HOPS = 3

_TAXONOMY_PREFIXES: dict[str, str] = {
    "us-gaap": "us-gaap:",
    "ifrs": "ifrs:",
    "dei": "dei:",
    "srt": "srt:",
}

_CONCEPT_TOKEN = re.compile(r"([A-Za-z][A-Za-z0-9-]*):([A-Za-z][A-Za-z0-9_]*)")


@tool
def taxonomy_descendants(concept: str, jurisdiction: str = "US", limit: int = 25) -> list[dict[str, Any]]:
    """Return up to `limit` descendant concepts in the same taxonomy.

    Used by the LLM to verify whether a parent/child relationship plausibly
    matches the unknown extension's role.
    """
    sql = """
        SELECT child_concept, role_uri, weight
        FROM xbrl_relationship_edge
        WHERE jurisdiction = %s
          AND parent_concept = %s
          AND relationship = 'parent-child'
        ORDER BY child_concept
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (jurisdiction, concept, limit))
            return [
                {"child_concept": row[0], "role_uri": row[1], "weight": float(row[2] or 1.0)}
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001 - tool failures returned to the LLM
        return [{"error": str(exc)[:200]}]


@tool
def concept_health_metrics(concept: str, jurisdiction: str = "US") -> dict[str, Any]:
    """Return aggregated observability data for a concept.

    Covers reporting fill rate, number of distinct filers, and the most recent
    fiscal year the concept has been observed.
    """
    sql = """
        SELECT COUNT(DISTINCT cik) AS filer_count,
               COUNT(*) AS observation_count,
               MAX(fiscal_year) AS latest_fy,
               AVG(value_completeness)::numeric(6,4) AS avg_completeness
        FROM ref_concept_universe_observation
        WHERE jurisdiction = %s AND concept_id = %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (jurisdiction, concept))
            row = cur.fetchone()
            if not row or not row[1]:
                return {"concept": concept, "observed": False}
            return {
                "concept": concept,
                "observed": True,
                "filer_count": int(row[0] or 0),
                "observation_count": int(row[1] or 0),
                "latest_fy": int(row[2]) if row[2] is not None else None,
                "avg_completeness": float(row[3] or 0.0),
            }
    except Exception as exc:  # noqa: BLE001
        return {"concept": concept, "error": str(exc)[:200]}


@tool
def similar_extension_mappings(concept_fragment: str, jurisdiction: str = "US", limit: int = 10) -> list[dict[str, Any]]:
    """Return recently approved mappings whose source name contains the fragment.

    The fragment is matched against the concept localname (case-insensitive).
    """
    sql = """
        SELECT source_concept, target_concept, target_taxonomy, confidence
        FROM mapping_suggestion_versioned
        WHERE jurisdiction = %s
          AND status = 'approved'
          AND lower(source_concept) LIKE %s
        ORDER BY decided_at DESC NULLS LAST, source_concept
        LIMIT %s
    """
    pattern = f"%{concept_fragment.lower()}%"
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (jurisdiction, pattern, limit))
            return [
                {
                    "source_concept": row[0],
                    "target_concept": row[1],
                    "target_taxonomy": row[2],
                    "confidence": float(row[3] or 0.0),
                }
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)[:200]}]


def _persist_proposal(
    proposal: ConceptMappingProposal,
    *,
    jurisdiction: str,
    thread_id: str | None,
) -> str:
    """Write the proposal into the mapping-review queue with status derived
    from the confidence threshold."""
    if proposal.confidence >= _AUTO_PROMOTE_THRESHOLD:
        status = "auto_promoted"
    elif proposal.confidence >= _REVIEW_THRESHOLD:
        status = "review_queued"
    else:
        status = "rejected"
    sql = """
        INSERT INTO mapping_review_queue
            (jurisdiction, source_concept, target_concept, target_taxonomy,
             confidence, rationale, status, classifier, classified_at, thread_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (jurisdiction, source_concept)
        DO UPDATE SET
            target_concept = EXCLUDED.target_concept,
            target_taxonomy = EXCLUDED.target_taxonomy,
            confidence = EXCLUDED.confidence,
            rationale = EXCLUDED.rationale,
            status = CASE
                WHEN mapping_review_queue.status = 'approved' THEN mapping_review_queue.status
                ELSE EXCLUDED.status
            END,
            classifier = EXCLUDED.classifier,
            classified_at = NOW()
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    jurisdiction,
                    proposal.source_concept,
                    proposal.target_concept,
                    proposal.target_taxonomy,
                    proposal.confidence,
                    proposal.rationale[:600],
                    status,
                    "deepseek_concept_mapping_agent",
                    thread_id,
                ),
            )
    except Exception:
        # Schema may not yet ship thread_id on the queue — best-effort write
        pass
    return status


def load_known_std_targets(jurisdiction: str = "US") -> set[str]:
    """Load the set of concept-IDs referenced by the standardize layer.

    A proposed target that isn't in this set will produce zero standardized
    rows — the mapping is structurally broken regardless of how confident the
    LLM was. Best-effort: on DB failure returns an empty set, and the caller
    then skips the target-existence check.
    """
    targets: set[str] = set()
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT line_item_id, std_concept_path FROM ref_standardized_line_items"
            )
            for line_item_id, path in cur.fetchall():
                if line_item_id:
                    targets.add(str(line_item_id))
                if path:
                    for prefix, name in _CONCEPT_TOKEN.findall(str(path)):
                        targets.add(f"{prefix}:{name}")
    except Exception:
        return set()
    return targets


def validate_proposal(
    proposal: ConceptMappingProposal,
    *,
    known_std_targets: set[str] | None = None,
) -> str | None:
    """Structural sanity-check on a mapping proposal.

    Returns ``None`` when the proposal looks structurally usable. Otherwise a
    short reason string suitable for feeding back to the LLM as a critique.
    Cheap: no LLM call, no full standardize run. Catches the two failure modes
    the current one-shot design silently promotes:

    - target_concept whose prefix disagrees with the declared target_taxonomy
      (e.g. taxonomy='us-gaap' with target='ifrs:Revenue')
    - target_concept that doesn't exist anywhere in the standardize schema
      (LLM hallucination). Skipped for target_taxonomy='custom' by design —
      custom is the escape hatch for genuinely new concepts.
    """
    target = (proposal.target_concept or "").strip()
    taxonomy = (proposal.target_taxonomy or "").strip()
    if not target:
        return "target_concept was empty; propose a concrete target."
    expected_prefix = _TAXONOMY_PREFIXES.get(taxonomy)
    if expected_prefix and not target.lower().startswith(expected_prefix):
        return (
            f"target_taxonomy='{taxonomy}' requires target_concept to start "
            f"with '{expected_prefix}' — you proposed '{target}'."
        )
    if taxonomy == "custom":
        return None
    if known_std_targets and target not in known_std_targets:
        return (
            f"target_concept '{target}' is not referenced anywhere in "
            f"ref_standardized_line_items — standardization would produce no "
            f"rows. Pick a concept that actually appears in the standard "
            f"line-item paths, or set target_taxonomy='custom' if it must be a "
            f"new extension."
        )
    return None


def _downgrade_to_review(proposal: ConceptMappingProposal, reason: str) -> ConceptMappingProposal:
    """After hop-cap, force the proposal into the human_review lane."""
    capped_confidence = min(proposal.confidence, _REVIEW_THRESHOLD)
    return proposal.model_copy(
        update={
            "action": "human_review",
            "confidence": capped_confidence,
            "rationale": (f"[critic-loop exhausted: {reason}] " + (proposal.rationale or ""))[:600],
        }
    )


def _build_agent(llm: ChatDeepSeek | None) -> ChatDeepSeek:
    llm = llm or ChatDeepSeek(model="deepseek-v4-flash", temperature=0.1, max_tokens=1500)
    llm.bind_tools(
        [taxonomy_descendants, concept_health_metrics, similar_extension_mappings]
    )
    return llm


_INSTRUCTIONS = (
    "You are auditing XBRL extension concepts for the SEC US-GAAP taxonomy. "
    "For the given source_concept, propose the best mapping target in us-gaap, "
    "ifrs, dei, srt, or custom. Use the available tools to verify the concept's "
    "real-world usage (filer count, recent years), find similar already-approved "
    "extensions, and inspect its parent/child relationships. Return one "
    "ConceptMappingProposal. Set action='auto_promote' only if confidence "
    "exceeds 0.85; otherwise use 'human_review'. Refuse with action='reject' "
    "when no usable signal exists."
)


def _propose_once(
    source_concept: str,
    *,
    jurisdiction: str,
    llm: ChatDeepSeek | None,
    previous_failures: list[str] | None,
) -> ConceptMappingProposal:
    """Single LLM invocation. Feedback from prior critic rounds gets appended
    to the prompt so the agent can course-correct instead of repeating the
    same mistake."""
    chat = _build_agent(llm)
    structured = chat.with_structured_output(ConceptMappingProposal)
    prompt_parts = [
        _INSTRUCTIONS,
        "",
        f"source_concept: {source_concept}",
        f"jurisdiction: {jurisdiction}",
    ]
    if previous_failures:
        prompt_parts.append("")
        prompt_parts.append(
            "Previous attempts failed the structural check. Do NOT repeat these mistakes:"
        )
        for i, reason in enumerate(previous_failures, 1):
            prompt_parts.append(f"  {i}. {reason}")
    prompt = "\n".join(prompt_parts)
    try:
        return structured.invoke(prompt)
    except Exception as exc:  # noqa: BLE001 - surfaces as a failed proposal
        return ConceptMappingProposal(
            source_concept=source_concept,
            target_concept="us-gaap:Unmapped",
            target_taxonomy="custom",
            rationale=f"LLM error: {exc}",
            similar_extensions=[],
            confidence=0.0,
            action="reject",
        )


def map_unknown_concept(
    source_concept: str,
    *,
    jurisdiction: str = "US",
    llm: ChatDeepSeek | None = None,
    thread_id: str | None = None,
    known_std_targets: set[str] | None = None,
    max_critic_hops: int = _MAX_CRITIC_HOPS,
) -> ConceptMappingProposal:
    """Critic-loop mapper: propose → validate → re-prompt with failure hints.

    Loops up to ``max_critic_hops`` times. On hop exhaustion the last proposal
    is downgraded to ``human_review`` (never silently auto-promoted) so a
    persistently-broken mapping cannot leak into the standardize layer.
    """
    if known_std_targets is None:
        known_std_targets = load_known_std_targets(jurisdiction)
    failures: list[str] = []
    proposal: ConceptMappingProposal | None = None
    last_reason: str | None = None
    for _ in range(max(1, max_critic_hops)):
        proposal = _propose_once(
            source_concept,
            jurisdiction=jurisdiction,
            llm=llm,
            previous_failures=failures or None,
        )
        reason = validate_proposal(proposal, known_std_targets=known_std_targets)
        if reason is None:
            last_reason = None
            break
        failures.append(reason)
        last_reason = reason
    assert proposal is not None
    if last_reason is not None:
        proposal = _downgrade_to_review(proposal, last_reason)
    _persist_proposal(proposal, jurisdiction=jurisdiction, thread_id=thread_id)
    return proposal


def fetch_unmapped_concepts(jurisdiction: str = "US", limit: int = 25) -> list[str]:
    """Return concepts that are observed in raw facts but lack any mapping."""
    sql = """
        SELECT DISTINCT o.concept_id
        FROM ref_concept_universe_observation o
        LEFT JOIN mapping_suggestion_versioned m
          ON m.jurisdiction = o.jurisdiction
         AND m.source_concept = o.concept_id
         AND m.status IN ('approved', 'pending')
        WHERE o.jurisdiction = %s
          AND m.source_concept IS NULL
        ORDER BY o.concept_id
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (jurisdiction, limit))
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def auto_map_batch(
    concepts: list[str] | None = None,
    *,
    jurisdiction: str = "US",
    limit: int = 25,
    llm: ChatDeepSeek | None = None,
    thread_id: str | None = None,
    max_critic_hops: int = _MAX_CRITIC_HOPS,
) -> dict[str, int]:
    """Map an explicit list of concepts or the next batch of unmapped ones.

    ``max_critic_hops`` caps the per-concept feedback loop. The known-std-target
    set is loaded once per batch and reused across all concepts to keep the
    critic cheap (single DB round-trip per batch, not per proposal)."""
    targets = concepts if concepts is not None else fetch_unmapped_concepts(jurisdiction, limit)
    auto = review = rejected = downgraded = 0
    known_std_targets = load_known_std_targets(jurisdiction)
    for concept in targets:
        proposal = map_unknown_concept(
            concept,
            jurisdiction=jurisdiction,
            llm=llm,
            thread_id=thread_id,
            known_std_targets=known_std_targets,
            max_critic_hops=max_critic_hops,
        )
        if proposal.action == "auto_promote" and proposal.confidence >= _AUTO_PROMOTE_THRESHOLD:
            auto += 1
        elif proposal.action == "human_review":
            review += 1
            if proposal.rationale and proposal.rationale.startswith("[critic-loop exhausted"):
                downgraded += 1
        else:
            rejected += 1
    return {
        "candidates": len(targets),
        "auto_promoted": auto,
        "queued_for_review": review,
        "rejected": rejected,
        "critic_downgraded": downgraded,
    }
