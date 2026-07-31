"""Audit which line_items in sector display profiles have no mapping support.

For each (accounting_standard, sector_scope, statement_type, line_item_id) in
ref_std_statement_display_profile that should be filled from raw facts (i.e.
not a CALCULATED/derived row), check whether at least one mapping in
map_concept_to_taxonomy_versioned routes some concept to that line_item with
a mapping_sector compatible with the profile's sector_scope.

Compatibility:
  corp                          -> mapping_sector IN ('BOTH', 'corp')
  bank_financial                -> mapping_sector IN ('BOTH', 'bank_financial')
  insurance / reit /            -> mapping_sector IN ('BOTH', 'non_bank_financial')
    asset_manager_other_financial

Jurisdiction compatibility: US_GAAP -> jurisdiction IN ('US','BOTH'),
JP_GAAP -> jurisdiction IN ('JP','BOTH').

Output: sector_mapping_gaps.csv with one row per gap, sortable by sector and
statement type. Exit code 1 when gaps exist (so it can be used in CI later).
"""
from __future__ import annotations

import csv
from pathlib import Path

from xbrl_sec.sec.db.connection import connect


_MAPPING_SECTOR_FOR_SCOPE = {
    "corp": ("BOTH", "corp"),
    "bank_financial": ("BOTH", "bank_financial"),
    "insurance": ("BOTH", "non_bank_financial"),
    "reit": ("BOTH", "non_bank_financial"),
    "asset_manager_other_financial": ("BOTH", "non_bank_financial"),
}

_JURISDICTION_FOR_STANDARD = {
    "US_GAAP": ("US", "BOTH"),
    "JP_GAAP": ("JP", "BOTH"),
}

# Derived/computed roles: don't expect direct mappings.
_DERIVED_ROLES = {"CALCULATED"}


def audit(output: Path) -> tuple[int, int]:
    """Returns (rows_audited, gap_count)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT accounting_standard, sector_scope, statement_type, line_item_id,
                   display_role, display_policy
            FROM ref_std_statement_display_profile
            WHERE display_policy != 'HIDE'
            ORDER BY accounting_standard, sector_scope, statement_type, display_order NULLS LAST, line_item_id
            """
        )
        rows = cur.fetchall()

    gaps: list[dict] = []
    rows_audited = 0
    with connect() as conn, conn.cursor() as cur:
        for acct_std, sector, stmt, line_item, role, policy in rows:
            if str(role or "").upper() in _DERIVED_ROLES:
                continue
            rows_audited += 1
            mapping_sectors = _MAPPING_SECTOR_FOR_SCOPE.get(sector)
            jurisdictions = _JURISDICTION_FOR_STANDARD.get(acct_std)
            if not mapping_sectors or not jurisdictions:
                continue
            cur.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT concept_id),
                       MIN(jurisdiction), MIN(mapping_sector)
                FROM map_concept_to_taxonomy_versioned
                WHERE target_variable = %s
                  AND jurisdiction = ANY(%s)
                  AND COALESCE(mapping_sector, 'BOTH') = ANY(%s)
                """,
                (line_item, list(jurisdictions), list(mapping_sectors)),
            )
            count, distinct_concepts, _j, _m = cur.fetchone()
            if count == 0:
                gaps.append({
                    "accounting_standard": acct_std,
                    "sector_scope": sector,
                    "statement_type": stmt,
                    "line_item_id": line_item,
                    "display_role": role,
                    "display_policy": policy,
                    "gap_kind": "no_mapping_at_all",
                    "concepts_found": 0,
                })

    with output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "accounting_standard", "sector_scope", "statement_type",
                "line_item_id", "display_role", "display_policy",
                "gap_kind", "concepts_found",
            ],
        )
        writer.writeheader()
        writer.writerows(gaps)
    return rows_audited, len(gaps)


def summarize_gaps(output: Path) -> dict:
    """Quick rollup by (accounting_standard, sector_scope, statement_type)."""
    counts: dict[tuple, int] = {}
    with output.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["accounting_standard"], row["sector_scope"], row["statement_type"])
            counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    out = Path("sector_mapping_gaps.csv")
    audited, gaps = audit(out)
    print(f"Audited {audited} profile rows. Gaps: {gaps}")
    print(f"Wrote {out}")
    if gaps:
        summary = summarize_gaps(out)
        print("Gaps by (standard, sector, statement):")
        for (std, sector, stmt), n in sorted(summary.items()):
            print(f"  {std:8s} {sector:30s} {stmt:22s} {n:>4}")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
