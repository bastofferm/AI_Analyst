"""Japan MOF JGB constant-maturity yield curve ingestion.

Pulls the official Ministry of Finance historical CSV:
https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv

Series are registered with ``source_id='mof_jp'`` and ``native_id`` values like
``jgbcme_all:2Y``.

Run:
    python -m xbrl_sec.sec.sources.mof_jgb_ingest --full
"""
from __future__ import annotations

import argparse
import csv
import io
from datetime import date

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _upsert
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'mof_jp' AND is_active = TRUE
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
            WHERE  s.source_id = 'mof_jp'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _load_curve() -> dict[str, list[tuple[date, float]]]:
    raw_bytes = macro_cache.fetch("mof_jp", "jgbcme_all", URL, ext="csv", attempts=2)
    if not raw_bytes:
        return {}

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))

    header: list[str] | None = None
    out: dict[str, list[tuple[date, float]]] = {}
    for row in reader:
        if not row:
            continue
        if row[0].strip() == "Date":
            header = [c.strip() for c in row]
            out = {tenor: [] for tenor in header[1:]}
            continue
        if header is None or len(row) < 2:
            continue
        try:
            y, m, day = [int(part) for part in row[0].strip().split("/")]
            d = date(y, m, day)
        except (ValueError, TypeError):
            continue

        for idx, tenor in enumerate(header[1:], start=1):
            if idx >= len(row):
                continue
            v_raw = row[idx].strip()
            if not v_raw or v_raw == "-":
                continue
            try:
                out.setdefault(tenor, []).append((d, float(v_raw)))
            except ValueError:
                continue
    return out


def fetch_mof_jgb(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    curve = _load_curve()
    total = 0

    with market_run("mof_jp_jgb", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "mof_jp", series_id)
            tenor = native_id.split(":", 1)[1] if native_id.startswith("jgbcme_all:") else ""
            rows = curve.get(tenor, [])
            if not full and series_id in latest:
                start = latest[series_id]
                rows = [(d, v) for d, v in rows if d > start]

            if rows:
                _upsert(series_id, rows)

            mark_item_done(
                ctx,
                "mof_jp",
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
    p = argparse.ArgumentParser(description="Ingest Japan MOF JGB curve data")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_mof_jgb(series_ids=args.series, full=args.full)
    print(f"mof_jgb_ingest: {n} rows")


if __name__ == "__main__":
    main()
