"""
US SEC institutional filings downloader — 13F, 13D, 13G.

Mirrors the sec_download.py pattern: download from SEC EDGAR / structured data,
store raw files on disk under MZQA/market_data/us_sec/institutional/.

13F-HR / 13F-NT: Quarterly ZIP data sets published by SEC at
    https://www.sec.gov/files/structureddata/data/form-13f-data-sets/
    One ZIP per quarter containing TSV files for all filing managers.

SC 13D / SC 13G: Individual filings accessed via SEC EDGAR full-text search API
    and submissions API. Downloaded as primary documents (HTML/XML) per CIK.

Usage (CLI):
    python -m xbrl_sec.sec.cli download-institutional 13f  --quarters 2013Q1-2026Q1
    python -m xbrl_sec.sec.cli download-institutional 13dg --cik 0000320193
    python -m xbrl_sec.sec.cli download-institutional 13dg --all-tracked
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import zipfile
import io
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xbrl_sec.sec.settings import load_settings

_USER_AGENT = "MZQA XBRL pipeline contact=bastian.offermann@gmail.com"

# ── URL patterns ──────────────────────────────────────────────────────────────
_SEC_13F_QUARTERLY_URL = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "{quarter}_form13f.zip"
)
_SEC_EDGAR_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&category=form-cat1&startdt={start_date}&enddt={end_date}&forms={form_type}&from={offset}&pageSize={page_size}"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_document}"

# ── 13F quarters to download ─────────────────────────────────────────────────
# SEC publishes structured 13F data sets from 2013Q1 onwards.
# Pre-2013 data exists in legacy format (bulk FTP archives) — not covered here.

_START_YEAR = 2013
_END_YEAR = 2026  # Current year as of spec date

# ── 13D/13G form type codes for SEC EDGAR full-text search ───────────────────
_FORM_13D = "SC 13D"
_FORM_13G = "SC 13G"


def _institutional_dir() -> Path:
    """Root for all institutional filing data on disk."""
    path = load_settings().market_data_root / "us_sec" / "institutional"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _13f_quarterly_dir() -> Path:
    """Where quarterly 13F ZIP files are stored."""
    path = _institutional_dir() / "13f"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _13dg_dir() -> Path:
    """Where individual 13D/13G filing documents are stored."""
    path = _institutional_dir() / "13dg"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_bytes(url: str, retries: int = 3, delay: float = 0.2) -> bytes:
    """Download raw bytes from a URL with retry logic."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept-Encoding": "identity",
                },
            )
            with urlopen(req, timeout=120) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(
        f"GET failed after {retries} attempts: {url}: {last_error}"
    )


def _get_json(url: str, retries: int = 3, delay: float = 0.2) -> dict[str, Any]:
    """Download JSON from a URL with retry logic."""
    data = _get_bytes(url, retries=retries, delay=delay)
    return json.loads(data.decode("utf-8"))


# ═══════════════════════════════════════════════════════════════════════════════
# 13F QUARTERLY DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def _iter_quarters(start_year: int, end_year: int) -> list[str]:
    """Generate list of quarter labels like '2013Q1', '2013Q2', ..."""
    quarters = []
    for year in range(start_year, end_year + 1):
        for qtr in range(1, 5):
            quarters.append(f"{year}Q{qtr}")
    return quarters


def _quarterly_zip_path(quarter: str) -> Path:
    """Filesystem path for a quarterly 13F ZIP."""
    return _13f_quarterly_dir() / f"{quarter.lower()}_form13f.zip"


def _quarterly_extract_dir(quarter: str) -> Path:
    """Directory where extracted TSV files for a quarter live."""
    path = _13f_quarterly_dir() / quarter
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_13f_quarter(quarter: str, force: bool = False) -> tuple[Path, int, str | None]:
    """Download a single 13F quarterly ZIP.

    Returns:
        (zip_path, file_count, error_message_or_None)
    """
    zip_path = _quarterly_zip_path(quarter)
    extract_dir = _quarterly_extract_dir(quarter)

    if zip_path.exists() and not force:
        # ZIP already downloaded — check if extracted
        existing_files = list(extract_dir.glob("*.tsv"))
        if existing_files:
            return zip_path, len(existing_files), None
        # ZIP exists but not extracted — extract now
        try:
            _extract_13f_zip(zip_path, extract_dir)
            return zip_path, len(list(extract_dir.glob("*.tsv"))), None
        except Exception as exc:
            return zip_path, 0, str(exc)

    url = _SEC_13F_QUARTERLY_URL.format(quarter=quarter.lower())

    try:
        data = _get_bytes(url, retries=5, delay=1.0)
    except HTTPError as exc:
        if exc.code == 404:
            return zip_path, 0, f"404 — quarter {quarter} not yet available"
        return zip_path, 0, f"HTTP {exc.code}"
    except Exception as exc:
        return zip_path, 0, str(exc)

    zip_path.write_bytes(data)
    _extract_13f_zip(zip_path, extract_dir)

    file_count = len(list(extract_dir.glob("*.tsv")))
    time.sleep(0.15)  # Respect SEC rate limit
    return zip_path, file_count, None


def _extract_13f_zip(zip_path: Path, extract_dir: Path) -> None:
    """Extract a 13F quarterly ZIP into the quarter directory."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            # Only extract TSV and INFOTABLE files — skip README, schema, etc.
            if member.endswith(".tsv") or "INFOTABLE" in member.upper():
                target = extract_dir / Path(member).name
                if not target.exists():
                    target.write_bytes(zf.read(member))


def download_13f_all_quarters(
    start_quarter: str = "2013Q1",
    end_quarter: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download all 13F quarterly data sets in the given range.

    Args:
        start_quarter: e.g. '2013Q1'
        end_quarter: e.g. '2026Q1'. If None, auto-detects latest available.
        force: Re-download even if ZIP already exists.

    Returns:
        Summary dict with success/failure counts and per-quarter details.
    """
    all_quarters = _iter_quarters(_START_YEAR, _END_YEAR)

    # Filter to requested range
    if start_quarter in all_quarters:
        all_quarters = all_quarters[all_quarters.index(start_quarter):]
    if end_quarter and end_quarter in all_quarters:
        all_quarters = all_quarters[:all_quarters.index(end_quarter) + 1]

    results = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "quarters_attempted": len(all_quarters),
        "quarters_succeeded": 0,
        "quarters_failed": 0,
        "total_tsv_files": 0,
        "details": [],
    }

    for quarter in all_quarters:
        zip_path, file_count, error = download_13f_quarter(quarter, force=force)
        detail = {
            "quarter": quarter,
            "zip_path": str(zip_path),
            "tsv_files": file_count,
            "error": error,
        }
        results["details"].append(detail)
        if error:
            results["quarters_failed"] += 1
            # 404 on recent quarters is expected — stop here
            if "404" in error:
                print(f"  {quarter}: not yet published (404) — stopping")
                break
        else:
            results["quarters_succeeded"] += 1
            results["total_tsv_files"] += file_count
            print(f"  {quarter}: {file_count} TSV files")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 13D / 13G FILING DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_cik(cik: str | int) -> str:
    """Normalize CIK to 10-digit zero-padded string."""
    return str(int(str(cik).lstrip("0"))).zfill(10)


def _13dg_filing_dir(cik: str, form_type: str) -> Path:
    """Directory for a specific CIK's 13D or 13G filings."""
    cik_norm = normalize_cik(cik)
    form_dir = "13D" if "13D" in form_type.upper() else "13G"
    path = _13dg_dir() / form_dir / cik_norm
    path.mkdir(parents=True, exist_ok=True)
    return path


def _download_archive_document(
    cik: str, accession_number: str, primary_document: str, target_dir: Path
) -> Path | None:
    """Download a single filing document from SEC EDGAR archive.

    The accession number in SEC submissions has dashes (e.g. '0000320193-24-000123').
    For the archive URL, dashes must be removed.
    """
    accession_no_dashes = accession_number.replace("-", "")
    cik_stripped = cik.lstrip("0")
    url = _SEC_ARCHIVE_URL.format(
        cik=cik_stripped,
        accession_no_dashes=accession_no_dashes,
        primary_document=primary_document,
    )

    # Filename: accession_number + document name
    safe_name = primary_document.replace("/", "_")
    target_path = target_dir / f"{accession_number}_{safe_name}"

    if target_path.exists():
        return target_path

    try:
        data = _get_bytes(url, retries=3, delay=0.5)
        target_path.write_bytes(data)
        time.sleep(0.12)
        return target_path
    except HTTPError as exc:
        if exc.code == 404:
            # Try alternate URL without the CIK path (some old filings)
            alt_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{accession_no_dashes}/{primary_document}"
            )
            try:
                data = _get_bytes(alt_url, retries=2, delay=0.5)
                target_path.write_bytes(data)
                time.sleep(0.12)
                return target_path
            except Exception:
                return None
        return None
    except Exception:
        return None


def download_13dg_filings_for_cik(
    cik: str | int,
    form_types: tuple[str, ...] = ("SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"),
    start_date: str = "2013-01-01",
    end_date: str | None = None,
    max_filings: int = 500,
) -> dict[str, Any]:
    """Download all 13D/13G filings for a given CIK using SEC EDGAR submissions API.

    The submissions API provides a filing history for the CIK. We filter to
    13D/13G form types and download each primary document.

    Args:
        cik: Company CIK number.
        form_types: SEC form types to download.
        start_date: Earliest filing date (YYYY-MM-DD).
        end_date: Latest filing date. None = today.
        max_filings: Maximum number of filings to download.

    Returns:
        Summary dict with download counts.
    """
    cik_norm = normalize_cik(cik)
    end_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Fetch submissions history
    submissions_url = _SEC_SUBMISSIONS_URL.format(cik=cik_norm)
    try:
        submissions = _get_json(submissions_url)
    except HTTPError as exc:
        if exc.code == 404:
            return {"cik": cik_norm, "error": "404 — CIK not found", "downloaded": 0}
        raise

    filings = submissions.get("filings", {}).get("recent", {})
    if not filings:
        # Try the older 'files' key
        filings_old = submissions.get("filings", {}).get("files", [])
        if not filings_old:
            return {"cik": cik_norm, "error": "No filings in submissions", "downloaded": 0}

        # Older format: need to fetch each file's detail
        all_filings = []
        for file_info in filings_old[:5]:  # Limit to recent files
            try:
                file_data = _get_json(
                    f"https://data.sec.gov/submissions/{file_info['name']}"
                )
                all_filings.extend(file_data.get("filings", {}).get("recent", []))
            except Exception:
                continue
        filings_list = all_filings
    else:
        # Modern format: flat arrays
        keys = list(filings.keys())
        if not keys:
            return {"cik": cik_norm, "error": "Empty filings list", "downloaded": 0}

        n = len(filings[keys[0]])
        filings_list = [{k: filings[k][i] for k in keys} for i in range(n)]

    # Filter by form type
    target_forms = {f.upper().replace(" ", "") for f in form_types}
    matched = []
    for f in filings_list:
        form = f.get("form", "").upper().replace(" ", "")
        filing_date = f.get("filingDate", "") or f.get("reportDate", "")
        if form in target_forms and filing_date >= start_date:
            matched.append(f)

    # Limit
    matched = matched[:max_filings]

    # Download each filing
    results = {"cik": cik_norm, "matched": len(matched), "downloaded": 0, "failed": 0, "filings": []}

    for filing in matched:
        form_type = filing.get("form", "")
        accession = filing.get("accessionNumber", "")
        primary_doc = filing.get("primaryDocument", "")
        filing_date = filing.get("filingDate", "")

        if not accession or not primary_doc:
            results["failed"] += 1
            continue

        target_dir = _13dg_filing_dir(cik_norm, form_type)
        path = _download_archive_document(cik_norm, accession, primary_doc, target_dir)

        filing_result = {
            "accession": accession,
            "form": form_type,
            "date": filing_date,
            "primary_document": primary_doc,
            "downloaded": path is not None,
            "path": str(path) if path else None,
        }
        results["filings"].append(filing_result)
        if path:
            results["downloaded"] += 1
        else:
            results["failed"] += 1

    return results


def download_13dg_for_all_tracked(
    form_types: tuple[str, ...] = ("SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"),
    max_per_cik: int = 200,
) -> dict[str, Any]:
    """Download 13D/13G filings for all companies in dim_company_us.

    Requires a database connection to enumerate CIKs.
    """
    from xbrl_sec.sec.db.connection import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cik
            FROM dim_company_us
            WHERE cik IS NOT NULL
              AND include_in_pipeline
            ORDER BY cik
            """
        )
        ciks = [r[0] for r in cur.fetchall()]

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_ciks": len(ciks),
        "total_downloaded": 0,
        "total_failed": 0,
        "cik_results": [],
    }

    for cik in ciks:
        result = download_13dg_filings_for_cik(
            cik, form_types=form_types, max_filings=max_per_cik
        )
        summary["cik_results"].append(result)
        summary["total_downloaded"] += result.get("downloaded", 0)
        summary["total_failed"] += result.get("failed", 0)

        if result.get("downloaded", 0) > 0:
            print(f"  CIK {cik}: {result['downloaded']} filings")

    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS / INDEX
# ═══════════════════════════════════════════════════════════════════════════════

def institutional_status() -> dict[str, Any]:
    """Return summary of what's on disk for institutional filings."""
    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "13f": {},
        "13dg": {},
    }

    # 13F status
    _13f_dir = _13f_quarterly_dir()
    if _13f_dir.exists():
        for quarter_dir in sorted(_13f_dir.iterdir()):
            if quarter_dir.is_dir():
                tsv_count = len(list(quarter_dir.glob("*.tsv")))
                status["13f"][quarter_dir.name] = {
                    "tsv_files": tsv_count,
                    "zip_exists": _quarterly_zip_path(quarter_dir.name).exists(),
                }

    # 13D/13G status
    _13dg = _13dg_dir()
    if _13dg.exists():
        for form_dir in _13dg.iterdir():
            if form_dir.is_dir():
                cik_count = len(list(form_dir.iterdir()))
                total_files = sum(
                    len(list(cik_dir.iterdir()))
                    for cik_dir in form_dir.iterdir()
                    if cik_dir.is_dir()
                )
                status["13dg"][form_dir.name] = {
                    "ciks": cik_count,
                    "total_files": total_files,
                }

    return status


# ═══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINTS (called from xbrl_sec.sec.cli)
# ═══════════════════════════════════════════════════════════════════════════════

def run_download_13f(
    start_quarter: str = "2013Q1",
    end_quarter: str | None = None,
    force: bool = False,
) -> None:
    """CLI entry: download 13F quarterly data sets."""
    print(f"Downloading 13F quarterly data sets: {start_quarter} through {end_quarter or 'latest'}")
    results = download_13f_all_quarters(
        start_quarter=start_quarter,
        end_quarter=end_quarter,
        force=force,
    )
    print(
        f"\nDone. Succeeded: {results['quarters_succeeded']}, "
        f"Failed: {results['quarters_failed']}, "
        f"Total TSV files: {results['total_tsv_files']:,}"
    )

    # Save download log
    log_path = _13f_quarterly_dir() / "download_log.json"
    log_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Log: {log_path}")


def run_download_13dg(cik: str | None = None, all_tracked: bool = False) -> None:
    """CLI entry: download 13D/13G filings."""
    if all_tracked:
        print("Downloading 13D/13G for all tracked companies...")
        results = download_13dg_for_all_tracked()
        print(
            f"\nDone. {results['total_ciks']} companies, "
            f"{results['total_downloaded']} filings downloaded, "
            f"{results['total_failed']} failed."
        )
        log_path = _13dg_dir() / "download_log.json"
        log_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Log: {log_path}")
    elif cik:
        print(f"Downloading 13D/13G for CIK {cik}...")
        results = download_13dg_filings_for_cik(cik)
        print(f"  Matched: {results['matched']}, Downloaded: {results['downloaded']}, Failed: {results['failed']}")
        for f in results.get("filings", []):
            status = "✓" if f["downloaded"] else "✗"
            print(f"  {status} {f['date']} {f['form']} {f['accession']}")
    else:
        print("Usage: download-institutional 13dg --cik CIK  OR  --all-tracked")


def run_institutional_status() -> None:
    """CLI entry: show download status."""
    status = institutional_status()
    print(json.dumps(status, indent=2, ensure_ascii=False))
