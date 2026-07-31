"""Local EDINET companyfacts index for the JP pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
import hashlib
import re
import time
from typing import Callable, Iterable
import zipfile

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.edinet_forms import normalize_doc_type_codes


_XBRL_NAME_RE = re.compile(
    r"^(?:(?P<doc_id>[^_]+)_)?(?P<form>[^_]+)_(?P<edinet_code>E\d{5})-[^_]*_"
    r"(?P<period_end>\d{4}-\d{2}-\d{2})_[^_]+_(?P<filed_date>\d{4}-\d{2}-\d{2})\.xbrl$",
    re.IGNORECASE,
)
_JP_TERMINAL_PACKAGE_STATUSES = (
    "jp_package_extracted",
    "jp_no_extractable_xbrl",
)
_EXTRACT_STATE_FLUSH_SIZE = 1000
_EXTRACT_PROGRESS_INTERVAL = 500
_SYNC_MARKER_NAME = ".sync_marker_jp"
ProgressCallback = Callable[[dict[str, int | str]], None]


def _sync_marker_path() -> Path:
    return companyfacts_dir() / _SYNC_MARKER_NAME


def _read_sync_marker() -> float | None:
    """Return the epoch time of the last successful full sync, or None."""
    path = _sync_marker_path()
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_sync_marker(at: float) -> None:
    path = _sync_marker_path()
    try:
        path.write_text(f"{at:.6f}\n", encoding="utf-8")
    except OSError:
        pass


def _entity_dirs_changed_since(marker_time: float) -> list[str] | None:
    """Return EDINET codes whose subdir mtime exceeds the marker.

    Returns:
        - None if the root is unreadable (caller should fall back to full scan).
        - [] if nothing changed since marker_time.
        - [edinet_code, ...] otherwise.

    Only checks top-level subdir mtimes (one stat per entity), which on Windows
    reflects entry additions/removals/renames inside the dir but not pure
    content changes. That matches the EDINET ingestion model: new filings are
    new files, never silent overwrites.
    """
    root = companyfacts_dir()
    try:
        entries = os.scandir(root)
    except OSError:
        return None
    changed: list[str] = []
    with entries as it:
        for entry in it:
            if not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if name.startswith("."):
                continue
            try:
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime > marker_time:
                changed.append(name)
    return changed


@dataclass(frozen=True)
class EdinetXbrlFile:
    path: Path
    doc_id: str
    edinet_code: str
    filing_type: str
    period_end: date
    filed_date: date
    source_hash: str
    cal_path: Path | None = None
    def_path: Path | None = None
    pre_path: Path | None = None
    lab_en_path: Path | None = None
    lab_ja_path: Path | None = None
    package_path: Path | None = None


def companyfacts_dir() -> Path:
    return load_settings().market_data_root / "japan_edinet" / "companyfacts"


def xbrl_zip_dir() -> Path:
    return load_settings().market_data_root / "japan_edinet" / "xbrl"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fast_file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"stat:{stat.st_size}:{stat.st_mtime_ns}"


def _filing_type_from_form(form: str) -> str:
    lower = form.lower()
    if "asr" in lower:
        return "120"
    if "q2r" in lower or "ssr" in lower or "h1" in lower:
        return "130"
    if "q1r" in lower or "q3r" in lower or "q" in lower:
        return "140"
    return "000"


def _is_publicdoc_form(form: str) -> bool:
    return not form.lower().startswith("jpaud")


def _companion_path(instance_path: Path, suffix: str) -> Path | None:
    candidate = instance_path.with_name(instance_path.stem + suffix)
    return candidate if candidate.exists() else None


def _metadata_from_name(path: Path) -> tuple[str, str, str, date, date] | None:
    match = _XBRL_NAME_RE.match(path.name)
    if not match:
        return None
    doc_id = path.stem
    form = match.group("form")
    if not _is_publicdoc_form(form):
        return None
    return (
        doc_id,
        match.group("edinet_code"),
        _filing_type_from_form(form),
        date.fromisoformat(match.group("period_end")),
        date.fromisoformat(match.group("filed_date")),
    )


def _source_doc_id_from_filing_id(filing_id: str) -> str:
    return filing_id.split("_", 1)[0]


def _load_package_filing_types(filing_ids: Iterable[str]) -> dict[str, str]:
    source_doc_ids = sorted({_source_doc_id_from_filing_id(str(filing_id)) for filing_id in filing_ids if filing_id})
    if not source_doc_ids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT filing_id, filing_type
            FROM source_filing_state
            WHERE jurisdiction = 'JP'
              AND source_kind = 'package'
              AND filing_id = ANY(%s)
            """,
            (source_doc_ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[1]}


def _safe_member_name(name: str) -> str:
    return Path(name.replace("\\", "/")).name


def _db_zip_candidates(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filing_types: tuple[str, ...] | None = None,
) -> list[Path]:
    src_root = xbrl_zip_dir()
    if doc_ids:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT filing_id, source_path
                FROM source_filing_state
                WHERE jurisdiction = 'JP'
                  AND filing_id = ANY(%s)
                """,
                (doc_ids,),
            )
            db_paths = {row[0]: Path(row[1]) for row in cur.fetchall() if row[1]}
        paths: list[Path] = []
        for doc_id in doc_ids:
            candidate = db_paths.get(doc_id, src_root / f"{doc_id}_xbrl.zip")
            paths.append(candidate)
        return paths

    join_sql = ""
    clauses = [
        "s.jurisdiction = 'JP'",
        "s.source_kind = 'package'",
        "s.downloaded",
        "s.source_path LIKE %s",
    ]
    params: list[object] = ["%_xbrl.zip"]
    if entity_ids:
        clauses.append("s.entity_id = ANY(%s)")
        params.append(entity_ids)
    else:
        join_sql = """
            JOIN dim_company_jp d
              ON d.edinet_code = s.entity_id
             AND d.include_in_pipeline
        """
    if filing_types is not None:
        clauses.append("s.filing_type = ANY(%s)")
        params.append(list(filing_types))
    if not force:
        clauses.append("COALESCE(s.extracted, false) IS NOT TRUE")
        clauses.append("NOT (COALESCE(s.xbrl_acquisition_status, '') = ANY(%s))")
        params.append(list(_JP_TERMINAL_PACKAGE_STATUSES))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT s.source_path
            FROM source_filing_state s
            {join_sql}
            WHERE {' AND '.join(clauses)}
            ORDER BY s.source_path
            """,
            params,
        )
        return [Path(row[0]) for row in cur.fetchall() if row[0]]


def _zip_candidates(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filing_types: tuple[str, ...] | None = None,
) -> list[Path]:
    try:
        paths = _db_zip_candidates(entity_ids=entity_ids, doc_ids=doc_ids, force=force, filing_types=filing_types)
        if paths or not force:
            return paths
    except Exception:
        if not force:
            raise
    return sorted(xbrl_zip_dir().glob("*_xbrl.zip"))


def _flush_package_extract_state(rows: list[tuple[str, str, bool, bool, str, str | None]]) -> None:
    if not rows:
        return
    sql = """
        UPDATE source_filing_state AS s
           SET source_path = v.source_path,
               downloaded = v.downloaded,
               extracted = v.extracted,
               source_kind = 'package',
               xbrl_acquisition_status = v.status,
               xbrl_error = v.error,
               updated_at = now()
          FROM (VALUES %s) AS v(filing_id, source_path, downloaded, extracted, status, error)
         WHERE s.jurisdiction = 'JP'
           AND s.filing_id = v.filing_id
    """
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)


def extract_xbrl_packages(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filing_types: list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[int], None] | None = None,
    progress_interval: int = _EXTRACT_PROGRESS_INTERVAL,
    state_flush_size: int = _EXTRACT_STATE_FLUSH_SIZE,
) -> int:
    """Extract type-1 EDINET XBRL ZIP packages into companyfacts/{edinet_code}.

    This extracts PublicDoc XBRL package files needed by the raw JP fact parser:
    instance documents, schemas, calculation/definition/presentation linkbases,
    labels, manifests, and inline HTML documents. Type-5 CSV ZIPs and AuditDoc
    package files are ignored for fact-table population.
    """
    doc_types = normalize_doc_type_codes(filing_types)
    src_root = xbrl_zip_dir()
    out_root = companyfacts_dir()
    if not src_root.exists():
        raise FileNotFoundError(src_root)
    wanted = set(entity_ids or [])
    extracted = 0
    processed = 0
    state_rows: list[tuple[str, str, bool, bool, str, str | None]] = []
    for zip_path in _zip_candidates(entity_ids=entity_ids, doc_ids=doc_ids, force=force, filing_types=doc_types):
        processed += 1
        doc_id = zip_path.name.removesuffix("_xbrl.zip")
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as exc:
            state_rows.append((doc_id, str(zip_path), False, False, "jp_zip_extract_error", str(exc)[:500]))
            if len(state_rows) >= state_flush_size:
                _flush_package_extract_state(state_rows)
                state_rows.clear()
            continue
        except OSError as exc:
            state_rows.append((doc_id, str(zip_path), False, False, "jp_missing_local_zip", str(exc)[:500]))
            if len(state_rows) >= state_flush_size:
                _flush_package_extract_state(state_rows)
                state_rows.clear()
            continue
        with zf:
            public_xbrls = [
                entry for entry in zf.infolist()
                if entry.filename.lower().endswith(".xbrl") and "/publicdoc/" in entry.filename.lower().replace("\\", "/")
            ]
            metadata = None
            for entry in public_xbrls:
                temp_name = Path(f"{doc_id}_{_safe_member_name(entry.filename)}")
                metadata = _metadata_from_name(temp_name)
                if metadata:
                    break
            if not metadata:
                state_rows.append(
                    (
                        doc_id,
                        str(zip_path),
                        True,
                        True,
                        "jp_no_extractable_xbrl",
                        "no PublicDoc XBRL instance matched the JP metadata pattern",
                    )
                )
                if len(state_rows) >= state_flush_size:
                    _flush_package_extract_state(state_rows)
                    state_rows.clear()
                if progress_callback and processed % max(progress_interval, 1) == 0:
                    progress_callback(processed)
                continue
            _, edinet_code, _, _, _ = metadata
            if wanted and edinet_code not in wanted:
                continue
            state_rows.append(
                (
                    doc_id,
                    str(zip_path),
                    True,
                    True,
                    "jp_package_extracted",
                    None,
                )
            )
            target_dir = out_root / edinet_code
            target_dir.mkdir(parents=True, exist_ok=True)
            for entry in zf.infolist():
                if entry.is_dir():
                    continue
                lower = entry.filename.lower().replace("\\", "/")
                if not lower.startswith("xbrl/publicdoc/"):
                    continue
                if not lower.endswith((".xbrl", ".xsd", ".xml", ".htm", ".html")):
                    continue
                target = target_dir / f"{doc_id}_{_safe_member_name(entry.filename)}"
                if target.exists() and not force:
                    continue
                with zf.open(entry) as src, target.open("wb") as dst:
                    dst.write(src.read())
                extracted += 1
        if len(state_rows) >= state_flush_size:
            _flush_package_extract_state(state_rows)
            state_rows.clear()
        if progress_callback and processed % max(progress_interval, 1) == 0:
            progress_callback(processed)
    _flush_package_extract_state(state_rows)
    if progress_callback and processed:
        progress_callback(processed)
    return extracted


def _iter_xbrl_paths(root: Path, entity_ids: Iterable[str] | None) -> Iterable[Path]:
    """Iterate .xbrl files, scoped to entity subdirs when entity_ids is set.

    Without this, an entity-scoped discovery still walks every .xbrl in the
    whole companyfacts tree and filters in Python (filter-after-glob). With
    entity_ids set, we glob each entity dir directly — for an N-entity scope
    this is O(N * files-per-entity) instead of O(total-files).
    """
    if entity_ids:
        for eid in entity_ids:
            entity_dir = root / eid
            if entity_dir.is_dir():
                yield from entity_dir.glob("*.xbrl")
    else:
        yield from root.glob("*/*.xbrl")


def discover_xbrl_files(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    hash_files: bool = True,
    progress_callback: ProgressCallback | None = None,
    progress_interval: int = 1000,
) -> list[EdinetXbrlFile]:
    doc_types = normalize_doc_type_codes(filing_types)
    root = companyfacts_dir()
    if not root.exists():
        raise FileNotFoundError(root)
    wanted = set(entity_ids or [])
    wanted_docs = set(doc_ids or [])
    eligible = _eligible_corporate_edinet_codes()
    candidates: list[tuple[Path, str, str, str, date, date]] = []
    files: list[EdinetXbrlFile] = []
    scanned = 0
    for path in _iter_xbrl_paths(root, entity_ids):
        scanned += 1
        metadata = _metadata_from_name(path)
        if not metadata:
            continue
        doc_id, edinet_code, filing_type, period_end, filed_date = metadata
        if filed_date_max is not None and filed_date > filed_date_max:
            continue
        if edinet_code not in eligible:
            continue
        if wanted and edinet_code not in wanted:
            continue
        if wanted_docs and not any(doc_id.startswith(f"{wanted_doc}_") for wanted_doc in wanted_docs):
            continue
        candidates.append((path, doc_id, edinet_code, filing_type, period_end, filed_date))
        if progress_callback and scanned % max(progress_interval, 1) == 0:
            progress_callback({"phase": "discover", "files": scanned, "rows": len(candidates)})
    package_filing_types = _load_package_filing_types(row[1] for row in candidates)
    for path, doc_id, edinet_code, filing_type, period_end, filed_date in candidates:
        source_doc_id = _source_doc_id_from_filing_id(doc_id)
        resolved_type = package_filing_types.get(source_doc_id, filing_type)
        if doc_types is not None and resolved_type not in doc_types:
            continue
        files.append(
            EdinetXbrlFile(
                path=path,
                doc_id=doc_id,
                edinet_code=edinet_code,
                filing_type=resolved_type,
                period_end=period_end,
                filed_date=filed_date,
                source_hash=_hash_file(path) if hash_files else _fast_file_fingerprint(path),
                cal_path=_companion_path(path, "_cal.xml"),
                def_path=_companion_path(path, "_def.xml"),
                pre_path=_companion_path(path, "_pre.xml"),
                lab_en_path=_companion_path(path, "_lab-en.xml"),
                lab_ja_path=_companion_path(path, "_lab.xml"),
            )
        )
    if progress_callback:
        progress_callback({"phase": "discover", "files": scanned, "rows": len(files)})
    return sorted(files, key=lambda item: (item.edinet_code, item.filed_date, item.doc_id))


def count_xbrl_files_after(
    filed_date_max: date,
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    root = companyfacts_dir()
    if not root.exists():
        raise FileNotFoundError(root)
    wanted_docs = set(doc_ids or [])
    candidates: list[tuple[str, str, date]] = []
    for path in _iter_xbrl_paths(root, entity_ids):
        metadata = _metadata_from_name(path)
        if not metadata:
            continue
        doc_id, _edinet_code, filing_type, _period_end, filed_date = metadata
        if wanted_docs and not any(doc_id.startswith(f"{wanted_doc}_") for wanted_doc in wanted_docs):
            continue
        candidates.append((doc_id, filing_type, filed_date))
    package_filing_types = _load_package_filing_types(row[0] for row in candidates)
    count = 0
    for doc_id, filing_type, filed_date in candidates:
        source_doc_id = _source_doc_id_from_filing_id(doc_id)
        resolved_type = package_filing_types.get(source_doc_id, filing_type)
        if doc_types is not None and resolved_type not in doc_types:
            continue
        if filed_date > filed_date_max:
            count += 1
    return count


def _eligible_corporate_edinet_codes() -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT edinet_code FROM dim_company_jp")
        return {row[0] for row in cur.fetchall()}


def _load_index_state(
    jurisdiction: str,
    entity_ids: list[str] | None,
) -> dict[str, tuple[float | None, str | None]]:
    """Return {filing_id: (source_mtime, source_hash)} for the given entities.

    When entity_ids is None, loads the whole jurisdiction. This is the
    "what do we already know?" snapshot that lets sync_xbrl_index skip
    files whose mtime hasn't changed.
    """
    with connect() as conn, conn.cursor() as cur:
        if entity_ids:
            cur.execute(
                """
                SELECT filing_id, source_mtime, source_hash
                  FROM source_filing_state
                 WHERE jurisdiction = %s
                   AND entity_id = ANY(%s)
                """,
                (jurisdiction, list(entity_ids)),
            )
        else:
            cur.execute(
                """
                SELECT filing_id, source_mtime, source_hash
                  FROM source_filing_state
                 WHERE jurisdiction = %s
                """,
                (jurisdiction,),
            )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


# Tolerance for matching a file's on-disk mtime against the indexed mtime.
# NTFS resolution is 100ns; floating-point round-trip via PostgreSQL DOUBLE
# PRECISION can introduce sub-microsecond drift. 10ms is generous and still
# tight enough that any real file rewrite (which bumps mtime to "now") is
# always detected.
_MTIME_MATCH_EPSILON = 0.01


def sync_xbrl_index(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    progress_callback: ProgressCallback | None = None,
    force_resync: bool = False,
) -> int:
    """Incrementally reconcile companyfacts/*.xbrl into source_filing_state.

    Two layers of short-circuit, both controlled by `force_resync`:

    1. Per-directory: an unscoped invocation consults `.sync_marker_jp` and
       only walks entity subdirs whose mtime is newer than the marker. If
       none changed, the whole call costs ~4ms.

    2. Per-file: within walked dirs, every candidate is compared against the
       stored `source_mtime` for its filing_id. Files whose mtime matches the
       DB are skipped without being opened or hashed. Only new or changed
       files pay the hash cost.

    The two layers compose: a 3-firm update drop costs a 4ms scandir + a
    handful of single-file hashes + one batched upsert.
    """
    doc_types = normalize_doc_type_codes(filing_types)
    user_scoped = bool(entity_ids) or bool(doc_ids) or bool(doc_types)
    scoped_entities = entity_ids
    if not user_scoped and not force_resync:
        marker_time = _read_sync_marker()
        if marker_time is not None:
            changed = _entity_dirs_changed_since(marker_time)
            if changed is not None:
                if not changed:
                    if progress_callback:
                        progress_callback({"phase": "skipped-fresh", "files": 0, "rows": 0})
                    return 0
                scoped_entities = changed

    sync_start = time.time()
    root = companyfacts_dir()
    if not root.exists():
        raise FileNotFoundError(root)
    eligible = _eligible_corporate_edinet_codes()
    wanted_docs = set(doc_ids or [])

    # Phase 1: cheap walk — collect (path, mtime, metadata) without hashing.
    scanned: list[tuple[Path, float, str, str, str, date, date]] = []
    scanned_count = 0
    for path in _iter_xbrl_paths(root, scoped_entities):
        scanned_count += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        metadata = _metadata_from_name(path)
        if not metadata:
            continue
        doc_id, edinet_code, filing_type, period_end, filed_date = metadata
        if filed_date_max is not None and filed_date > filed_date_max:
            continue
        if edinet_code not in eligible:
            continue
        if wanted_docs and not any(doc_id.startswith(f"{wd}_") for wd in wanted_docs):
            continue
        scanned.append((path, mtime, doc_id, edinet_code, filing_type, period_end, filed_date))
        if progress_callback and scanned_count % 1000 == 0:
            progress_callback({"phase": "discover", "files": scanned_count, "rows": len(scanned)})

    if progress_callback:
        progress_callback({"phase": "discover", "files": scanned_count, "rows": len(scanned)})

    if not scanned:
        if progress_callback:
            progress_callback({"phase": "upsert", "files": scanned_count, "rows": 0})
        if not user_scoped and not filed_date_max:
            _write_sync_marker(sync_start)
        return 0

    package_filing_types = _load_package_filing_types(row[2] for row in scanned)
    resolved_scanned: list[tuple[Path, float, str, str, str, date, date]] = []
    for path, mtime, doc_id, edinet_code, filing_type, period_end, filed_date in scanned:
        source_doc_id = _source_doc_id_from_filing_id(doc_id)
        resolved_type = package_filing_types.get(source_doc_id, filing_type)
        if doc_types is not None and resolved_type not in doc_types:
            continue
        resolved_scanned.append((path, mtime, doc_id, edinet_code, resolved_type, period_end, filed_date))
    scanned = resolved_scanned

    if not scanned:
        if progress_callback:
            progress_callback({"phase": "upsert", "files": scanned_count, "rows": 0})
        if not user_scoped and not filed_date_max:
            _write_sync_marker(sync_start)
        return 0

    # Phase 2: load existing per-file state, decide skip vs hash-and-upsert.
    # When we narrowed to a subset of entity dirs (either via user scope or
    # the dir-mtime gate), restrict the load to those. Otherwise load the
    # whole jurisdiction — at JP scale (~313k rows) this is still cheap.
    load_scope = list(scoped_entities) if scoped_entities is not None else None
    existing = _load_index_state("JP", load_scope)

    to_upsert: list[tuple] = []
    skipped = 0
    for path, mtime, doc_id, edinet_code, filing_type, period_end, filed_date in scanned:
        existing_entry = existing.get(doc_id)
        if existing_entry is not None:
            ex_mtime, _ex_hash = existing_entry
            if ex_mtime is not None and abs(ex_mtime - mtime) < _MTIME_MATCH_EPSILON:
                skipped += 1
                continue
        # New file OR mtime drifted → hash and upsert.
        source_hash = _hash_file(path)
        to_upsert.append((
            "JP", doc_id, edinet_code, filing_type, filed_date, period_end,
            str(path), source_hash, mtime, True, True, "instance",
        ))
        if progress_callback and len(to_upsert) % 1000 == 0:
            progress_callback({"phase": "hash", "files": scanned_count, "rows": len(to_upsert)})

    if progress_callback:
        progress_callback({"phase": "hash", "files": scanned_count, "rows": len(to_upsert)})

    if not to_upsert:
        if progress_callback:
            progress_callback({"phase": "upsert", "files": scanned_count, "rows": 0, "skipped": skipped})
        if not user_scoped and not filed_date_max:
            _write_sync_marker(sync_start)
        return 0

    sql = """
        INSERT INTO source_filing_state
            (jurisdiction, filing_id, entity_id, filing_type, filed_date, period_end,
             source_path, source_hash, source_mtime, downloaded, extracted, source_kind)
        VALUES %s
        ON CONFLICT (jurisdiction, filing_id) DO UPDATE SET
            entity_id = EXCLUDED.entity_id,
            filing_type = EXCLUDED.filing_type,
            filed_date = EXCLUDED.filed_date,
            period_end = EXCLUDED.period_end,
            source_path = EXCLUDED.source_path,
            source_hash = EXCLUDED.source_hash,
            source_mtime = EXCLUDED.source_mtime,
            downloaded = true,
            extracted = true,
            source_kind = 'instance',
            parsed = CASE
                WHEN source_filing_state.source_hash IS DISTINCT FROM EXCLUDED.source_hash THEN false
                ELSE source_filing_state.parsed
            END,
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, sql, to_upsert, page_size=5000)

    if progress_callback:
        progress_callback({"phase": "upsert", "files": scanned_count, "rows": written, "skipped": skipped})
    if not user_scoped and not filed_date_max:
        _write_sync_marker(sync_start)
    return written


def changed_or_unparsed_xbrl(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    hash_files: bool = True,  # back-compat shim; no longer drives behavior
    progress_callback: ProgressCallback | None = None,
) -> list[EdinetXbrlFile]:
    """Return JP filing candidates that need (re-)parsing.

    Reads source_filing_state directly instead of re-walking and re-hashing
    the entire companyfacts/ tree. Assumes sync_xbrl_index has been called
    upstream and has the current on-disk hash + mtime for every file
    (the normal run_incremental flow guarantees this).

    Semantics preserved from the previous filesystem-scan implementation:

    - A file is a candidate iff its row has parsed=false. sync_xbrl_index's
      upsert flips parsed back to false on any hash change, so "parsed=false"
      already means "changed since last parse OR never parsed".
    - force=True returns every indexed instance (parsed or not).
    - Filters by entity, doc, filed_date_max, and eligibility against
      dim_company_jp are honored. doc_ids stay as filename prefixes.

    Cost: one indexed SQL query + one stat() per candidate, replacing the
    previous O(all on-disk .xbrl) walk that hashed every file.

    `hash_files` is accepted for call-site compatibility but no longer affects
    behavior — hashes come from the index, not from re-reading files here.
    """
    del hash_files  # silence the lint; param kept for caller back-compat

    where = [
        "s.jurisdiction = 'JP'",
        "s.source_kind = 'instance'",
        "s.source_path IS NOT NULL",
    ]
    params: list = []
    doc_types = normalize_doc_type_codes(filing_types)
    if not force:
        where.append("s.parsed = false")
    if entity_ids:
        where.append("s.entity_id = ANY(%s)")
        params.append(list(entity_ids))
    if filed_date_max is not None:
        where.append("s.filed_date <= %s")
        params.append(filed_date_max)
    if doc_types is not None:
        where.append("s.filing_type = ANY(%s)")
        params.append(list(doc_types))
    sql = f"""
        SELECT s.filing_id, s.entity_id, s.filing_type, s.filed_date,
               s.period_end, s.source_path, s.source_hash
          FROM source_filing_state s
          JOIN dim_company_jp d ON d.edinet_code = s.entity_id
         WHERE {' AND '.join(where)}
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    total_indexed = len(rows)
    if progress_callback:
        progress_callback({"phase": "candidate_query", "files": total_indexed, "rows": total_indexed})

    # Optional doc-id prefix filter (matches the old startswith pattern).
    wanted_docs = set(doc_ids or [])

    files: list[EdinetXbrlFile] = []
    missing = 0
    for filing_id, entity_id, filing_type, filed_date, period_end, source_path, source_hash in rows:
        if wanted_docs and not any(filing_id.startswith(f"{wd}_") for wd in wanted_docs):
            continue
        path = Path(source_path)
        if not path.exists():
            missing += 1
            continue
        files.append(
            EdinetXbrlFile(
                path=path,
                doc_id=filing_id,
                edinet_code=entity_id,
                filing_type=filing_type or "",
                period_end=period_end,
                filed_date=filed_date,
                source_hash=source_hash or "",
                cal_path=_companion_path(path, "_cal.xml"),
                def_path=_companion_path(path, "_def.xml"),
                pre_path=_companion_path(path, "_pre.xml"),
                lab_en_path=_companion_path(path, "_lab-en.xml"),
                lab_ja_path=_companion_path(path, "_lab.xml"),
            )
        )

    if progress_callback:
        progress_callback(
            {
                "phase": "candidate_filter",
                "files": total_indexed,
                "rows": len(files),
                "missing": missing,
            }
        )
    return sorted(files, key=lambda item: (item.edinet_code, item.filed_date, item.doc_id))
