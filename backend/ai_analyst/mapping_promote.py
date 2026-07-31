"""Promote one committee_dq_agent review-queue proposal into the governed mapping table.

Powers the per-proposal "Promote" button. Reuses the vetted promotion helpers from
``promote_sector_mapping_queue`` so a single proposal is written to
``map_concept_to_taxonomy_versioned`` exactly like the batch promoter, and the queue row
is stamped ``approved``/``PROMOTED``. This is the deliberate approval step — the committee
node only ever writes the review queue; nothing reaches production without this call.

Writes go through ``xbrl_sec.sec.db.connection.connect`` (ai_analyst._db is read-only).
"""
from __future__ import annotations

import logging
from typing import Any

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.scripts.promote_sector_mapping_queue import (
    _aggregation_priority,
    _legacy_tier,
    _multiplier_for_sign,
    find_existing_mapping,
)

from .dq_triage import governed_mapping_sector

logger = logging.getLogger(__name__)

_MAPPING_SOURCE = "committee_dq_agent_v1"
PROMOTION_SOURCE = "committee_dq_agent_promotion"
_EFFECTIVE_FROM_YEAR = 1900
# The LLM proposals don't set aggregation metadata; DIRECT → tier 1, priority 10.
_DEFAULT_AGG = "DIRECT"
_DEFAULT_SIGN = "as_reported"


class PromoteError(Exception):
    """A proposal could not be promoted (bad input, missing/duplicate queue row, DB refusal)."""


def promote_proposal(
    *,
    jurisdiction: str,
    concept_id: str,
    mapping_sector: str | None = None,
    target_variable: str | None = None,
) -> dict[str, Any]:
    """Promote the queued committee proposal for one concept into production.

    Located by (mapping_source=committee_dq_agent_v1, jurisdiction, concept_id,
    mapping_sector, review_status='queued'). Updates an existing versioned row or inserts
    a new one, then marks the queue row approved. Raises PromoteError on any problem.
    """
    juris = (jurisdiction or "").upper()
    if juris not in ("US", "JP"):
        raise PromoteError(f"invalid jurisdiction: {jurisdiction!r}")
    concept_id = (concept_id or "").strip()
    if not concept_id:
        raise PromoteError("concept_id is required")
    sector = governed_mapping_sector(mapping_sector)

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT queue_id, top_candidate_label, suggested_target_variable,
                       suggested_aggregation_type, suggested_sign_policy
                FROM map_concept_to_taxonomy_review_queue
                WHERE mapping_source = %s
                  AND jurisdiction = %s
                  AND normalized_concept_id = %s
                  AND COALESCE(mapping_sector, '') = %s
                  AND review_status = 'queued'
                ORDER BY queue_id DESC
                LIMIT 1
                """,
                (_MAPPING_SOURCE, juris, concept_id, sector),
            )
            row = cur.fetchone()
            if not row:
                raise PromoteError(
                    "no queued committee proposal found for this concept/sector "
                    "(already promoted, or the analysis run did not queue it)"
                )
            queue_id, top_label, sugg_target, agg_type, sign_policy = row
            target = (target_variable or top_label or sugg_target or "").strip()
            if not target:
                raise PromoteError("proposal has no target line item to promote")
            agg_type = agg_type or _DEFAULT_AGG
            sign_policy = sign_policy or _DEFAULT_SIGN
            tier = _legacy_tier(agg_type)
            priority = _aggregation_priority(agg_type)
            multiplier = _multiplier_for_sign(sign_policy)

            existing = find_existing_mapping(cur, concept_id, juris, sector)
            if existing:
                cur.execute(
                    """
                    UPDATE map_concept_to_taxonomy_versioned
                    SET target_variable = %s, aggregation_type = %s, sign_policy = %s,
                        tier = %s, multiplier = %s, aggregation_priority = %s,
                        mapping_source = %s, updated_at = now()
                    WHERE mapping_id = %s
                    """,
                    (target, agg_type, sign_policy, tier, multiplier, priority,
                     PROMOTION_SOURCE, existing["mapping_id"]),
                )
                action, mapping_id = "updated", existing["mapping_id"]
            else:
                cur.execute(
                    """
                    INSERT INTO map_concept_to_taxonomy_versioned
                        (concept_id, target_variable, jurisdiction, mapping_sector, tier,
                         multiplier, aggregation_type, sign_policy, aggregation_priority,
                         effective_from_year, review_status, mapping_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING mapping_id
                    """,
                    (concept_id, target, juris, sector, tier, multiplier, agg_type, sign_policy,
                     priority, _EFFECTIVE_FROM_YEAR, "promoted", PROMOTION_SOURCE),
                )
                action, mapping_id = "inserted", cur.fetchone()[0]

            cur.execute(
                """
                UPDATE map_concept_to_taxonomy_review_queue
                SET review_status = 'approved', decision = 'PROMOTED',
                    approved_by = %s, approved_at = now(), reviewed_at = now(),
                    reviewed_by = %s, updated_at = now()
                WHERE queue_id = %s
                """,
                (PROMOTION_SOURCE, PROMOTION_SOURCE, queue_id),
            )
    except PromoteError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface DB refusals (e.g. overlap trigger) to the caller
        logger.warning("promote_proposal failed for %s/%s/%s: %s", juris, sector, concept_id, exc)
        raise PromoteError(f"{type(exc).__name__}: {exc}") from exc

    return {
        "status": "promoted",
        "action": action,
        "mapping_id": int(mapping_id),
        "concept_id": concept_id,
        "target_variable": target,
        "mapping_sector": sector,
        "jurisdiction": juris,
    }
