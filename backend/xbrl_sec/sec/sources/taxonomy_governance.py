"""Taxonomy version governance for the MZQA concept mapping pipeline.

Handles lifecycle management of taxonomy element versions:
- Differential analysis when a new taxonomy year is released
- New element detection and review-queue insertion
- Deprecated element retirement from production mappings
- Query utilities for taxonomy year filtering
"""
from __future__ import annotations

import site
from typing import Any

site.addsitedir(site.getusersitepackages())
import psycopg2
import psycopg2.extensions

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.db.bulk import execute_values


# ---------------------------------------------------------------------------
# Taxonomy diff
# ---------------------------------------------------------------------------

def diff_taxonomy_releases(
    conn: psycopg2.extensions.connection,
    namespace: str,
    old_year: int,
    new_year: int,
) -> dict[str, list]:
    """Compare taxonomy elements between two release years.

    Returns a dict with three keys:

    * ``new_elements`` — local_names present only in *new_year*
    * ``deprecated_elements`` — local_names present in *old_year* that are
      either absent from *new_year* or marked ``is_deprecated=True``
    * ``changed_documentation`` — local_names whose documentation text
      differs between the two years
    """
    with conn.cursor() as cur:
        # New elements: in new year but not old year
        cur.execute(
            """
            SELECT local_name
            FROM sec.ref_taxonomy_element
            WHERE namespace = %s AND taxonomy_year = %s
              AND local_name NOT IN (
                  SELECT local_name
                  FROM sec.ref_taxonomy_element
                  WHERE namespace = %s AND taxonomy_year = %s
              )
            ORDER BY local_name
            """,
            (namespace, new_year, namespace, old_year),
        )
        new_elements = [r[0] for r in cur.fetchall()]

        # Deprecated: in old year but either absent from new or explicitly deprecated
        cur.execute(
            """
            SELECT local_name
            FROM sec.ref_taxonomy_element
            WHERE namespace = %s AND taxonomy_year = %s
              AND (
                  is_deprecated = true
                  OR local_name NOT IN (
                      SELECT local_name
                      FROM sec.ref_taxonomy_element
                      WHERE namespace = %s AND taxonomy_year = %s
                  )
              )
            ORDER BY local_name
            """,
            (namespace, old_year, namespace, new_year),
        )
        deprecated_elements = [r[0] for r in cur.fetchall()]

        # Changed documentation: same local_name, different doc text
        cur.execute(
            """
            SELECT old.local_name, old.documentation, new.documentation
            FROM sec.ref_taxonomy_element old
            JOIN sec.ref_taxonomy_element new
              ON old.namespace = new.namespace
             AND old.local_name = new.local_name
            WHERE old.namespace = %s
              AND old.taxonomy_year = %s
              AND new.taxonomy_year = %s
              AND old.documentation IS DISTINCT FROM new.documentation
            ORDER BY old.local_name
            """,
            (namespace, old_year, new_year),
        )
        changed_documentation = [
            {"local_name": r[0], "old_doc": r[1], "new_doc": r[2]}
            for r in cur.fetchall()
        ]

    return {
        "new_elements": new_elements,
        "deprecated_elements": deprecated_elements,
        "changed_documentation": changed_documentation,
    }


# ---------------------------------------------------------------------------
# New element queuing
# ---------------------------------------------------------------------------

def queue_new_elements(
    conn: psycopg2.extensions.connection,
    namespace: str,
    new_elements: list[str],
    year: int,
) -> int:
    """Ensure new taxonomy elements are visible to the suggestion pipeline.

    For each local_name in *new_elements* that does NOT already have a
    mapping in ``map_concept_to_taxonomy_versioned``, inserts a row into
    the review queue so it becomes discoverable.

    Returns the number of rows inserted.
    """
    if not new_elements:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT local_name
            FROM map_concept_to_taxonomy_versioned
            WHERE concept_id = ANY(%s)
            """,
            ([f"{namespace}/{ln}" for ln in new_elements],),
        )
        already_mapped = {r[0].split("/", 1)[-1] for r in cur.fetchall()}

    to_queue = [
        ln for ln in new_elements if ln not in already_mapped
    ]

    if not to_queue:
        return 0

    rows = [
        (namespace, ln, year) for ln in to_queue
    ]

    with conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO sec.map_concept_to_taxonomy_review_queue
                (jurisdiction, normalized_concept_id, review_status, reasoning)
            VALUES
                ('US', %s || '/' || %s, 'queued',
                 'New element introduced in taxonomy year ' || %s || '.')
            ON CONFLICT DO NOTHING
            """,
            rows,
            page_size=1000,
        )


# ---------------------------------------------------------------------------
# Deprecated element retirement
# ---------------------------------------------------------------------------

def retire_deprecated_mappings(
    conn: psycopg2.extensions.connection,
    namespace: str,
    deprecated_elements: list[str],
) -> int:
    """Close the effective date range for deprecated taxonomy elements.

    Sets ``effective_to_year`` = CURRENT_DATE on all production mappings
    whose ``concept_id`` matches one of the deprecated elements.

    Returns the number of rows updated.
    """
    if not deprecated_elements:
        return 0

    concept_ids = [f"{namespace}/{ln}" for ln in deprecated_elements]

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sec.map_concept_to_taxonomy_versioned
            SET effective_to_year = EXTRACT(YEAR FROM CURRENT_DATE) :: int
            WHERE concept_id = ANY(%s)
              AND effective_to_year IS NULL
            """,
            (concept_ids,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_taxonomy_diff(
    conn: psycopg2.extensions.connection,
    namespace: str,
    old_year: int,
    new_year: int,
) -> None:
    """Run a full taxonomy diff: compare, queue new, retire old, log summary.

    Prints a human-readable diff summary to stdout.
    """
    diff = diff_taxonomy_releases(conn, namespace, old_year, new_year)

    print(f"\nTaxonomy diff: {namespace} {old_year} → {new_year}")
    print(f"  New elements:       {len(diff['new_elements'])}")
    print(f"  Deprecated elements: {len(diff['deprecated_elements'])}")
    print(f"  Changed documentation: {len(diff['changed_documentation'])}")

    if diff["new_elements"]:
        queued = queue_new_elements(conn, namespace, diff["new_elements"], new_year)
        print(f"  Queued for review:  {queued}")

    if diff["deprecated_elements"]:
        retired = retire_deprecated_mappings(conn, namespace, diff["deprecated_elements"])
        print(f"  Retired mappings:   {retired}")

    if diff["changed_documentation"]:
        print(f"  Changed docs (first 10):")
        for ch in diff["changed_documentation"][:10]:
            old_doc = (ch["old_doc"] or "")[:80]
            print(f"    {ch['local_name']}:  {old_doc}...")


# ---------------------------------------------------------------------------
# Utility: taxonomy year filter
# ---------------------------------------------------------------------------

def apply_taxonomy_year_filter(
    base_query: str, taxonomy_year: int | None
) -> str:
    """Append a taxonomy-year WHERE clause to a query string.

    If *taxonomy_year* is ``None`` the query is returned unchanged.
    """
    if taxonomy_year is None:
        return base_query
    return f"{base_query}\n  AND taxonomy_year = {int(taxonomy_year)}"
