"""US SEC raw XBRL ZIP extraction for linkbase enrichment."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import os
import struct
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile
import zlib

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows production path
    msvcrt = None


_KINDS = ("cal", "def", "lab", "pre")
_USER_AGENT = "MZQA XBRL pipeline contact=bastian.offermann@gmail.com"
_HTML_EXCLUDE_TOKENS = ("exhibit", "supplement", "note")
_DOWNLOAD_STATE_FLUSH_SIZE = 1000
_EXTRACT_STATE_FLUSH_SIZE = 1000
_EXTRACT_PROGRESS_INTERVAL = 500
_DEFAULT_SEC_REQUESTS_PER_SECOND = 8.0
_DEFAULT_THROTTLE_LOCK_TIMEOUT_SECONDS = 30.0
_TERMINAL_EXTRACT_STATUSES = ("extracted_full", "extracted_partial", "no_linkbases")
_LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
_LOCAL_FILE_SIGNATURE = 0x04034B50
_UTF8_FLAG = 0x800
_DATA_DESCRIPTOR_FLAG = 0x08


def _us_root() -> Path:
    return load_settings().market_data_root / "us_sec"


def xbrl_zip_dir() -> Path:
    return Path(os.environ.get("XBRL_SEC_US_XBRL_ZIP_DIR", str(_us_root() / "xbrl")))


def _xbrl_zip_dirs() -> list[Path]:
    """Primary ZIP dir plus the legacy market_data path for existing inventory."""
    primary = xbrl_zip_dir()
    legacy = _us_root() / "xbrl"
    dirs = [primary]
    if legacy != primary and legacy.exists():
        dirs.append(legacy)
    return dirs


def _kind_dir(kind: str) -> Path:
    path = _us_root() / f"xbrl_{kind}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def xbrl_html_dir() -> Path:
    path = _us_root() / "xbrl_html"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _zip_candidates(entity_ids: list[str] | None = None) -> list[Path]:
    roots = [root for root in _xbrl_zip_dirs() if root.exists()]
    if not roots:
        raise FileNotFoundError(xbrl_zip_dir())
    if entity_ids:
        wanted = {str(value).strip().zfill(10) for value in entity_ids}
        return sorted(
            path
            for root in roots
            for path in root.glob("CIK*_xbrl.zip")
            if path.name[3:13] in wanted
        )
    return sorted(path for root in roots for path in root.glob("CIK*_xbrl.zip"))


def _db_zip_candidates_for_extraction(
    entity_ids: list[str] | None = None,
    include_terminal: bool = False,
) -> tuple[list[Path], dict[str, int]]:
    """Return local ZIP paths whose source_filing_state extraction status still needs work."""
    local_inventory = _local_zip_inventory(entity_ids=entity_ids)
    params: list[object] = []
    clauses = [
        "jurisdiction='US'",
        "entity_id IS NOT NULL",
        "filing_id IS NOT NULL",
        "xbrl_downloaded",
    ]
    if entity_ids:
        clauses.append("entity_id = ANY(%s)")
        params.append([str(value).strip().zfill(10) for value in entity_ids])
    if not include_terminal:
        clauses.append("NOT (COALESCE(xbrl_acquisition_status, '') = ANY(%s))")
        params.append(list(_TERMINAL_EXTRACT_STATUSES))

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT entity_id, filing_id, xbrl_package_path
              FROM source_filing_state
             WHERE {" AND ".join(clauses)}
             ORDER BY filed_date DESC NULLS LAST, entity_id, filing_id
            """,
            params,
        )
        rows = cur.fetchall()

    candidates: list[Path] = []
    seen: set[tuple[str, str]] = set()
    missing_local_zips = 0
    for entity_id, filing_id, package_path in rows:
        identity = (str(entity_id).strip().zfill(10), str(filing_id).strip())
        if identity in seen:
            continue
        seen.add(identity)
        path = local_inventory.get(identity)
        if path is None and package_path:
            candidate = Path(str(package_path))
            if candidate.exists():
                path = candidate
        if path is None:
            missing_local_zips += 1
            continue
        candidates.append(path)
    return candidates, {"db_candidates": len(rows), "missing_local_zips": missing_local_zips}


def _local_zip_inventory(entity_ids: list[str] | None = None) -> dict[tuple[str, str], Path]:
    """Map local (CIK, accession) ZIP identities without per-filing path probes."""
    wanted = {str(value).strip().zfill(10) for value in entity_ids} if entity_ids else None
    inventory: dict[tuple[str, str], Path] = {}
    for root in _xbrl_zip_dirs():
        if not root.exists():
            continue
        for path in root.glob("CIK*_xbrl.zip"):
            identity = _zip_identity(path)
            if not identity:
                continue
            cik, accession = identity
            if wanted and cik not in wanted:
                continue
            inventory.setdefault((cik, accession), path)
    return inventory


def _xbrl_zip_path(cik: str, accession: str) -> Path:
    return xbrl_zip_dir() / f"CIK{str(cik).zfill(10)}_{accession}_xbrl.zip"


def _existing_xbrl_zip_path(cik: str, accession: str) -> Path | None:
    name = f"CIK{str(cik).zfill(10)}_{accession}_xbrl.zip"
    for root in _xbrl_zip_dirs():
        path = root / name
        if path.exists():
            return path
    return None


def _download_url(cik: str, accession: str) -> str:
    cik_int = int(cik)
    acc_no_dashes = accession.replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_no_dashes}/{accession}-xbrl.zip"
    )


def _sec_request_interval() -> float:
    try:
        requests_per_second = float(
            os.environ.get("XBRL_SEC_REQUESTS_PER_SECOND", str(_DEFAULT_SEC_REQUESTS_PER_SECOND))
        )
    except ValueError:
        requests_per_second = _DEFAULT_SEC_REQUESTS_PER_SECOND
    return 1.0 / max(1.0, min(10.0, requests_per_second))


def _throttle_lock_timeout() -> float:
    try:
        return float(
            os.environ.get(
                "XBRL_SEC_THROTTLE_LOCK_TIMEOUT_SECONDS",
                str(_DEFAULT_THROTTLE_LOCK_TIMEOUT_SECONDS),
            )
        )
    except ValueError:
        return _DEFAULT_THROTTLE_LOCK_TIMEOUT_SECONDS


def _respect_sec_rate_limit() -> None:
    """Process-shared SEC throttle for parallel workers."""
    interval = _sec_request_interval()
    if msvcrt is None:
        time.sleep(interval)
        return
    path = xbrl_zip_dir().parent / ".sec_request_throttle"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        fh.seek(0)
        deadline = time.monotonic() + max(1.0, _throttle_lock_timeout())
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for SEC throttle lock at {path}")
                time.sleep(0.05)
        try:
            fh.seek(0)
            raw = fh.read().decode("ascii", errors="ignore").strip()
            try:
                last_request = float(raw) if raw else 0.0
            except ValueError:
                last_request = 0.0
            now = time.monotonic()
            wait_seconds = last_request + interval - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
            fh.seek(0)
            fh.truncate()
            fh.write(f"{now:.9f}".encode("ascii"))
            fh.flush()
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def _candidate_filings(
    entity_ids: list[str] | None = None,
    limit: int | None = None,
    statuses: tuple[str, ...] | None = None,
    forms: tuple[str, ...] | None = None,
    filed_date_from: str | None = None,
    worker_index: int | None = None,
    worker_count: int | None = None,
) -> list[tuple[str, str]]:
    params: list = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND entity_id = ANY(%s)"
        params.append([str(value).zfill(10) for value in entity_ids])
    status_filter = ""
    if statuses:
        status_filter = "AND xbrl_acquisition_status = ANY(%s)"
        params.append(list(statuses))
    form_filter = ""
    if forms:
        form_filter = "AND filing_type = ANY(%s)"
        params.append(list(forms))
    filed_date_filter = ""
    if filed_date_from:
        filed_date_filter = "AND filed_date >= %s"
        params.append(filed_date_from)
    shard_filter = ""
    if worker_count is not None:
        if worker_index is None:
            raise ValueError("worker_index is required when worker_count is set")
        if worker_count < 1 or worker_index < 0 or worker_index >= worker_count:
            raise ValueError("worker_index must be in [0, worker_count)")
        shard_filter = "AND mod((hashtext(entity_id || ':' || filing_id) & 2147483647), %s) = %s"
        params.extend([worker_count, worker_index])
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT %s"
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT entity_id, filing_id
            FROM source_filing_state
            WHERE jurisdiction='US'
              AND filing_id IS NOT NULL
              AND entity_id IS NOT NULL
              {entity_filter}
              {status_filter}
              {form_filter}
              {filed_date_filter}
              {shard_filter}
            ORDER BY filed_date DESC NULLS LAST, entity_id, filing_id
            {limit_clause}
            """,
            params,
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _mark_download_state(rows: list[tuple[str, str, str, bool, bool, str, str | None]]) -> None:
    if not rows:
        return
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            UPDATE source_filing_state AS s
               SET xbrl_package_path = v.path,
                   xbrl_downloaded = v.downloaded,
                   xbrl_download_attempted = v.attempted,
                   xbrl_acquisition_status = CASE
                       WHEN s.xbrl_extracted AND s.xbrl_cal_extracted AND s.xbrl_pre_extracted
                            AND s.xbrl_def_extracted AND s.xbrl_lab_extracted THEN 'extracted_full'
                       WHEN s.xbrl_extracted THEN 'extracted_partial'
                       ELSE v.status
                   END,
                   xbrl_error = v.error,
                   xbrl_last_attempted_at = CASE WHEN v.attempted THEN now() ELSE s.xbrl_last_attempted_at END,
                   updated_at = now()
              FROM (VALUES %s) AS v(entity_id, filing_id, path, downloaded, attempted, status, error)
             WHERE s.jurisdiction = 'US'
               AND s.entity_id = v.entity_id
               AND s.filing_id = v.filing_id
            """,
            rows,
            page_size=5000,
        )


def _zip_identity(zip_path: Path) -> tuple[str, str] | None:
    stem = zip_path.name.removesuffix("_xbrl.zip")
    if not stem.startswith("CIK") or "_" not in stem:
        return None
    cik_part, accession = stem.split("_", 1)
    return cik_part.removeprefix("CIK"), accession


def _mark_extract_state(rows: list[tuple]) -> None:
    if not rows:
        return
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            UPDATE source_filing_state AS s
               SET xbrl_package_path = v.path,
                   xbrl_downloaded = true,
                   xbrl_extracted = v.extracted,
                   xbrl_cal_extracted = v.cal,
                   xbrl_pre_extracted = v.pre,
                   xbrl_def_extracted = v.definition,
                   xbrl_lab_extracted = v.lab,
                   xbrl_html_extracted = v.html,
                   xbrl_acquisition_status = CASE
                       WHEN NOT v.extracted THEN 'extract_error'
                       WHEN v.cal AND v.pre AND v.definition AND v.lab THEN 'extracted_full'
                       WHEN v.cal OR v.pre OR v.definition OR v.lab THEN 'extracted_partial'
                       ELSE 'no_linkbases'
                   END,
                   xbrl_error = CASE WHEN v.extracted THEN NULL ELSE 'zip_extract_error' END,
                   updated_at = now()
              FROM (VALUES %s) AS v(entity_id, filing_id, path, extracted, cal, pre, definition, lab, html)
             WHERE s.jurisdiction = 'US'
               AND s.entity_id = v.entity_id
               AND s.filing_id = v.filing_id
            """,
            rows,
            page_size=5000,
        )


def download_us_xbrl_zips(
    entity_ids: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
    statuses: tuple[str, ...] | None = ("pending",),
    forms: tuple[str, ...] | None = None,
    filed_date_from: str | None = None,
    worker_index: int | None = None,
    worker_count: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    xbrl_zip_dir().mkdir(parents=True, exist_ok=True)
    result = {"candidates": 0, "downloaded": 0, "skipped": 0, "not_found": 0, "errors": 0}
    state_rows: list[tuple[str, str, str, bool, bool, str, str | None]] = []
    candidate_statuses = None if force else statuses
    local_inventory = {} if force else _local_zip_inventory(entity_ids=entity_ids)
    filters = {
        "force": force,
        "limit": limit,
        "statuses": candidate_statuses,
        "forms": forms,
        "filed_date_from": filed_date_from,
        "worker_index": worker_index,
        "worker_count": worker_count,
        "entity_scope_count": len(entity_ids or []),
        "local_inventory_count": len(local_inventory),
    }
    candidates = _candidate_filings(
        entity_ids=entity_ids,
        limit=limit,
        statuses=candidate_statuses,
        forms=forms,
        filed_date_from=filed_date_from,
        worker_index=worker_index,
        worker_count=worker_count,
    )
    total = len(candidates)
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
        processed = result["candidates"]
        now = time.monotonic()
        if not force_emit and total > 5 and processed % 100 != 0 and now - last_emit < 15:
            return
        last_emit = now
        event_type = "stage_progress"
        if phase in {"started", "finished"}:
            event_type = f"stage_{phase}"
        elif phase == "error":
            event_type = "stage_error"
        progress_callback({
            "event_type": event_type,
            "message": (
                f"US XBRL {phase}: {processed}/{total} "
                f"downloaded={result['downloaded']} skipped={result['skipped']} "
                f"not_found={result['not_found']} errors={result['errors']}"
            ),
            "phase": phase,
            "rows_in": total,
            "rows_out": result["downloaded"],
            "total": total,
            "processed": processed,
            "downloaded": result["downloaded"],
            "skipped": result["skipped"],
            "not_found": result["not_found"],
            "errors": result["errors"],
            "current": current,
            "filters": filters,
            "error": error,
        })

    def flush_if_needed() -> None:
        if len(state_rows) >= _DOWNLOAD_STATE_FLUSH_SIZE:
            _mark_download_state(state_rows)
            state_rows.clear()

    emit("started", force_emit=True)
    for cik, accession in candidates:
        result["candidates"] += 1
        dest = _xbrl_zip_path(cik, accession)
        url = _download_url(cik, accession)
        current = {
            "entity_id": str(cik).zfill(10),
            "filing_id": accession,
            "url": url,
            "path": str(dest),
        }
        emit("attempting", current=current, force_emit=result["candidates"] == 1 or total <= 5)
        existing = local_inventory.get((str(cik).zfill(10), accession))
        if existing and not force:
            result["skipped"] += 1
            state_rows.append((cik, accession, str(existing), True, False, "downloaded", None))
            emit("progress", current=current)
            flush_if_needed()
            continue
        req = Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            _respect_sec_rate_limit()
            with urlopen(req, timeout=60) as response:
                dest.write_bytes(response.read())
            result["downloaded"] += 1
            state_rows.append((cik, accession, str(dest), True, True, "downloaded", None))
            emit("progress", current=current)
        except HTTPError as exc:
            if exc.code == 404:
                result["not_found"] += 1
                state_rows.append((cik, accession, str(dest), False, True, "not_found", "404"))
                emit("error", current=current, force_emit=True, error="404")
                flush_if_needed()
                continue
            result["errors"] += 1
            state_rows.append((cik, accession, str(dest), False, True, "download_error", f"HTTP {exc.code}"))
            emit("error", current=current, force_emit=True, error=f"HTTP {exc.code}")
        except (URLError, TimeoutError) as exc:
            result["errors"] += 1
            error = str(exc) or exc.__class__.__name__
            state_rows.append((cik, accession, str(dest), False, True, "download_error", error[:400]))
            emit("error", current=current, force_emit=True, error=error[:400])
        flush_if_needed()
    _mark_download_state(state_rows)
    emit("finished", force_emit=True)
    return result


def reconcile_local_xbrl_inventory(entity_ids: list[str] | None = None) -> dict[str, int]:
    """Mark already-present local XBRL ZIPs before any network download."""
    rows: list[tuple[str, str, str, bool, bool, str, str | None]] = []
    candidates = _candidate_filings(entity_ids=entity_ids)
    local_inventory = _local_zip_inventory(entity_ids=entity_ids)
    for cik, accession in candidates:
        dest = local_inventory.get((str(cik).zfill(10), accession))
        if dest:
            rows.append((cik, accession, str(dest), True, False, "downloaded", None))
    _mark_download_state(rows)
    return {"candidates": len(candidates), "local_zips": len(rows)}


def _member_for_kind(zf: zipfile.ZipFile, kind: str) -> zipfile.ZipInfo | None:
    suffix = f"_{kind}.xml"
    for entry in zf.infolist():
        if entry.is_dir():
            continue
        name = Path(entry.filename.replace("\\", "/")).name.lower()
        if name.endswith(suffix):
            return entry
    return None


def _is_root_html(entry: zipfile.ZipInfo) -> bool:
    if entry.is_dir():
        return False
    normalized = entry.filename.replace("\\", "/")
    if "/" in normalized:
        return False
    name = normalized.lower()
    if not (name.endswith(".htm") or name.endswith(".html")):
        return False
    return not any(token in name for token in _HTML_EXCLUDE_TOKENS)


def _is_root_html_name(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    if "/" in normalized:
        return False
    name = normalized.lower()
    if not (name.endswith(".htm") or name.endswith(".html")):
        return False
    return not any(token in name for token in _HTML_EXCLUDE_TOKENS)


def _html_marker_score(zf: zipfile.ZipFile, entry: zipfile.ZipInfo) -> int:
    try:
        with zf.open(entry) as src:
            raw = src.read(512_000).lower()
    except Exception:
        return 0
    score = 0
    for marker in (b"ix:nonnumeric", b"ix:nonfraction", b"dei:documenttype"):
        if marker in raw:
            score += 1
    return score


def _html_marker_score_bytes(raw: bytes) -> int:
    sample = raw[:512_000].lower()
    score = 0
    for marker in (b"ix:nonnumeric", b"ix:nonfraction", b"dei:documenttype"):
        if marker in sample:
            score += 1
    return score


def _member_for_html(zf: zipfile.ZipFile, accession: str) -> zipfile.ZipInfo | None:
    candidates = [entry for entry in zf.infolist() if _is_root_html(entry)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    acc_no_dash = accession.replace("-", "").lower()
    acc_tail = "-".join(accession.lower().split("-")[1:])

    def _rank(entry: zipfile.ZipInfo) -> tuple[int, int, int]:
        name = Path(entry.filename).name.lower()
        acc_match = int(acc_no_dash in name or (acc_tail and acc_tail in name))
        return (_html_marker_score(zf, entry), acc_match, int(entry.file_size or entry.compress_size or 0))

    return max(candidates, key=_rank)


def _iter_local_zip_entries(zip_path: Path):
    seen = False
    with zip_path.open("rb") as fh:
        while True:
            header = fh.read(_LOCAL_FILE_HEADER.size)
            if not header:
                break
            if len(header) < _LOCAL_FILE_HEADER.size:
                break
            (
                signature,
                _version,
                flags,
                method,
                _mod_time,
                _mod_date,
                _crc32,
                compressed_size,
                _uncompressed_size,
                name_size,
                extra_size,
            ) = _LOCAL_FILE_HEADER.unpack(header)
            if signature != _LOCAL_FILE_SIGNATURE:
                break
            seen = True
            raw_name = fh.read(name_size)
            encoding = "utf-8" if flags & _UTF8_FLAG else "cp437"
            name = raw_name.decode(encoding, errors="replace")
            fh.seek(extra_size, os.SEEK_CUR)
            if flags & _DATA_DESCRIPTOR_FLAG:
                raise zipfile.BadZipFile(f"unsupported streaming ZIP entry: {name}")
            compressed = fh.read(compressed_size)
            if len(compressed) != compressed_size:
                raise zipfile.BadZipFile(f"truncated ZIP entry: {name}")
            if method == zipfile.ZIP_STORED:
                data = compressed
            elif method == zipfile.ZIP_DEFLATED:
                data = zlib.decompress(compressed, -zlib.MAX_WBITS)
            else:
                continue
            yield name, data
    if not seen:
        raise zipfile.BadZipFile(zip_path)


def _extract_one_from_local_headers(zip_path: Path, force: bool = False) -> dict[str, int]:
    stem = zip_path.name.removesuffix("_xbrl.zip")
    out = {"processed": 1, "written": 0, "skipped": 0, "missing": 0, "errors": 0}
    for kind in _KINDS:
        out[kind] = 0
    out["html"] = 0

    pending_kinds: set[str] = set()
    for kind in _KINDS:
        target = _kind_dir(kind) / f"{stem}_{kind}.xml"
        if target.exists() and not force:
            out["skipped"] += 1
            out[kind] = 1
        else:
            pending_kinds.add(kind)

    ident = _zip_identity(zip_path)
    html_target: Path | None = None
    html_pending = False
    html_candidates: list[tuple[str, bytes]] = []
    if ident:
        cik, accession = ident
        html_target = xbrl_html_dir() / f"CIK{str(cik).zfill(10)}_{accession}.htm"
        if html_target.exists() and not force:
            out["skipped"] += 1
            out["html"] = 1
        else:
            html_pending = True

    for entry_name, data in _iter_local_zip_entries(zip_path):
        base_name = Path(entry_name.replace("\\", "/")).name
        lower_name = base_name.lower()
        for kind in tuple(pending_kinds):
            if lower_name.endswith(f"_{kind}.xml"):
                target = _kind_dir(kind) / f"{stem}_{kind}.xml"
                target.write_bytes(data)
                out["written"] += 1
                out[kind] = 1
                pending_kinds.remove(kind)
                break
        if html_pending and _is_root_html_name(entry_name):
            html_candidates.append((entry_name, data))

    out["missing"] += len(pending_kinds)
    if html_pending:
        if not html_candidates:
            out["missing"] += 1
        else:
            assert ident is not None
            assert html_target is not None
            _cik, accession = ident
            acc_no_dash = accession.replace("-", "").lower()
            acc_tail = "-".join(accession.lower().split("-")[1:])

            def _rank(candidate: tuple[str, bytes]) -> tuple[int, int, int]:
                name = Path(candidate[0]).name.lower()
                acc_match = int(acc_no_dash in name or (acc_tail and acc_tail in name))
                return (_html_marker_score_bytes(candidate[1]), acc_match, len(candidate[1]))

            _entry_name, html_data = max(html_candidates, key=_rank)
            html_target.write_bytes(html_data)
            out["written"] += 1
            out["html"] = 1
    return out


def _extract_one(zip_path: Path, force: bool = False) -> dict[str, int]:
    stem = zip_path.name.removesuffix("_xbrl.zip")
    out = {"processed": 1, "written": 0, "skipped": 0, "missing": 0, "errors": 0}
    for kind in _KINDS:
        out[kind] = 0
    out["html"] = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for kind in _KINDS:
                target = _kind_dir(kind) / f"{stem}_{kind}.xml"
                if target.exists() and not force:
                    out["skipped"] += 1
                    out[kind] = 1
                    continue
                member = _member_for_kind(zf, kind)
                if member is None:
                    out["missing"] += 1
                    continue
                with zf.open(member) as src, target.open("wb") as dst:
                    dst.write(src.read())
                out["written"] += 1
                out[kind] = 1
            ident = _zip_identity(zip_path)
            if ident:
                cik, accession = ident
                target = xbrl_html_dir() / f"CIK{str(cik).zfill(10)}_{accession}.htm"
                if target.exists() and not force:
                    out["skipped"] += 1
                    out["html"] = 1
                else:
                    member = _member_for_html(zf, accession)
                    if member is None:
                        out["missing"] += 1
                    else:
                        with zf.open(member) as src, target.open("wb") as dst:
                            dst.write(src.read())
                        out["written"] += 1
                        out["html"] = 1
    except Exception:
        try:
            return _extract_one_from_local_headers(zip_path, force=force)
        except Exception:
            out["errors"] += 1
    return out


def _extract_one_worker(args: tuple[Path, bool]) -> tuple[Path, dict[str, int]]:
    zip_path, force = args
    return zip_path, _extract_one(zip_path, force=force)


def _existing_output_stems() -> set[str]:
    stems: set[str] = set()
    for kind in _KINDS:
        suffix = f"_{kind}.xml"
        root = _us_root() / f"xbrl_{kind}"
        if not root.exists():
            continue
        for path in root.glob(f"CIK*{suffix}"):
            stems.add(path.name.removesuffix(suffix))
    html_root = xbrl_html_dir()
    for pattern in ("CIK*.htm", "CIK*.html"):
        for path in html_root.glob(pattern):
            stems.add(path.stem)
    return stems


def _existing_extract_state(zip_path: Path) -> tuple[bool, bool, bool, bool, bool] | None:
    ident = _zip_identity(zip_path)
    if not ident:
        return None
    stem = zip_path.name.removesuffix("_xbrl.zip")
    cik, accession = ident
    cal = (_us_root() / "xbrl_cal" / f"{stem}_cal.xml").exists()
    pre = (_us_root() / "xbrl_pre" / f"{stem}_pre.xml").exists()
    definition = (_us_root() / "xbrl_def" / f"{stem}_def.xml").exists()
    lab = (_us_root() / "xbrl_lab" / f"{stem}_lab.xml").exists()
    html = (xbrl_html_dir() / f"CIK{str(cik).zfill(10)}_{accession}.htm").exists()
    return cal, pre, definition, lab, html


def _extract_chunksize(candidate_count: int, workers: int) -> int:
    if candidate_count <= 0:
        return 1
    # Keep IPC overhead modest without making any single task chunk too large to rebalance.
    return max(1, min(100, candidate_count // max(1, workers * 16)))


def extract_us_linkbases(
    entity_ids: list[str] | None = None,
    force: bool = False,
    workers: int = 1,
    skip_existing_stems: bool = False,
    db_driven: bool = False,
    progress_callback: Callable[[dict[str, int]], None] | None = None,
    progress_interval: int = _EXTRACT_PROGRESS_INTERVAL,
    state_flush_size: int = _EXTRACT_STATE_FLUSH_SIZE,
) -> dict[str, int]:
    total = {
        "processed": 0,
        "written": 0,
        "skipped": 0,
        "missing": 0,
        "errors": 0,
        "skipped_existing_stems": 0,
        "skipped_completed_outputs": 0,
        "db_candidates": 0,
        "missing_local_zips": 0,
        "candidate_files": 0,
    }
    state_rows: list[tuple] = []
    if db_driven and not force:
        candidates, candidate_stats = _db_zip_candidates_for_extraction(entity_ids=entity_ids)
        total["db_candidates"] = candidate_stats["db_candidates"]
        total["missing_local_zips"] = candidate_stats["missing_local_zips"]
    else:
        candidates = _zip_candidates(entity_ids)
    if skip_existing_stems and not force:
        existing_stems = _existing_output_stems()
        before_count = len(candidates)
        candidates = [
            zip_path
            for zip_path in candidates
            if zip_path.name.removesuffix("_xbrl.zip") not in existing_stems
        ]
        total["skipped_existing_stems"] = before_count - len(candidates)
    if not force:
        remaining_candidates: list[Path] = []
        for zip_path in candidates:
            existing_state = _existing_extract_state(zip_path)
            if existing_state and all(existing_state[:4]):
                ident = _zip_identity(zip_path)
                if ident:
                    cik, accession = ident
                    total["processed"] += 1
                    total["skipped_completed_outputs"] += 1
                    state_rows.append((cik, accession, str(zip_path), True, *existing_state))
                    if len(state_rows) >= max(1, state_flush_size):
                        _mark_extract_state(state_rows)
                        state_rows.clear()
                continue
            remaining_candidates.append(zip_path)
        candidates = remaining_candidates
        if progress_callback and total["processed"]:
            progress_callback(dict(total))
    total["candidate_files"] = len(candidates)
    worker_count = max(1, int(workers or 1))

    if worker_count <= 1 or len(candidates) <= 1:
        results = ((zip_path, _extract_one(zip_path, force=force)) for zip_path in candidates)
    else:
        executor = ProcessPoolExecutor(max_workers=worker_count)
        results = executor.map(
            _extract_one_worker,
            ((zip_path, force) for zip_path in candidates),
            chunksize=_extract_chunksize(len(candidates), worker_count),
        )

    try:
        for zip_path, result in results:
            for key in ("processed", "written", "skipped", "missing", "errors"):
                value = result[key]
                total[key] += value
            ident = _zip_identity(zip_path)
            if ident:
                cik, accession = ident
                state_rows.append(
                    (
                        cik,
                        accession,
                        str(zip_path),
                        result["errors"] == 0,
                        bool(result["cal"]),
                        bool(result["pre"]),
                        bool(result["def"]),
                        bool(result["lab"]),
                        bool(result["html"]),
                    )
                )
            if len(state_rows) >= max(1, state_flush_size):
                _mark_extract_state(state_rows)
                state_rows.clear()
            if progress_callback and total["processed"] % max(1, progress_interval) == 0:
                progress_callback(dict(total))
    finally:
        if worker_count > 1 and len(candidates) > 1:
            executor.shutdown(wait=True)

    _mark_extract_state(state_rows)
    if progress_callback:
        progress_callback(dict(total))
    return total
