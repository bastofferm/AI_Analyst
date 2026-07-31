"""Philadelphia Fed direct-download ingest.

Currently covers a single series:

* ``PHILLYFED:ADS`` (native_id=``direct:ads``) — Aruoba-Diebold-Scotti
  Business Conditions Index, daily, served as an .xlsx from
  ``philadelphiafed.org``. The series is not on FRED.

The download routes through ``macro_cache`` so the workbook lands at
``D:/macroData/raw/phillyfed/ads/`` with full manifest tracking.

Run:
    python -m xbrl_sec.sec.sources.phillyfed_ingest --full
    python -m xbrl_sec.sec.sources.phillyfed_ingest --series PHILLYFED:ADS
"""
from __future__ import annotations

import argparse
import io
import logging
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _upsert

logger = logging.getLogger("mzqa.phillyfed_ingest")

NATIVE_TO_URL: dict[str, str] = {
    "direct:ads": "https://www.philadelphiafed.org/-/media/frbp/assets/surveys-and-data/ads/ads_index_most_current_vintage.xlsx",
}


def _parse_ads_xlsx(raw: bytes, start: str) -> list[tuple[date, float]]:
    """Sheet1 columns: Date | ADS_Index | RECBARS. Date format YYYY:MM:DD."""
    import pandas as pd

    df = pd.read_excel(io.BytesIO(raw), sheet_name="Sheet1")
    if df.empty or "ADS_Index" not in df.columns:
        return []
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    val_col = "ADS_Index"
    start_d = date.fromisoformat(start)

    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        raw_d = str(row[date_col]).strip()
        raw_v = row[val_col]
        if pd.isna(raw_v):
            continue
        try:
            # Philly Fed uses 'YYYY:MM:DD' (colons, not dashes).
            y, m, d = raw_d.split(":")
            dt = date(int(y), int(m), int(d))
            v = float(raw_v)
        except (ValueError, TypeError):
            continue
        if dt < start_d:
            continue
        out.append((dt, v))
    out.sort(key=lambda r: r[0])
    return out


def _fetch(native_id: str, start: str) -> list[tuple[date, float]]:
    url = NATIVE_TO_URL.get(native_id)
    if not url:
        raise RuntimeError(f"phillyfed_ingest: unknown native_id {native_id!r}")
    raw = macro_cache.fetch("phillyfed", native_id, url, ext="xlsx", attempts=3)
    if not raw:
        raw = macro_cache.read_latest("phillyfed", native_id, "xlsx")
        if raw:
            logger.info("phillyfed: using prior cached copy from D:/macroData/latest")
    if not raw:
        return []
    if native_id == "direct:ads":
        return _parse_ads_xlsx(raw, start)
    raise RuntimeError(f"phillyfed_ingest: no parser for {native_id!r}")


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'phillyfed' AND is_active = TRUE
            ORDER  BY series_id
            """
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _latest_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.series_id, MAX(f.date)
            FROM   fact_macro f JOIN ref_macro_series s ON s.series_id = f.series_id
            WHERE  s.source_id = 'phillyfed'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_phillyfed(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("phillyfed", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "phillyfed", series_id)
            start = (latest[series_id] + timedelta(days=1)).isoformat() if (not full and series_id in latest) else "2000-01-01"
            try:
                obs = _fetch(native_id, start)
            except Exception as exc:
                logger.warning("phillyfed_ingest %s failed: %s", series_id, exc)
                mark_item_done(ctx, "phillyfed", series_id, status="failed", error=str(exc)[:4000])
                continue
            if obs:
                _upsert(series_id, obs)
                total += len(obs)
            mark_item_done(
                ctx, "phillyfed", series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest Philadelphia Fed direct series")
    p.add_argument("--full", action="store_true")
    p.add_argument("--series", nargs="*")
    args = p.parse_args()
    n = fetch_phillyfed(series_ids=args.series, full=args.full)
    print(f"phillyfed_ingest: {n} rows")


if __name__ == "__main__":
    main()
