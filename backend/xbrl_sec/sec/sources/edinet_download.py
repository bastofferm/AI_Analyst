"""EDINET API access for JP master, filing index, and XBRL ZIP downloads."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import time
import hashlib
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.edinet_forms import DOC_TYPE_CODES, normalize_doc_type_codes
from xbrl_sec.sec.sources.jp_identifiers import normalize_jp_primary_ticker


BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
USER_AGENT = "MZQA XBRL pipeline contact=bastian.offermann@gmail.com"
CORP_CLASSIFICATION_CODES = {"010", "020", "030", "040"}
AUDIT_FORM_CODES = {"080000"}


def _has_xbrl_payload(doc: dict[str, Any]) -> bool:
    return str(doc.get("xbrlFlag") or "").strip() == "1"


def _api_key() -> str | None:
    key = os.environ.get("EDINET_API_KEY")
    if key:
        return key
    if os.name == "nt":
        try:
            import winreg

            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, "Environment"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as handle:
                        value, _ = winreg.QueryValueEx(handle, "EDINET_API_KEY")
                        if value:
                            return str(value)
                except OSError:
                    continue
        except Exception:
            pass
    return None


def _xbrl_dir() -> Path:
    path = load_settings().market_data_root / "japan_edinet" / "xbrl"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    key = _api_key()
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    return headers


def _request(url: str, params: dict[str, Any], retries: int = 3, timeout: int = 120) -> bytes:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    full_url = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(Request(full_url, headers=_headers()), timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"EDINET request failed: {full_url}: {last_error}")


def _get_json(day: date) -> dict[str, Any]:
    payload = _request(f"{BASE_URL}/documents.json", {"date": day.isoformat(), "type": 2}, timeout=60)
    data = json.loads(payload.decode("utf-8"))
    if "statusCode" in data and data.get("statusCode") != 200:
        raise RuntimeError(f"EDINET API error {data.get('statusCode')}: {data.get('message')}")
    return data


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _date_range_desc(start: date, end: date):
    current = end
    while current >= start:
        yield current
        current -= timedelta(days=1)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _doc_rows(day: date) -> list[dict[str, Any]]:
    return (_get_json(day).get("results") or [])


def refresh_master(full: bool = False, days: int = 400, start_date: date | None = None, end_date: date | None = None) -> int:
    end = end_date or date.today()
    start = start_date or (date(2009, 1, 1) if full else end - timedelta(days=days))
    sql = """
        INSERT INTO dim_company_jp
            (edinet_code, name, primary_ticker, sec_code, jcn)
        VALUES %s
        ON CONFLICT (edinet_code) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, dim_company_jp.name),
            primary_ticker = COALESCE(EXCLUDED.primary_ticker, dim_company_jp.primary_ticker),
            sec_code = COALESCE(EXCLUDED.sec_code, dim_company_jp.sec_code),
            jcn = COALESCE(EXCLUDED.jcn, dim_company_jp.jcn),
            updated_at = now()
    """
    rows: dict[str, tuple] = {}
    written = 0
    days_scanned = 0
    date_iter = _date_range_desc(start, end) if full else _date_range(start, end)
    for day in date_iter:
        for doc in _doc_rows(day):
            ordinance = (doc.get("ordinanceCode") or "").strip()
            edinet_code = (doc.get("edinetCode") or "").strip()
            sec_code = (doc.get("secCode") or "").strip()
            name = (doc.get("filerName") or "").strip() or None
            if ordinance not in CORP_CLASSIFICATION_CODES or not edinet_code:
                continue
            rows[edinet_code] = (
                edinet_code,
                name,
                normalize_jp_primary_ticker(sec_code),
                sec_code or None,
                (doc.get("JCN") or "").strip() or None,
            )
        days_scanned += 1
        if rows and (len(rows) >= 100 or days_scanned % 30 == 0):
            with connect() as conn, conn.cursor() as cur:
                written += execute_values(cur, sql, list(rows.values()), page_size=1000)
            rows.clear()
        time.sleep(0.3)
    if not rows:
        return written
    with connect() as conn, conn.cursor() as cur:
        return written + execute_values(cur, sql, list(rows.values()), page_size=1000)


def _known_edinet_codes() -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT edinet_code
            FROM dim_company_jp
            WHERE edinet_code IS NOT NULL
              AND include_in_pipeline
            """
        )
        return {row[0] for row in cur.fetchall()}


def _next_index_start(full: bool, backfill_start: date) -> date:
    if full:
        return backfill_start
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(filed_date) FROM source_filing_state WHERE jurisdiction = 'JP'")
        row = cur.fetchone()
        return (row[0] + timedelta(days=1)) if row and row[0] else date.today() - timedelta(days=30)


def index_filings(
    full: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
    backfill_start: date = date(2009, 1, 1),
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    known = _known_edinet_codes()
    if not known:
        raise RuntimeError("dim_company_jp is empty. Run refresh-master JP first.")
    doc_types = normalize_doc_type_codes(filing_types)
    start = start_date or _next_index_start(full, backfill_start)
    end = end_date or date.today()
    rows: list[tuple] = []
    for day in _date_range(start, end):
        for doc in _doc_rows(day):
            doc_id = (doc.get("docID") or "").strip()
            edinet_code = (doc.get("edinetCode") or "").strip()
            doc_type = (doc.get("docTypeCode") or "").strip()
            form_code = (doc.get("formCode") or "").strip()
            ordinance = (doc.get("ordinanceCode") or "").strip()
            if not doc_id or edinet_code not in known:
                continue
            if doc_type not in DOC_TYPE_CODES or form_code in AUDIT_FORM_CODES:
                continue
            if doc_types is not None and doc_type not in doc_types:
                continue
            if not _has_xbrl_payload(doc):
                continue
            if ordinance and ordinance not in CORP_CLASSIFICATION_CODES:
                continue
            rows.append(
                (
                    "JP",
                    doc_id,
                    edinet_code,
                    doc_type,
                    _parse_date(doc.get("submitDateTime")),
                    _parse_date(doc.get("periodEnd")),
                    str(_xbrl_dir() / f"{doc_id}_xbrl.zip"),
                    json.dumps(doc, ensure_ascii=False),
                    "package",
                )
            )
        time.sleep(0.3)
    sql = """
        INSERT INTO source_filing_state
            (jurisdiction, filing_id, entity_id, filing_type, filed_date, period_end, source_path, raw_payload, source_kind)
        VALUES %s
        ON CONFLICT (jurisdiction, filing_id) DO UPDATE SET
            entity_id = EXCLUDED.entity_id,
            filing_type = EXCLUDED.filing_type,
            filed_date = COALESCE(EXCLUDED.filed_date, source_filing_state.filed_date),
            period_end = COALESCE(EXCLUDED.period_end, source_filing_state.period_end),
            source_path = EXCLUDED.source_path,
            raw_payload = EXCLUDED.raw_payload,
            source_kind = 'package',
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows, page_size=5000)


def _pending_doc_ids(
    force: bool = False,
    limit: int | None = None,
    doc_ids: list[str] | None = None,
    filing_types: tuple[str, ...] | None = None,
) -> list[tuple[str, bool]]:
    if doc_ids:
        selected = doc_ids[:limit] if limit else doc_ids
        return [(doc_id, force) for doc_id in selected]
    with connect() as conn, conn.cursor() as cur:
        pending_filter = "" if force else """
              AND (
                    NOT s.downloaded
                 OR s.xbrl_acquisition_status IN ('jp_zip_extract_error', 'jp_missing_local_zip')
              )
        """
        cur.execute(
            f"""
            SELECT s.filing_id, s.source_path, s.downloaded, s.xbrl_acquisition_status
            FROM source_filing_state s
            JOIN dim_company_jp d
              ON d.edinet_code = s.entity_id
             AND d.include_in_pipeline
            WHERE s.jurisdiction = 'JP'
              AND s.source_kind = 'package'
              AND COALESCE(s.raw_payload->>'xbrlFlag', '1') = '1'
              AND COALESCE(s.xbrl_acquisition_status, '') <> '{JP_UNAVAILABLE_STATUS}'
              {'' if filing_types is None else 'AND s.filing_type = ANY(%s)'}
              {pending_filter}
            ORDER BY s.filed_date DESC NULLS LAST, s.filing_id
            """,
            (() if filing_types is None else (list(filing_types),)),
        )
        rows = cur.fetchall()
    pending: list[tuple[str, bool]] = []
    for doc_id, source_path, downloaded, status in rows:
        redownload = force or status in {"jp_zip_extract_error", "jp_missing_local_zip"}
        pending.append((doc_id, redownload))
        if limit and len(pending) >= limit:
            break
    return pending


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _mark_downloaded(doc_id: str, path: Path, source_hash: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_filing_state
            SET downloaded = true,
                source_path = %s,
                source_hash = %s,
                source_kind = 'package',
                xbrl_acquisition_status = 'downloaded',
                xbrl_error = NULL,
                updated_at = now()
            WHERE jurisdiction = 'JP' AND filing_id = %s
            """,
            (str(path), source_hash, doc_id),
        )


# Terminal status for documents EDINET no longer serves (past its retention window).
JP_UNAVAILABLE_STATUS = "jp_unavailable"


def _classify_non_zip(payload: bytes) -> str:
    """Classify a non-ZIP EDINET document response.

    EDINET returns HTTP 200 with a JSON body such as
    ``{"metadata": {"status": "404", "message": "Not Found"}}`` (or a top-level
    ``{"status": "404", ...}``) when it can no longer serve a document. Those are
    'not_found'; any other non-ZIP body is a real 'error'.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        return "error"
    node = data.get("metadata") if isinstance(data, dict) and isinstance(data.get("metadata"), dict) else data
    if not isinstance(node, dict):
        return "error"
    status = str(node.get("status") or "").strip()
    message = str(node.get("message") or "").strip().lower()
    if status == "404" or "not found" in message:
        return "not_found"
    return "error"


def _logical_not_found_terminal(
    filed_date: date | None,
    attempt_count: int,
    retention_years: int,
    max_retries: int,
    today: date | None = None,
) -> bool:
    """Pure policy decision: should a not-found document be retired permanently?

    Terminal when the filing predates EDINET's retention window, or after enough
    consecutive logical-404s that it's clearly gone rather than a transient blip."""
    today = today or date.today()
    cutoff = today - timedelta(days=int(round(retention_years * 365.25)))
    if filed_date is not None and filed_date < cutoff:
        return True
    return attempt_count >= max(int(max_retries), 1)


def _mark_logical_not_found(doc_id: str) -> str:
    """Record a not-found outcome for a JP document and apply the retention/retry
    policy. Returns 'unavailable' if the document was marked terminal (and will be
    excluded from the pending set), otherwise 'pending' (it will be retried)."""
    settings = load_settings()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT filed_date, COALESCE((raw_payload->>'edinet_404_count')::int, 0)
            FROM source_filing_state
            WHERE jurisdiction='JP' AND filing_id=%s
            """,
            (doc_id,),
        )
        row = cur.fetchone()
        filed_date = row[0] if row else None
        count = (row[1] if row else 0) + 1
        terminal = _logical_not_found_terminal(
            filed_date, count, settings.jp_retention_years, settings.jp_max_404_retries
        )
        if terminal:
            cur.execute(
                """
                UPDATE source_filing_state
                   SET xbrl_acquisition_status=%s,
                       xbrl_error='edinet_logical_404',
                       raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s::jsonb,
                       updated_at=now()
                 WHERE jurisdiction='JP' AND filing_id=%s
                """,
                (JP_UNAVAILABLE_STATUS, json.dumps({"edinet_404_count": count}), doc_id),
            )
        else:
            cur.execute(
                """
                UPDATE source_filing_state
                   SET raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s::jsonb,
                       updated_at=now()
                 WHERE jurisdiction='JP' AND filing_id=%s
                """,
                (json.dumps({"edinet_404_count": count}), doc_id),
            )
    return "unavailable" if terminal else "pending"


def download_xbrl_packages(
    force: bool = False,
    limit: int | None = None,
    doc_ids: list[str] | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Download pending JP XBRL packages from EDINET.

    Returns honest counters: ``fetched`` (newly written ZIPs), ``skipped_existing``
    (already on disk), ``not_found`` (EDINET can't serve — logical-404 JSON or real
    HTTP 404), ``unavailable`` (subset of not_found newly marked terminal this run),
    and ``errors`` (real failures only).
    """
    doc_types = normalize_doc_type_codes(filing_types)
    pending = _pending_doc_ids(force=force, limit=limit, doc_ids=doc_ids, filing_types=doc_types)
    total = len(pending)
    fetched = skipped_existing = not_found = unavailable = errors = 0
    last_emit = 0.0

    def emit(
        phase: str,
        current: dict[str, Any] | None = None,
        force_emit: bool = False,
        error: str | None = None,
    ) -> None:
        nonlocal last_emit
        if progress_callback is None:
            return
        processed = fetched + skipped_existing + not_found + errors
        now = time.monotonic()
        if not force_emit and total > 5 and processed % 100 != 0 and now - last_emit < 15:
            return
        last_emit = now
        event_type = "stage_progress"
        if phase in {"started", "finished"}:
            event_type = f"stage_{phase}"
        elif phase == "error":
            event_type = "stage_error"
        ok = fetched + skipped_existing
        progress_callback({
            "event_type": event_type,
            "message": (
                f"JP XBRL {phase}: {processed}/{total} "
                f"fetched={fetched} skipped_existing={skipped_existing} "
                f"not_found={not_found} unavailable={unavailable} errors={errors}"
            ),
            "phase": phase,
            "rows_in": total,
            "rows_out": ok,
            "total": total,
            "processed": processed,
            "ok": ok,
            "fetched": fetched,
            "downloaded": fetched,
            "skipped_existing": skipped_existing,
            "not_found": not_found,
            "unavailable": unavailable,
            "errors": errors,
            "current": current,
            "filters": {"force": force, "limit": limit, "doc_ids": len(doc_ids or []), "filing_types": doc_types},
            "error": error,
        })

    emit("started", force_emit=True)
    for doc_id, redownload in pending:
        path = _xbrl_dir() / f"{doc_id}_xbrl.zip"
        current = {"doc_id": doc_id, "url": f"{BASE_URL}/documents/{doc_id}", "path": str(path), "redownload": redownload}
        emit("attempting", current=current, force_emit=fetched + skipped_existing + not_found + errors == 0 or total <= 5)
        if path.exists() and not force and not redownload:
            _mark_downloaded(doc_id, path, hashlib.sha256(path.read_bytes()).hexdigest())
            skipped_existing += 1
            emit("progress", current=current)
            continue
        try:
            payload = _request(f"{BASE_URL}/documents/{doc_id}", {"type": 1}, timeout=180)
            if payload[:4] != b"PK\x03\x04":
                # EDINET delivers a logical-404 as HTTP 200 + JSON; distinguish that
                # (recoverable retire) from a genuinely malformed/error response.
                if _classify_non_zip(payload) == "not_found":
                    not_found += 1
                    if _mark_logical_not_found(doc_id) == "unavailable":
                        unavailable += 1
                    emit("error", current=current, force_emit=True, error="logical_404")
                else:
                    errors += 1
                    emit("error", current=current, force_emit=True, error="response_not_zip")
                continue
            path.write_bytes(payload)
            _mark_downloaded(doc_id, path, _hash_bytes(payload))
            fetched += 1
            emit("progress", current=current)
        except HTTPError as exc:
            if exc.code != 404:
                errors += 1
                emit("error", current=current, force_emit=True, error=f"HTTP {exc.code}")
            else:
                not_found += 1
                if _mark_logical_not_found(doc_id) == "unavailable":
                    unavailable += 1
                emit("error", current=current, force_emit=True, error="404")
        except Exception:
            errors += 1
            emit("error", current=current, force_emit=True, error="download_error")
        time.sleep(0.3)
    emit("finished", force_emit=True)
    return {
        "fetched": fetched,
        "skipped_existing": skipped_existing,
        "not_found": not_found,
        "unavailable": unavailable,
        "errors": errors,
    }
