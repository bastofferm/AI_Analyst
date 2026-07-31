"""Step 3 of the long-tail mapping fix plan: LLM scoring of entity-gap clusters.

For each unscored cluster (filtered by entity_count threshold), build a compact
prompt with concept_id + calc-parent evidence + sample tickers, ask DeepSeek
for a target proposal with aggregation_type + sign_policy + confidence + a
short reasoning, and write the result back to map_entity_gap_cluster.

Cost: each DeepSeek call is ~$0.0025. The --min-entities filter is the
primary cost control.

Usage::

    python -m xbrl_sec.sec.sources.score_entity_gap_clusters \\
        --batch entity_gap_202606_us_v2 --min-entities 10 \\
        --limit 1000 --dry-run

    python -m xbrl_sec.sec.sources.score_entity_gap_clusters \\
        --batch entity_gap_202606_us_v2 --min-entities 10
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.llm_client import get_llm_client, get_llm_model


_SYSTEM_PROMPT = (
    "You are a senior financial-statement taxonomy expert. You map raw US-GAAP "
    "or JP-GAAP XBRL concepts to a small canonical set of standardized "
    "line_item_ids. You consider: the concept's name, its calculation-linkbase "
    "parent in real filings, the sector context (corp/bank/insurance/REIT/"
    "asset-manager), the entity count, and example tickers that file the concept.\n\n"
    "Output JSON only with these fields:\n"
    "  target_variable: a line_item_id from the provided allowed list, OR null if UNMAPPED.\n"
    "  aggregation_type: one of ROOT, CHILD_SUM, DIRECT, FALLBACK_TOTAL, EXCLUDE.\n"
    "    ROOT          = filed total (use it directly).\n"
    "    DIRECT        = single concept maps 1:1 (no other ROOT exists for this target).\n"
    "    CHILD_SUM     = component to be summed under target when no ROOT is reported.\n"
    "    FALLBACK_TOTAL= alternative total when the preferred ROOT is absent.\n"
    "    EXCLUDE       = audit/display-only; do not standardize.\n"
    "  sign_policy: one of as_reported, flip, force_negative, force_positive.\n"
    "  confidence: float 0.0 to 1.0 (your subjective confidence in this proposal).\n"
    "  reasoning: 1-2 sentence rationale citing concept semantics or parent.\n\n"
    "Hard rules:\n"
    "  * Do NOT map a P&L expense component to a balance-sheet liability target.\n"
    "  * Do NOT map an income-statement subtotal to itself plus its parent (would double-count).\n"
    "  * If the concept is a footnote disclosure (tax reconciliation, segment table, "
    "rate, ratio, member, axis), return target_variable=null and decision=UNMAPPED.\n"
    "  * If you are uncertain (confidence < 0.5), prefer target_variable=null over guessing.\n"
    "  * Capitalized/deduction concepts (e.g. InterestCostsCapitalized) usually need sign_policy=flip.\n"
)


_NOISE_PATTERNS = (
    "TextBlock", "Abstract", "Axis", "Domain", "Member", "Table", "LineItems",
    "RollForward", "Reconciliation",
)


def _looks_like_noise(concept_id: str) -> bool:
    local = concept_id.split("/", 1)[-1] if "/" in concept_id else concept_id
    return any(p in local for p in _NOISE_PATTERNS)


def _load_clusters(
    cur,
    batch: str,
    min_entities: int,
    limit: int | None,
    rescore: bool,
    jurisdiction: str | None,
) -> list[dict[str, Any]]:
    where = [
        "cluster_batch = %s",
        "gap_kind = 'unmapped_concept'",
        "normalized_concept_id IS NOT NULL",
        "entity_count >= %s",
    ]
    params: list[Any] = [batch, min_entities]
    if not rescore:
        where.append("llm_decision IS NULL")
    if jurisdiction:
        where.append("jurisdiction = %s")
        params.append(jurisdiction)
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    cur.execute(
        f"""
        SELECT cluster_id, jurisdiction, mapping_sector, normalized_concept_id,
               inferred_target_line_item, proposed_aggregation_type, proposed_sign_policy,
               entity_count, total_fact_count, sample_tickers,
               calc_parent_concept_id, calc_parent_support_pct,
               linkbase_only_eligible, entity_specificity, gap_kind
        FROM map_entity_gap_cluster
        WHERE {' AND '.join(where)}
        ORDER BY entity_count DESC, cluster_id
        {limit_sql}
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_allowed_targets(cur, mapping_sector: str, jurisdiction: str) -> list[str]:
    standard = "US_GAAP" if jurisdiction == "US" else "JP_GAAP"
    # sector_scope candidates compatible with mapping_sector
    if mapping_sector == "bank_financial":
        scopes = ("bank_financial",)
    elif mapping_sector == "non_bank_financial":
        scopes = ("insurance", "reit", "asset_manager_other_financial", "non_bank_financial")
    else:
        scopes = ("corp",)
    cur.execute(
        """
        SELECT DISTINCT dp.line_item_id
        FROM ref_std_statement_display_profile dp
        JOIN ref_standardized_line_items r ON r.line_item_id = dp.line_item_id
        WHERE dp.accounting_standard = %s
          AND dp.sector_scope = ANY(%s)
          AND COALESCE(r.is_filed, FALSE) = TRUE
          AND dp.display_role <> 'CALCULATED'
          AND dp.display_policy <> 'HIDE'
        ORDER BY dp.line_item_id
        """,
        (standard, list(scopes)),
    )
    return [r[0] for r in cur.fetchall()]


def _build_prompt(cluster: dict[str, Any], allowed_targets: list[str]) -> str:
    samples = (cluster.get("sample_tickers") or [])[:5]
    payload = {
        "concept_id": cluster["normalized_concept_id"],
        "jurisdiction": cluster["jurisdiction"],
        "mapping_sector": cluster["mapping_sector"],
        "entity_count": cluster["entity_count"],
        "total_fact_count": cluster["total_fact_count"],
        "sample_tickers": samples,
        "calc_linkbase_evidence": {
            "parent_concept_id": cluster.get("calc_parent_concept_id"),
            "parent_already_maps_to": cluster.get("inferred_target_line_item"),
            "parent_support_pct": float(cluster.get("calc_parent_support_pct") or 0),
        },
        "allowed_targets": allowed_targets,
        "hint": (
            "The calc parent maps to "
            f"{cluster.get('inferred_target_line_item')!r}. If the concept is a clean "
            "component of that target, propose CHILD_SUM with target_variable="
            f"{cluster.get('inferred_target_line_item')!r}. If the concept is itself "
            "a known subtotal that matches a different line item more specifically, "
            "propose that more-specific target with ROOT or DIRECT instead. If the "
            "concept is noise (footnote, ratio, member), return target_variable=null."
        ),
    }
    return json.dumps(payload, indent=2)


def _parse_response(text: str) -> dict[str, Any]:
    """Tolerate fenced code blocks and trailing prose."""
    blob = text.strip()
    # Strip code fences if any
    blob = re.sub(r"^```(?:json)?\s*", "", blob)
    blob = re.sub(r"\s*```$", "", blob)
    # Find first { ... } object
    m = re.search(r"\{.*\}", blob, re.DOTALL)
    if m:
        blob = m.group(0)
    try:
        return json.loads(blob)
    except Exception:
        return {"target_variable": None, "confidence": 0.0, "reasoning": f"PARSE_ERROR: {text[:200]}"}


def _score_one(client, model: str, cluster: dict[str, Any], allowed_targets: list[str]) -> dict[str, Any]:
    user = _build_prompt(cluster, allowed_targets)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=400,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else ""
    return _parse_response(content)


def _write_back(cur, cluster_id: int, parsed: dict[str, Any], model: str, decision_override: str | None = None) -> None:
    target = parsed.get("target_variable")
    if isinstance(target, str) and target.strip().lower() in ("null", "none", "unmapped", ""):
        target = None
    agg = parsed.get("aggregation_type") if target else None
    sign = parsed.get("sign_policy") if target else None
    conf = parsed.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    reasoning = parsed.get("reasoning") or ""
    if decision_override:
        decision = decision_override
    elif target is None:
        decision = "UNMAPPED"
    elif conf is None or conf < 0.5:
        decision = "NEEDS_REVIEW"
    else:
        decision = "PROPOSE"
    cur.execute(
        """
        UPDATE map_entity_gap_cluster
        SET llm_suggested_target_variable = %s,
            llm_suggested_aggregation_type = %s,
            llm_suggested_sign_policy = %s,
            llm_confidence = %s,
            llm_reasoning = %s,
            llm_decision = %s,
            llm_model_name = %s,
            llm_scored_at = now()
        WHERE cluster_id = %s
        """,
        (target, agg, sign, conf, reasoning, decision, model, cluster_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--jurisdiction", default=None)
    parser.add_argument("--min-entities", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score clusters that already have an LLM decision.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan; do not call LLM or write back.")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    with connect() as conn, conn.cursor() as cur:
        clusters = _load_clusters(
            cur, args.batch, args.min_entities, args.limit, args.rescore, args.jurisdiction,
        )

    if args.dry_run:
        print(f"DRY-RUN: would score {len(clusters)} clusters")
        for c in clusters[:5]:
            print(f"  cluster_id={c['cluster_id']} concept={c['normalized_concept_id']} "
                  f"entities={c['entity_count']} inferred_target={c['inferred_target_line_item']}")
        if len(clusters) > 5:
            print(f"  ... and {len(clusters)-5} more")
        return 0

    if not clusters:
        print("No clusters to score.")
        return 0

    model = get_llm_model()
    client = get_llm_client()
    print(f"Scoring {len(clusters)} clusters via {model}...", flush=True)

    target_cache: dict[tuple, list[str]] = {}
    stats = {"propose": 0, "unmapped": 0, "needs_review": 0, "skip_noise": 0, "errors": 0}
    t0 = time.time()
    for idx, c in enumerate(clusters, 1):
        if _looks_like_noise(c["normalized_concept_id"]):
            with connect() as conn, conn.cursor() as cur:
                _write_back(
                    cur, c["cluster_id"],
                    {"target_variable": None, "confidence": 0.0, "reasoning": "noise pattern (filtered without LLM call)"},
                    model, decision_override="SKIP_NOISE",
                )
            stats["skip_noise"] += 1
            continue

        cache_key = (c["mapping_sector"], c["jurisdiction"])
        targets = target_cache.get(cache_key)
        if targets is None:
            with connect() as conn, conn.cursor() as cur:
                targets = _load_allowed_targets(cur, c["mapping_sector"], c["jurisdiction"])
            target_cache[cache_key] = targets

        try:
            parsed = _score_one(client, model, c, targets)
        except Exception as exc:
            print(f"  cluster_id={c['cluster_id']} ERROR: {exc}", flush=True)
            stats["errors"] += 1
            continue

        with connect() as conn, conn.cursor() as cur:
            _write_back(cur, c["cluster_id"], parsed, model)
        decision = "PROPOSE" if parsed.get("target_variable") and float(parsed.get("confidence") or 0) >= 0.5 else ("UNMAPPED" if not parsed.get("target_variable") else "NEEDS_REVIEW")
        stats[decision.lower()] += 1
        if idx % args.progress_every == 0:
            elapsed = time.time() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (len(clusters) - idx) / rate if rate > 0 else 0
            print(
                f"  [{idx}/{len(clusters)}] {elapsed:.0f}s elapsed, {rate:.2f}/s, ETA {eta/60:.1f} min, stats={stats}",
                flush=True,
            )

    print(f"\nDone. Total: {time.time()-t0:.0f}s. Final stats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
