"""BOJ (Bank of Japan) macro series ingestion.

Strategy: BOJ's official Stat-Search API works for rows identified as
``api:<DB>:<SERIES_CODE>``. We still proxy via FRED for series whose
``native_id`` is prefixed ``fred:`` and keep the legacy direct scraper as a
last-resort fallback for older UI codes.

Series whitelist comes from ``ref_macro_series WHERE source_id='boj'``.

Run:
    python -m xbrl_sec.sec.sources.boj_ingest --full
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date, timedelta
from typing import Any

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

logger = logging.getLogger("mzqa.boj_ingest")


def _fred_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FRED_API_KEY environment variable not set. "
            "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    return key


def _fetch_via_fred(native_id: str, start_date: str, api_key: str) -> list[tuple[date, float]]:
    import urllib.request
    import json

    series_code = native_id.split(":", 1)[1]
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_code}"
        f"&observation_start={start_date}"
        f"&api_key={api_key}"
        f"&file_type=json&sort_order=asc&limit=100000"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    out: list[tuple[date, float]] = []
    for obs in data.get("observations", []):
        v = obs.get("value", ".")
        if v == "." or v is None:
            continue
        try:
            out.append((date.fromisoformat(obs["date"]), float(v)))
        except (ValueError, KeyError):
            continue
    return out


def _api_date(raw: str) -> date | None:
    """Parse BOJ API SURVEY_DATES values into our fact date convention."""
    import calendar

    s = (raw or "").strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{8}", s):
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if re.fullmatch(r"\d{6}", s):
            return date(int(s[:4]), int(s[4:6]), 1)
        m = re.fullmatch(r"(\d{4})Q([1-4])", s, flags=re.IGNORECASE)
        if m:
            y = int(m.group(1))
            month = int(m.group(2)) * 3
            return date(y, month, calendar.monthrange(y, month)[1])
    except ValueError:
        return None
    return None


def _fetch_via_boj_api(native_id: str, start_date: str) -> list[tuple[date, float]]:
    """Fetch a BOJ Stat-Search API series.

    ``native_id`` is ``api:<DB>:<SERIES_CODE>``. The endpoint returns a CSV
    preamble followed by rows keyed by ``SURVEY_DATES`` and ``VALUES``.
    """
    import csv
    import io
    import urllib.parse

    from xbrl_sec.sec.sources import macro_cache

    parts = native_id.split(":")
    if len(parts) != 3 or parts[0] != "api":
        raise ValueError(f"Invalid BOJ API native_id: {native_id}")

    _, db, code = parts
    base = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
    start_param = start_date.replace("-", "")
    if len(start_param) >= 6:
        start_param = start_param[:6]

    rows: list[tuple[date, float]] = []
    next_position = ""
    for _ in range(20):
        params = {
            "format": "csv",
            "lang": "en",
            "db": db,
            "code": code,
            "startDate": start_param,
        }
        if next_position:
            params["startPosition"] = next_position
        url = f"{base}?{urllib.parse.urlencode(params)}"
        cache_key = f"{native_id}:{next_position or 'start'}"
        raw_bytes = macro_cache.fetch("boj_api", cache_key, url, ext="csv", attempts=2)
        if not raw_bytes:
            break

        raw = raw_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(raw))
        header: list[str] | None = None
        idx_date = idx_value = None
        next_position = ""

        for record in reader:
            if not record:
                continue
            if record[0] == "NEXTPOSITION" and len(record) > 1:
                next_position = record[1].strip()
                continue
            if record[0] == "SERIES_CODE":
                header = record
                idx_date = header.index("SURVEY_DATES")
                idx_value = header.index("VALUES")
                continue
            if header is None or idx_date is None or idx_value is None:
                continue
            if len(record) <= max(idx_date, idx_value):
                continue
            value_raw = (record[idx_value] or "").strip()
            if value_raw.lower() in {"", "null", "na", "nd", "..", "-"}:
                continue
            d = _api_date(record[idx_date])
            if d is None:
                continue
            try:
                rows.append((d, float(value_raw.replace(",", ""))))
            except ValueError:
                continue

        if not next_position:
            break

    start_d = date.fromisoformat(start_date)
    return sorted({d: v for d, v in rows if d >= start_d}.items(), key=lambda r: r[0])


# ---------------------------------------------------------------------------
# Tankan snapshot extractor.
#
# BOJ's English statistics site does NOT publish Business Conditions DI as a
# stable historical .csv/.txt — that data only comes out of stat-search,
# which requires a multi-step browser session we don't run. The pragmatic
# workaround: every quarter BOJ releases a `tka<YYMM>.zip` summary at
# /en/statistics/tk/gaiyo/<YYYY>/<filename>. The ZIP contains GA_E1.xlsx;
# its TABLE1 sheet exposes the current-survey DI for Large Enterprises
# (Manufacturing, Non-Manufacturing, All Industries).
#
# Each invocation reads the most recent snapshot and contributes ONE new
# (period_end, value) row per quarter. To backfill history, drop a CSV at
#   D:/macroData/drops/boj/tankan_<component>_history.csv
# in the standard period_end,value format — `_fetch_via_boj_snapshot()`
# merges it with the snapshot reading.
# ---------------------------------------------------------------------------

# TABLE1 row/column layout in GA_E1.xlsx (validated against the March 2026
# release). Row index is 0-based; column index 3 is the current-survey
# Actual Result for Large Enterprises.
_TANKAN_TABLE1_ROW = {
    "lmfg":   18,  # "Manufacturing" row, Large Enterprises Actual = col 3
    "lnmfg":  37,  # "Nonmanufacturing" row, same
    "lall":   50,  # "All industries" row, same
}
_TANKAN_TABLE1_COL = 3  # current-survey Actual Result column for Large Enterprises


def _tankan_quarter_end_from_filename(filename: str) -> date | None:
    """Map ``tka2603.zip`` → date(2026, 3, 31).

    BOJ encodes the **survey quarter end month** in the filename (not the
    release month). ``tka<YY><MM>.zip`` where MM ∈ {03, 06, 09, 12}. The
    output is the last day of that month.
    """
    import calendar
    m = re.fullmatch(r"tka(\d{2})(\d{2})\.zip", filename)
    if not m:
        return None
    yy = 2000 + int(m.group(1))
    mm = int(m.group(2))
    if not (1 <= mm <= 12):
        return None
    return date(yy, mm, calendar.monthrange(yy, mm)[1])


def _latest_tankan_zip_url() -> tuple[str, str] | None:
    """Scrape the Tankan landing for the most recent tka*.zip URL.

    Returns (url, basename) or None if the landing is unreachable.
    """
    from xbrl_sec.sec.sources import macro_cache

    raw = macro_cache.fetch("boj", "tk_landing", "https://www.boj.or.jp/en/statistics/tk/index.htm", ext="html", attempts=2)
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    candidates = re.findall(r'href="(/en/statistics/tk/gaiyo/\d{4}/tka\d{4}\.zip)"', text)
    if not candidates:
        return None
    # The landing always lists the most-recent file. Sort defensively by name.
    latest_path = sorted(set(candidates))[-1]
    basename = latest_path.rsplit("/", 1)[-1]
    return f"https://www.boj.or.jp{latest_path}", basename


def _extract_tankan_di(zip_bytes: bytes, component: str) -> float | None:
    """Open GA_E1.xlsx inside the Tankan ZIP and read the headline DI."""
    import io as _io
    import zipfile

    import pandas as pd

    row = _TANKAN_TABLE1_ROW.get(component)
    if row is None:
        logger.warning("boj_ingest: unknown Tankan component %r", component)
        return None
    try:
        with zipfile.ZipFile(_io.BytesIO(zip_bytes)) as zf:
            xlsx_name = next((n for n in zf.namelist() if n.endswith(".xlsx")), None)
            if not xlsx_name:
                return None
            with zf.open(xlsx_name) as fp:
                df = pd.read_excel(_io.BytesIO(fp.read()), sheet_name="TABLE1", header=None)
        val = df.iat[row, _TANKAN_TABLE1_COL]
        if pd.isna(val):
            return None
        return float(val)
    except Exception as exc:
        logger.warning("boj_ingest: Tankan extraction failed for %s: %s", component, exc)
        return None


def _fetch_via_boj_snapshot(native_id: str, start: str) -> list[tuple[date, float]]:
    """Extract the latest-quarter DI from BOJ's Tankan ZIP, plus any operator backfill drop.

    ``native_id`` is of the form ``snapshot:lmfg|lnmfg|lall``. The operator
    drop file (optional) lives at
    ``D:/macroData/drops/boj/tankan_<component>_history.csv`` and follows
    the canonical ``period_end,value`` format.
    """
    from xbrl_sec.sec.sources import macro_cache

    component = native_id.split(":", 1)[1] if ":" in native_id else ""
    out: list[tuple[date, float]] = []

    # 1) Operator drop for history backfill.
    drop = macro_cache.read_drop("boj", f"tankan_{component}_history")
    if drop:
        import csv as _csv
        import io as _io
        reader = _csv.DictReader(_io.StringIO(drop.decode("utf-8-sig", errors="replace")))
        for r in reader:
            try:
                d = date.fromisoformat((r.get("period_end") or r.get("date") or "").strip())
                v = float((r.get("value") or "").strip())
                out.append((d, v))
            except (ValueError, TypeError):
                continue

    # 2) Latest snapshot from the most recent Tankan ZIP.
    info = _latest_tankan_zip_url()
    if info:
        url, basename = info
        zip_bytes = macro_cache.fetch("boj", f"tankan_{basename.replace('.zip','')}", url, ext="zip", attempts=2)
        if zip_bytes:
            di = _extract_tankan_di(zip_bytes, component)
            d = _tankan_quarter_end_from_filename(basename)
            if di is not None and d is not None:
                out.append((d, di))

    # Dedup (period_end key — keep the latest source: snapshot wins over drop).
    by_date: dict[date, float] = {}
    for d, v in out:
        by_date[d] = v
    start_d = date.fromisoformat(start)
    return sorted(((d, v) for d, v in by_date.items() if d >= start_d), key=lambda r: r[0])


def _fetch_via_boj_direct(native_id: str, start_date: str) -> list[tuple[date, float]]:
    """Direct BOJ stat-search CSV endpoint. Best-effort.

    Routes the download through ``macro_cache.fetch`` so every raw BOJ
    response is archived under ``D:/macroData/raw/boj/<slug>/<ts>.csv`` —
    useful for diagnosing BOJ format changes after the fact. Returns []
    if the endpoint returns the multi-step HTML form (the common case
    for the stat-search workflow which needs a real session) or if the
    download fails.
    """
    import urllib.parse
    import csv
    import io

    from xbrl_sec.sec.sources import macro_cache

    base = "https://www.stat-search.boj.or.jp/ssi/cgi-bin/famecgi2"
    qs = urllib.parse.urlencode(
        {
            "cgi": "$nme_a000_en",
            "hdnYyyyMmIni": start_date.replace("-", "")[:6],
            "hdnYyyyMmEnd": "999912",
            "hdnCode": native_id,
            "rdoOutput": "CSV",
        }
    )
    url = f"{base}?{qs}"
    raw_bytes = macro_cache.fetch("boj", native_id, url, ext="csv", attempts=2)
    if not raw_bytes:
        return []
    # BOJ pages are Shift-JIS by default; tolerate replacement on failures.
    raw = raw_bytes.decode("shift_jis", errors="replace")
    # When the endpoint responds with the multi-step HTML form instead of a
    # CSV, the first bytes look like '<!DOCTYPE…'. Detect and skip — the
    # CSV will need a session-based scraper (deferred ticket).
    if raw.lstrip().startswith("<"):
        logger.debug("boj_ingest: stat-search returned HTML for %s — needs session scraper", native_id)
        return []

    rows: list[tuple[date, float]] = []
    reader = csv.reader(io.StringIO(raw))
    for r in reader:
        if not r or len(r) < 2:
            continue
        d_raw, v_raw = r[0].strip(), r[1].strip()
        if not d_raw or not v_raw or v_raw in ("NA", "ND", "..", "-"):
            continue
        try:
            # BOJ formats: YYYY/MM/DD, YYYY/MM, YYYY-Q
            d_parts = d_raw.replace("/", "-").split("-")
            if len(d_parts) == 3:
                d = date(int(d_parts[0]), int(d_parts[1]), int(d_parts[2]))
            elif len(d_parts) == 2:
                d = date(int(d_parts[0]), int(d_parts[1]), 1)
            else:
                continue
            rows.append((d, float(v_raw.replace(",", ""))))
        except (ValueError, IndexError):
            continue
    return rows


def _active_series() -> list[tuple[str, str]]:
    """Return [(series_id, native_id), ...] for active BOJ series."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'boj' AND is_active = TRUE
            ORDER  BY series_id
            """
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _latest_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.series_id, MAX(f.date)
            FROM   fact_macro f
            JOIN   ref_macro_series s ON s.series_id = f.series_id
            WHERE  s.source_id = 'boj'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_boj(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    fred_key: str | None = None
    latest = {} if full else _latest_dates()
    total = 0
    with market_run("boj", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "boj", series_id)
            if not full and series_id in latest:
                start = (latest[series_id] + timedelta(days=1)).isoformat()
            else:
                start = "2000-01-01"

            try:
                if native_id.startswith("fred:"):
                    if fred_key is None:
                        fred_key = _fred_api_key()
                    obs = _fetch_via_fred(native_id, start, fred_key)
                elif native_id.startswith("api:"):
                    obs = _fetch_via_boj_api(native_id, start)
                elif native_id.startswith("snapshot:"):
                    obs = _fetch_via_boj_snapshot(native_id, start)
                else:
                    obs = _fetch_via_boj_direct(native_id, start)
            except Exception as exc:
                mark_item_done(ctx, "boj", series_id, status="failed", error=str(exc)[:4000])
                continue

            # For CPI headline series stored as an index, also derive a YoY %
            if obs:
                _upsert(series_id, obs)

            mark_item_done(
                ctx,
                "boj",
                series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs),
                rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
            total += len(obs)
    return total


def _upsert(series_id: str, rows: list[tuple[date, float]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fact_macro (series_id, date, value)
            VALUES (%s, %s, %s)
            ON CONFLICT (series_id, date) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = now()
            """,
            [(series_id, d, v) for d, v in rows],
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest BOJ macro series")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_boj(series_ids=args.series, full=args.full)
    print(f"boj_ingest: {n} rows")


if __name__ == "__main__":
    main()
