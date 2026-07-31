"""Euroframe Indicator + Bank of Spain Euro-STING nowcast ingestion.

Both indicators are quarterly Euro-area growth nowcasts published outside of
machine-readable APIs. Strategy: attempt to fetch a public URL first; if that
fails (404, network, format change), fall back to a manually-maintained CSV
drop in ``data/macro_drops/{native_id}.csv``. This keeps the pipeline alive
during the (frequent) URL/format moves these projects make.

Series registered in ``ref_macro_series``:

* ``EUROFRAME:EFI``  (source_id='euroframe', native_id='efi')
* ``BDE:EUROSTING``  (source_id='bde',       native_id='eurosting')

CSV drop format (UTF-8, header row required):

    period_end,value
    2024-03-31,0.31
    2024-06-30,0.42

Run:
    python -m xbrl_sec.sec.sources.euroframe_ingest --full
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _upsert

logger = logging.getLogger("mzqa.euroframe_ingest")

# Public publication URLs. Set to None to skip directly to the operator drop
# (drops live at D:/macroData/drops/{source}/{native_id}.csv per macro_cache).
NATIVE_TO_URL: dict[str, str | None] = {
    "efi":       None,  # Euroframe publishes PDFs; values transcribed via CSV drop
    "eurosting": None,  # Bank of Spain publishes Excel; values transcribed via CSV drop
}

# Which source_id covers which native_id. Both share this ingestion path.
NATIVE_TO_SOURCE: dict[str, str] = {
    "efi": "euroframe",
    "eurosting": "bde",
}


def _fetch_csv(native_id: str, start: str) -> list[tuple[date, float]]:
    """Read a (period_end, value) CSV from URL or operator drop. Returns (date, value)."""
    raw_bytes: bytes | None = None
    src = NATIVE_TO_SOURCE.get(native_id, "euroframe")
    url = NATIVE_TO_URL.get(native_id)
    if url:
        raw_bytes = macro_cache.fetch(src, native_id, url, ext="csv", attempts=3)

    if raw_bytes is None:
        raw_bytes = macro_cache.read_drop(src, native_id)
        if raw_bytes is None:
            logger.warning(
                "euroframe_ingest: no drop file at D:/macroData/drops/%s/%s.csv — skipping",
                src, native_id,
            )
            return []
        logger.info("euroframe_ingest: using operator drop for %s/%s", src, native_id)
    raw = raw_bytes.decode("utf-8-sig", errors="replace")

    start_d = date.fromisoformat(start)
    out: list[tuple[date, float]] = []
    reader = csv.DictReader(io.StringIO(raw))
    for row in reader:
        try:
            d = date.fromisoformat((row.get("period_end") or row.get("date") or "").strip())
            v = float((row.get("value") or "").strip())
        except (ValueError, TypeError):
            continue
        if d < start_d:
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _active_series() -> list[tuple[str, str, str]]:
    """Return [(series_id, native_id, source_id), ...] for both euroframe and bde sources."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id, source_id
            FROM   ref_macro_series
            WHERE  source_id IN ('euroframe','bde') AND is_active = TRUE
            ORDER  BY series_id
            """
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _latest_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.series_id, MAX(f.date)
            FROM   fact_macro f
            JOIN   ref_macro_series s ON s.series_id = f.series_id
            WHERE  s.source_id IN ('euroframe','bde')
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_euroframe(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid, src) for sid, nid, src in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("euroframe", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id, src in all_series:
            mark_item_running(ctx, src, series_id)
            start = (latest[series_id] + timedelta(days=1)).isoformat() if (not full and series_id in latest) else "2000-01-01"
            try:
                obs = _fetch_csv(native_id, start)
            except Exception as exc:
                logger.warning("euroframe_ingest %s failed: %s", series_id, exc)
                mark_item_done(ctx, src, series_id, status="failed", error=str(exc)[:4000])
                continue
            if obs:
                _upsert(series_id, obs)
                total += len(obs)
            mark_item_done(
                ctx, src, series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest Euroframe + Euro-STING quarterly nowcasts")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_euroframe(series_ids=args.series, full=args.full)
    print(f"euroframe_ingest: {n} rows")


if __name__ == "__main__":
    main()
