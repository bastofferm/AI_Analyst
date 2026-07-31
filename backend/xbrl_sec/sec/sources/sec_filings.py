"""SEC source-file indexing for copied companyfacts data."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.sec_forms import is_core_fundamental_form, normalize_form


_ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
_QUARTERLY_FORMS = {"10-Q", "10-Q/A"}


@dataclass(frozen=True)
class CompanyFactsFile:
    cik: str
    path: Path
    source_hash: str


def companyfacts_dir() -> Path:
    return load_settings().market_data_root / "us_sec" / "companyfacts"


def normalize_cik(value: Any) -> str:
    return str(value).strip().zfill(10)


ProgressCallback = Callable[[dict[str, int | str]], None]


def iter_companyfacts_files(
    entity_ids: Iterable[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval: int = 500,
) -> list[CompanyFactsFile]:
    root = companyfacts_dir()
    wanted = {normalize_cik(v) for v in entity_ids} if entity_ids else None
    files = []
    for path in sorted(root.glob("CIK*.json")):
        cik = path.stem.replace("CIK", "").zfill(10)
        if wanted and cik not in wanted:
            continue
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        files.append(CompanyFactsFile(cik=cik, path=path, source_hash=digest))
        if progress_callback and len(files) % max(progress_interval, 1) == 0:
            progress_callback({"phase": "hash", "files": len(files), "rows": 0})
    if progress_callback:
        progress_callback({"phase": "hash", "files": len(files), "rows": 0})
    return files


def load_companyfacts(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _duration_days(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def _period_candidate_rank(form: str | None, fp: str | None, start: date | None, end: date | None) -> int:
    if end is None:
        return 0
    raw_fp = str(fp or "").strip().upper()
    days = _duration_days(start, end)
    if form in _ANNUAL_FORMS and raw_fp == "FY" and days is not None and 300 <= days <= 380:
        return 3
    if form in _ANNUAL_FORMS and raw_fp == "FY":
        return 2
    if form in _QUARTERLY_FORMS and raw_fp in {"Q1", "Q2", "Q3"} and days is not None and 70 <= days <= 110:
        return 3
    if form in _QUARTERLY_FORMS and raw_fp in {"Q1", "Q2", "Q3"}:
        return 2
    return 1


def extract_filings(cik: str, payload: dict[str, Any], source_hash: str, source_path: Path) -> list[tuple]:
    filings: dict[str, dict[str, Any]] = {}
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        if not isinstance(concepts, dict):
            continue
        for concept in concepts.values():
            units = concept.get("units") or {}
            for facts in units.values():
                if not isinstance(facts, list):
                    continue
                for fact in facts:
                    accn = fact.get("accn") or fact.get("accession")
                    if not accn:
                        continue
                    current = filings.setdefault(
                        accn,
                        {
                            "forms": set(),
                            "filed_date": None,
                            "period_end": None,
                            "period_rank": 0,
                            "source_hash": source_hash,
                        },
                    )
                    form = normalize_form(fact.get("form"))
                    if not is_core_fundamental_form(form):
                        continue
                    if form:
                        current["forms"].add(form)
                    filed = _parse_date(fact.get("filed"))
                    start = _parse_date(fact.get("start"))
                    end = _parse_date(fact.get("end"))
                    if filed and (current["filed_date"] is None or filed > current["filed_date"]):
                        current["filed_date"] = filed
                    period_rank = _period_candidate_rank(form, fact.get("fp"), start, end)
                    if end and (
                        period_rank > current["period_rank"]
                        or (
                            period_rank == current["period_rank"]
                            and (current["period_end"] is None or end > current["period_end"])
                        )
                    ):
                        current["period_end"] = end
                        current["period_rank"] = period_rank
    rows = []
    for accn, rec in filings.items():
        forms = sorted(rec["forms"])
        rows.append((
            "US", accn, cik, ",".join(forms) if forms else None,
            rec["filed_date"], rec["period_end"], source_hash,
            True, True, False, str(source_path), json.dumps({"forms": forms}), "companyfacts",
        ))
    return rows


def _filing_row_rank(row: tuple) -> tuple:
    filing_id = row[1] or ""
    cik = row[2] or ""
    accession_owner = filing_id.split("-", 1)[0].zfill(10) if filing_id else ""
    matches_accession_owner = cik == accession_owner
    has_filing_type = row[3] is not None
    has_filed_date = row[4] is not None
    has_period_end = row[5] is not None
    return (
        matches_accession_owner,
        has_filing_type,
        has_filed_date,
        has_period_end,
        cik,
    )


def dedupe_filing_rows(rows: Iterable[tuple]) -> list[tuple]:
    by_filing: dict[tuple[str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[1])
        current = by_filing.get(key)
        if current is None or _filing_row_rank(row) > _filing_row_rank(current):
            by_filing[key] = row
    return list(by_filing.values())


def sync_companyfacts_index(
    entity_ids: Iterable[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval: int = 250,
) -> int:
    rows = []
    files = iter_companyfacts_files(
        entity_ids,
        progress_callback=progress_callback,
        progress_interval=progress_interval,
    )
    for index, item in enumerate(files, start=1):
        payload = load_companyfacts(item.path)
        rows.extend(extract_filings(item.cik, payload, item.source_hash, item.path))
        if progress_callback and index % max(progress_interval, 1) == 0:
            progress_callback({"phase": "index", "files": index, "rows": len(rows)})
    rows = dedupe_filing_rows(rows)
    if progress_callback:
        progress_callback({"phase": "dedupe", "files": len(files), "rows": len(rows)})
    sql = """
        INSERT INTO source_filing_state
            (jurisdiction, filing_id, entity_id, filing_type, filed_date, period_end,
             source_hash, downloaded, extracted, parsed, source_path, raw_payload, source_kind)
        VALUES %s
        ON CONFLICT (jurisdiction, filing_id) DO UPDATE SET
            entity_id = EXCLUDED.entity_id,
            filing_type = COALESCE(EXCLUDED.filing_type, source_filing_state.filing_type),
            filed_date = COALESCE(EXCLUDED.filed_date, source_filing_state.filed_date),
            period_end = COALESCE(EXCLUDED.period_end, source_filing_state.period_end),
            source_hash = EXCLUDED.source_hash,
            downloaded = EXCLUDED.downloaded,
            extracted = EXCLUDED.extracted,
            source_path = EXCLUDED.source_path,
            raw_payload = EXCLUDED.raw_payload,
            source_kind = 'companyfacts',
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, sql, rows, page_size=5000)
    if progress_callback:
        progress_callback({"phase": "upsert", "files": len(files), "rows": written})
    return written


def changed_or_unparsed_files(
    entity_ids: Iterable[str] | None = None,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> list[CompanyFactsFile]:
    files = iter_companyfacts_files(entity_ids, progress_callback=progress_callback)
    if force:
        if progress_callback:
            progress_callback({"phase": "candidate_filter", "files": len(files), "rows": len(files)})
        return files
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, source_hash
            FROM pipeline_entity_state
            WHERE jurisdiction='US' AND stage='raw_parse' AND status='succeeded'
            """
        )
        current = {row[0]: row[1] for row in cur.fetchall()}
    changed = [f for f in files if current.get(f.cik) != f.source_hash]
    if progress_callback:
        progress_callback({"phase": "candidate_filter", "files": len(files), "rows": len(changed)})
    return changed
