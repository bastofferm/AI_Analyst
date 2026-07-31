from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.mda.common import extraction_quality, file_sha256
from xbrl_sec.sec.mda.local_reader import read_html
from xbrl_sec.sec.mda.settings import lookback_years_from_env
from xbrl_sec.sec.mda.text import clean_html_to_text
from xbrl_sec.sec.state.store import finish_run, start_run


_JP_DOC_TYPES = ("120", "130", "140")


@dataclass(frozen=True)
class JPFiling:
    edinet_code: str
    filing_id: str
    doc_type_code: str
    filed_date: date
    period_end: date | None
    html_path: Path


def _doc_prefix(filing_id: str) -> str:
    return filing_id.split("_", 1)[0]


def _business_status_html(source_path: str | None, filing_id: str) -> Path | None:
    if not source_path:
        return None
    source = Path(source_path)
    if not source.exists():
        return None
    parent = source.parent
    prefix = _doc_prefix(filing_id)
    candidates = sorted(parent.glob(f"{prefix}_0102010_honbun*.htm")) + sorted(parent.glob(f"{prefix}_0102010_honbun*.html"))
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_size)
    return None


def discover(edinet_code: str | None = None, years: int | None = None) -> dict[str, int]:
    years = years or lookback_years_from_env()
    params: list = [list(_JP_DOC_TYPES), years]
    entity_filter = ""
    if edinet_code:
        entity_filter = "AND s.entity_id = %s"
        params.append(edinet_code)
    ctx = start_run(
        "JP",
        "mda_discover",
        "incremental",
        scope=json.dumps({"edinet_code": edinet_code, "years": years}),
    )
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.entity_id, s.filing_id, s.filing_type, s.filed_date, s.source_path
                FROM source_filing_state s
                JOIN dim_company_jp d ON d.edinet_code = s.entity_id
                WHERE s.jurisdiction = 'JP'
                  AND COALESCE(d.include_in_pipeline, true)
                  AND s.source_kind = 'instance'
                  AND s.filing_type = ANY(%s)
                  AND s.filed_date >= CURRENT_DATE - (%s * INTERVAL '1 year')
                  {entity_filter}
                ORDER BY s.entity_id, s.filed_date DESC, s.filing_id
                """,
                params,
            )
            rows = []
            available = missing_html = missing_zip = 0
            for entity_id, filing_id, doc_type, filed_date, source_path in cur.fetchall():
                path = _business_status_html(source_path, filing_id)
                if path:
                    status = "available"
                    available += 1
                    html_path = str(path)
                    size = path.stat().st_size
                elif not source_path or not Path(source_path).exists():
                    status = "missing_zip"
                    missing_zip += 1
                    html_path = None
                    size = None
                else:
                    status = "missing_html"
                    missing_html += 1
                    html_path = None
                    size = None
                rows.append((entity_id, filing_id, doc_type, filed_date, status, html_path, source_path, file_sha256(html_path), size))
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO source_mda_state_jp
                        (edinet_code, filing_id, doc_type_code, filed_date, availability_status,
                         html_path, source_package_path, source_html_sha256, html_size_bytes)
                    VALUES %s
                    ON CONFLICT (edinet_code, filing_id) DO UPDATE SET
                        doc_type_code = EXCLUDED.doc_type_code,
                        filed_date = EXCLUDED.filed_date,
                        availability_status = EXCLUDED.availability_status,
                        html_path = EXCLUDED.html_path,
                        source_package_path = EXCLUDED.source_package_path,
                        source_html_sha256 = EXCLUDED.source_html_sha256,
                        html_size_bytes = EXCLUDED.html_size_bytes,
                        updated_at = now()
                    """,
                    rows,
                    page_size=5000,
                )
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=available)
        return {"discovered": len(rows), "available": available, "missing_html": missing_html, "missing_zip": missing_zip}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def _candidate_filings(edinet_code: str | None = None, limit: int | None = None, retry_dirty: bool = False) -> list[JPFiling]:
    params: list = []
    entity_filter = ""
    if edinet_code:
        entity_filter = "AND m.edinet_code = %s"
        params.append(edinet_code)
    success_filter = "" if retry_dirty else "AND NOT m.extraction_succeeded"
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT %s"
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT m.edinet_code, m.filing_id, m.doc_type_code, m.filed_date, s.period_end, m.html_path
            FROM source_mda_state_jp m
            JOIN source_filing_state s
              ON s.jurisdiction = 'JP'
             AND s.entity_id = m.edinet_code
             AND s.filing_id = m.filing_id
            WHERE m.availability_status = 'available'
              AND m.html_path IS NOT NULL
              {success_filter}
              {entity_filter}
            ORDER BY m.edinet_code, m.filed_date DESC, m.filing_id
            {limit_clause}
            """,
            params,
        )
        return [
            JPFiling(entity_id, filing_id, doc_type, filed_date, period_end, Path(html_path))
            for entity_id, filing_id, doc_type, filed_date, period_end, html_path in cur.fetchall()
        ]


def _threshold(doc_type_code: str) -> int:
    return 1000 if doc_type_code == "120" else 300


def extract(edinet_code: str | None = None, limit: int | None = None, retry_dirty: bool = False) -> dict[str, int]:
    filings = _candidate_filings(edinet_code=edinet_code, limit=limit, retry_dirty=retry_dirty)
    ctx = start_run(
        "JP",
        "mda_extract",
        "incremental",
        scope=json.dumps({"edinet_code": edinet_code, "limit": limit}),
    )
    section_rows = []
    state_rows = []
    attempted = succeeded = dirty = failed = 0
    try:
        for filing in filings:
            attempted += 1
            try:
                text = clean_html_to_text(read_html(filing.html_path))
                if not text:
                    failed += 1
                    state_rows.append((filing.edinet_code, filing.filing_id, True, False, "empty_after_cleaning"))
                    continue
                quality, quality_error = extraction_quality(len(text), _threshold(filing.doc_type_code))
                dirty += int(quality == "dirty")
                section_rows.append(
                    (
                        filing.edinet_code, filing.filing_id, "business_status",
                        filing.doc_type_code, filing.filed_date, filing.period_end,
                        text, len(text), "edinet_html_file", quality,
                        quality_error,
                    )
                )
                succeeded += 1
                state_rows.append((filing.edinet_code, filing.filing_id, True, True, None))
            except Exception as exc:
                failed += 1
                state_rows.append((filing.edinet_code, filing.filing_id, True, False, str(exc)[:2000]))
        with connect() as conn, conn.cursor() as cur:
            if section_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO fact_mda_sections_jp
                        (edinet_code, filing_id, section_id, doc_type_code, filed_date, period_end,
                         section_text, char_count, extraction_method, extraction_quality, extraction_error)
                    VALUES %s
                    ON CONFLICT (edinet_code, filing_id, section_id) DO UPDATE SET
                        doc_type_code = EXCLUDED.doc_type_code,
                        filed_date = EXCLUDED.filed_date,
                        period_end = EXCLUDED.period_end,
                        section_text = EXCLUDED.section_text,
                        char_count = EXCLUDED.char_count,
                        extraction_method = EXCLUDED.extraction_method,
                        extraction_quality = EXCLUDED.extraction_quality,
                        extraction_error = EXCLUDED.extraction_error,
                        updated_at = now()
                    """,
                    section_rows,
                    page_size=1000,
                )
            if state_rows:
                execute_values(
                    cur,
                    """
                    UPDATE source_mda_state_jp AS s
                       SET extraction_attempted = v.attempted,
                           extraction_succeeded = v.succeeded,
                           last_attempted_at = now(),
                           error_message = v.error_message,
                           updated_at = now()
                      FROM (VALUES %s) AS v(edinet_code, filing_id, attempted, succeeded, error_message)
                     WHERE s.edinet_code = v.edinet_code
                       AND s.filing_id = v.filing_id
                    """,
                    state_rows,
                    page_size=5000,
                )
        finish_run(ctx, "succeeded", rows_in=attempted, rows_out=len(section_rows))
        return {"filings": len(filings), "attempted": attempted, "succeeded": succeeded, "failed": failed, "sections": len(section_rows), "dirty": dirty}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def ingest(edinet_code: str | None = None, years: int | None = None, limit: int | None = None) -> dict[str, int]:
    d_counts = discover(edinet_code=edinet_code, years=years)
    e_counts = extract(edinet_code=edinet_code, limit=limit)
    return {f"discover_{key}": value for key, value in d_counts.items()} | {f"extract_{key}": value for key, value in e_counts.items()}


def status(edinet_code: str | None = None) -> dict[str, int | str | None]:
    params: list = []
    entity_filter = ""
    filing_filter = ""
    if edinet_code:
        entity_filter = "WHERE edinet_code = %s"
        filing_filter = "AND entity_id = %s"
        params.append(edinet_code)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE source_path IS NOT NULL)
            FROM source_filing_state
            WHERE jurisdiction = 'JP'
              AND filing_type = ANY(%s)
              {filing_filter}
            """,
            [list(_JP_DOC_TYPES)] + ([edinet_code] if edinet_code else []),
        )
        eligible, source_paths = cur.fetchone()
        cur.execute(
            f"""
            SELECT availability_status, extraction_attempted, extraction_succeeded, COUNT(*)
            FROM source_mda_state_jp
            {entity_filter}
            GROUP BY 1,2,3
            ORDER BY 1,2,3
            """,
            params,
        )
        out = {}
        out["eligible_filings"] = eligible
        out["source_paths"] = source_paths
        for availability, attempted, succeeded, count in cur.fetchall():
            out[f"{availability}_attempted_{attempted}_succeeded_{succeeded}"] = count
        fact_filter = "WHERE edinet_code = %s" if edinet_code else ""
        cur.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT edinet_code),
                   COUNT(*) FILTER (WHERE extraction_quality = 'dirty'),
                   MAX(filed_date)
            FROM fact_mda_sections_jp
            {fact_filter}
            """,
            [edinet_code] if edinet_code else [],
        )
        sections, companies, dirty_sections, latest_filed = cur.fetchone()
        out["sections"] = sections
        out["companies"] = companies
        out["dirty_sections"] = dirty_sections
        out["latest_filed_date"] = latest_filed.isoformat() if latest_filed else None
        return out
