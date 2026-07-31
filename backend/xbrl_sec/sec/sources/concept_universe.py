"""Build concept-universe evidence tables from parsed raw facts."""
from __future__ import annotations

import json
import re
from collections import defaultdict
import xml.etree.ElementTree as ET
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.edinet_filings import _companion_path, _metadata_from_name, companyfacts_dir
from xbrl_sec.sec.sources.sec_filings import iter_companyfacts_files, load_companyfacts
from xbrl_sec.sec.state.store import finish_run, start_run


_SECTOR_TABLES = {
    "corp": "ref_concept_universe_corp",
    "bank_financial": "ref_concept_universe_bank_financial",
    "non_bank_financial": "ref_concept_universe_non_bank_financial",
}
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_LINK_NS = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_XLINK_HREF = f"{{{_XLINK}}}href"
_XLINK_LABEL = f"{{{_XLINK}}}label"
_XLINK_FROM = f"{{{_XLINK}}}from"
_XLINK_TO = f"{{{_XLINK}}}to"
_XLINK_ROLE = f"{{{_XLINK}}}role"


def _split_concept_sql(concept_expr: str) -> tuple[str, str]:
    namespace = f"""
        CASE
            WHEN POSITION('/' IN {concept_expr}) > 0 THEN split_part({concept_expr}, '/', 1)
            WHEN POSITION(':' IN {concept_expr}) > 0 THEN split_part({concept_expr}, ':', 1)
            ELSE ''
        END
    """
    local = f"""
        CASE
            WHEN POSITION('/' IN {concept_expr}) > 0 THEN split_part({concept_expr}, '/', 2)
            WHEN POSITION(':' IN {concept_expr}) > 0 THEN split_part({concept_expr}, ':', 2)
            ELSE {concept_expr}
        END
    """
    return namespace, local


def _accounting_standard_sql(taxonomy_expr: str) -> str:
    return f"""
        CASE
            WHEN lower(COALESCE({taxonomy_expr}, '')) LIKE 'us-gaap%%' THEN 'US_GAAP'
            WHEN lower(COALESCE({taxonomy_expr}, '')) LIKE 'ifrs%%' THEN 'IFRS'
            WHEN lower(COALESCE({taxonomy_expr}, '')) LIKE 'jp%%' THEN 'JP_GAAP'
            ELSE NULLIF({taxonomy_expr}, '')
        END
    """


def _clear_generated_tables(jurisdiction: str | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        if jurisdiction:
            cur.execute("DELETE FROM ref_concept_universe_observation WHERE jurisdiction=%s", (jurisdiction,))
            for table in _SECTOR_TABLES.values():
                cur.execute(f"DELETE FROM {table} WHERE jurisdiction=%s", (jurisdiction,))
        else:
            cur.execute("TRUNCATE ref_concept_universe_observation RESTART IDENTITY")
            for table in _SECTOR_TABLES.values():
                cur.execute(f"TRUNCATE {table}")


def _insert_observations(jurisdiction: str) -> int:
    cfg = {
        # US reads the latest-vintage view so a concept observed across multiple
        # filing vintages of the same period is counted once (see migration 113).
        "US": ("v_fact_fundamentals_us_latest", "dim_company_us", "cik"),
        "JP": ("fact_fundamentals_jp", "dim_company_jp", "edinet_code"),
    }[jurisdiction]
    fact_table, dim_table, entity_col = cfg
    namespace_sql, local_sql = _split_concept_sql("f.concept_id")
    accounting_sql = _accounting_standard_sql("f.taxonomy")
    sql = f"""
        INSERT INTO ref_concept_universe_observation (
            jurisdiction, concept_id, namespace, local_name, fiscal_year, taxonomy,
            accounting_standard, mapping_sector, gics_sector_code, gics_sector_name,
            gics_industry_group_code, gics_industry_group_name, statement_type,
            root_id, parent_id, concept_path, concept_id_level, unit, value_type,
            reporter_count, filing_count, fact_count, first_period_end, last_period_end,
            first_filed_date, last_filed_date, sample_entities, sample_filings,
            sample_units, sample_concept_paths
        )
        SELECT
            %s AS jurisdiction,
            f.concept_id,
            {namespace_sql} AS namespace,
            {local_sql} AS local_name,
            -- Bin by the fact's period year, not the filing year. The raw
            -- fiscal_year column is the SEC companyfacts filing `fy`; for
            -- early-FYE filers a comparative fact would otherwise be attributed
            -- to the filing year and could miss its mapping's effective window
            -- in unmapped_concepts / the review queue.
            (CASE
                WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL
                    THEN EXTRACT(YEAR FROM f.period_end)::int
                ELSE f.fiscal_year
            END)::smallint AS fiscal_year,
            f.taxonomy,
            {accounting_sql} AS accounting_standard,
            COALESCE(NULLIF(d.mapping_sector, ''), 'corp') AS mapping_sector,
            d.gics_sector_code,
            d.gics_sector_name,
            d.gics_industry_group_code,
            d.gics_industry_group_name,
            f.statement_type,
            f.root_id,
            f.parent_id,
            f.concept_path,
            f.concept_id_level,
            f.unit,
            f.value_type,
            COUNT(DISTINCT f.{entity_col})::integer AS reporter_count,
            COUNT(DISTINCT f.filing_id)::integer AS filing_count,
            COUNT(*)::bigint AS fact_count,
            MIN(f.period_end) AS first_period_end,
            MAX(f.period_end) AS last_period_end,
            MIN(f.filed_date) AS first_filed_date,
            MAX(f.filed_date) AS last_filed_date,
            (array_agg(DISTINCT f.{entity_col}) FILTER (WHERE f.{entity_col} IS NOT NULL))[1:10],
            (array_agg(DISTINCT f.filing_id) FILTER (WHERE f.filing_id IS NOT NULL))[1:10],
            (array_agg(DISTINCT f.unit) FILTER (WHERE f.unit IS NOT NULL))[1:10],
            (array_agg(DISTINCT f.concept_path) FILTER (WHERE f.concept_path IS NOT NULL))[1:5]
        FROM {fact_table} f
        LEFT JOIN {dim_table} d ON d.{entity_col} = f.{entity_col}
        WHERE f.fiscal_year IS NOT NULL
          AND f.value IS NOT NULL
        GROUP BY
            f.concept_id, namespace, local_name,
            (CASE
                WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL
                    THEN EXTRACT(YEAR FROM f.period_end)::int
                ELSE f.fiscal_year
            END), f.taxonomy,
            accounting_standard, mapping_sector, d.gics_sector_code,
            d.gics_sector_name, d.gics_industry_group_code,
            d.gics_industry_group_name, f.statement_type, f.root_id,
            f.parent_id, f.concept_path, f.concept_id_level, f.unit, f.value_type
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (jurisdiction,))
        return cur.rowcount


def _rollup_sector(sector: str, table: str, jurisdiction: str | None = None) -> int:
    where = "WHERE mapping_sector = %s"
    params: list[Any] = [sector]
    if jurisdiction:
        where += " AND jurisdiction = %s"
        params.append(jurisdiction)
    sql = f"""
        INSERT INTO {table} (
            jurisdiction, concept_id, namespace, local_name, mapping_sector,
            label_en, label_ja, description, first_seen_year, last_seen_year,
            reporter_count, filing_count, fact_count, statement_types, taxonomies,
            units, gics_sector_codes, gics_industry_group_codes, sample_entities,
            sample_filings, sample_concept_paths
        )
        SELECT
            jurisdiction,
            concept_id,
            MAX(namespace),
            MAX(local_name),
            mapping_sector,
            MAX(label_en),
            MAX(label_ja),
            MAX(description),
            MIN(fiscal_year)::smallint,
            MAX(fiscal_year)::smallint,
            MAX(reporter_count)::integer,
            SUM(filing_count)::integer,
            SUM(fact_count)::bigint,
            (array_agg(DISTINCT statement_type) FILTER (WHERE statement_type IS NOT NULL))[1:20],
            (array_agg(DISTINCT taxonomy) FILTER (WHERE taxonomy IS NOT NULL))[1:20],
            (array_agg(DISTINCT unit) FILTER (WHERE unit IS NOT NULL))[1:20],
            (array_agg(DISTINCT gics_sector_code) FILTER (WHERE gics_sector_code IS NOT NULL))[1:20],
            (array_agg(DISTINCT gics_industry_group_code) FILTER (WHERE gics_industry_group_code IS NOT NULL))[1:20],
            (array_agg(DISTINCT o.sample_entities[1]) FILTER (WHERE o.sample_entities[1] IS NOT NULL))[1:20],
            (array_agg(DISTINCT o.sample_filings[1]) FILTER (WHERE o.sample_filings[1] IS NOT NULL))[1:20],
            (array_agg(DISTINCT o.sample_concept_paths[1]) FILTER (WHERE o.sample_concept_paths[1] IS NOT NULL))[1:10]
        FROM ref_concept_universe_observation o
        {where}
        GROUP BY jurisdiction, concept_id, mapping_sector
        ON CONFLICT (jurisdiction, concept_id, mapping_sector) DO UPDATE SET
            namespace = EXCLUDED.namespace,
            local_name = EXCLUDED.local_name,
            label_en = EXCLUDED.label_en,
            label_ja = EXCLUDED.label_ja,
            description = EXCLUDED.description,
            first_seen_year = EXCLUDED.first_seen_year,
            last_seen_year = EXCLUDED.last_seen_year,
            reporter_count = EXCLUDED.reporter_count,
            filing_count = EXCLUDED.filing_count,
            fact_count = EXCLUDED.fact_count,
            statement_types = EXCLUDED.statement_types,
            taxonomies = EXCLUDED.taxonomies,
            units = EXCLUDED.units,
            gics_sector_codes = EXCLUDED.gics_sector_codes,
            gics_industry_group_codes = EXCLUDED.gics_industry_group_codes,
            sample_entities = EXCLUDED.sample_entities,
            sample_filings = EXCLUDED.sample_filings,
            sample_concept_paths = EXCLUDED.sample_concept_paths,
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def _derive_label_from_local(local_name: str | None) -> str | None:
    if not local_name:
        return None
    clean = re.sub(r"_E\d+.*$", "", local_name)
    return _CAMEL_RE.sub(" ", clean).strip() or None


def _concept_from_href(href: str | None) -> str | None:
    if not href or "#" not in href:
        return None
    fragment = href.rsplit("#", 1)[-1]
    if "_" not in fragment:
        return fragment
    parts = fragment.split("_", 2)
    if len(parts) == 3:
        prefix = f"{parts[0]}_{parts[1]}"
        local = parts[2]
    else:
        prefix, local = fragment.split("_", 1)
    return f"{prefix}/{local}"


def _parse_label_linkbase(path) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    labels: dict[str, dict[str, str]] = {}
    for link in root.iter(f"{{{_LINK_NS}}}labelLink"):
        locs: dict[str, str] = {}
        resources: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for loc in link.iter(f"{{{_LINK_NS}}}loc"):
            label_id = loc.get(_XLINK_LABEL)
            concept = _concept_from_href(loc.get(_XLINK_HREF))
            if label_id and concept:
                locs[label_id] = concept
        for resource in link.iter(f"{{{_LINK_NS}}}label"):
            label_id = resource.get(_XLINK_LABEL)
            text = (resource.text or "").strip()
            if not label_id or not text:
                continue
            role = resource.get(_XLINK_ROLE, "")
            lang = resource.get("{http://www.w3.org/XML/1998/namespace}lang", "")
            resources[label_id].append((role, lang, text))
        for arc in link.iter(f"{{{_LINK_NS}}}labelArc"):
            concept = locs.get(arc.get(_XLINK_FROM, ""))
            if not concept:
                continue
            for role, lang, text in resources.get(arc.get(_XLINK_TO, ""), []):
                entry = labels.setdefault(concept, {})
                role_lower = role.lower()
                lang_lower = lang.lower()
                if "documentation" in role_lower:
                    entry.setdefault("description", text)
                elif lang_lower.startswith("ja"):
                    if "verbose" in role_lower:
                        entry.setdefault("label_ja", text)
                    else:
                        entry["label_ja"] = text
                elif "verbose" in role_lower:
                    entry.setdefault("label_en_verbose", text)
                elif "terse" in role_lower:
                    entry.setdefault("label_en_terse", text)
                else:
                    entry["label_en"] = text
    for concept, entry in labels.items():
        if "label_en" not in entry:
            entry["label_en"] = entry.get("label_en_terse") or entry.get("label_en_verbose", "")
    return labels


def _load_us_companyfacts_labels(entity_ids: list[str] | None = None) -> dict[str, tuple[str | None, str | None]]:
    labels: dict[str, tuple[str | None, str | None]] = {}
    for item in iter_companyfacts_files(entity_ids):
        payload = load_companyfacts(item.path)
        for taxonomy, concepts in (payload.get("facts") or {}).items():
            if not isinstance(concepts, dict):
                continue
            for local_name, concept in concepts.items():
                if not isinstance(concept, dict):
                    continue
                concept_id = f"{taxonomy}/{local_name}"
                label = concept.get("label")
                description = concept.get("description")
                existing = labels.get(concept_id)
                if existing is None or (description and not existing[1]):
                    labels[concept_id] = (label, description)
    return labels


def _load_jp_xbrl_labels(entity_ids: list[str] | None = None) -> dict[str, tuple[str | None, str | None, str | None]]:
    labels: dict[str, tuple[str | None, str | None, str | None]] = {}
    wanted = set(entity_ids or [])
    root = companyfacts_dir()
    if not root.exists():
        return labels
    seen_paths = set()
    for path in root.glob("*/*.xbrl"):
        metadata = _metadata_from_name(path)
        if not metadata:
            continue
        _doc_id, edinet_code, _filing_type, _period_end, _filed_date = metadata
        if wanted and edinet_code not in wanted:
            continue
        lab_paths = (_companion_path(path, "_lab-en.xml"), _companion_path(path, "_lab.xml"))
        for lab_path in lab_paths:
            if lab_path is None or lab_path in seen_paths:
                continue
            seen_paths.add(lab_path)
            parsed = _parse_label_linkbase(lab_path)
            for concept_id, values in parsed.items():
                label_en = values.get("label_en") or values.get("label_en_verbose") or values.get("label_en_terse")
                label_ja = values.get("label_ja")
                description = values.get("description")
                existing = labels.get(concept_id, (None, None, None))
                labels[concept_id] = (
                    existing[0] or label_en,
                    existing[1] or label_ja,
                    existing[2] or description,
                )
    return labels


def _apply_labels(jurisdiction: str, labels: dict[str, tuple]) -> int:
    rows = [
        (
            jurisdiction,
            concept_id,
            values[0] if len(values) > 0 else None,
            values[1] if len(values) > 2 else None,
            values[2] if len(values) > 2 else values[1] if len(values) > 1 else None,
        )
        for concept_id, values in labels.items()
        if any(values)
    ]
    if not rows:
        return 0
    sql = """
        UPDATE ref_concept_universe_observation o
           SET label_en = COALESCE(v.label_en, o.label_en),
               label_ja = COALESCE(v.label_ja, o.label_ja),
               description = COALESCE(v.description, o.description),
               updated_at = now()
          FROM (VALUES %s) AS v(jurisdiction, concept_id, label_en, label_ja, description)
         WHERE o.jurisdiction = v.jurisdiction
           AND o.concept_id = v.concept_id
    """
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=5000)
        return cur.rowcount


def _apply_derived_labels(jurisdiction: str | None = None) -> int:
    params: list[Any] = []
    where = ""
    if jurisdiction:
        where = "WHERE jurisdiction=%s"
        params.append(jurisdiction)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT observation_id, local_name
            FROM ref_concept_universe_observation
            {where}
              {"AND" if where else "WHERE"} label_en IS NULL
            """,
            params,
        )
        rows = [(obs_id, _derive_label_from_local(local)) for obs_id, local in cur.fetchall()]
        rows = [(obs_id, label) for obs_id, label in rows if label]
        if not rows:
            return 0
        execute_values(
            cur,
            """
            UPDATE ref_concept_universe_observation o
               SET label_en = v.label_en,
                   updated_at = now()
              FROM (VALUES %s) AS v(observation_id, label_en)
             WHERE o.observation_id = v.observation_id
            """,
            rows,
            page_size=5000,
        )
        return cur.rowcount


def _refresh_rollups(jurisdiction: str | None = None) -> dict[str, int]:
    return {sector: _rollup_sector(sector, table, jurisdiction) for sector, table in _SECTOR_TABLES.items()}


def build_concept_universe(
    jurisdiction: str | None = None,
    entity_ids: list[str] | None = None,
    enrich_labels: bool = False,
) -> dict[str, int]:
    """Rebuild generated concept evidence for US, JP, or both.

    entity_ids currently scopes US label extraction only; the SQL fact rebuild
    is jurisdiction-level so sector rollups stay internally consistent.
    """
    jurisdictions = [jurisdiction] if jurisdiction else ["US", "JP"]
    ctx = start_run(jurisdiction or "GLOBAL", "concept_universe", "full_refresh")
    try:
        _clear_generated_tables(jurisdiction)
        out: dict[str, int] = defaultdict(int)
        for jur in jurisdictions:
            inserted = _insert_observations(jur)
            out[f"{jur.lower()}_observations"] = inserted
            if enrich_labels and jur == "US":
                out["us_labels"] = _apply_labels("US", _load_us_companyfacts_labels(entity_ids))
            if enrich_labels and jur == "JP":
                out["jp_xbrl_labels"] = _apply_labels("JP", _load_jp_xbrl_labels(entity_ids))
            out[f"{jur.lower()}_derived_labels"] = _apply_derived_labels(jur)
        for sector, count in _refresh_rollups(jurisdiction).items():
            out[f"rollup_{sector}"] = count
        total_out = sum(v for key, v in out.items() if key.endswith("_observations"))
        finish_run(ctx, "succeeded", rows_out=total_out)
        return dict(out)
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def unmapped_concepts(jurisdiction: str, limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.jurisdiction, o.concept_id, o.mapping_sector, o.label_en,
                   o.description, SUM(o.fact_count) AS fact_count,
                   MAX(o.reporter_count) AS reporter_count,
                   MIN(o.fiscal_year) AS first_seen_year,
                   MAX(o.fiscal_year) AS last_seen_year
            FROM ref_concept_universe_observation o
            WHERE o.jurisdiction = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM map_concept_to_taxonomy_versioned m
                  WHERE m.concept_id IN (o.concept_id, replace(o.concept_id, ':', '/'))
                    AND m.jurisdiction IN (o.jurisdiction, 'BOTH')
                    AND m.mapping_sector IN (o.mapping_sector, 'corp', '')
                    AND COALESCE(m.effective_to_year, 9999) >= o.fiscal_year
                    AND m.effective_from_year <= o.fiscal_year
              )
            GROUP BY o.jurisdiction, o.concept_id, o.mapping_sector, o.label_en, o.description
            ORDER BY SUM(o.fact_count) DESC, MAX(o.reporter_count) DESC, o.concept_id
            LIMIT %s
            """,
            (jurisdiction, limit),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def unmapped_concepts_json(jurisdiction: str, limit: int = 100) -> str:
    return json.dumps(unmapped_concepts(jurisdiction, limit), default=str, ensure_ascii=False, indent=2)
