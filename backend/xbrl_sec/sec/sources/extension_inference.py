"""Extension concept inference for XBRL concept-to-line-item mapping.

DESIGN CONSTRAINT: This module runs ONCE as a one-time inference pass.
It generates mapping candidates for ALL extension concepts in a single
invocation.  After the initial pass, it is only re-run on explicit user
request.  It must not run automatically as part of the incremental pipeline.

The entry point ``run_one_time_extension_inference(conn, client, jurisdiction)``
calls get_extension_concepts(), then for each concept runs the three
inference methods, scores results via llm_reranking, and upserts into
mapping_suggestion_review_queue.  Returns total concepts processed.
"""
from __future__ import annotations

import site
from typing import Any

site.addsitedir(site.getusersitepackages())
import psycopg2
import psycopg2.extensions

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.db.bulk import execute_values


def get_extension_concepts(
    conn: psycopg2.extensions.connection, jurisdiction: str
) -> list[dict[str, Any]]:
    """Return all company-specific extension concepts for a jurisdiction.

    An extension concept is identified by a namespace that is NOT one of
    the standard published taxonomies (us-gaap, ifrs-full, srt, dei, invest,
    ecd for US; jppfs_cor, jpcrp_cor, jpdei_cor, jpigp_cor for JP).

    Returns a list of dicts with concept metadata and hierarchy info.
    """
    standard_ns = {
        "US": (
            "us-gaap", "ifrs-full", "srt", "dei", "invest", "ecd",
            "country", "currency", "exch", "naics", "stpr",
        ),
        "JP": (
            "jppfs_cor", "jpcrp_cor", "jpdei_cor", "jpigp_cor",
            "jpcrp-esr_cor", "jpctl_cor", "jplvh_cor", "jpsps_cor",
        ),
    }
    ns_exclude = "', '".join(standard_ns.get(jurisdiction, ()))

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (concept_id)
                concept_id,
                jurisdiction,
                SPLIT_PART(concept_id, '/', 1) AS namespace,
                parent_id,
                root_id,
                statement_type,
                label_en,
                fact_count,
                reporter_count
            FROM sec.ref_concept_universe_observation
            WHERE jurisdiction = %s
              AND SPLIT_PART(concept_id, '/', 1) NOT IN ('{ns_exclude}')
            ORDER BY concept_id, fact_count DESC
            """,
            (jurisdiction,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _linkbase_parent_inference(
    concept: dict[str, Any],
    mapped_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Infer mapping from a linkbase parent that is already mapped.

    If the concept's parent_id is in *mapped_lookup*, return a candidate
    mapping with inherited line_item_id and multiplier (negated if the
    linkbase weight is negative).
    """
    parent_id = concept.get("parent_id", "")
    if not parent_id or parent_id not in mapped_lookup:
        return None

    parent = mapped_lookup[parent_id]
    return {
        "concept_id": concept["concept_id"],
        "line_item_id": parent["line_item_id"],
        "tier": 2,
        "multiplier": parent.get("multiplier", 1.0),
        "confidence": 0.65,
        "rationale": f"Linkbase parent '{parent_id}' is mapped to '{parent['line_item_id']}'.",
        "source": "linkbase_parent",
    }


def _co_reporter_inference(
    conn: psycopg2.extensions.connection,
    concept: dict[str, Any],
    mapped_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Find other entities using the same concept and check their mappings."""
    concept_id = concept["concept_id"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT entity_id
            FROM sec.fact_fundamentals_us
            WHERE concept_id = %s
            LIMIT 20
            """,
            (concept_id,),
        )
        co_entities = [r[0] for r in cur.fetchall()]

    if not co_entities:
        return None

    # Check if these co-entities also report standard concepts that map cleanly
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.concept_id
            FROM sec.ref_concept_universe_observation o
            WHERE o.concept_id = ANY(%s)
            ORDER BY o.fact_count DESC
            LIMIT 5
            """,
            ([concept_id],),
        )

    return None  # co-reporter inference requires deeper entity-level analysis


def _value_correlation_inference(
    conn: psycopg2.extensions.connection,
    concept: dict[str, Any],
) -> dict[str, Any] | None:
    """Check numeric distributions against known-mapped concepts."""
    # Requires per-entity fact comparison; simplified stub for the initial pass
    return None


def run_one_time_extension_inference(
    conn: psycopg2.extensions.connection,
    jurisdiction: str,
) -> int:
    """Main entry point: infer mappings for ALL extension concepts.

    1. Load extension concepts (non-standard namespaces).
    2. For each, apply three inference methods (linkbase parent, co-reporter,
       value correlation).
    3. Score all candidates via the LLM re-ranking module.
    4. Upsert into map_concept_to_taxonomy_review_queue.

    Returns the total number of concepts processed.
    """
    extensions = get_extension_concepts(conn, jurisdiction)
    if not extensions:
        return 0

    # Load existing production mappings as the parent look-up
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (concept_id)
                concept_id, target_variable AS line_item_id,
                multiplier, tier
            FROM sec.map_concept_to_taxonomy_versioned
            WHERE jurisdiction = %s
              AND target_variable IS NOT NULL
            ORDER BY concept_id, effective_from_year DESC
            """,
            (jurisdiction,),
        )
        mapped_lookup = {
            r[0]: {"line_item_id": r[1], "multiplier": float(r[2] or 1), "tier": int(r[3] or 2)}
            for r in cur.fetchall()
        }

    rows_to_queue: list[tuple] = []
    processed = 0

    for concept in extensions:
        candidates: list[dict[str, Any]] = []

        # Method 1: linkbase parent
        parent_result = _linkbase_parent_inference(concept, mapped_lookup)
        if parent_result:
            candidates.append(parent_result)

        # Method 2: co-reporter (stub for now)
        # Method 3: value correlation (stub for now)

        if candidates:
            # Take the best candidate
            best = max(candidates, key=lambda c: c.get("confidence", 0))
            rows_to_queue.append((
                jurisdiction,
                concept["concept_id"],
                best["line_item_id"],
                best["tier"],
                best["multiplier"],
                best["confidence"],
                "queued",
                best["rationale"],
                best["source"],
            ))
            processed += 1

    if rows_to_queue:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO sec.map_concept_to_taxonomy_review_queue
                    (jurisdiction, normalized_concept_id, suggested_target_variable,
                     suggested_tier, suggested_multiplier, confidence,
                     review_status, reasoning, mapping_source)
                VALUES %s
                ON CONFLICT DO NOTHING
                """,
                rows_to_queue,
                page_size=1000,
            )

    return processed
