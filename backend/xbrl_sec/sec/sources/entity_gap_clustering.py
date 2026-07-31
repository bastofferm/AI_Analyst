"""Cluster the per-entity mapping gap backlog (Step 2 of long-tail fix plan).

Reads from the CSV produced by audit_entity_mapping_gaps.py (or directly
from sec.v_entity_mapping_gap), groups by:

    (jurisdiction, mapping_sector, normalized_concept_id, inferred_target_line_item)

and writes one row per cluster to sec.map_entity_gap_cluster.

Cluster classification:

- Lane A (linkbase_only_eligible = TRUE): calc_parent_support_pct >= 0.80
  AND entity_count >= 10. These can be auto-promoted to versioned mappings
  using the calc-parent's target without any LLM call.
- Lane B (linkbase_only_eligible = FALSE): needs Step 3 DeepSeek scoring.

entity_specificity:
- shared if entity_count >= 5 -> sector mapping promotion path.
- narrow if entity_count in 1..4 -> exception table promotion path.

Usage::

    python -m xbrl_sec.sec.sources.entity_gap_clustering \\
        --input entity_mapping_gaps.csv --batch entity_gap_202606
    # or pull straight from the view:
    python -m xbrl_sec.sec.sources.entity_gap_clustering --jurisdiction US
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.connection import connect


# Lane A thresholds.
_LANE_A_MIN_SUPPORT = Decimal("0.80")
_LANE_A_MIN_ENTITIES = 10
_SHARED_MIN_ENTITIES = 5


def _read_input(input_csv: Path | None, jurisdictions: list[str]) -> list[dict[str, Any]]:
    if input_csv is not None and input_csv.exists():
        with input_csv.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    # Fallback: pull straight from the view.
    rows: list[dict[str, Any]] = []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT jurisdiction, entity_id, ticker, mapping_sector, sector_scope,
                   gics_industry_group, fiscal_year, fiscal_period,
                   gap_kind, line_item_id, statement_type,
                   concept_id, normalized_concept_id, fact_count,
                   sample_filing_ids
            FROM v_entity_mapping_gap
            WHERE jurisdiction = ANY(%s)
            """,
            (jurisdictions,),
        )
        cols = [d[0] for d in cur.description]
        for raw in cur.fetchall():
            r = dict(zip(cols, raw))
            r["fiscal_year"] = int(r["fiscal_year"]) if r["fiscal_year"] is not None else None
            r["fact_count"] = int(r["fact_count"] or 0)
            rows.append(r)
    return rows


def _attach_calc_parent_evidence_inline(
    rows: list[dict[str, Any]],
) -> None:
    """If the input is from the view (rather than the CSV which already has
    the evidence), do a one-pass enrichment here for unmapped_concept rows.
    """
    if any("evidence_calc_parent_target" in r and r.get("evidence_calc_parent_target") for r in rows):
        return  # CSV-fed input already enriched.

    unmapped = [r for r in rows if r.get("gap_kind") == "unmapped_concept" and r.get("concept_id")]
    if not unmapped:
        return
    keyed: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in unmapped:
        keyed[(r["jurisdiction"], r["entity_id"], r["concept_id"])].append(r)
    keys = list(keyed.keys())
    with connect() as conn, conn.cursor() as cur:
        batch_size = 500
        for start in range(0, len(keys), batch_size):
            batch = keys[start:start + batch_size]
            ent_ids = list({k[1] for k in batch})
            concept_ids = list({k[2] for k in batch})
            cur.execute(
                """
                SELECT e.jurisdiction, e.entity_id, e.child_concept_id,
                       e.parent_concept_id,
                       AVG(COALESCE(e.weight,1))::numeric AS avg_weight,
                       COUNT(DISTINCT e.filing_id) AS filings
                FROM ref_xbrl_relationship_edge e
                WHERE e.entity_id = ANY(%s)
                  AND e.child_concept_id = ANY(%s)
                  AND e.linkbase_type = 'calculation'
                  AND e.parent_concept_id IS NOT NULL
                GROUP BY e.jurisdiction, e.entity_id, e.child_concept_id, e.parent_concept_id
                """,
                (ent_ids, concept_ids),
            )
            ranked: dict[tuple, tuple[str, float, int]] = {}
            for jr, ent, child, parent, avg_w, fcount in cur.fetchall():
                key = (jr, ent, child)
                existing = ranked.get(key)
                if existing is None or fcount > existing[2]:
                    ranked[key] = (parent, float(avg_w or 1), int(fcount))
            parent_pairs = list({(jr, parent) for (jr, _, _), (parent, _, _) in ranked.items()})
            parent_targets: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
            if parent_pairs:
                cur.execute(
                    """
                    SELECT jurisdiction, concept_id, COALESCE(mapping_sector,'BOTH') AS scope,
                           target_variable
                    FROM map_concept_to_taxonomy_versioned
                    WHERE (jurisdiction, concept_id) IN %s
                    """,
                    (tuple(parent_pairs),),
                )
                for jr, cid, scope, target in cur.fetchall():
                    parent_targets[(jr, cid)].append((scope, target))
            for (jr, ent, child), (parent, weight, _filings) in ranked.items():
                target_rows = keyed.get((jr, ent, child))
                if not target_rows:
                    continue
                sector = target_rows[0].get("mapping_sector")
                target = None
                for scope, t in parent_targets.get((jr, parent), []):
                    if scope == sector:
                        target = t
                        break
                    if scope == "BOTH" and target is None:
                        target = t
                for r in target_rows:
                    r["evidence_calc_parent_concept_id"] = parent
                    r["evidence_calc_parent_weight"] = weight
                    r["evidence_calc_parent_target"] = target


def _proposed_sign(weight: float | None) -> str:
    if weight is None:
        return "as_reported"
    return "flip" if float(weight) < 0 else "as_reported"


def _cluster(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        jr = r["jurisdiction"]
        sector = r.get("mapping_sector") or "corp"
        concept = r.get("normalized_concept_id") or r.get("concept_id")
        gap_kind = r.get("gap_kind")

        # Cluster key
        if gap_kind == "unmapped_concept":
            inferred_target = (
                r.get("evidence_calc_parent_target") if r.get("evidence_calc_parent_target") else None
            )
            cluster_key = (jr, sector, concept, inferred_target)
            display_gap_kind = "unmapped_concept"
        else:  # unfilled_line_item
            line_item = r.get("line_item_id")
            cluster_key = (jr, sector, None, line_item)
            display_gap_kind = "unfilled_line_item"

        bucket = groups.get(cluster_key)
        if bucket is None:
            bucket = {
                "jurisdiction": jr,
                "mapping_sector": sector,
                "normalized_concept_id": concept if gap_kind == "unmapped_concept" else None,
                "inferred_target_line_item": cluster_key[3],
                "proposed_aggregation_type": None,
                "proposed_sign_policy": None,
                "_entities": set(),
                "_tickers": set(),
                "total_fact_count": 0,
                "_calc_parents": defaultdict(int),
                "_calc_weights": defaultdict(list),
                "_entities_with_parent": set(),
                "gap_kind": display_gap_kind,
            }
            groups[cluster_key] = bucket
        else:
            if bucket["gap_kind"] != display_gap_kind:
                bucket["gap_kind"] = "mixed"

        entity_id = r.get("entity_id")
        if entity_id:
            bucket["_entities"].add(entity_id)
        ticker = r.get("ticker")
        if ticker:
            bucket["_tickers"].add(ticker)
        bucket["total_fact_count"] += int(r.get("fact_count") or 0)

        if gap_kind == "unmapped_concept":
            parent = r.get("evidence_calc_parent_concept_id")
            if parent:
                bucket["_calc_parents"][parent] += 1
                bucket["_entities_with_parent"].add(entity_id)
                weight = r.get("evidence_calc_parent_weight")
                if weight not in (None, ""):
                    try:
                        bucket["_calc_weights"][parent].append(float(weight))
                    except (TypeError, ValueError):
                        pass

    clusters: list[dict[str, Any]] = []
    for bucket in groups.values():
        entity_count = len(bucket["_entities"])
        # Pick modal calc parent.
        modal_parent = None
        modal_count = 0
        modal_weight = None
        for parent, count in bucket["_calc_parents"].items():
            if count > modal_count:
                modal_parent = parent
                modal_count = count
                weights = bucket["_calc_weights"].get(parent) or []
                modal_weight = sum(weights) / len(weights) if weights else None
        support_pct = (
            Decimal(str(len(bucket["_entities_with_parent"]) / entity_count))
            if entity_count > 0 else Decimal("0")
        )
        linkbase_only = (
            bucket["gap_kind"] == "unmapped_concept"
            and bucket["inferred_target_line_item"] is not None
            and support_pct >= _LANE_A_MIN_SUPPORT
            and entity_count >= _LANE_A_MIN_ENTITIES
        )
        entity_specificity = "shared" if entity_count >= _SHARED_MIN_ENTITIES else "narrow"
        proposed_agg = "CHILD_SUM" if bucket["inferred_target_line_item"] is not None else None
        proposed_sign = _proposed_sign(modal_weight) if modal_weight is not None else None
        sample_entities = sorted(bucket["_entities"])[:5]
        sample_tickers = sorted(bucket["_tickers"])[:5]
        clusters.append({
            "jurisdiction": bucket["jurisdiction"],
            "mapping_sector": bucket["mapping_sector"],
            "normalized_concept_id": bucket["normalized_concept_id"],
            "inferred_target_line_item": bucket["inferred_target_line_item"],
            "proposed_aggregation_type": proposed_agg,
            "proposed_sign_policy": proposed_sign,
            "entity_count": entity_count,
            "total_fact_count": bucket["total_fact_count"],
            "sample_entity_ids": sample_entities,
            "sample_tickers": sample_tickers,
            "calc_parent_concept_id": modal_parent,
            "calc_parent_support_pct": support_pct,
            "linkbase_only_eligible": linkbase_only,
            "entity_specificity": entity_specificity,
            "gap_kind": bucket["gap_kind"],
        })
    return clusters


def _persist(clusters: list[dict[str, Any]], batch: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM map_entity_gap_cluster WHERE cluster_batch = %s",
            (batch,),
        )
        for c in clusters:
            cur.execute(
                """
                INSERT INTO map_entity_gap_cluster
                    (jurisdiction, mapping_sector, normalized_concept_id,
                     inferred_target_line_item, proposed_aggregation_type,
                     proposed_sign_policy,
                     entity_count, total_fact_count,
                     sample_entity_ids, sample_tickers,
                     calc_parent_concept_id, calc_parent_support_pct,
                     linkbase_only_eligible, entity_specificity, gap_kind,
                     cluster_batch)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    c["jurisdiction"], c["mapping_sector"], c["normalized_concept_id"],
                    c["inferred_target_line_item"], c["proposed_aggregation_type"],
                    c["proposed_sign_policy"],
                    c["entity_count"], c["total_fact_count"],
                    c["sample_entity_ids"], c["sample_tickers"],
                    c["calc_parent_concept_id"], c["calc_parent_support_pct"],
                    c["linkbase_only_eligible"], c["entity_specificity"], c["gap_kind"],
                    batch,
                ),
            )
    return len(clusters)


def _write_summary_csv(clusters: list[dict[str, Any]], path: Path) -> None:
    cols = [
        "jurisdiction", "mapping_sector", "normalized_concept_id",
        "inferred_target_line_item", "entity_count", "total_fact_count",
        "calc_parent_concept_id", "calc_parent_support_pct",
        "linkbase_only_eligible", "entity_specificity", "gap_kind",
        "sample_tickers",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(cols)
        # Sort: Lane A first then by entity_count desc.
        ordered = sorted(
            clusters,
            key=lambda c: (
                not c["linkbase_only_eligible"],
                -c["entity_count"],
                c["jurisdiction"],
                c["mapping_sector"],
            ),
        )
        for c in ordered:
            writer.writerow([
                c["jurisdiction"], c["mapping_sector"], c["normalized_concept_id"],
                c["inferred_target_line_item"], c["entity_count"], c["total_fact_count"],
                c["calc_parent_concept_id"],
                f"{float(c['calc_parent_support_pct']):.3f}" if c["calc_parent_support_pct"] is not None else "",
                c["linkbase_only_eligible"], c["entity_specificity"], c["gap_kind"],
                "|".join(c["sample_tickers"][:5]),
            ])


def _print_summary(clusters: list[dict[str, Any]]) -> None:
    by_sector: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"clusters": 0, "lane_a": 0, "lane_b": 0, "shared": 0, "narrow": 0}
    )
    for c in clusters:
        key = (c["jurisdiction"], c["mapping_sector"])
        s = by_sector[key]
        s["clusters"] += 1
        if c["linkbase_only_eligible"]:
            s["lane_a"] += 1
        else:
            s["lane_b"] += 1
        s[c["entity_specificity"]] += 1
    print(f"\n{'jur':<4} {'mapping_sector':<25} {'clusters':>9} {'lane_a':>8} {'lane_b':>8} {'shared':>8} {'narrow':>8}")
    print("-" * 75)
    for (jr, sect), s in sorted(by_sector.items()):
        print(f"{jr:<4} {sect:<25} {s['clusters']:>9,} {s['lane_a']:>8,} {s['lane_b']:>8,} {s['shared']:>8,} {s['narrow']:>8,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=None, help="Path to entity_mapping_gaps CSV")
    parser.add_argument("--jurisdiction", choices=("US", "JP", "BOTH"), default="BOTH")
    parser.add_argument("--batch", default=None, help="cluster_batch tag. Default: entity_gap_<YYYYMM>")
    parser.add_argument("--output", default="entity_gap_clusters_summary.csv")
    args = parser.parse_args()

    batch = args.batch or f"entity_gap_{datetime.utcnow().strftime('%Y%m')}"
    jurisdictions = ["US", "JP"] if args.jurisdiction == "BOTH" else [args.jurisdiction]

    t0 = time.time()
    print(f"Loading gap rows (input={args.input!r} jurisdictions={jurisdictions})...")
    rows = _read_input(Path(args.input) if args.input else None, jurisdictions)
    print(f"  loaded {len(rows):,} rows in {time.time()-t0:.1f}s")

    if args.input is None:
        # Need to enrich here since the view doesn't carry the evidence columns.
        t1 = time.time()
        print("Attaching calc-parent evidence inline...")
        _attach_calc_parent_evidence_inline(rows)
        print(f"  done in {time.time()-t1:.1f}s")

    t2 = time.time()
    clusters = _cluster(rows)
    print(f"Clustered into {len(clusters):,} clusters in {time.time()-t2:.1f}s")

    written = _persist(clusters, batch)
    print(f"Persisted {written:,} cluster rows to map_entity_gap_cluster (batch={batch!r})")

    _write_summary_csv(clusters, Path(args.output))
    print(f"Wrote summary CSV to {args.output}")
    _print_summary(clusters)
    return 0 if clusters else 1


if __name__ == "__main__":
    sys.exit(main())
