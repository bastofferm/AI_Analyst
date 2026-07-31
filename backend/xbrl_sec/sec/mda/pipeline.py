from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.mda.common import extraction_quality, file_sha256
from xbrl_sec.sec.mda.html_extractor import extract_html_section
from xbrl_sec.sec.mda.ixbrl_extractor import extract_ixbrl_textblock
from xbrl_sec.sec.mda.local_reader import read_html
from xbrl_sec.sec.mda.settings import dirty_threshold, forms_from_env, html_dir_from_env, lookback_years_from_env
from xbrl_sec.sec.sources.sec_xbrl import extract_us_linkbases
from xbrl_sec.sec.state.store import finish_run, start_run


@dataclass(frozen=True)
class Filing:
    cik: str
    filing_id: str
    form_type: str
    filed_date: date
    period_end: date | None
    html_path: Path


def _html_path(cik: str, filing_id: str) -> Path:
    return html_dir_from_env() / f"CIK{str(cik).zfill(10)}_{filing_id}.htm"


def reextract_html(entity_ids: list[str] | None = None, force: bool = False) -> dict[str, int]:
    return extract_us_linkbases(entity_ids=entity_ids, force=force)


def discover(cik: str | None = None, years: int | None = None, forms: tuple[str, ...] | None = None) -> dict[str, int]:
    forms = forms or forms_from_env()
    years = years or lookback_years_from_env()
    params: list = [list(forms), years]
    cik_filter = ""
    if cik:
        cik_filter = "AND s.entity_id = %s"
        params.append(str(cik).zfill(10))
    ctx = start_run(
        "US",
        "mda_discover",
        "incremental",
        scope=json.dumps({"cik": cik, "years": years, "forms": list(forms)}),
    )
    try:
        html_counts = reextract_html(entity_ids=[cik] if cik else None, force=False)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.entity_id, s.filing_id, s.filing_type, s.filed_date,
                       s.xbrl_package_path, s.xbrl_html_extracted
                FROM source_filing_state s
                JOIN dim_company_us d ON d.cik = s.entity_id
                WHERE s.jurisdiction = 'US'
                  AND COALESCE(d.include_in_pipeline, true)
                  AND s.filing_type = ANY(%s)
                  AND s.filed_date >= CURRENT_DATE - (%s * INTERVAL '1 year')
                  AND s.xbrl_package_path IS NOT NULL
                  AND (
                        s.xbrl_acquisition_status IN ('extracted_full','extracted_partial','no_linkbases')
                        OR COALESCE(s.xbrl_html_extracted, false)
                      )
                  {cik_filter}
                ORDER BY s.entity_id, s.filed_date DESC, s.filing_id
                """,
                params,
            )
            rows = []
            available = missing_html = missing_zip = 0
            for cik_value, filing_id, form_type, filed_date, zip_path, html_extracted in cur.fetchall():
                path = _html_path(cik_value, filing_id)
                if path.exists():
                    status = "available"
                    available += 1
                    size = path.stat().st_size
                    html_path = str(path)
                elif not zip_path or not Path(zip_path).exists():
                    status = "missing_zip"
                    missing_zip += 1
                    size = None
                    html_path = None
                else:
                    status = "missing_html"
                    missing_html += 1
                    size = None
                    html_path = str(path) if html_extracted else None
                rows.append((
                    cik_value, filing_id, form_type, filed_date, status, html_path,
                    str(zip_path) if zip_path else None, file_sha256(html_path), size,
                ))
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO source_mda_state
                        (cik, filing_id, form_type, filed_date, availability_status,
                         html_path, source_package_path, source_html_sha256, html_size_bytes)
                    VALUES %s
                    ON CONFLICT (cik, filing_id) DO UPDATE SET
                        form_type = EXCLUDED.form_type,
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
        counts = {
            "discovered": len(rows), "available": available,
            "missing_html": missing_html, "missing_zip": missing_zip,
            "html_processed": html_counts.get("processed", 0),
            "html_written": html_counts.get("written", 0),
            "html_missing": html_counts.get("missing", 0),
            "html_errors": html_counts.get("errors", 0),
        }
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=available)
        return counts
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def _section_ids(form_type: str, include_item_7a: bool) -> tuple[str, ...]:
    if form_type in {"10-K", "10-K/A"}:
        return ("item_7", "item_7a") if include_item_7a else ("item_7",)
    if form_type in {"10-Q", "10-Q/A"}:
        return ("item_2",)
    return ()


def _extract_section(html: str, section_id: str) -> tuple[str | None, str | None]:
    text = extract_ixbrl_textblock(html, section_id)
    if text:
        return text, "ixbrl_textblock"
    text = extract_html_section(html, section_id)
    if text:
        return text, "html_regex"
    return None, None


def _candidate_filings(cik: str | None = None, limit: int | None = None, retry_dirty: bool = False) -> list[Filing]:
    params: list = []
    cik_filter = ""
    if cik:
        cik_filter = "AND m.cik = %s"
        params.append(str(cik).zfill(10))
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT %s"
        params.append(limit)
    success_filter = "" if retry_dirty else "AND NOT m.extraction_succeeded"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT m.cik, m.filing_id, m.form_type, m.filed_date, s.period_end, m.html_path
            FROM source_mda_state m
            JOIN source_filing_state s
              ON s.jurisdiction = 'US'
             AND s.entity_id = m.cik
             AND s.filing_id = m.filing_id
            WHERE m.availability_status = 'available'
              AND m.html_path IS NOT NULL
              {success_filter}
              {cik_filter}
            ORDER BY m.cik, m.filed_date DESC, m.filing_id
            {limit_clause}
            """,
            params,
        )
        return [
            Filing(cik_value, filing_id, form_type, filed_date, period_end, Path(html_path))
            for cik_value, filing_id, form_type, filed_date, period_end, html_path in cur.fetchall()
        ]


def extract(cik: str | None = None, limit: int | None = None, include_item_7a: bool = True, retry_dirty: bool = False) -> dict[str, int]:
    filings = _candidate_filings(cik=cik, limit=limit, retry_dirty=retry_dirty)
    ctx = start_run(
        "US",
        "mda_extract",
        "incremental",
        scope=json.dumps({"cik": cik, "limit": limit, "item_7a": include_item_7a}),
    )
    section_rows = []
    state_rows = []
    attempted = succeeded = dirty = failed = 0
    try:
        for filing in filings:
            attempted += 1
            try:
                html = read_html(filing.html_path)
                inserted_for_filing = 0
                errors = []
                for section_id in _section_ids(filing.form_type, include_item_7a):
                    text, method = _extract_section(html, section_id)
                    if not text or not method:
                        errors.append(f"{section_id}:empty_after_cleaning")
                        continue
                    quality, quality_error = extraction_quality(len(text), dirty_threshold(filing.form_type))
                    dirty += int(quality == "dirty")
                    section_rows.append(
                        (
                            filing.cik, filing.filing_id, section_id, filing.form_type,
                            filing.filed_date, filing.period_end, text, len(text),
                            method, quality, quality_error,
                        )
                    )
                    inserted_for_filing += 1
                if inserted_for_filing:
                    succeeded += 1
                    state_rows.append((filing.cik, filing.filing_id, True, True, None))
                else:
                    failed += 1
                    state_rows.append((filing.cik, filing.filing_id, True, False, "; ".join(errors) or "empty_extraction"))
            except Exception as exc:
                failed += 1
                state_rows.append((filing.cik, filing.filing_id, True, False, str(exc)[:2000]))
        with connect() as conn, conn.cursor() as cur:
            if section_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO fact_mda_sections_us
                        (cik, filing_id, section_id, form_type, filed_date, period_end,
                         section_text, char_count, extraction_method, extraction_quality, extraction_error)
                    VALUES %s
                    ON CONFLICT (cik, filing_id, section_id) DO UPDATE SET
                        form_type = EXCLUDED.form_type,
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
                    UPDATE source_mda_state AS s
                       SET extraction_attempted = v.attempted,
                           extraction_succeeded = v.succeeded,
                           last_attempted_at = now(),
                           error_message = v.error_message,
                           updated_at = now()
                      FROM (VALUES %s) AS v(cik, filing_id, attempted, succeeded, error_message)
                     WHERE s.cik = v.cik
                       AND s.filing_id = v.filing_id
                    """,
                    state_rows,
                    page_size=5000,
                )
        counts = {"filings": len(filings), "attempted": attempted, "succeeded": succeeded, "failed": failed, "sections": len(section_rows), "dirty": dirty}
        finish_run(ctx, "succeeded", rows_in=attempted, rows_out=len(section_rows))
        return counts
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def ingest(cik: str | None = None, years: int | None = None, limit: int | None = None, include_item_7a: bool = True) -> dict[str, int]:
    d_counts = discover(cik=cik, years=years)
    e_counts = extract(cik=cik, limit=limit, include_item_7a=include_item_7a)
    return {f"discover_{key}": value for key, value in d_counts.items()} | {f"extract_{key}": value for key, value in e_counts.items()}


def status(cik: str | None = None) -> dict[str, int | str | None]:
    params: list = []
    cik_filter = ""
    filing_filter = ""
    if cik:
        cik_filter = "WHERE cik = %s"
        filing_filter = "AND entity_id = %s"
        params.append(str(cik).zfill(10))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE xbrl_package_path IS NOT NULL),
                   COUNT(*) FILTER (WHERE COALESCE(xbrl_html_extracted, false))
            FROM source_filing_state
            WHERE jurisdiction = 'US'
              AND filing_type IN ('10-K','10-K/A','10-Q','10-Q/A')
              {filing_filter}
            """,
            params,
        )
        eligible, packages, html_extracted = cur.fetchone()
        cur.execute(
            f"""
            SELECT availability_status, extraction_attempted, extraction_succeeded, COUNT(*)
            FROM source_mda_state
            {cik_filter}
            GROUP BY 1,2,3
            ORDER BY 1,2,3
            """,
            params,
        )
        out = {}
        out["eligible_filings"] = eligible
        out["xbrl_packages"] = packages
        out["html_extracted"] = html_extracted
        for availability, attempted, succeeded, count in cur.fetchall():
            out[f"{availability}_attempted_{attempted}_succeeded_{succeeded}"] = count
        fact_filter = "WHERE cik = %s" if cik else ""
        cur.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT cik),
                   COUNT(*) FILTER (WHERE extraction_quality = 'dirty'),
                   MAX(filed_date)
            FROM fact_mda_sections_us
            {fact_filter}
            """,
            [str(cik).zfill(10)] if cik else [],
        )
        sections, companies, dirty_sections, latest_filed = cur.fetchone()
        out["sections"] = sections
        out["companies"] = companies
        out["dirty_sections"] = dirty_sections
        out["latest_filed_date"] = latest_filed.isoformat() if latest_filed else None
        return out
