"""Japan METI industrial-production ingestion.

METI's historical Excel files are the preferred full-history source, but they
can be slow/unreliable from this Windows environment.  This ingester therefore
starts with the official current-result HTML page and writes the latest IIP
snapshot; the registry keeps the source isolated so a full Excel parser can be
added without changing the public macro slot.

Run:
    python -m xbrl_sec.sec.sources.meti_jp_ingest
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import date, datetime, timedelta, timezone

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _upsert
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

logger = logging.getLogger("mzqa.meti_jp_ingest")

_METI_IIP_URL = "https://www.meti.go.jp/english/statistics/tyo/iip/index.html"
_JST = timezone(timedelta(hours=9))

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_LABEL_TO_SERIES = {
    "Production": "METI:JP_IIP_PRODUCTION_SA",
    "Shipments": "METI:JP_IIP_SHIPMENTS_SA",
    "Inventories": "METI:JP_IIP_INVENTORIES_SA",
    "Inventory Ratio": "METI:JP_IIP_INVENTORY_RATIO_SA",
}


def _active_series() -> dict[str, str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'meti_jp' AND is_active = TRUE
            ORDER  BY series_id
            """
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _latest_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.series_id, MAX(f.date)
            FROM   fact_macro f
            JOIN   ref_macro_series s ON s.series_id = f.series_id
            WHERE  s.source_id = 'meti_jp'
            GROUP  BY f.series_id
            """
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _month_num(token: str) -> int | None:
    return _MONTHS.get(token.strip().strip(".").lower())


def _fetch_current_html() -> str | None:
    raw = macro_cache.fetch("meti_jp", "iip_current", _METI_IIP_URL, ext="html", attempts=2)
    if not raw:
        raw = macro_cache.read_latest("meti_jp", "iip_current", "html")
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _html_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", "\n", html)
        return re.sub(r"\s+", "\n", text)


def _parse_report_context(text: str) -> tuple[date, datetime] | None:
    m = re.search(
        r"(?:Preliminary|Revised)\s+Report\s+for\s+([A-Za-z]+)\s+(\d{4})"
        r"\s+\(released\s+at\s+(\d{1,2}):(\d{2}),\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    period_month = _month_num(m.group(1))
    release_month = _month_num(m.group(5))
    if period_month is None or release_month is None:
        return None
    period = date(int(m.group(2)), period_month, 1)
    release = datetime(
        int(m.group(7)),
        release_month,
        int(m.group(6)),
        int(m.group(3)),
        int(m.group(4)),
        tzinfo=_JST,
    )
    return period, release


def _parse_current_rows(text: str, period: date) -> dict[str, list[tuple[date, float]]]:
    rows: dict[str, list[tuple[date, float]]] = {}
    for label, series_id in _LABEL_TO_SERIES.items():
        m = re.search(
            rf"{re.escape(label)}\s+([-+]?\d+(?:\.\d+)?)\s+[-+]?\d+(?:\.\d+)?\s+[-+]?\d+(?:\.\d+)?\s+[-+]?\d+(?:\.\d+)?",
            text,
            re.IGNORECASE,
        )
        if not m:
            continue
        rows[series_id] = [(period, float(m.group(1)))]
    return rows


def _sync_release_calendar(release_at: datetime, period: date, series_ids: list[str]) -> int:
    if not series_ids:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fact_macro_release
                (series_id, release_at, period_end, source_release_id)
            VALUES (%s, %s, %s, 'METI IIP current result')
            ON CONFLICT (series_id, release_at) DO UPDATE SET
                period_end = EXCLUDED.period_end,
                source_release_id = EXCLUDED.source_release_id
            """,
            [(sid, release_at, period, ) for sid in series_ids],
        )
        return cur.rowcount


def fetch_meti_jp(series_ids: list[str] | None = None, full: bool = False) -> int:
    active = _active_series()
    if series_ids:
        wanted = set(series_ids)
        active = {sid: nid for sid, nid in active.items() if sid in wanted}
    if not active:
        return 0

    html = _fetch_current_html()
    if not html:
        return 0
    text = _html_text(html)
    context = _parse_report_context(text)
    if not context:
        logger.warning("meti_jp: could not parse current IIP report context")
        return 0
    period, release_at = context
    parsed = _parse_current_rows(text, period)
    latest = {} if full else _latest_dates()

    total = 0
    with market_run("meti_jp", full, {"series": len(active)}) as ctx:
        for series_id in active:
            mark_item_running(ctx, "meti_jp", series_id)
            rows = parsed.get(series_id, [])
            if not full and series_id in latest:
                rows = [(d, v) for d, v in rows if d > latest[series_id]]
            if rows:
                _upsert(series_id, rows)
            mark_item_done(
                ctx,
                "meti_jp",
                series_id,
                status="succeeded" if rows else "skipped",
                rows_in=len(rows),
                rows_out=len(rows),
                min_date=min(d for d, _ in rows) if rows else None,
                max_date=max(d for d, _ in rows) if rows else None,
            )
            total += len(rows)

    try:
        _sync_release_calendar(release_at, period, [sid for sid in active if sid in parsed])
    except Exception as exc:
        logger.warning("meti_jp: release-calendar sync failed: %s", exc)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Ingest METI Japan industrial-production current result")
    p.add_argument("--full", action="store_true")
    p.add_argument("--series", nargs="*")
    args = p.parse_args()
    n = fetch_meti_jp(series_ids=args.series, full=args.full)
    print(f"meti_jp_ingest: {n} rows")


if __name__ == "__main__":
    main()
