"""Japan Cabinet Office (ESRI) macro ingestion.

Same FRED-mirror strategy as boj_ingest. Series registered in
``ref_macro_series WHERE source_id='cao_jp'`` with ``native_id`` of form
``fred:<code>`` are pulled from FRED.

Run:
    python -m xbrl_sec.sec.sources.cao_jp_ingest --full
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urljoin

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

# Reuse the FRED fetcher from boj_ingest so we have one canonical path
from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _fetch_via_fred, _upsert

logger = logging.getLogger("mzqa.cao_jp_ingest")

# ---------------------------------------------------------------------------
# ESRI CI (Indices of Business Conditions) direct download.
#
# ESRI publishes the monthly composite-index workbook at
#   https://www.esri.cao.go.jp/jp/stat/di/{MMYY}ci.xlsx
# where MM is the release month and YY is the two-digit year.
# The landing page (di-e.html) always lists the most-recent filename, so
# scraping it once per run yields a stable URL. Each download is archived
# under D:/macroData/raw/cao_jp/. We don't currently parse these files —
# the production data flows through FRED proxies — but having them on
# disk lets us bring up a parser without re-fetching history.
# ---------------------------------------------------------------------------

_CAO_DI_LANDING = "https://www.esri.cao.go.jp/en/stat/di/di-e.html"
_CAO_DI_RE = re.compile(r'href="\.\./\.\./\.\./jp/stat/di/(\d{4}ci(?:_cont\d|\d)?\.xlsx)"', re.IGNORECASE)
_CAO_JUCHU_LANDING = "https://www.esri.cao.go.jp/en/stat/juchu/juchu-e.html"
_CAO_JUCHU_RE = re.compile(r'href="([^"]*chouki-1\.xlsx)"', re.IGNORECASE)
_CAO_SCHEDULE_URL = "https://www.esri.cao.go.jp/en/stat/stat-schedule-e.html"
_JST = timezone(timedelta(hours=9))


def _scrape_cao_di_latest_urls() -> list[str]:
    """Return the most-recent CAO composite-index xlsx URLs found on the EN landing.

    Returns an empty list if the landing page is unreachable or the link
    pattern changes — the caller treats that as "skip".
    """
    raw = macro_cache.fetch("cao_jp", "_di_landing", _CAO_DI_LANDING, ext="html", attempts=2)
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    files = sorted(set(_CAO_DI_RE.findall(text)))
    return [f"https://www.esri.cao.go.jp/jp/stat/di/{f}" for f in files]


def cache_cao_di_drop() -> int:
    """Snapshot the current month's ESRI CI Excel files into D:/macroData.

    Side-effect: returns the count of files cached. The main ``ci``
    workbook is then parsed directly by ``_parse_cao_ci_workbook`` for
    native_ids of the form ``direct:leading|coincident|lagging``.
    """
    urls = _scrape_cao_di_latest_urls()
    n = 0
    for url in urls:
        # native_id = basename without extension, e.g. '0526ci'
        native = os.path.splitext(os.path.basename(url))[0]
        if macro_cache.fetch("cao_jp", f"di_{native}", url, ext="xlsx", attempts=2):
            n += 1
    return n


# ---------------------------------------------------------------------------
# ESRI machinery orders direct workbook.
# ---------------------------------------------------------------------------

def _scrape_cao_juchu_latest_url() -> str | None:
    """Return the current machinery-orders sectors workbook URL."""
    raw = macro_cache.fetch("cao_jp", "_juchu_landing", _CAO_JUCHU_LANDING, ext="html", attempts=2)
    if not raw:
        raw = macro_cache.read_latest("cao_jp", "_juchu_landing", "html")
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    m = _CAO_JUCHU_RE.search(text)
    if not m:
        return None
    return urljoin(_CAO_JUCHU_LANDING, m.group(1))


def cache_cao_juchu_drop() -> int:
    """Snapshot the current ESRI machinery-orders historical workbook."""
    url = _scrape_cao_juchu_latest_url()
    if not url:
        return 0
    raw = macro_cache.fetch("cao_jp", "juchu_machinery_orders_sectors", url, ext="xlsx", attempts=2)
    return 1 if raw else 0


# ---------------------------------------------------------------------------
# ESRI CI workbook parser
# ---------------------------------------------------------------------------

# Maps the direct-source native_id suffix to the column index in the
# Indexes sheet (after header rows are stripped). The columns are:
#   0: Time Monthly Code   1: Year   2: Month
#   3: Leading Index       4: Coincident Index       5: Lagging Index
#   6/7/8: outlier-replacement-free variants (ignored)
_CI_COLUMN = {"leading": 3, "coincident": 4, "lagging": 5}

# Each CI workbook is named ``{MMYY}ci.xlsx`` (e.g. ``0526ci`` for the
# May 2026 release). To find the most recent workbook on disk we list
# the latest/ directory and pick the largest YYMM prefix.
_CI_FILE_RE = re.compile(r"^di_(\d{4})ci\.xlsx$", re.IGNORECASE)


def _latest_ci_workbook_path() -> str | None:
    latest_dir = macro_cache.latest_dir("cao_jp")
    candidates: list[tuple[int, str]] = []
    if not latest_dir.exists():
        return None
    for entry in latest_dir.iterdir():
        m = _CI_FILE_RE.match(entry.name)
        if m:
            # Re-order MMYY → YYMM so str sort = chronological sort
            mmyy = m.group(1)
            sortable = mmyy[2:] + mmyy[:2]
            candidates.append((int(sortable), str(entry)))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _parse_cao_ci_workbook(component: str) -> list[tuple[date, float]]:
    """Parse the most-recent ESRI CI workbook for *component*.

    component ∈ {"leading", "coincident", "lagging"}. Returns the full
    monthly time series available in the workbook. Dates pin to the
    last day of the month (consistent with our ref_macro_series
    frequency='M' convention). Returns [] if no workbook is cached
    or the sheet structure has shifted (parser logs and degrades).
    """
    import calendar

    import pandas as pd

    col = _CI_COLUMN.get(component)
    if col is None:
        logger.warning("cao_jp: unknown CI component %r", component)
        return []

    path = _latest_ci_workbook_path()
    if not path:
        logger.warning("cao_jp: no CI workbook in D:/macroData/latest/cao_jp")
        return []

    try:
        xl = pd.ExcelFile(path)
        sheet = next((s for s in xl.sheet_names if "Index" in s or "指数" in s), xl.sheet_names[0])
        df = pd.read_excel(path, sheet_name=sheet, header=None)
    except Exception as exc:
        logger.warning("cao_jp: failed to read %s: %s", path, exc)
        return []

    out: list[tuple[date, float]] = []
    # Data starts after the 6-row header block. Iterate from row 6 onward.
    for i in range(6, len(df)):
        try:
            year = df.iat[i, 1]
            month = df.iat[i, 2]
            val = df.iat[i, col]
        except (IndexError, KeyError):
            continue
        if pd.isna(year) or pd.isna(month) or pd.isna(val):
            continue
        try:
            y = int(year)
            m = int(month)
            if not (1 <= m <= 12):
                continue
            d = date(y, m, calendar.monthrange(y, m)[1])
            v = float(val)
        except (ValueError, TypeError):
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


_MACHINERY_COLUMN = {
    # Sheet: "Machinery Orders by Sectors ... (Seasonally adjusted, Monthly)"
    # Values are published in million yen.
    "machinery_orders_total_sa": 3,
    "machinery_orders_private_sa": 7,
    "machinery_orders_private_ex_ships_sa": 8,
    "machinery_orders_core_sa": 9,  # private sector ex ships and electric power
    "machinery_orders_domestic_demand_sa": 15,
}


def _parse_cao_machinery_workbook(component: str) -> list[tuple[date, float]]:
    """Parse ESRI machinery-orders historical workbook for *component*.

    The current workbook is linked from the English machinery-orders landing
    page and stored as ``juchu_machinery_orders_sectors.xlsx``.  The monthly
    seasonally adjusted sheet has year/month in columns 1/2 and the core
    private-sector ex-volatile series in column 9.
    """
    import pandas as pd

    col = _MACHINERY_COLUMN.get(component)
    if col is None:
        logger.warning("cao_jp: unknown machinery-orders component %r", component)
        return []

    raw = macro_cache.read_latest("cao_jp", "juchu_machinery_orders_sectors", "xlsx")
    if not raw:
        raw = macro_cache.read_drop("cao_jp", "juchu_machinery_orders_sectors")
    if not raw:
        logger.warning("cao_jp: no machinery-orders workbook cached")
        return []

    try:
        df = pd.read_excel(BytesIO(raw), sheet_name=1, header=None)
    except Exception as exc:
        logger.warning("cao_jp: failed to read machinery-orders workbook: %s", exc)
        return []

    out: list[tuple[date, float]] = []
    for i in range(9, len(df)):
        try:
            year = df.iat[i, 1]
            month = df.iat[i, 2]
            val = df.iat[i, col]
        except (IndexError, KeyError):
            continue
        if pd.isna(year) or pd.isna(month) or pd.isna(val):
            continue
        try:
            y = int(year)
            m = int(month)
            if not (1 <= m <= 12):
                continue
            out.append((date(y, m, 1), float(val)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda r: r[0])
    return out


def _fetch_via_cao_direct(native_id: str, start: str) -> list[tuple[date, float]]:
    """Parse the cached ESRI workbook for native_ids of form ``direct:<component>``."""
    component = native_id.split(":", 1)[1] if ":" in native_id else ""
    if component.startswith("machinery_orders"):
        rows = _parse_cao_machinery_workbook(component)
        start_d = date.fromisoformat(start)
        return [(d, v) for (d, v) in rows if d >= start_d]
    rows = _parse_cao_ci_workbook(component)
    start_d = date.fromisoformat(start)
    return [(d, v) for (d, v) in rows if d >= start_d]


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_SCHEDULE_SERIES = {
    "Indexes of Business Conditions (Preliminary Release)": ["CAO_JP:CI_LEAD", "CAO_JP:CI_COIN"],
    "Indexes of Business Conditions (Revision of the Preliminary Release)": ["CAO_JP:CI_LEAD", "CAO_JP:CI_COIN"],
    "Machinery Orders": ["CAO_JP:MACH_ORDERS"],
    "Consumer Confidence Survey": ["CAO_JP:CONS_CONF"],
}


def _month_num(token: str) -> int | None:
    key = token.strip().strip(".").lower()
    return _MONTHS.get(key)


def _parse_release_cell(cell: str, default_year: int) -> tuple[datetime, str] | None:
    m = re.search(r"([A-Za-z]+)\.?\s*(\d{1,2})(?:,\s*(\d{4}))?\s*\(([^)]+)\)", cell)
    if not m:
        return None
    month = _month_num(m.group(1))
    if month is None:
        return None
    day = int(m.group(2))
    year = int(m.group(3) or default_year)
    return datetime(year, month, day, 8, 50, tzinfo=_JST), m.group(4).strip()


def _period_end_from_label(label: str, release_date: date) -> date | None:
    import calendar

    parts = re.findall(r"([A-Za-z]+)\.?", label)
    if not parts:
        return None
    month = _month_num(parts[-1])
    if month is None:
        return None
    year = release_date.year
    if month > release_date.month:
        year -= 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _cache_cao_release_schedule() -> bytes | None:
    raw = macro_cache.fetch("cao_jp", "release_schedule", _CAO_SCHEDULE_URL, ext="html", attempts=2)
    if raw:
        return raw
    return macro_cache.read_latest("cao_jp", "release_schedule", "html")


def sync_cao_release_calendar() -> int:
    """Parse ESRI's release schedule into ``fact_macro_release``."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        logger.warning("cao_jp: BeautifulSoup unavailable for release schedule: %s", exc)
        return 0

    raw = _cache_cao_release_schedule()
    if not raw:
        return 0
    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    table = soup.find("table")
    if table is None:
        return 0

    caption = table.find("caption")
    default_year = date.today().year
    if caption:
        m = re.search(r"(\d{4})", caption.get_text(" ", strip=True))
        if m:
            default_year = int(m.group(1))

    rows = table.find_all("tr")
    if not rows:
        return 0
    headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    payload: list[tuple[str, datetime, date, str]] = []

    for tr in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        for header, cell in zip(headers, cells):
            series_ids = _SCHEDULE_SERIES.get(header)
            if not series_ids or not cell:
                continue
            parsed = _parse_release_cell(cell, default_year)
            if not parsed:
                continue
            release_at, period_label = parsed
            period_end = _period_end_from_label(period_label, release_at.date())
            if period_end is None:
                continue
            for series_id in series_ids:
                payload.append((series_id, release_at, period_end, header))

    if not payload:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fact_macro_release
                (series_id, release_at, period_end, source_release_id)
            SELECT %s, %s, %s, %s
            WHERE EXISTS (SELECT 1 FROM ref_macro_series WHERE series_id = %s)
            ON CONFLICT (series_id, release_at) DO UPDATE SET
                period_end = EXCLUDED.period_end,
                source_release_id = EXCLUDED.source_release_id
            """,
            [(sid, rel, pend, src, sid) for sid, rel, pend, src in payload],
        )
        return cur.rowcount


def _fred_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY environment variable not set")
    return key


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'cao_jp' AND is_active = TRUE
            ORDER  BY series_id
            """
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _latest_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.series_id, MAX(f.date)
            FROM   fact_macro f JOIN ref_macro_series s ON s.series_id=f.series_id
            WHERE  s.source_id = 'cao_jp'
            GROUP  BY f.series_id
            """
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def fetch_cao_jp(series_ids: list[str] | None = None, full: bool = False) -> int:
    series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        series = [(sid, nid) for sid, nid in series if sid in wanted]
    if not series:
        return 0

    # Side-effect: archive the current ESRI CI workbook into D:/macroData
    # for future direct-parse use. Best-effort; doesn't block ingest if it
    # fails. We log the count but don't treat zero as an error.
    try:
        cached = cache_cao_di_drop()
        if cached:
            logger.info("cao_jp_ingest: archived %d ESRI CI xlsx file(s) into D:/macroData", cached)
    except Exception as exc:
        logger.warning("cao_jp_ingest: CI workbook archive failed: %s", exc)
    try:
        cached = cache_cao_juchu_drop()
        if cached:
            logger.info("cao_jp_ingest: archived ESRI machinery-orders workbook into D:/macroData")
    except Exception as exc:
        logger.warning("cao_jp_ingest: machinery-orders archive failed: %s", exc)
    try:
        synced = sync_cao_release_calendar()
        if synced:
            logger.info("cao_jp_ingest: synced %d ESRI release-calendar row(s)", synced)
    except Exception as exc:
        logger.warning("cao_jp_ingest: release-calendar sync failed: %s", exc)

    latest = {} if full else _latest_dates()
    fred_key: str | None = None
    total = 0
    with market_run("cao_jp", full, {"series": len(series)}) as ctx:
        for series_id, native_id in series:
            mark_item_running(ctx, "cao_jp", series_id)
            start = (
                (latest[series_id] + timedelta(days=1)).isoformat()
                if not full and series_id in latest
                else "2000-01-01"
            )

            try:
                if native_id.startswith("fred:"):
                    if fred_key is None:
                        fred_key = _fred_api_key()
                    obs = _fetch_via_fred(native_id, start, fred_key)
                elif native_id.startswith("direct:"):
                    obs = _fetch_via_cao_direct(native_id, start)
                else:
                    obs = []
            except Exception as exc:
                mark_item_done(ctx, "cao_jp", series_id, status="failed", error=str(exc)[:4000])
                continue

            if obs:
                _upsert(series_id, obs)
            mark_item_done(
                ctx, "cao_jp", series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
            total += len(obs)
    return total


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true")
    p.add_argument("--series", nargs="*")
    args = p.parse_args()
    n = fetch_cao_jp(series_ids=args.series, full=args.full)
    print(f"cao_jp_ingest: {n} rows")


if __name__ == "__main__":
    main()
