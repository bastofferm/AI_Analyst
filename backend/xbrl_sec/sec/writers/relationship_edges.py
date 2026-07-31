"""Bulk writer for sec.ref_xbrl_relationship_edge.

Consumes the arc dicts emitted by ``iter_cal_arcs`` / ``iter_pre_arcs`` /
``iter_def_arcs`` and persists them as normalized rows. Idempotent via the
``uq_rxre_natural_key`` unique index — re-running the backfill for the same
filing is safe.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.parsers.xbrl_linkbase import iter_cal_arcs, iter_def_arcs, iter_pre_arcs


_COLUMNS = (
    "jurisdiction",
    "entity_id",
    "filing_id",
    "taxonomy",
    "role_uri",
    "linkbase_type",
    "parent_concept_id",
    "child_concept_id",
    "weight",
    "order_index",
    "arcrole",
    "preferred_label",
    "dimension_axis",
    "dimension_member",
    "usable",
    "source_path",
)


def _row(
    arc: dict[str, Any],
    *,
    jurisdiction: str,
    entity_id: str | None,
    filing_id: str | None,
    taxonomy: str | None,
    source_path: str | None,
) -> tuple:
    return (
        jurisdiction,
        entity_id,
        filing_id,
        taxonomy,
        arc.get("role_uri"),
        arc.get("linkbase_type"),
        arc.get("parent_concept_id"),
        arc.get("child_concept_id"),
        arc.get("weight"),
        arc.get("order_index"),
        arc.get("arcrole"),
        arc.get("preferred_label"),
        arc.get("dimension_axis"),
        arc.get("dimension_member"),
        arc.get("usable"),
        source_path,
    )


def upsert_relationship_edges(rows: Iterable[tuple], page_size: int = 5000) -> int:
    # Dedupe by the natural key so ON CONFLICT cannot fire within one batch.
    # Natural key positions (0-indexed) in _COLUMNS: jurisdiction(0), filing_id(2),
    # linkbase_type(5), role_uri(4), parent_concept_id(6), child_concept_id(7),
    # dimension_axis(12), dimension_member(13).
    deduped: dict[tuple, tuple] = {}
    for row in rows:
        key = (
            row[0],            # jurisdiction
            row[2] or "",      # filing_id
            row[5],            # linkbase_type
            row[4] or "",      # role_uri
            row[6] or "",      # parent_concept_id
            row[7],            # child_concept_id
            row[12] or "",     # dimension_axis
            row[13] or "",     # dimension_member
        )
        deduped[key] = row
    values = list(deduped.values())
    if not values:
        return 0
    cols = ", ".join(_COLUMNS)
    sql = f"""
        INSERT INTO ref_xbrl_relationship_edge ({cols}) VALUES %s
        ON CONFLICT (
            jurisdiction,
            COALESCE(filing_id, ''),
            linkbase_type,
            COALESCE(role_uri, ''),
            COALESCE(parent_concept_id, ''),
            child_concept_id,
            COALESCE(dimension_axis, ''),
            COALESCE(dimension_member, '')
        )
        DO UPDATE SET
            weight = EXCLUDED.weight,
            order_index = EXCLUDED.order_index,
            arcrole = EXCLUDED.arcrole,
            preferred_label = EXCLUDED.preferred_label,
            usable = EXCLUDED.usable,
            taxonomy = COALESCE(EXCLUDED.taxonomy, ref_xbrl_relationship_edge.taxonomy),
            entity_id = COALESCE(EXCLUDED.entity_id, ref_xbrl_relationship_edge.entity_id),
            source_path = COALESCE(EXCLUDED.source_path, ref_xbrl_relationship_edge.source_path)
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, values, page_size=page_size)


def edges_from_filing(
    *,
    jurisdiction: str,
    entity_id: str | None,
    filing_id: str | None,
    taxonomy: str | None,
    cal_path: Path | None = None,
    pre_path: Path | None = None,
    def_path: Path | None = None,
) -> list[tuple]:
    """Build the row tuples for one filing's three linkbases."""
    rows: list[tuple] = []
    for arc in iter_cal_arcs(cal_path):
        rows.append(_row(
            arc,
            jurisdiction=jurisdiction,
            entity_id=entity_id,
            filing_id=filing_id,
            taxonomy=taxonomy,
            source_path=str(cal_path) if cal_path else None,
        ))
    for arc in iter_pre_arcs(pre_path):
        rows.append(_row(
            arc,
            jurisdiction=jurisdiction,
            entity_id=entity_id,
            filing_id=filing_id,
            taxonomy=taxonomy,
            source_path=str(pre_path) if pre_path else None,
        ))
    for arc in iter_def_arcs(def_path):
        rows.append(_row(
            arc,
            jurisdiction=jurisdiction,
            entity_id=entity_id,
            filing_id=filing_id,
            taxonomy=taxonomy,
            source_path=str(def_path) if def_path else None,
        ))
    return rows


def write_filing_edges(
    *,
    jurisdiction: str,
    entity_id: str | None,
    filing_id: str | None,
    taxonomy: str | None,
    cal_path: Path | None = None,
    pre_path: Path | None = None,
    def_path: Path | None = None,
    page_size: int = 5000,
) -> int:
    """Convenience: parse one filing's linkbases and persist all edges."""
    rows = edges_from_filing(
        jurisdiction=jurisdiction,
        entity_id=entity_id,
        filing_id=filing_id,
        taxonomy=taxonomy,
        cal_path=cal_path,
        pre_path=pre_path,
        def_path=def_path,
    )
    return upsert_relationship_edges(rows, page_size=page_size)
