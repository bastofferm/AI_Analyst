"""Per-entity XBRL mapping gap audit (Step 1 of long-tail fix plan).

Reads sec.v_entity_mapping_gap (created by migration 100) and:
  1. Attaches linkbase calc-parent evidence to each unmapped_concept row via
     a second pass against sec.ref_xbrl_relationship_edge (groups arcs by
     child_concept_id within the entity's filings).
  2. Applies label-based noise filters (tax components, ESG, area metrics,
     supplemental numerics, rates/ratios) borrowed from mapping_suggestions.py.
  3. Writes the result to CSV with one row per gap.
  4. Prints a summary by (jurisdiction, sector_scope, gap_kind).

Usage::

    python -m xbrl_sec.sec.scripts.audit_entity_mapping_gaps --jurisdiction US
    python -m xbrl_sec.sec.scripts.audit_entity_mapping_gaps \\
        --jurisdiction BOTH --output entity_mapping_gaps.csv \\
        --min-fact-count 3
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.mapping_suggestions import (
    _ESG_NOISE_PATTERNS,
    _RATE_PATTERNS,
    _SUPPLEMENTAL_NUMERIC_PATTERNS,
    _TAX_COMPONENT_PATTERNS,
)


_LABEL_NOISE_SUFFIXES = (
    "Abstract", "TextBlock", "Axis", "Domain", "Member", "Table", "LineItems",
    "RollForward",
)

_NOISE_GROUPS = (
    _TAX_COMPONENT_PATTERNS,
    _SUPPLEMENTAL_NUMERIC_PATTERNS,
    _RATE_PATTERNS,
    _ESG_NOISE_PATTERNS,
)


def _looks_noisy(concept_id: str | None) -> bool:
    """Suffix check against the XBRL boilerplate names. Labels-based noise is
    a separate filter applied if labels are joinable.
    """
    if not concept_id:
        return False
    local = concept_id.split("/", 1)[-1]
    return any(local.endswith(suffix) for suffix in _LABEL_NOISE_SUFFIXES)


def _label_noise(label: str | None) -> bool:
    if not label:
        return False
    lower = label.lower()
    for group in _NOISE_GROUPS:
        if any(pattern in lower for pattern in group):
            return True
    return False


def _fetch_gap_rows(
    cur,
    jurisdictions: list[str],
    min_fact_count: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    where_parts = ["jurisdiction = ANY(%s)"]
    params: list[Any] = [jurisdictions]
    where_parts.append("(gap_kind = 'unfilled_line_item' OR fact_count >= %s)")
    params.append(min_fact_count)
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    cur.execute(
        f"""
        SELECT
            jurisdiction, entity_id, ticker, mapping_sector, sector_scope,
            gics_industry_group, fiscal_year, fiscal_period,
            gap_kind, line_item_id, statement_type, display_role, display_policy,
            concept_id, normalized_concept_id, fact_count,
            sample_filing_ids
        FROM v_entity_mapping_gap
        WHERE {' AND '.join(where_parts)}
        ORDER BY jurisdiction, sector_scope, gap_kind, fact_count DESC NULLS LAST
        {limit_sql}
        """,
        params,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _attach_calc_parent_evidence(
    cur,
    rows: list[dict[str, Any]],
) -> None:
    """Second-pass linkbase enrichment. For each unmapped_concept row, find
    the most common calculation-arc parent across the entity's filings and
    record whether that parent itself maps to a standardized target.
    """
    unmapped = [
        r for r in rows
        if r["gap_kind"] == "unmapped_concept" and r["concept_id"]
    ]
    if not unmapped:
        return

    # Dedupe (jurisdiction, entity_id, concept_id) and resolve in batches.
    keyed: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in unmapped:
        keyed[(r["jurisdiction"], r["entity_id"], r["concept_id"])].append(r)

    keys = list(keyed.keys())
    batch_size = 500
    for start in range(0, len(keys), batch_size):
        batch = keys[start:start + batch_size]
        ent_ids = list({k[1] for k in batch})
        concept_ids = list({k[2] for k in batch})
        # The view doesn't carry jurisdiction in the edge table; both edges
        # are scoped by entity_id so a per-jurisdiction filter is implicit.
        cur.execute(
            """
            SELECT
                e.jurisdiction,
                e.entity_id,
                e.child_concept_id,
                e.parent_concept_id,
                AVG(COALESCE(e.weight, 1))::numeric AS avg_weight,
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
        # Pick the modal parent per (jurisdiction, entity_id, child).
        ranked: dict[tuple, tuple[str, float, int]] = {}
        for jr, ent, child, parent, avg_w, fcount in cur.fetchall():
            key = (jr, ent, child)
            existing = ranked.get(key)
            if existing is None or fcount > existing[2]:
                ranked[key] = (parent, float(avg_w) if avg_w is not None else 1.0, int(fcount))

        # For each (jr, parent), resolve target via versioned mapping. Use
        # mapping_sector compatibility: BOTH or the entity's mapping_sector.
        parent_keys = list({(jr, parent) for (jr, ent, child), (parent, _, _) in ranked.items()})
        if parent_keys:
            # Build a (jurisdiction, concept_id) -> target lookup by sector.
            cur.execute(
                """
                SELECT jurisdiction, concept_id, COALESCE(mapping_sector,'BOTH') AS scope,
                       target_variable
                FROM map_concept_to_taxonomy_versioned
                WHERE (jurisdiction, concept_id) IN %s
                """,
                (tuple(parent_keys),),
            )
            parent_targets: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
            for jr, cid, scope, target in cur.fetchall():
                parent_targets[(jr, cid)].append((scope, target))
        else:
            parent_targets = {}

        for (jr, ent, child), (parent, weight, filings) in ranked.items():
            target_rows = keyed.get((jr, ent, child))
            if not target_rows:
                # The edge query is a cross-product within the batch and can
                # return (entity, concept) pairs not present in the input
                # gap rows; skip those.
                continue
            sector = target_rows[0]["mapping_sector"]
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


def _attach_concept_labels(
    cur,
    rows: list[dict[str, Any]],
) -> None:
    """Optional: pull human labels from ref_concept_universe_observation if
    present. Used to apply label-based noise filtering."""
    unmapped = [r for r in rows if r["gap_kind"] == "unmapped_concept" and r["concept_id"]]
    if not unmapped:
        return
    concept_ids = list({r["concept_id"] for r in unmapped})
    try:
        cur.execute(
            """
            SELECT concept_id, MIN(label) AS label
            FROM ref_concept_universe_observation
            WHERE concept_id = ANY(%s)
            GROUP BY concept_id
            """,
            (concept_ids,),
        )
        labels = {cid: lbl for cid, lbl in cur.fetchall()}
    except Exception:
        # Table may not exist in older schemas; treat as no labels.
        labels = {}
    for r in unmapped:
        r["label"] = labels.get(r["concept_id"])


def _filter_noise(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    dropped = {"suffix_noise": 0, "label_noise": 0}
    for r in rows:
        if r["gap_kind"] != "unmapped_concept":
            kept.append(r)
            continue
        if _looks_noisy(r.get("concept_id")):
            dropped["suffix_noise"] += 1
            continue
        if _label_noise(r.get("label")):
            dropped["label_noise"] += 1
            continue
        kept.append(r)
    return kept, dropped


def _summarize(rows: list[dict[str, Any]]) -> dict:
    summary: dict[tuple, dict[str, int]] = defaultdict(lambda: {"unfilled_line_item": 0, "unmapped_concept": 0})
    for r in rows:
        key = (r["jurisdiction"], r["sector_scope"])
        summary[key][r["gap_kind"]] += 1
    return summary


_OUTPUT_COLS = [
    "jurisdiction", "entity_id", "ticker", "mapping_sector", "sector_scope",
    "gics_industry_group", "fiscal_year", "fiscal_period",
    "gap_kind", "line_item_id", "statement_type", "display_role", "display_policy",
    "concept_id", "normalized_concept_id", "fact_count",
    "evidence_calc_parent_concept_id", "evidence_calc_parent_target",
    "evidence_calc_parent_weight",
    "sample_filing_ids",
]


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_OUTPUT_COLS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row_out = {k: r.get(k) for k in _OUTPUT_COLS}
            # CSV-safe sample_filing_ids
            if isinstance(row_out.get("sample_filing_ids"), list):
                row_out["sample_filing_ids"] = "|".join(str(x) for x in row_out["sample_filing_ids"][:5])
            writer.writerow(row_out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurisdiction", choices=("US", "JP", "BOTH"), default="BOTH")
    parser.add_argument("--min-fact-count", type=int, default=3,
                        help="Drop unmapped_concept rows with fewer than N raw facts. Default 3.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", default="entity_mapping_gaps.csv")
    args = parser.parse_args()

    jurisdictions = ["US", "JP"] if args.jurisdiction == "BOTH" else [args.jurisdiction]

    t0 = time.time()
    print(f"Querying v_entity_mapping_gap for {jurisdictions} (min_fact_count={args.min_fact_count})...", flush=True)
    with connect() as conn, conn.cursor() as cur:
        rows = _fetch_gap_rows(cur, jurisdictions, args.min_fact_count, args.limit)
        print(f"  fetched {len(rows):,} rows in {time.time()-t0:.1f}s", flush=True)

        t1 = time.time()
        print("Attaching linkbase calc-parent evidence...", flush=True)
        _attach_calc_parent_evidence(cur, rows)
        print(f"  done in {time.time()-t1:.1f}s", flush=True)

        t2 = time.time()
        print("Attaching concept labels for noise filtering...", flush=True)
        _attach_concept_labels(cur, rows)
        print(f"  done in {time.time()-t2:.1f}s", flush=True)

    rows, dropped = _filter_noise(rows)
    print(f"Noise filter dropped: {dropped}", flush=True)

    out_path = Path(args.output)
    _write_csv(rows, out_path)
    print(f"Wrote {len(rows):,} gap rows to {out_path}", flush=True)

    summary = _summarize(rows)
    print("\nSummary by (jurisdiction, sector_scope):")
    for (jr, sect), counts in sorted(summary.items()):
        print(f"  {jr} {sect:30s} unfilled={counts['unfilled_line_item']:>8,}  unmapped={counts['unmapped_concept']:>8,}")
    print(f"\nTotal runtime: {time.time()-t0:.1f}s")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
