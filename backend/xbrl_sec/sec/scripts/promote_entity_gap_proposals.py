"""Promote LLM-scored entity-gap cluster proposals into map_concept_to_taxonomy_versioned.

Reads `llm_decision = 'PROPOSE'` clusters from map_entity_gap_cluster for the
specified batch, applies safety filters (unit_mismatch, doublecount_risk, manual
holdouts), and INSERTs the survivors as new sector-specific mapping rows.

Each successful insert flips the cluster's llm_decision to 'PROMOTED'.

Usage::

    python -m xbrl_sec.sec.scripts.promote_entity_gap_proposals \\
        --batch entity_gap_202606_us_v2 --dry-run
    python -m xbrl_sec.sec.scripts.promote_entity_gap_proposals \\
        --batch entity_gap_202606_us_v2 --apply
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from xbrl_sec.sec.db.connection import connect


EFFECTIVE_FROM_YEAR = 1900
PROMOTION_SOURCE = "entity_gap_llm_promotion_202606"

# Manual holdouts — clusters the operator chose not to promote even though
# the LLM proposed them and they passed the unit/double-count filters.
# Keyed by (concept_id, mapping_sector) so a re-score doesn't accidentally
# resurrect them.
HOLDOUTS = {
    ("us-gaap/CommonStockParOrStatedValuePerShare", "bank_financial"),         # per-share vs total $
    ("us-gaap/TaxesExcludingIncomeAndExciseTaxes", "corp"),                    # belongs in SG&A not CFO catch-all
    ("us-gaap/InvestmentCompanyIncentiveFeeToAverageNetAssets", "non_bank_financial"),  # ratio, not revenue
    ("us-gaap/DefinedContributionPlanCostRecognized", "bank_financial"),       # not SBC; LLM hallucinated for bank
}


def _legacy_tier(agg_type: str) -> int:
    return 1 if agg_type in ("ROOT", "DIRECT", "FALLBACK_TOTAL") else 2


def _multiplier_for_sign(sign_policy: str | None) -> Decimal:
    return Decimal("-1") if (sign_policy or "").lower() == "flip" else Decimal("1")


def _aggregation_priority(agg_type: str) -> int:
    return {"ROOT": 10, "DIRECT": 10, "FALLBACK_TOTAL": 100, "CHILD_SUM": 200, "EXCLUDE": 999}.get(agg_type, 100)


def load_clean_clusters(cur, batch: str):
    cur.execute(
        """
        WITH proposals AS (
            SELECT c.cluster_id, c.jurisdiction, c.mapping_sector,
                   c.normalized_concept_id AS concept_id,
                   c.llm_suggested_target_variable AS target,
                   c.llm_suggested_aggregation_type AS agg,
                   c.llm_suggested_sign_policy AS sign_policy,
                   c.llm_confidence AS confidence,
                   c.entity_count
            FROM map_entity_gap_cluster c
            WHERE c.cluster_batch = %s
              AND c.llm_decision = 'PROPOSE'
        ),
        target_meta AS (SELECT line_item_id, unit_type FROM ref_standardized_line_items),
        existing_roots AS (
            SELECT DISTINCT m.target_variable, COALESCE(m.mapping_sector,'BOTH') AS scope
            FROM map_concept_to_taxonomy_versioned m
            WHERE COALESCE(m.aggregation_type,'') IN ('ROOT','DIRECT','FALLBACK_TOTAL')
               OR m.tier = 1
        )
        SELECT p.cluster_id, p.jurisdiction, p.mapping_sector, p.concept_id,
               p.target, p.agg, p.sign_policy, p.confidence, p.entity_count
        FROM proposals p
        LEFT JOIN target_meta t ON t.line_item_id = p.target
        WHERE
          -- unit mismatch filter
          NOT (p.concept_id ILIKE '%%Shares%%'
               AND COALESCE(t.unit_type,'') NOT IN ('shares','share','COUNT','PURE'))
          AND NOT (p.concept_id NOT ILIKE '%%Shares%%'
                   AND COALESCE(t.unit_type,'') IN ('shares','share','COUNT'))
          -- doublecount filter
          AND NOT (p.agg = 'CHILD_SUM' AND EXISTS (
              SELECT 1 FROM existing_roots er
              WHERE er.target_variable = p.target
                AND er.scope IN ('BOTH', p.mapping_sector)
          ))
        ORDER BY p.entity_count DESC
        """,
        (batch,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def already_exists(cur, jurisdiction: str, concept_id: str, mapping_sector: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM map_concept_to_taxonomy_versioned
        WHERE concept_id = %s AND jurisdiction = %s
          AND COALESCE(mapping_sector,'') = %s
        LIMIT 1
        """,
        (concept_id, jurisdiction, mapping_sector),
    )
    return cur.fetchone() is not None


def promote(batch: str, dry_run: bool = True) -> dict:
    stats = {"considered": 0, "skipped_holdout": 0, "skipped_existing": 0, "inserted": 0}
    with connect() as conn, conn.cursor() as cur:
        clusters = load_clean_clusters(cur, batch)
        stats["considered"] = len(clusters)
        plan = []
        for c in clusters:
            key = (c["concept_id"], c["mapping_sector"])
            if key in HOLDOUTS:
                stats["skipped_holdout"] += 1
                continue
            if already_exists(cur, c["jurisdiction"], c["concept_id"], c["mapping_sector"]):
                stats["skipped_existing"] += 1
                continue
            plan.append(c)

        print(f"Plan (dry_run={dry_run}): {len(plan)} new mappings to insert")
        for c in plan:
            print(f"  INSERT {c['jurisdiction']}/{c['mapping_sector']:18s} {c['concept_id']:60s} -> {c['target']:35s} {c['agg']}/{c['sign_policy']} ents={c['entity_count']}")

        if not dry_run:
            for c in plan:
                tier = _legacy_tier(c["agg"])
                multiplier = _multiplier_for_sign(c["sign_policy"])
                priority = _aggregation_priority(c["agg"])
                cur.execute(
                    """
                    INSERT INTO map_concept_to_taxonomy_versioned
                        (concept_id, target_variable, jurisdiction, mapping_sector,
                         tier, multiplier, aggregation_type, sign_policy,
                         aggregation_priority, effective_from_year,
                         review_status, mapping_source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        c["concept_id"], c["target"], c["jurisdiction"], c["mapping_sector"],
                        tier, multiplier, c["agg"], c["sign_policy"],
                        priority, EFFECTIVE_FROM_YEAR,
                        "promoted", PROMOTION_SOURCE,
                    ),
                )
                cur.execute(
                    """
                    UPDATE map_entity_gap_cluster
                    SET llm_decision = 'PROMOTED'
                    WHERE cluster_id = %s
                    """,
                    (c["cluster_id"],),
                )
                stats["inserted"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    stats = promote(args.batch, dry_run=not args.apply)
    print(f"\nStats: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
