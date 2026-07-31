"""Statistics Japan CPI ingestion via DBnomics.

DBnomics mirrors official Statistics Bureau of Japan datasets and exposes a
stable JSON API. Series are registered with ``source_id='statjp'`` and
``native_id`` values like ``CPIm:733``.

Run:
    python -m xbrl_sec.sec.sources.statjp_dbnomics_ingest --full
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import date

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.boj_ingest import _upsert
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

BASE = "https://api.db.nomics.world/v22/series/STATJP"


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'statjp' AND is_active = TRUE
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
            WHERE  s.source_id = 'statjp'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _period_to_date(period: str) -> date | None:
    try:
        if len(period) == 7:
            return date(int(period[:4]), int(period[5:7]), 1)
        if len(period) == 4:
            return date(int(period), 1, 1)
    except ValueError:
        return None
    return None


def _fetch_series(native_id: str, start_date: date | None = None) -> list[tuple[date, float]]:
    try:
        dataset, code = native_id.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"Invalid STATJP native_id: {native_id}") from exc

    qs = urllib.parse.urlencode({"observations": "1"})
    url = f"{BASE}/{urllib.parse.quote(dataset)}/{urllib.parse.quote(code)}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    docs = ((payload.get("series") or {}).get("docs") or [])
    if not docs:
        return []

    doc = docs[0]
    periods = doc.get("period") or []
    values = doc.get("value") or []
    rows: list[tuple[date, float]] = []
    for period, value in zip(periods, values):
        if value is None:
            continue
        d = _period_to_date(str(period))
        if d is None:
            continue
        if start_date is not None and d <= start_date:
            continue
        try:
            rows.append((d, float(value)))
        except (TypeError, ValueError):
            continue
    return rows


def fetch_statjp(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("statjp", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "statjp", series_id)
            try:
                rows = _fetch_series(native_id, latest.get(series_id))
            except Exception as exc:
                mark_item_done(ctx, "statjp", series_id, status="failed", error=str(exc)[:4000])
                continue

            if rows:
                _upsert(series_id, rows)

            mark_item_done(
                ctx,
                "statjp",
                series_id,
                status="succeeded" if rows else "skipped",
                rows_in=len(rows),
                rows_out=len(rows),
                min_date=min(d for d, _ in rows) if rows else None,
                max_date=max(d for d, _ in rows) if rows else None,
            )
            total += len(rows)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest Statistics Japan series via DBnomics")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_statjp(series_ids=args.series, full=args.full)
    print(f"statjp_dbnomics_ingest: {n} rows")


if __name__ == "__main__":
    main()
