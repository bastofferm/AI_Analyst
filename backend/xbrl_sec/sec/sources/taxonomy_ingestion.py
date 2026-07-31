"""Ingest XBRL taxonomy label/schema/reference linkbases into sec.ref_taxonomy_element.

Populates the element metadata table from official taxonomy ZIP packages
(US-GAAP, IFRS, JP-GAAP). When combined with pipeline observation data
this table enables LLM concept re-ranking with authoritative descriptions.
"""
from __future__ import annotations

import json
import re
import site
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import ZipFile

site.addsitedir(site.getusersitepackages())
import psycopg2
import psycopg2.extras

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.db.bulk import execute_values


# ---------------------------------------------------------------------------
# XBRL namespace constants
# ---------------------------------------------------------------------------
NS_LINK = "http://www.xbrl.org/2003/linkbase"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS_XSD = "http://www.w3.org/2001/XMLSchema"

# Label roles (standard XBRL 2.1)
ROLE_LABEL = "http://www.xbrl.org/2003/role/label"
ROLE_TERSE = "http://www.xbrl.org/2003/role/terseLabel"
ROLE_VERBOSE = "http://www.xbrl.org/2003/role/verboseLabel"
ROLE_DOC = "http://www.xbrl.org/2003/role/documentation"

# Patterns to classify statement type from concept name conventions
_RE_INCOME = re.compile(
    r"Revenue|Sales|Income|Expense|CostOf|Earnings|Tax|GrossProfit|"
    r"SG&A|R&D|Depreciation|Amortization|Interest|Dividend|"
    r"Operating(?:Income|Expenses?|Margin)",
    re.I,
)
_RE_BALANCE = re.compile(
    r"Assets?$|Liabilities?$|Equity$|Cash|Receivable|Inventory|Payable|"
    r"PPE|Property|Goodwill|Intangible|Debt|Borrowing|Stockholders|"
    r"Retained|APIC|AOCI|Treasury|Prepaid|Accrued|Deferred(?:Tax|Revenue)",
    re.I,
)
_RE_CASHFLOW = re.compile(
    r"NetCash|ProceedsFrom|PaymentsTo|PaymentsFor|IncreaseDecrease|"
    r"CashFlow|CapitalExpenditure|DividendsPaid|Repurchase|"
    r"IssuanceOf|RepaymentsOf",
    re.I,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str | None) -> str | None:
    """Collapse whitespace and strip; return None for empty."""
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned or None


def _local_name_from_href(href: str) -> str | None:
    """Extract local name from an xlink:href like 'namespace.xsd#ConceptName'."""
    if "#" in href:
        return href.split("#", 1)[1]
    return None


def _ns_tag(tag: str, ns: str) -> str:
    """Build an ElementTree tag with a specific namespace URI."""
    return f"{{{ns}}}{tag}"


def _classify_statement_type(local_name: str) -> str | None:
    """Guess the statement type from the local concept name."""
    if _RE_INCOME.search(local_name):
        return "IncomeStatement"
    elif _RE_CASHFLOW.search(local_name):
        return "CashFlow"
    elif _RE_BALANCE.search(local_name):
        return "BalanceSheet"
    return None


# ---------------------------------------------------------------------------
# Label linkbase parsing
# ---------------------------------------------------------------------------

def parse_label_linkbase(
    zip_path: Path, namespace: str, year: int
) -> list[dict[str, Any]]:
    """Parse an XBRL label linkbase XML and return per-element label data.

    Looks for ``*-label.xml`` or ``*-lab-en.xml`` inside the ZIP archive.
    Returns a list of dicts with keys:

    * ``local_name``     — concept local name (e.g. ``Revenue``)
    * ``label``          — standard label text
    * ``label_terse``    — terse label text (optional)
    * ``label_verbose``  — verbose label text (optional)
    * ``documentation``  — documentation text (optional)
    """
    _ = namespace  # namespace is embedded in the href or file content
    _ = year       # year is metadata, not used for parsing

    with ZipFile(zip_path, "r") as zf:
        # Prefer the English label file; fall back to generic -label.xml
        candidates = [
            f
            for f in zf.namelist()
            if ("lab-en" in f.lower() or f.endswith("-label.xml"))
            and "deprecated" not in f.lower()
        ]
        if not candidates:
            # Broad last-resort: any -label.xml
            candidates = [f for f in zf.namelist() if "label" in f.lower() and f.endswith(".xml")]
        if not candidates:
            return []

        label_xml = zf.read(candidates[0])

    root = ET.fromstring(label_xml)

    # --- Pass 1: loc elements ---
    locs: dict[str, str] = {}
    for loc in root.iter(_ns_tag("loc", NS_LINK)):
        href = loc.get(_ns_tag("href", NS_XLINK), "")
        label_ref = loc.get(_ns_tag("label", NS_XLINK), "")
        local = _local_name_from_href(href)
        if label_ref and local:
            locs[label_ref] = local

    # --- Pass 2: labelArc -> links loc to resource label ---
    arcs: dict[str, str] = {}
    for arc in root.iter(_ns_tag("labelArc", NS_LINK)):
        fr = arc.get(_ns_tag("from", NS_XLINK), "")
        to = arc.get(_ns_tag("to", NS_XLINK), "")
        if fr and to and fr in locs:
            arcs[fr] = to

    # --- Pass 3: label resources ---
    resources: dict[str, dict[str, str]] = {}
    for lbl in root.iter(_ns_tag("label", NS_LINK)):
        role = lbl.get(_ns_tag("role", NS_XLINK), "")
        res_label = lbl.get(_ns_tag("label", NS_XLINK), "")
        text = _clean_text(lbl.text)
        if res_label and text and role:
            resources.setdefault(res_label, {})[role] = text

    # --- Match loc → arc → resource ---
    results: list[dict[str, Any]] = []
    for loc_label, local_name in locs.items():
        if loc_label not in arcs:
            continue
        res_label = arcs[loc_label]
        role_map = resources.get(res_label, {})
        entry: dict[str, Any] = {"local_name": local_name}
        for role, text in role_map.items():
            if role == ROLE_LABEL:
                entry["label"] = text
            elif role == ROLE_TERSE:
                entry["label_terse"] = text
            elif role == ROLE_VERBOSE:
                entry["label_verbose"] = text
            elif role == ROLE_DOC:
                entry["documentation"] = text
        if "label" in entry:
            results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Schema XSD parsing
# ---------------------------------------------------------------------------

def parse_schema_elements(
    zip_path: Path, namespace: str, year: int
) -> dict[str, dict[str, Any]]:
    """Parse taxonomy XSD and return element metadata keyed by local name.

    Extracts: ``periodType``, ``balance_type`` (debit/credit), ``data_type``,
    ``is_abstract``, ``is_deprecated`` (from substitutionGroup).

    Returns a dict mapping ``local_name`` → dict of attributes.
    """
    _ = namespace
    _ = year

    with ZipFile(zip_path, "r") as zf:
        xsd_files = [
            f for f in zf.namelist() if f.endswith(".xsd") and "label" not in f.lower()
        ]
        if not xsd_files:
            return {}

        # Pick the core schema (shortest path, not an import)
        xsd_files.sort(key=len)
        xsd_data = zf.read(xsd_files[0])

    root = ET.fromstring(xsd_data)
    result: dict[str, dict[str, Any]] = {}

    for elem in root.iter(_ns_tag("element", NS_XSD)):
        local_name = elem.get("name", "")
        if not local_name:
            continue

        attrs: dict[str, Any] = {}

        # periodType
        period_type = elem.get(_ns_tag("periodType", NS_XLINK), "")
        if period_type:
            attrs["period_type"] = period_type

        # balance (credit/debit)
        balance = elem.get(_ns_tag("balance", NS_XLINK), "")
        if balance:
            attrs["balance_type"] = balance

        # type
        type_qname = elem.get("type", "")
        if ":" in type_qname:
            attrs["data_type"] = type_qname.rsplit(":", 1)[-1]
        elif type_qname:
            attrs["data_type"] = type_qname

        # abstract
        is_abstract = elem.get("abstract", "false").lower() == "true"
        attrs["is_abstract"] = is_abstract

        # deprecated (via substitutionGroup ending in "-deprecated")
        sub_group = elem.get("substitutionGroup", "")
        attrs["is_deprecated"] = sub_group.endswith("-deprecated") if sub_group else False

        result[local_name] = attrs

    return result


# ---------------------------------------------------------------------------
# Reference linkbase parsing
# ---------------------------------------------------------------------------

def parse_reference_linkbase(
    zip_path: Path, namespace: str, year: int
) -> dict[str, list[dict[str, str]]]:
    """Parse XBRL reference linkbase and return per-element authoritative refs.

    Returns a dict mapping ``local_name`` → list of ``{standard, topic, paragraph}``.
    """
    _ = namespace
    _ = year

    with ZipFile(zip_path, "r") as zf:
        ref_files = [
            f
            for f in zf.namelist()
            if "reference" in f.lower() and f.endswith(".xml")
        ]
        if not ref_files:
            return {}
        ref_xml = zf.read(ref_files[0])

    root = ET.fromstring(ref_xml)

    # --- loc ---
    locs: dict[str, str] = {}
    for loc in root.iter(_ns_tag("loc", NS_LINK)):
        href = loc.get(_ns_tag("href", NS_XLINK), "")
        label_ref = loc.get(_ns_tag("label", NS_XLINK), "")
        local = _local_name_from_href(href)
        if label_ref and local:
            locs[label_ref] = local

    # --- referenceArc ---
    arcs: dict[str, str] = {}
    for arc in root.iter(_ns_tag("referenceArc", NS_LINK)):
        fr = arc.get(_ns_tag("from", NS_XLINK), "")
        to = arc.get(_ns_tag("to", NS_XLINK), "")
        if fr and to and fr in locs:
            arcs[fr] = to

    # --- reference resources ---
    ref_parts: dict[str, dict[str, str]] = {}
    for ref_elem in root.iter(_ns_tag("reference", NS_LINK)):
        res_label = ref_elem.get(_ns_tag("label", NS_XLINK), "")
        if not res_label:
            continue
        parts: dict[str, str] = {}
        for part in ref_elem:
            tag = part.tag.split("}")[-1] if "}" in part.tag else part.tag
            text = _clean_text(part.text)
            if text:
                parts[tag] = text
        if parts:
            ref_parts[res_label] = parts

    # --- match ---
    result: dict[str, list[dict[str, str]]] = {}
    for loc_label, local_name in locs.items():
        if loc_label not in arcs:
            continue
        res_label = arcs[loc_label]
        entry = ref_parts.get(res_label)
        if entry:
            result.setdefault(local_name, []).append(entry)

    return result


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------

_COLS = [
    "namespace",
    "local_name",
    "taxonomy_year",
    "label",
    "label_terse",
    "label_verbose",
    "documentation",
    "period_type",
    "balance_type",
    "data_type",
    "is_abstract",
    "is_deprecated",
    "authoritative_refs",
    "parent_concept",
    "statement_type",
    "sector_scope",
]

_SQL_UPSERT = f"""
    INSERT INTO sec.ref_taxonomy_element ({", ".join(_COLS)})
    VALUES %s
    ON CONFLICT (namespace, local_name, taxonomy_year) DO UPDATE SET
        label           = EXCLUDED.label,
        label_terse     = EXCLUDED.label_terse,
        label_verbose   = EXCLUDED.label_verbose,
        documentation   = EXCLUDED.documentation,
        period_type     = EXCLUDED.period_type,
        balance_type    = EXCLUDED.balance_type,
        data_type       = EXCLUDED.data_type,
        is_abstract     = EXCLUDED.is_abstract,
        is_deprecated   = EXCLUDED.is_deprecated,
        authoritative_refs  = EXCLUDED.authoritative_refs,
        parent_concept  = EXCLUDED.parent_concept,
        statement_type  = EXCLUDED.statement_type,
        sector_scope    = EXCLUDED.sector_scope,
        updated_at      = now()
"""


def upsert_taxonomy_elements(
    conn: psycopg2.extensions.connection, rows: list[dict[str, Any]]
) -> int:
    """Bulk upsert taxonomy elements into ``sec.ref_taxonomy_element``.

    Returns the number of rows inserted/updated.
    """
    if not rows:
        return 0

    tuples = [
        tuple(r.get(c) for c in _COLS)
        for r in rows
    ]

    with conn.cursor() as cur:
        return execute_values(cur, _SQL_UPSERT, tuples, page_size=5000)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def ingest_taxonomy(
    zip_path: Path, namespace: str, year: int, conn: psycopg2.extensions.connection
) -> int:
    """Ingest one taxonomy ZIP into ``sec.ref_taxonomy_element``.

    1. Parse labels, schema elements, and references from the ZIP.
    2. Merge per-element data by local_name.
    3. Bulk upsert and return the total number of rows written.
    """
    labels = parse_label_linkbase(zip_path, namespace, year)
    schema = parse_schema_elements(zip_path, namespace, year)
    references = parse_reference_linkbase(zip_path, namespace, year)

    # Merge by local_name
    merged: dict[str, dict[str, Any]] = {}
    for entry in labels:
        ln = entry["local_name"]
        base = merged.setdefault(ln, {
            "namespace": namespace,
            "local_name": ln,
            "taxonomy_year": year,
        })
        for key in ("label", "label_terse", "label_verbose", "documentation"):
            if key in entry:
                base[key] = entry[key]

    for ln, schema_attrs in schema.items():
        base = merged.setdefault(ln, {
            "namespace": namespace,
            "local_name": ln,
            "taxonomy_year": year,
        })
        for key in ("period_type", "balance_type", "data_type", "is_abstract", "is_deprecated"):
            if key in schema_attrs:
                base[key] = schema_attrs[key]

    for ln, ref_list in references.items():
        base = merged.setdefault(ln, {
            "namespace": namespace,
            "local_name": ln,
            "taxonomy_year": year,
        })
        base["authoritative_refs"] = psycopg2.extras.Json(ref_list)

    # Classify statement type for each element
    for ln, base in merged.items():
        if "statement_type" not in base:
            base["statement_type"] = _classify_statement_type(ln)

    row_list = list(merged.values())
    return upsert_taxonomy_elements(conn, row_list)


# ---------------------------------------------------------------------------
# Convenience: ingesting with automatic connection
# ---------------------------------------------------------------------------

def ingest_taxonomy_with_connect(zip_path: Path, namespace: str, year: int) -> int:
    """Same as ``ingest_taxonomy`` but opens and closes a connection internally."""
    with connect() as conn:
        return ingest_taxonomy(zip_path, namespace, year, conn)


def _default_spec_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "spec"


def _split_concept_id(concept_id: str) -> tuple[str, str] | None:
    if "/" in concept_id:
        namespace, local_name = concept_id.split("/", 1)
    elif ":" in concept_id:
        namespace, local_name = concept_id.split(":", 1)
    else:
        return None
    if not namespace or not local_name:
        return None
    return namespace, local_name


def _all_years_row(concept_id: str, meta: dict[str, Any], year: int) -> dict[str, Any] | None:
    split = _split_concept_id(concept_id)
    if split is None:
        return None
    namespace, local_name = split
    label = meta.get("label_en") or meta.get("label") or meta.get("name")
    return {
        "namespace": namespace,
        "local_name": local_name,
        "taxonomy_year": year,
        "label": label,
        "label_terse": meta.get("label_terse"),
        "label_verbose": meta.get("label_verbose") or label,
        "documentation": meta.get("description") or meta.get("documentation"),
        "period_type": meta.get("period_type"),
        "balance_type": meta.get("balance_type"),
        "data_type": meta.get("data_type"),
        "is_abstract": bool(meta.get("is_abstract", False)),
        "is_deprecated": bool(meta.get("is_deprecated", False)),
        "authoritative_refs": psycopg2.extras.Json(meta.get("references")) if meta.get("references") is not None else None,
        "parent_concept": meta.get("parent_concept") or meta.get("parent_id"),
        "statement_type": meta.get("statement_type") or _classify_statement_type(local_name),
        "sector_scope": meta.get("sector_scope"),
    }


def ingest_all_years_file(
    path: Path,
    conn: psycopg2.extensions.connection,
    batch_size: int = 5000,
) -> int:
    """Load one ``*_all_years.json`` taxonomy evidence file.

    The spec files store first/last taxonomy years per concept. The DB table is
    versioned by year, so this expands each concept across its active year
    range. The operation is idempotent through the table's unique key.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object at top level in {path}")

    total = 0
    batch: list[dict[str, Any]] = []
    for concept_id, meta in payload.items():
        if not isinstance(meta, dict):
            continue
        first_year = int(meta.get("first_year") or meta.get("year") or 1900)
        last_year = int(meta.get("last_year") or first_year)
        if last_year < first_year:
            first_year, last_year = last_year, first_year
        for year in range(first_year, last_year + 1):
            row = _all_years_row(concept_id, meta, year)
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= batch_size:
                total += upsert_taxonomy_elements(conn, batch)
                batch.clear()
    if batch:
        total += upsert_taxonomy_elements(conn, batch)
    return total


def ingest_all_years_specs(
    spec_dir: Path | None = None,
    files: list[str] | None = None,
) -> dict[str, int]:
    """Load US-GAAP, IFRS, and JP-GAAP ``*_all_years`` evidence into DB."""
    spec_dir = spec_dir or _default_spec_dir()
    files = files or [
        "us_gaap_all_years.json",
        "ifrs_all_years.json",
        "jp_gaap_all_years.json",
    ]
    out: dict[str, int] = {}
    with connect() as conn:
        for filename in files:
            path = spec_dir / filename
            if not path.exists():
                out[filename] = 0
                continue
            out[filename] = ingest_all_years_file(path, conn)
    return out


def apply_taxonomy_evidence_to_observations(jurisdiction: str | None = None) -> int:
    """Backfill concept-universe labels/descriptions from ref_taxonomy_element.

    Existing filing-derived labels/descriptions are preserved. The lookup uses
    the latest taxonomy element not later than the observation fiscal year.
    """
    params: list[Any] = []
    where = ""
    if jurisdiction:
        where = "WHERE o.jurisdiction = %s"
        params.append(jurisdiction)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH matched AS (
                SELECT o.observation_id,
                       COALESCE(t.label, t.label_verbose, t.label_terse) AS label_en,
                       t.documentation
                FROM ref_concept_universe_observation o
                JOIN LATERAL (
                    SELECT label, label_verbose, label_terse, documentation
                    FROM ref_taxonomy_element t
                    WHERE t.concept_id = o.concept_id
                      AND t.taxonomy_year <= COALESCE(o.fiscal_year, 9999)
                    ORDER BY t.taxonomy_year DESC
                    LIMIT 1
                ) t ON true
                {where}
            )
            UPDATE ref_concept_universe_observation o
               SET label_en = COALESCE(NULLIF(o.label_en, ''), matched.label_en),
                   description = COALESCE(NULLIF(o.description, ''), matched.documentation),
                   updated_at = now()
              FROM matched
             WHERE o.observation_id = matched.observation_id
               AND (
                    (NULLIF(o.label_en, '') IS NULL AND matched.label_en IS NOT NULL)
                 OR (NULLIF(o.description, '') IS NULL AND matched.documentation IS NOT NULL)
               )
            """,
            params,
        )
        return cur.rowcount


def ingest_all_years_with_connect(spec_dir: Path | None = None, enrich_observations: bool = True) -> dict[str, int]:
    """Load all ``*_all_years`` specs and optionally backfill observations."""
    out = ingest_all_years_specs(spec_dir=spec_dir)
    if enrich_observations:
        out["observation_backfill"] = apply_taxonomy_evidence_to_observations()
    return out
