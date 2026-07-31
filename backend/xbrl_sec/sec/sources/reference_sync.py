"""Sync legacy/staging concept mappings from quant into xbrl_sec.sec.

This module intentionally writes only to map_concept_to_taxonomy, the legacy
snapshot table. Standardized line items are controlled by
spec/line_item_metric_registry.json via registry_sync.py. The governed
production mapping table map_concept_to_taxonomy_versioned contains
paid/curated mappings and must not be truncated or bulk-overwritten by sync
commands.
"""
from __future__ import annotations

import os
import site
from collections import defaultdict
from typing import Iterable, Sequence

site.addsitedir(site.getusersitepackages())
import psycopg2

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


def _quant_url() -> str:
    return os.environ.get("QUANT_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/quant")


def _fetch(sql: str) -> list[tuple]:
    with psycopg2.connect(_quant_url()) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _prefer_mapping(row: tuple) -> tuple:
    tier = int(row[2] or 999)
    created_at = row[6]
    has_reasoning = 1 if row[4] else 0
    return (tier, -has_reasoning, created_at is None, created_at)


def _dedupe_mappings(rows: Iterable[tuple]) -> list[tuple]:
    deduped: dict[tuple[str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[5])
        existing = deduped.get(key)
        if existing is None or _prefer_mapping(row) < _prefer_mapping(existing):
            deduped[key] = row
    return list(deduped.values())


def _mapping_duplicate_audit(rows: Iterable[tuple]) -> list[tuple]:
    grouped: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in rows:
        grouped[(row[0], row[5])].append(row)

    audit_rows: list[tuple] = []
    for (concept_id, mapping_sector), group in grouped.items():
        if len(group) <= 1:
            continue
        targets = sorted({row[1] for row in group if row[1]})
        tiers = {row[2] for row in group if row[2] is not None}
        audit_rows.append(
            (
                concept_id,
                mapping_sector,
                len(group),
                len(targets),
                len(tiers),
                targets[:8],
            )
        )
    return audit_rows


def sync_reference_tables() -> tuple[int, int]:
    mappings = _fetch(
        """
        SELECT concept_id, target_variable, tier, multiplier, reasoning,
               COALESCE(mapping_sector, '') AS mapping_sector, created_at
        FROM public.map_concept_to_taxonomy
        WHERE target_variable IS NOT NULL
          AND target_variable <> 'UNMAPPED'
          AND tier IS NOT NULL
        """
    )
    duplicate_audit = _mapping_duplicate_audit(mappings)
    mappings = _dedupe_mappings(mappings)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE audit_reference_mapping_duplicates")
        if duplicate_audit:
            execute_values(
                cur,
                """
                INSERT INTO audit_reference_mapping_duplicates
                    (concept_id, mapping_sector, duplicate_count, distinct_target_variables,
                     distinct_tiers, sample_target_variables)
                VALUES %s
                ON CONFLICT (concept_id, mapping_sector) DO UPDATE SET
                    duplicate_count = EXCLUDED.duplicate_count,
                    distinct_target_variables = EXCLUDED.distinct_target_variables,
                    distinct_tiers = EXCLUDED.distinct_tiers,
                    sample_target_variables = EXCLUDED.sample_target_variables,
                    synced_at = now()
                """,
                duplicate_audit,
                page_size=1000,
            )
        cur.execute("TRUNCATE map_concept_to_taxonomy")
        execute_values(
            cur,
            """
            INSERT INTO map_concept_to_taxonomy
                (concept_id, target_variable, tier, multiplier, reasoning, mapping_sector, created_at)
            VALUES %s
            ON CONFLICT (concept_id, mapping_sector) DO UPDATE SET
                target_variable = EXCLUDED.target_variable,
                tier = EXCLUDED.tier,
                multiplier = EXCLUDED.multiplier,
                reasoning = EXCLUDED.reasoning
            """,
            mappings,
            page_size=5000,
        )
    return len(mappings), 0
