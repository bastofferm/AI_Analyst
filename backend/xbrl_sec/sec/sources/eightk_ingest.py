"""SEC Form 8-K index + body downloader.

Two phases:

1. **Index** — walk the per-CIK submissions JSON files already on disk under
   `market_data/us_sec/submissions/`, extract every row whose `form` is `'8-K'`
   or `'8-K/A'`, and UPSERT into `sec.dim_eightk_filing`. Carries the filing's
   `items` array (e.g. `{'5.03','8.01'}`) so downstream parsers can pre-filter.
   No new network requests — purely reads files we already have.

2. **Download bodies** — for filings whose `local_path IS NULL`, fetch the
   primary document HTML from EDGAR
   (`https://www.sec.gov/Archives/edgar/data/<cik_int>/<accession_nodash>/<primary>`),
   store under `market_data/us_sec/eightk/<cik>/<accession>.htm`, and stamp
   `downloaded_at`. SEC fair-use throttle: 10 req/s (sleep 0.11s between requests).

CLI:
    python -m xbrl_sec.sec.sources.eightk_ingest --index           # index only
    python -m xbrl_sec.sec.sources.eightk_ingest --download        # body fetch only
    python -m xbrl_sec.sec.sources.eightk_ingest --index --download
    python -m xbrl_sec.sec.sources.eightk_ingest --download --limit 1000 [--items 5.03 8.01]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.sec_filings import normalize_cik
from xbrl_sec.sec.sources.sec_forms import (
    CORPORATE_ACTION_FORMS,
    normalize_form,
)


logger = logging.getLogger(__name__)

_USER_AGENT = "MZQA XBRL pipeline contact=bastian.offermann@gmail.com"
_THROTTLE_SECONDS = 0.105          # ~9.5 req/s, just under SEC's 10 req/s ceiling


# ---------------------------------------------------------------------------
# Global rate gate
#
# Concurrency only buys throughput if we still respect SEC's 10 req/s ceiling
# in AGGREGATE. A simple time-stamped lock acts as a token-bucket of size 1:
# each fetcher acquires the lock, waits long enough since the previous fetch,
# then releases. With N workers and gap=0.11s, the steady-state rate stays at
# 1/gap ≈ 9 req/s regardless of N.
# ---------------------------------------------------------------------------

_RATE_GATE_LOCK = threading.Lock()
_RATE_GATE_LAST: list[float] = [0.0]  # mutable container for thread-shared state


def _rate_gate(gap: float = _THROTTLE_SECONDS) -> None:
    """Block the caller until at least `gap` seconds have passed since the
    previous gate exit. Thread-safe."""
    with _RATE_GATE_LOCK:
        now = time.monotonic()
        wait = _RATE_GATE_LAST[0] + gap - now
        if wait > 0:
            time.sleep(wait)
            _RATE_GATE_LAST[0] = time.monotonic()
        else:
            _RATE_GATE_LAST[0] = now


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _submissions_dir() -> Path:
    return load_settings().market_data_root / "us_sec" / "submissions"


def _eightk_dir() -> Path:
    p = load_settings().eightk_root
    p.mkdir(parents=True, exist_ok=True)
    return p


def _eightk_cik_dir(cik: str) -> Path:
    p = _eightk_dir() / f"CIK{normalize_cik(cik)}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _local_path(cik: str, accession: str) -> str:
    r"""Return the absolute path string for a downloaded 8-K body.

    Absolute paths are stored in `dim_eightk_filing.local_path` because the
    eightk_root can differ from market_data_root (e.g. cold storage on E:\).
    """
    return str(_eightk_cik_dir(cik) / f"{accession}.htm")


# ---------------------------------------------------------------------------
# Submission JSON walking
# ---------------------------------------------------------------------------

import re as _re
_CIK_JSON_RE = _re.compile(r"^CIK\d{10}\.json$")


def _iter_submission_files() -> Iterator[Path]:
    """Yield top-level CIK<10-digit>.json files only.

    Paged-history files (named CIK<digits>-submissions-<n>.json) have a
    different schema (no top-level `cik` field) and cover only older
    filings — for the 8-K indexer that's an acceptable miss in v1.
    """
    root = _submissions_dir()
    if not root.exists():
        return
    for p in root.iterdir():
        if p.is_file() and _CIK_JSON_RE.match(p.name):
            yield p


def _submission_array(payload: dict[str, Any], key: str) -> list[Any]:
    filings = payload.get("filings") if "filings" in payload else payload
    recent = filings.get("recent") if isinstance(filings, dict) else None
    source = recent if isinstance(recent, dict) else payload
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, list) else []


def _cik_from_payload(payload: dict[str, Any]) -> str | None:
    raw = payload.get("cik")
    if not raw:
        return None
    try:
        return normalize_cik(str(raw))
    except Exception:
        return None


def _parse_items(raw: str | None) -> list[str]:
    if not raw:
        return []
    # Items in submissions feed are comma-separated like "2.02,9.01"
    parts = [p.strip() for p in str(raw).split(",")]
    return [p for p in parts if p]


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Index phase: read submissions → dim_eightk_filing
# ---------------------------------------------------------------------------

_UPSERT_INDEX_SQL = """
INSERT INTO sec.dim_eightk_filing
    (cik, accession, filed_date, period_of_report, items,
     primary_doc, raw_url, local_path, downloaded_at, parsed_at)
VALUES %s
ON CONFLICT (cik, accession) DO UPDATE SET
    filed_date       = EXCLUDED.filed_date,
    period_of_report = EXCLUDED.period_of_report,
    items            = COALESCE(EXCLUDED.items, dim_eightk_filing.items),
    primary_doc      = EXCLUDED.primary_doc,
    raw_url          = EXCLUDED.raw_url,
    updated_at       = now()
"""


def _eightk_rows_from_payload(cik: str, payload: dict[str, Any]) -> list[tuple]:
    accessions    = _submission_array(payload, "accessionNumber")
    forms         = _submission_array(payload, "form")
    filed_dates   = _submission_array(payload, "filingDate")
    report_dates  = _submission_array(payload, "reportDate")
    items_strs    = _submission_array(payload, "items")
    primary_docs  = _submission_array(payload, "primaryDocument")

    rows: list[tuple] = []
    for idx, raw_acc in enumerate(accessions):
        accession = str(raw_acc or "").strip()
        if not accession:
            continue
        form = normalize_form(forms[idx] if idx < len(forms) else None)
        if form not in CORPORATE_ACTION_FORMS:
            continue
        filed   = _date(filed_dates[idx] if idx < len(filed_dates) else None)
        if filed is None:
            continue
        period  = _date(report_dates[idx] if idx < len(report_dates) else None)
        items   = _parse_items(items_strs[idx] if idx < len(items_strs) else None) or None
        primary = (primary_docs[idx] if idx < len(primary_docs) else None) or None

        acc_nodash = accession.replace("-", "")
        raw_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{primary}"
            if primary else
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K"
        )

        rows.append((
            cik, accession, filed, period, items,
            primary, raw_url,
            None,  # local_path — set after body download
            None,  # downloaded_at
            None,  # parsed_at
        ))
    return rows


def _scoped_ciks(scope: str = "dim_company") -> set[str] | None:
    """Return the CIK set to index. `scope`:
      * 'all'         → None (index every CIK<digits>.json on disk; very long)
      * 'dim_company' → CIKs present in sec.dim_company_us (default; ~5k)
      * 'pipeline'    → CIKs flagged include_in_pipeline=true (~500)
    """
    if scope == "all":
        return None
    where = "cik IS NOT NULL"
    if scope == "pipeline":
        where += " AND COALESCE(include_in_pipeline, true) = true"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT cik FROM sec.dim_company_us WHERE {where}")
        return {normalize_cik(r[0]) for r in cur.fetchall()}


def index_all(scope: str = "dim_company") -> dict[str, int]:
    """Walk submission JSONs and UPSERT 8-K rows.

    Scope defaults to CIKs present in sec.dim_company_us — ~5k filings rather
    than the ~930k submission JSONs that include every inactive entity SEC has
    ever issued a CIK to.
    """
    scoped = _scoped_ciks(scope)
    all_files = list(_iter_submission_files())
    if scoped is None:
        files = all_files
    else:
        files = [p for p in all_files if normalize_cik(p.stem[3:]) in scoped]
    logger.info("scope=%s: walking %d submission files (of %d on disk)",
                scope, len(files), len(all_files))

    batch: list[tuple] = []
    files_seen = 0
    rows_total = 0
    with connect() as conn, conn.cursor() as cur:
        for path in files:
            files_seen += 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("skip %s: %s", path.name, exc)
                continue
            cik = _cik_from_payload(payload)
            if cik is None:
                try:
                    cik = normalize_cik(path.stem[3:])
                except Exception:
                    continue
            batch.extend(_eightk_rows_from_payload(cik, payload))

            if len(batch) >= 2000:
                execute_values(cur, _UPSERT_INDEX_SQL, batch, page_size=1000)
                rows_total += len(batch)
                batch.clear()
                if files_seen % 500 == 0:
                    logger.info("indexed %d files, %d 8-K rows so far", files_seen, rows_total)

        if batch:
            execute_values(cur, _UPSERT_INDEX_SQL, batch, page_size=1000)
            rows_total += len(batch)

    logger.info("index done: scope=%s files=%d 8-K rows=%d", scope, files_seen, rows_total)
    return {"files_seen": files_seen, "rows_written": rows_total}


# ---------------------------------------------------------------------------
# Body download phase
# ---------------------------------------------------------------------------

def _fetch_body(url: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.info().get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                logger.debug("HTTP %s for %s (non-retry)", exc.code, url)
                return None
            time.sleep(0.5 * (attempt + 1))
        except (URLError, TimeoutError) as exc:
            logger.debug("fetch error for %s: %s", url, exc)
            time.sleep(0.5 * (attempt + 1))
    return None


_FETCH_BATCH_SQL = """
SELECT cik, accession, raw_url
FROM   sec.dim_eightk_filing
WHERE  downloaded_at IS NULL
  AND  raw_url IS NOT NULL
  AND  filed_date >= %s
  {item_filter}
ORDER  BY filed_date DESC
LIMIT  %s
"""


_FLUSH_SQL = """
UPDATE sec.dim_eightk_filing AS d
SET    local_path      = v.local_path,
       file_size_bytes = v.file_size_bytes,
       downloaded_at   = now(),
       updated_at      = now()
FROM (VALUES %s) AS v(local_path, file_size_bytes, cik, accession)
WHERE  d.cik = v.cik AND d.accession = v.accession
"""


def _flush_updates(updates: list[tuple]) -> None:
    if not updates:
        return
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, _FLUSH_SQL, updates, page_size=500)


def _fetch_and_write(cik: str, accession: str, url: str) -> tuple | None:
    """Worker function — acquires the global rate gate, downloads + writes.

    Returns the DB-update tuple on success, or None on failure. Safe to call
    from multiple threads concurrently.
    """
    _rate_gate()
    data = _fetch_body(url)
    if data is None:
        return None
    try:
        local_dir = _eightk_cik_dir(cik)
        local_file = local_dir / f"{accession}.htm"
        local_file.write_bytes(data)
        return (_local_path(cik, accession), len(data), cik, accession)
    except OSError as exc:
        logger.warning("write failed for %s: %s", accession, exc)
        return None


def download_pending_bodies(
    limit: int = 1000,
    items_filter: list[str] | None = None,
    since_date: str = "2008-01-01",
    commit_every: int = 100,
    workers: int = 6,
) -> dict[str, int]:
    """Fetch up to `limit` undownloaded 8-K bodies in parallel.

    `since_date` filters to filings with `filed_date >= <date>` (ISO format).
    Defaults to 2008-01-01.

    `commit_every` flushes DB updates every N completed downloads — durable
    against crashes.

    `workers` is the size of the fetch thread pool. The global rate gate
    (`_rate_gate`) ensures combined throughput stays under SEC's 10 req/s
    policy regardless of the worker count. With workers=6 and gap=0.11s,
    steady-state rate is ~9 req/s, ~3× the single-threaded baseline.
    """
    item_filter = ""
    params: list = [since_date, limit]
    if items_filter:
        item_filter = " AND items && %s::text[]"
        params = [since_date, items_filter, limit]
    sql = _FETCH_BATCH_SQL.format(item_filter=item_filter)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    if not rows:
        logger.info("no pending 8-K bodies to download (since=%s)", since_date)
        return {"requested": 0, "downloaded": 0, "failed": 0}

    logger.info(
        "downloading %d bodies (since=%s, workers=%d, commit_every=%d)",
        len(rows), since_date, workers, commit_every,
    )

    downloaded = failed = 0
    updates: list[tuple] = []
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eightk") as ex:
        futures = {
            ex.submit(_fetch_and_write, cik, accession, url): (cik, accession)
            for cik, accession, url in rows
        }
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:
                logger.debug("worker exception: %s", exc)
                row = None
            if row is None:
                failed += 1
                continue
            updates.append(row)
            downloaded += 1
            if downloaded % commit_every == 0:
                _flush_updates(updates)
                updates.clear()
                elapsed = time.monotonic() - t_start
                rate = downloaded / elapsed if elapsed > 0 else 0
                eta_min = (len(rows) - downloaded) / rate / 60 if rate > 0 else 0
                logger.info(
                    "downloaded %d / %d  (rate %.1f req/s, eta %.1f min)",
                    downloaded, len(rows), rate, eta_min,
                )

    _flush_updates(updates)

    logger.info(
        "download done: requested=%d downloaded=%d failed=%d (%.1f min total)",
        len(rows), downloaded, failed, (time.monotonic() - t_start) / 60,
    )
    return {"requested": len(rows), "downloaded": downloaded, "failed": failed}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SEC Form 8-K index + body downloader.")
    parser.add_argument("--index",    action="store_true", help="Walk submissions JSONs and upsert dim_eightk_filing")
    parser.add_argument("--download", action="store_true", help="Fetch undownloaded 8-K bodies")
    parser.add_argument("--scope",    choices=["all", "dim_company", "pipeline"],
                        default="dim_company",
                        help="Which CIKs to index ('all'=~930k files, 'dim_company'=~5k, 'pipeline'=~500)")
    parser.add_argument("--limit",    type=int, default=1000, help="Max bodies to download in this run (default 1000)")
    parser.add_argument("--items",    type=str, nargs="*", default=None,
                        help="Restrict downloads to filings whose items[] overlap this list (e.g. --items 5.03 8.01)")
    parser.add_argument("--since",    type=str, default="2008-01-01",
                        help="Only download 8-Ks with filed_date >= this ISO date (default 2008-01-01)")
    parser.add_argument("--commit-every", type=int, default=100,
                        help="Flush DB updates after every N successful downloads (default 100, lower = more resumable)")
    parser.add_argument("--workers", type=int, default=6,
                        help="Concurrent fetch workers (default 6). Global rate gate keeps combined throughput ≤9 req/s")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not (args.index or args.download):
        parser.error("specify --index and/or --download")

    if args.index:
        index_all(scope=args.scope)
    if args.download:
        download_pending_bodies(
            limit=args.limit,
            items_filter=args.items,
            since_date=args.since,
            commit_every=args.commit_every,
            workers=args.workers,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
