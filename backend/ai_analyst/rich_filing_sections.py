"""Rich iXBRL TextBlock disclosures for committee evidence.

This is an evidence sidecar, not a valuation input layer. It extracts high-value
tables and narrative from local SEC XBRL HTML filings, caches the result, and
returns a compact packet for the investment committee prompts.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

import pandas as pd

from . import services
from ._db import read_sql
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.mda.settings import html_dir_from_env
from xbrl_sec.sec.mda.text import clean_html_to_text, soup_from_html


EXTRACTION_VERSION = "rich-xbrl-textblock-v1"
MIN_SECTION_SCORE = 25.0
MAX_TEXT_CHARS = 20000
MAX_EXCERPT_CHARS = 900
MAX_TABLES_PER_SECTION = 4
MAX_TABLE_ROWS = 30
MAX_TABLE_COLS = 12
DEFAULT_MAX_SECTIONS = 16

_FORMS_ANNUAL = ("10-K", "10-K/A")
_FORMS_QUARTERLY = ("10-Q", "10-Q/A")

_POLICY_NOISE = re.compile(
    r"(accountingpolic|basisofpresentation|summaryofsignificantaccountingpolic|"
    r"organizationconsolidation|subsequentevents)",
    re.I,
)

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS fact_rich_filing_sections_us (
    rich_section_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cik                   TEXT NOT NULL,
    ticker                TEXT,
    accession_no          TEXT NOT NULL,
    filing_id             TEXT NOT NULL,
    form_type             TEXT,
    filing_date           DATE,
    fiscal_year           INTEGER,
    fiscal_period         TEXT,
    concept_name          TEXT NOT NULL,
    section_family        TEXT NOT NULL,
    sector_scope          TEXT,
    section_title         TEXT,
    plain_text            TEXT,
    tables_jsonb          JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics_preview_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_html_path      TEXT,
    source_anchor         TEXT,
    text_hash             TEXT NOT NULL,
    quality_score         NUMERIC(8,2) NOT NULL DEFAULT 0,
    extraction_version    TEXT NOT NULL DEFAULT 'rich-xbrl-textblock-v1',
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cik, accession_no, concept_name, text_hash, extraction_version)
);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_cik_date
    ON fact_rich_filing_sections_us(cik, filing_date DESC, quality_score DESC);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_ticker_date
    ON fact_rich_filing_sections_us(UPPER(ticker), filing_date DESC, quality_score DESC);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_family
    ON fact_rich_filing_sections_us(section_family, sector_scope, quality_score DESC);
"""


def fetch_rich_filing_sections(
    ticker: str,
    years: list[int] | None = None,
    *,
    limit_filings: int = 2,
    max_sections: int = DEFAULT_MAX_SECTIONS,
) -> dict[str, Any]:
    """Fetch cache-first rich XBRL sections for a US ticker.

    The returned packet is deliberately advisory. It gives the committee richer
    filing context and evidence IDs; it does not feed canonical valuation math.
    """
    ticker = str(ticker or "").upper().strip()
    warnings: list[str] = []
    if not ticker:
        return _empty_packet(ticker=ticker, warnings=["ticker is required"])

    profile = _company_profile(ticker)
    if not profile.get("found"):
        return _empty_packet(ticker=ticker, warnings=[f"company profile missing for {ticker}"])
    if str(profile.get("jurisdiction") or "").upper() != "US":
        return _empty_packet(ticker=ticker, warnings=["rich filing sections are US-only in v1"])

    cik = str(profile.get("cik") or "").zfill(10)
    if not cik or cik == "0000000000":
        return _empty_packet(ticker=ticker, warnings=["US CIK missing"])

    filings = _target_filings(cik, years=years, limit_filings=limit_filings)
    if not filings:
        filings = _local_file_filing_fallback(cik, ticker, limit_filings=limit_filings)
        if filings:
            warnings.append("source_filing_state unavailable; inferred filings from local XBRL HTML files")
    if not filings:
        return _empty_packet(ticker=ticker, cik=cik, warnings=["no annual or quarterly XBRL HTML filings found"])

    filing_ids = [str(row.get("filing_id") or row.get("accession_no") or "") for row in filings if row.get("filing_id") or row.get("accession_no")]
    cached = _read_cached_sections(cik, filing_ids)
    cached_filing_ids = {str(row.get("filing_id") or row.get("accession_no") or "") for row in cached}

    parsed: list[dict[str, Any]] = []
    html_root = html_dir_from_env()
    for rank, filing in enumerate(filings):
        filing_id = str(filing.get("filing_id") or filing.get("accession_no") or "")
        if not filing_id or filing_id in cached_filing_ids:
            continue
        path = _html_path(html_root, cik, filing_id)
        if not path.exists():
            warnings.append(f"local XBRL HTML missing for {filing_id}")
            continue
        html = _read_html_file(path)
        if not html:
            warnings.append(f"could not read local XBRL HTML for {filing_id}")
            continue
        filing_meta = {
            **filing,
            "ticker": ticker,
            "cik": cik,
            "accession_no": filing_id,
            "filing_id": filing_id,
            "source_html_path": str(path),
            "filing_rank": rank,
        }
        parsed.extend(extract_rich_sections_from_html(html, filing_meta, profile, max_sections=32))

    if parsed:
        try:
            _persist_sections(parsed)
        except Exception as exc:  # noqa: BLE001 - evidence must degrade gracefully
            warnings.append(f"rich filing cache persist failed: {exc.__class__.__name__}: {str(exc)[:160]}")

    sections = _rank_sections([*cached, *parsed], max_sections=max_sections)
    packet = {
        "available": bool(sections),
        "ticker": ticker,
        "cik": cik,
        "source": "sec.fact_rich_filing_sections_us + local XBRL HTML fallback",
        "extraction_version": EXTRACTION_VERSION,
        "filings": [_compact_filing(row) for row in filings],
        "sections": sections,
        "warnings": list(dict.fromkeys(warnings)),
    }
    packet["compact"] = compact_rich_filing_sections(packet)
    return packet


def extract_rich_sections_from_html(
    html: str,
    filing: dict[str, Any],
    company: dict[str, Any] | None = None,
    *,
    max_sections: int = DEFAULT_MAX_SECTIONS,
) -> list[dict[str, Any]]:
    """Extract ranked rich TextBlock sections from one inline-XBRL HTML document."""
    if not html:
        return []
    company = company or {}
    sector_scope = str(company.get("sector_scope") or services.sector_scope_from_company(company) or "corp")
    soup = soup_from_html(html)
    sections: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        concept_name = str(tag.get("name") or "").strip()
        if not concept_name:
            continue
        local = concept_name.split(":")[-1]
        if not local.lower().endswith("textblock"):
            continue

        raw_html = tag.decode_contents() or tag.get_text(" ")
        plain_text = clean_html_to_text(raw_html)
        if len(plain_text) < 120:
            continue

        tables = _extract_tables(raw_html)
        family, family_score, inferred_scope, tags = _classify_section(local, sector_scope, company)
        table_count = len(tables)
        score = _quality_score(
            family_score=family_score,
            local_name=local,
            plain_text=plain_text,
            table_count=table_count,
            filing=filing,
        )
        if score < MIN_SECTION_SCORE:
            continue

        normalized_text = _clean_text(plain_text)
        text_hash = hashlib.sha1(normalized_text.encode("utf-8")).hexdigest()
        title = _section_title(local, normalized_text)
        metrics_preview = _metrics_preview(tables)
        summary = _section_summary(filing, family, title, normalized_text, table_count)
        source_anchor = str(tag.get("id") or tag.get("contextref") or concept_name)
        sections.append(
            {
                "cik": str(filing.get("cik") or "").zfill(10),
                "ticker": str(filing.get("ticker") or company.get("ticker") or "").upper() or None,
                "accession_no": str(filing.get("accession_no") or filing.get("filing_id") or ""),
                "filing_id": str(filing.get("filing_id") or filing.get("accession_no") or ""),
                "form_type": _clean_text(filing.get("form_type") or filing.get("filing_type") or ""),
                "filing_date": _date_text(filing.get("filing_date") or filing.get("filed_date")),
                "fiscal_year": _int_or_none(filing.get("fiscal_year")),
                "fiscal_period": _clean_text(filing.get("fiscal_period") or _period_from_form(filing.get("form_type") or filing.get("filing_type"))),
                "concept_name": concept_name,
                "section_family": family,
                "sector_scope": inferred_scope or sector_scope,
                "section_title": title,
                "summary": summary,
                "excerpt": _truncate(normalized_text, MAX_EXCERPT_CHARS),
                "plain_text": normalized_text[:MAX_TEXT_CHARS],
                "tables_jsonb": tables,
                "metrics_preview_jsonb": metrics_preview,
                "table_count": table_count,
                "source_html_path": str(filing.get("source_html_path") or ""),
                "source_anchor": source_anchor,
                "text_hash": text_hash,
                "quality_score": score,
                "extraction_version": EXTRACTION_VERSION,
                "tags": list(dict.fromkeys([*tags, "xbrl-html", "textblock"])),
            }
        )
    return _rank_sections(sections, max_sections=max_sections)


def compact_rich_filing_sections(packet: dict[str, Any] | list[dict[str, Any]] | None, *, max_sections: int = 12) -> dict[str, Any]:
    """Small prompt-ready view of rich sections."""
    if isinstance(packet, list):
        sections = packet
        warnings: list[str] = []
    elif isinstance(packet, dict):
        sections = packet.get("sections") or []
        warnings = list(packet.get("warnings") or [])
    else:
        sections = []
        warnings = []
    compact_sections = []
    for section in sections[:max_sections]:
        compact_sections.append(
            {
                "family": section.get("section_family"),
                "sector_scope": section.get("sector_scope"),
                "title": section.get("section_title"),
                "form_type": section.get("form_type"),
                "filing_date": section.get("filing_date"),
                "concept_name": section.get("concept_name"),
                "quality_score": section.get("quality_score"),
                "table_count": section.get("table_count") or len(section.get("tables_jsonb") or []),
                "summary": _truncate(section.get("summary") or section.get("excerpt") or section.get("plain_text") or "", 320),
                "metrics_preview": section.get("metrics_preview_jsonb") or {},
            }
        )
    return {
        "available": bool(compact_sections),
        "sections": compact_sections,
        "warnings": warnings,
        "truncated": len(sections) > max_sections,
    }


def _company_profile(ticker: str) -> dict[str, Any]:
    df = read_sql(
        """
        SELECT jurisdiction, uid, cik, edinet_code, ticker, name, exchange, country_code,
               gics_sector_name, gics_industry_group_name, gics_industry_name,
               gics_sub_industry_name, mapping_sector
        FROM v_dim_company
        WHERE UPPER(ticker) = UPPER(%(ticker)s)
        LIMIT 1
        """,
        {"ticker": ticker},
    )
    rows = _records(df)
    if not rows:
        return {"ticker": ticker, "found": False}
    row = rows[0]
    row["found"] = True
    row["sector_scope"] = services.sector_scope_from_company(row)
    return row


def _target_filings(cik: str, years: list[int] | None, limit_filings: int) -> list[dict[str, Any]]:
    if limit_filings <= 0:
        return []
    # Always prefer the latest annual plus latest quarterly disclosure. The
    # committee's target_years are valuation-history years and should not exclude
    # a fresh 10-Q filed after the latest fiscal year in the numeric packet.
    annual = _filing_rows(cik, _FORMS_ANNUAL, None, limit=1)
    quarterly = _filing_rows(cik, _FORMS_QUARTERLY, None, limit=1)
    if years and not annual:
        annual = _filing_rows(cik, _FORMS_ANNUAL, years, limit=1)
    if years and not quarterly:
        quarterly = _filing_rows(cik, _FORMS_QUARTERLY, years, limit=1)
    rows = [*quarterly, *annual]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: str(r.get("filed_date") or r.get("period_end") or ""), reverse=True):
        filing_id = str(row.get("filing_id") or "")
        if filing_id and filing_id not in seen:
            seen.add(filing_id)
            out.append(row)
    return out[: max(1, limit_filings)]


def _filing_rows(cik: str, forms: tuple[str, ...], years: list[int] | None, *, limit: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"cik": str(cik).zfill(10), "forms": list(forms), "limit": int(limit)}
    year_filter = ""
    if years:
        params["years"] = [int(y) for y in years]
        year_filter = "AND EXTRACT(YEAR FROM COALESCE(period_end, filed_date))::int = ANY(%(years)s)"
    try:
        df = read_sql(
            f"""
            SELECT filing_id,
                   filing_id AS accession_no,
                   filing_type AS form_type,
                   filed_date,
                   filed_date AS filing_date,
                   period_end,
                   EXTRACT(YEAR FROM period_end)::int AS fiscal_year,
                   CASE WHEN filing_type IN ('10-K','10-K/A') THEN 'FY' ELSE 'Q' END AS fiscal_period
            FROM source_filing_state
            WHERE jurisdiction = 'US'
              AND entity_id = %(cik)s
              AND filing_type = ANY(%(forms)s)
              AND filing_id IS NOT NULL
              {year_filter}
            ORDER BY filed_date DESC NULLS LAST, period_end DESC NULLS LAST, filing_id DESC
            LIMIT %(limit)s
            """,
            params,
        )
    except Exception:
        return []
    return _records(df)


def _local_file_filing_fallback(cik: str, ticker: str, *, limit_filings: int) -> list[dict[str, Any]]:
    root = html_dir_from_env()
    if not root.exists():
        return []
    paths = sorted(root.glob(f"CIK{str(cik).zfill(10)}_*.htm"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in paths[: max(1, limit_filings)]:
        filing_id = path.stem.split("_", 1)[-1]
        rows.append(
            {
                "filing_id": filing_id,
                "accession_no": filing_id,
                "form_type": "",
                "filing_date": None,
                "filed_date": None,
                "period_end": None,
                "fiscal_year": None,
                "fiscal_period": "",
                "ticker": ticker,
                "cik": cik,
            }
        )
    return rows


def _read_cached_sections(cik: str, filing_ids: list[str]) -> list[dict[str, Any]]:
    if not filing_ids or not _relation_exists("sec.fact_rich_filing_sections_us"):
        return []
    cik10 = str(cik).zfill(10)
    cik_raw = cik10.lstrip("0") or cik10
    try:
        df = read_sql(
            """
            SELECT cik, ticker, accession_no, filing_id, form_type, filing_date,
                   fiscal_year, fiscal_period, concept_name, section_family, sector_scope,
                   section_title, plain_text, tables_jsonb, metrics_preview_jsonb,
                   source_html_path, source_anchor, text_hash, quality_score,
                   extraction_version
            FROM sec.fact_rich_filing_sections_us
            WHERE cik IN (%(cik)s, %(cik_raw)s)
              AND accession_no = ANY(%(filing_ids)s)
              AND extraction_version = %(version)s
            ORDER BY quality_score DESC, filing_date DESC NULLS LAST
            """,
            {"cik": cik10, "cik_raw": cik_raw, "filing_ids": filing_ids, "version": EXTRACTION_VERSION},
        )
    except Exception:
        return []
    rows = []
    for row in _records(df):
        row["filing_date"] = _date_text(row.get("filing_date"))
        row["quality_score"] = _float_or_none(row.get("quality_score")) or 0.0
        row["tables_jsonb"] = row.get("tables_jsonb") or []
        row["metrics_preview_jsonb"] = row.get("metrics_preview_jsonb") or {}
        row["table_count"] = len(row.get("tables_jsonb") or [])
        row["summary"] = _section_summary(row, row.get("section_family"), row.get("section_title"), row.get("plain_text") or "", row["table_count"])
        row["excerpt"] = _truncate(row.get("plain_text") or "", MAX_EXCERPT_CHARS)
        row["tags"] = [row.get("section_family"), row.get("sector_scope"), "xbrl-html", "textblock"]
        rows.append(row)
    return rows


def _persist_sections(sections: list[dict[str, Any]]) -> int:
    if not sections:
        return 0
    from psycopg2.extras import Json

    rows = []
    for section in sections:
        rows.append(
            (
                section.get("cik"),
                section.get("ticker"),
                section.get("accession_no"),
                section.get("filing_id") or section.get("accession_no"),
                section.get("form_type"),
                section.get("filing_date") or None,
                section.get("fiscal_year"),
                section.get("fiscal_period"),
                section.get("concept_name"),
                section.get("section_family"),
                section.get("sector_scope"),
                section.get("section_title"),
                section.get("plain_text"),
                Json(section.get("tables_jsonb") or []),
                Json(section.get("metrics_preview_jsonb") or {}),
                section.get("source_html_path"),
                section.get("source_anchor"),
                section.get("text_hash"),
                section.get("quality_score") or 0,
                section.get("extraction_version") or EXTRACTION_VERSION,
            )
        )
    with connect() as conn, conn.cursor() as cur:
        _ensure_cache_table(cur)
        return execute_values(
            cur,
            """
            INSERT INTO fact_rich_filing_sections_us
                (cik, ticker, accession_no, filing_id, form_type, filing_date,
                 fiscal_year, fiscal_period, concept_name, section_family, sector_scope,
                 section_title, plain_text, tables_jsonb, metrics_preview_jsonb,
                 source_html_path, source_anchor, text_hash, quality_score,
                 extraction_version)
            VALUES %s
            ON CONFLICT (cik, accession_no, concept_name, text_hash, extraction_version)
            DO UPDATE SET
                ticker = EXCLUDED.ticker,
                filing_id = EXCLUDED.filing_id,
                form_type = EXCLUDED.form_type,
                filing_date = EXCLUDED.filing_date,
                fiscal_year = EXCLUDED.fiscal_year,
                fiscal_period = EXCLUDED.fiscal_period,
                section_family = EXCLUDED.section_family,
                sector_scope = EXCLUDED.sector_scope,
                section_title = EXCLUDED.section_title,
                plain_text = EXCLUDED.plain_text,
                tables_jsonb = EXCLUDED.tables_jsonb,
                metrics_preview_jsonb = EXCLUDED.metrics_preview_jsonb,
                source_html_path = EXCLUDED.source_html_path,
                source_anchor = EXCLUDED.source_anchor,
                quality_score = EXCLUDED.quality_score,
                extracted_at = now()
            """,
            rows,
            page_size=1000,
        )


def _ensure_cache_table(cur: Any) -> None:
    cur.execute(_CACHE_DDL)


def _relation_exists(name: str) -> bool:
    try:
        df = read_sql("SELECT to_regclass(%(name)s)::text AS relation_name", {"name": name})
    except Exception:
        return False
    rows = _records(df)
    return bool(rows and rows[0].get("relation_name"))


def _extract_tables(inner_html: str) -> list[dict[str, Any]]:
    try:
        tables = pd.read_html(io.StringIO(inner_html))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for table in tables[:8]:
        parsed = _table_to_json(table)
        if parsed:
            out.append(parsed)
        if len(out) >= MAX_TABLES_PER_SECTION:
            break
    return out


def _table_to_json(table: pd.DataFrame) -> dict[str, Any] | None:
    if table is None or table.empty:
        return None
    table = table.dropna(how="all").dropna(axis=1, how="all")
    if table.empty or table.shape[1] < 2:
        return None
    original_rows, original_cols = int(table.shape[0]), int(table.shape[1])
    columns = [_column_label(col, i) for i, col in enumerate(table.columns[:MAX_TABLE_COLS])]
    rows: list[dict[str, str]] = []
    for _, row in table.iloc[:MAX_TABLE_ROWS, :MAX_TABLE_COLS].iterrows():
        item: dict[str, str] = {}
        for i, col in enumerate(table.columns[:MAX_TABLE_COLS]):
            value = _clean_cell(row.get(col))
            if value:
                item[columns[i]] = value
        if item:
            rows.append(item)
    if not rows:
        return None
    return {
        "columns": columns,
        "rows": rows,
        "row_count": original_rows,
        "col_count": original_cols,
    }


def _classify_section(local_name: str, sector_scope: str, company: dict[str, Any]) -> tuple[str, float, str | None, list[str]]:
    key = _squash(local_name)
    scope = str(sector_scope or company.get("mapping_sector") or "corp")
    gics_blob = _squash(" ".join(str(company.get(k) or "") for k in (
        "gics_sector_name", "gics_industry_group_name", "gics_industry_name", "gics_sub_industry_name"
    )))
    tags: list[str] = []

    if "segmentreporting" in key or "reportablesegment" in key or ("segment" in key and "reporting" in key):
        return "segment_reporting", 52.0, scope, ["segment"]
    if "disaggregationofrevenue" in key or "revenuefromcontract" in key:
        return "revenue_disaggregation", 48.0, scope, ["revenue"]
    if (
        ("revenue" in key or "externalcustomers" in key or "sales" in key)
        and any(term in key for term in ("geographic", "foreigncountries", "productsandservices", "productorservice", "customer"))
    ):
        return "geography_product_revenue", 44.0, scope, ["revenue", "mix"]
    if any(term in key for term in ("derivative", "hedg", "fairvalue", "marketrisk", "interestrate", "foreigncurrency")):
        return "market_risk_derivatives", 34.0, scope, ["market-risk"]
    if any(term in key for term in ("allowanceforcreditloss", "creditloss", "financingreceivable", "loan", "deposit")):
        inferred = "bank_financial" if "bank" in scope or "bank" in gics_blob else scope
        return "credit_loss_financing", 40.0, inferred, ["credit", "financing"]
    if any(term in key for term in ("debt", "borrowings", "creditfacility", "maturit", "notespayable", "liquidity")):
        return "debt_liquidity", 32.0, scope, ["debt", "liquidity"]
    if "lease" in key:
        return "leases", 28.0, scope, ["leases"]

    if scope == "insurance" or "insurance" in gics_blob:
        if any(term in key for term in ("premium", "claim", "lossreserve", "unpaidloss", "reinsurance", "underwriting", "policyholder")):
            return "industry_specific", 46.0, "insurance", ["insurance"]
    if scope == "reit" or "realestate" in gics_blob:
        if any(term in key for term in ("realestate", "rental", "lease", "tenant", "property", "noi", "occupancy")):
            return "industry_specific", 42.0, "reit", ["real-estate"]
    if "energy" in gics_blob or any(term in gics_blob for term in ("oilgas", "oilandgas")):
        if any(term in key for term in ("oilgas", "provedreserve", "reserve", "drilling", "production", "exploration")):
            return "industry_specific", 42.0, "energy", ["energy"]
    if "utilities" in gics_blob:
        if any(term in key for term in ("regulated", "electric", "rate", "power", "fuel", "derivative")):
            return "industry_specific", 38.0, "utility", ["utility"]

    if any(term in key for term in ("repurchase", "sharebased", "stockcompensation", "incometaxes", "tax")):
        return "other_supplemental", 18.0, scope, ["supplemental"]
    return "other_supplemental", 0.0, scope, []


def _quality_score(
    *,
    family_score: float,
    local_name: str,
    plain_text: str,
    table_count: int,
    filing: dict[str, Any],
) -> float:
    if family_score <= 0 and table_count <= 0:
        return 0.0
    score = float(family_score)
    score += min(table_count, 5) * 6.0
    score += min(len(plain_text) / 4000.0, 2.0) * 4.0
    form = str(filing.get("form_type") or filing.get("filing_type") or "").upper()
    rank = _int_or_none(filing.get("filing_rank")) or 0
    score += max(0.0, 9.0 - rank * 3.0)
    if "10-Q" in form:
        score += 6.0
    elif "10-K" in form:
        score += 4.0
    if _POLICY_NOISE.search(local_name) and family_score < 45.0:
        score -= 18.0
    if table_count == 0 and family_score < 40.0:
        score -= 8.0
    return round(max(score, 0.0), 2)


def _rank_sections(sections: list[dict[str, Any]], *, max_sections: int) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for section in sections:
        key = (str(section.get("accession_no") or section.get("filing_id") or ""), str(section.get("text_hash") or ""))
        if key not in best or float(section.get("quality_score") or 0) > float(best[key].get("quality_score") or 0):
            best[key] = _normalize_section(section)
    ranked = sorted(
        best.values(),
        key=lambda row: (
            float(row.get("quality_score") or 0),
            str(row.get("filing_date") or ""),
            str(row.get("section_family") or ""),
        ),
        reverse=True,
    )
    return ranked[:max(1, max_sections)]


def _normalize_section(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["filing_date"] = _date_text(out.get("filing_date"))
    out["fiscal_year"] = _int_or_none(out.get("fiscal_year"))
    out["quality_score"] = _float_or_none(out.get("quality_score")) or 0.0
    out["tables_jsonb"] = out.get("tables_jsonb") or []
    out["metrics_preview_jsonb"] = out.get("metrics_preview_jsonb") or {}
    out["table_count"] = out.get("table_count") or len(out.get("tables_jsonb") or [])
    out["plain_text"] = _clean_text(out.get("plain_text") or "")
    out["excerpt"] = out.get("excerpt") or _truncate(out.get("plain_text") or "", MAX_EXCERPT_CHARS)
    out["summary"] = out.get("summary") or _section_summary(out, out.get("section_family"), out.get("section_title"), out.get("plain_text") or "", out["table_count"])
    out["tags"] = [tag for tag in (out.get("tags") or []) if tag]
    return out


def _metrics_preview(tables: list[dict[str, Any]]) -> dict[str, Any]:
    sample_rows = []
    for table in tables[:2]:
        for row in (table.get("rows") or [])[:4]:
            if any(_looks_numeric(value) for value in row.values()):
                sample_rows.append(row)
            if len(sample_rows) >= 6:
                break
        if len(sample_rows) >= 6:
            break
    return {
        "table_count": len(tables),
        "sample_rows": sample_rows,
    }


def _section_summary(filing: dict[str, Any], family: Any, title: Any, plain_text: str, table_count: int) -> str:
    form = _clean_text(filing.get("form_type") or filing.get("filing_type") or "")
    date = _date_text(filing.get("filing_date") or filing.get("filed_date"))
    prefix = " ".join(part for part in [form, date, str(family or "").replace("_", " ")] if part)
    table_note = f"{table_count} embedded table(s). " if table_count else ""
    return _truncate(f"{prefix}: {title}. {table_note}{plain_text}", 360)


def _section_title(local_name: str, plain_text: str) -> str:
    candidate = re.sub(r"TextBlock$", "", local_name)
    candidate = re.sub(r"Disclosure$", "", candidate)
    candidate = re.sub(r"ScheduleOf", "", candidate)
    candidate = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", candidate).strip()
    first_line = next((line.strip() for line in re.split(r"[\r\n]+", plain_text) if 4 <= len(line.strip()) <= 120), "")
    if first_line and not re.search(r"\d{4}|\$|percent|million|table", first_line, re.I):
        return first_line[:140]
    return candidate[:140] or local_name[:140]


def _html_path(root: Path, cik: str, filing_id: str) -> Path:
    return root / f"CIK{str(cik).zfill(10)}_{filing_id}.htm"


def _read_html_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _compact_filing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "filing_id": row.get("filing_id") or row.get("accession_no"),
        "accession_no": row.get("accession_no") or row.get("filing_id"),
        "form_type": row.get("form_type") or row.get("filing_type"),
        "filing_date": _date_text(row.get("filing_date") or row.get("filed_date")),
        "period_end": _date_text(row.get("period_end")),
        "fiscal_year": _int_or_none(row.get("fiscal_year")),
        "fiscal_period": row.get("fiscal_period"),
    }


def _empty_packet(ticker: str, cik: str | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    packet = {
        "available": False,
        "ticker": ticker,
        "cik": cik,
        "source": "sec.fact_rich_filing_sections_us + local XBRL HTML fallback",
        "extraction_version": EXTRACTION_VERSION,
        "filings": [],
        "sections": [],
        "warnings": warnings or [],
    }
    packet["compact"] = compact_rich_filing_sections(packet)
    return packet


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "empty") and df.empty:
        return []
    if hasattr(df, "to_dict"):
        rows = df.to_dict(orient="records")
    elif isinstance(df, list):
        rows = df
    else:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in dict(row).items():
            if value is None:
                clean[key] = None
            elif isinstance(value, float) and pd.isna(value):
                clean[key] = None
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            else:
                clean[key] = value
        out.append(clean)
    return out


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return _clean_text(value)[:240]


def _column_label(column: Any, index: int) -> str:
    if isinstance(column, tuple):
        label = " ".join(_clean_text(part) for part in column if _clean_text(part) and not str(part).startswith("Unnamed"))
    else:
        label = _clean_text(column)
    if not label or label.lower().startswith("unnamed"):
        label = f"Column {index + 1}"
    return label[:120]


def _period_from_form(form: Any) -> str:
    form_s = str(form or "").upper()
    if "10-K" in form_s:
        return "FY"
    if "10-Q" in form_s:
        return "Q"
    return ""


def _date_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text[:10]


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_numeric(value: Any) -> bool:
    return bool(re.search(r"\(?-?\$?\d[\d,]*(?:\.\d+)?%?\)?", str(value or "")))


def _squash(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _truncate(value: Any, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
