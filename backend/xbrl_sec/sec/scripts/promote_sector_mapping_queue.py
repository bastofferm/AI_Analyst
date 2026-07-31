"""Promote sector_gap_fill queue rows into map_concept_to_taxonomy_versioned.

Each queue row in batch `sector_gap_fill_2026_06` proposes either:
  - sector_scope action: route a concept to a different target for a sector
    that currently has no specific mapping (or has the wrong one)
  - alternate_total action: FALLBACK_TOTAL mapping when the preferred ROOT
    is absent

For each row, this script either:
  (a) UPDATES an existing (concept_id, jurisdiction, mapping_sector) row in
      the production table to point at the new target with the new
      aggregation metadata, or
  (b) INSERTS a new sector-specific row when no such row exists.

By default runs in --dry-run mode (no DB writes, just a diff report).
Pass --apply to commit.

Promoted queue rows are marked review_status='approved', decision='PROMOTED'.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.connection import connect


REVIEW_BATCH = "sector_gap_fill_2026_06"
EFFECTIVE_FROM_YEAR = 1900   # cover all historical data
PROMOTION_SOURCE = "sector_gap_fill_promotion_2026_06"


def _aggregation_priority(agg_type: str) -> int:
    """Lower number = higher priority within the same concept × target."""
    return {
        "ROOT": 10,
        "DIRECT": 10,
        "FALLBACK_TOTAL": 100,
        "CHILD_SUM": 200,
        "EXCLUDE": 999,
    }.get(agg_type, 100)


def _legacy_tier(agg_type: str) -> int:
    """Map new aggregation_type to legacy tier for backwards compat."""
    return 1 if agg_type in ("ROOT", "DIRECT", "FALLBACK_TOTAL") else 2


def _multiplier_for_sign(sign_policy: str) -> Decimal:
    """Translate sign_policy into a legacy multiplier value.

    For 'as_reported'/'force_negative'/'force_positive': multiplier 1.
    For 'flip': multiplier -1 (so the legacy resolver also negates).
    """
    return Decimal("-1") if sign_policy == "flip" else Decimal("1")


def load_queue_rows(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT queue_id, jurisdiction, normalized_concept_id, mapping_sector,
               top_candidate_label,
               suggested_aggregation_type, suggested_sign_policy,
               proposed_action, reasoning, evidence, current_mapping_id
        FROM map_concept_to_taxonomy_review_queue
        WHERE review_batch = %s
          AND review_status = 'queued'
        ORDER BY jurisdiction, mapping_sector, normalized_concept_id
        """,
        (REVIEW_BATCH,),
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "queue_id": r[0],
            "jurisdiction": r[1],
            "concept_id": r[2],
            "mapping_sector": r[3],
            "target_variable": r[4],
            "aggregation_type": r[5],
            "sign_policy": r[6],
            "proposed_action": r[7],
            "reasoning": r[8],
            "evidence": r[9],
            "current_mapping_id": r[10],
        })
    return rows


def find_existing_mapping(cur, concept_id: str, jurisdiction: str,
                          mapping_sector: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT mapping_id, jurisdiction, mapping_sector, target_variable,
               tier, multiplier, aggregation_type, sign_policy, aggregation_priority
        FROM map_concept_to_taxonomy_versioned
        WHERE concept_id = %s
          AND jurisdiction = %s
          AND COALESCE(mapping_sector, '') = %s
        LIMIT 1
        """,
        (concept_id, jurisdiction, mapping_sector),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "mapping_id": r[0],
        "jurisdiction": r[1],
        "mapping_sector": r[2],
        "target_variable": r[3],
        "tier": r[4],
        "multiplier": r[5],
        "aggregation_type": r[6],
        "sign_policy": r[7],
        "aggregation_priority": r[8],
    }


def promote(dry_run: bool = True) -> dict[str, int]:
    stats = {"queue_rows": 0, "would_update": 0, "would_insert": 0,
             "skipped_no_change": 0, "applied": 0}
    plan: list[dict[str, Any]] = []

    with connect() as conn, conn.cursor() as cur:
        queue_rows = load_queue_rows(cur)
        stats["queue_rows"] = len(queue_rows)

        for q in queue_rows:
            tier = _legacy_tier(q["aggregation_type"])
            priority = _aggregation_priority(q["aggregation_type"])
            multiplier = _multiplier_for_sign(q["sign_policy"])
            existing = find_existing_mapping(
                cur, q["concept_id"], q["jurisdiction"], q["mapping_sector"]
            )
            if existing:
                changes = {}
                if existing["target_variable"] != q["target_variable"]:
                    changes["target_variable"] = (existing["target_variable"], q["target_variable"])
                if (existing["aggregation_type"] or "") != q["aggregation_type"]:
                    changes["aggregation_type"] = (existing["aggregation_type"], q["aggregation_type"])
                if (existing["sign_policy"] or "") != q["sign_policy"]:
                    changes["sign_policy"] = (existing["sign_policy"], q["sign_policy"])
                if existing["tier"] != tier:
                    changes["tier"] = (existing["tier"], tier)
                if existing["multiplier"] != multiplier:
                    changes["multiplier"] = (str(existing["multiplier"]), str(multiplier))
                if (existing["aggregation_priority"] or 100) != priority:
                    changes["aggregation_priority"] = (existing["aggregation_priority"], priority)
                if not changes:
                    stats["skipped_no_change"] += 1
                    continue
                plan.append({
                    "kind": "UPDATE",
                    "queue_id": q["queue_id"],
                    "mapping_id": existing["mapping_id"],
                    "concept": q["concept_id"],
                    "jurisdiction": q["jurisdiction"],
                    "mapping_sector": q["mapping_sector"],
                    "target_new": q["target_variable"],
                    "aggregation_type": q["aggregation_type"],
                    "sign_policy": q["sign_policy"],
                    "tier": tier,
                    "multiplier": multiplier,
                    "aggregation_priority": priority,
                    "changes": changes,
                })
                stats["would_update"] += 1
            else:
                plan.append({
                    "kind": "INSERT",
                    "queue_id": q["queue_id"],
                    "concept": q["concept_id"],
                    "jurisdiction": q["jurisdiction"],
                    "mapping_sector": q["mapping_sector"],
                    "target": q["target_variable"],
                    "tier": tier,
                    "multiplier": str(multiplier),
                    "aggregation_type": q["aggregation_type"],
                    "sign_policy": q["sign_policy"],
                    "aggregation_priority": priority,
                })
                stats["would_insert"] += 1

        if not dry_run:
            for item in plan:
                if item["kind"] == "UPDATE":
                    cur.execute(
                        """
                        UPDATE map_concept_to_taxonomy_versioned
                        SET target_variable = %s,
                            aggregation_type = %s,
                            sign_policy = %s,
                            tier = %s,
                            multiplier = %s,
                            aggregation_priority = %s,
                            mapping_source = %s,
                            updated_at = now()
                        WHERE mapping_id = %s
                        """,
                        (
                            item["target_new"],
                            item["aggregation_type"],
                            item["sign_policy"],
                            item["tier"],
                            item["multiplier"],
                            item["aggregation_priority"],
                            PROMOTION_SOURCE,
                            item["mapping_id"],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO map_concept_to_taxonomy_versioned
                            (concept_id, target_variable, jurisdiction, mapping_sector,
                             tier, multiplier, aggregation_type, sign_policy,
                             aggregation_priority, effective_from_year,
                             review_status, mapping_source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            item["concept"], item["target"], item["jurisdiction"], item["mapping_sector"],
                            item["tier"], Decimal(item["multiplier"]),
                            item["aggregation_type"], item["sign_policy"],
                            item["aggregation_priority"], EFFECTIVE_FROM_YEAR,
                            "promoted", PROMOTION_SOURCE,
                        ),
                    )
                cur.execute(
                    """
                    UPDATE map_concept_to_taxonomy_review_queue
                    SET review_status = 'approved',
                        decision = 'PROMOTED',
                        approved_by = %s,
                        approved_at = now(),
                        reviewed_at = now(),
                        reviewed_by = %s,
                        updated_at = now()
                    WHERE queue_id = %s
                    """,
                    (PROMOTION_SOURCE, PROMOTION_SOURCE, item["queue_id"]),
                )
                stats["applied"] += 1

    return stats, plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes. Without this, dry-run prints the plan only.")
    args = parser.parse_args()
    stats, plan = promote(dry_run=not args.apply)
    print(f"Plan ({'APPLY' if args.apply else 'DRY-RUN'}):")
    for item in plan:
        if item["kind"] == "UPDATE":
            change_summary = ", ".join(
                f"{k}: {old!r}->{new!r}" for k, (old, new) in item["changes"].items()
            )
            print(f"  UPDATE {item['jurisdiction']}/{item['mapping_sector']}/{item['concept']:50s}  -> {change_summary}")
        else:
            print(f"  INSERT {item['jurisdiction']}/{item['mapping_sector']}/{item['concept']:50s}  -> {item['target']} [{item['aggregation_type']}/{item['sign_policy']}]")
    print()
    print(f"Stats: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
