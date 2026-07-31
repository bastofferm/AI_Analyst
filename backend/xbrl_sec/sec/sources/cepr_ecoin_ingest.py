"""€-coin (CEPR / Banca d'Italia) coincident-indicator ingestion.

The €-coin indicator is published monthly by CEPR jointly with the Bank of
Italy. The historical series is downloadable as a public CSV from the project
home page.

Series registered in ``ref_macro_series`` with ``source_id='cepr'``:

* ``CEPR:ECOIN`` — €-coin coincident indicator of Eurozone GDP growth

Run:
    python -m xbrl_sec.sec.sources.cepr_ecoin_ingest --full
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

logger = logging.getLogger("mzqa.cepr_ecoin_ingest")

# Stable public publication. Format: two columns — month (YYYY-MM) and value.
# When the CEPR origin is unreachable (recurring 521 / Cloudflare blocks),
# fall back order is: (1) most-recent successfully-cached copy in
# D:/macroData/latest/cepr/, then (2) operator drop in D:/macroData/drops/cepr/.
NATIVE_TO_URL: dict[str, str] = {
    "eurocoin": "https://eurocoin.cepr.org/sites/default/files/eurocoin_history.csv",
}


def _fetch_csv(native_id: str, start: str) -> list[tuple[date, float]]:
    url = NATIVE_TO_URL.get(native_id)
    if not url:
        raise RuntimeError(f"cepr_ecoin_ingest: unknown native_id {native_id!r}")

    import pandas as pd

    raw_bytes = macro_cache.fetch("cepr", native_id, url, ext="csv", attempts=3)
    if not raw_bytes:
        raw_bytes = macro_cache.read_latest("cepr", native_id, "csv")
        if raw_bytes:
            logger.info("cepr_ecoin: using prior cached copy from D:/macroData/latest")
    if not raw_bytes:
        raw_bytes = macro_cache.read_drop("cepr", native_id)
        if raw_bytes:
            logger.info("cepr_ecoin: using operator drop from D:/macroData/drops/cepr")
    if not raw_bytes:
        return []
    raw = raw_bytes.decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return []

    # The CEPR CSV is sometimes semicolon-separated and sometimes comma. Try both.
    try:
        df = pd.read_csv(io.StringIO(raw))
        if df.shape[1] < 2:
            df = pd.read_csv(io.StringIO(raw), sep=";")
    except Exception:
        df = pd.read_csv(io.StringIO(raw), sep=";")

    if df.empty or df.shape[1] < 2:
        return []

    date_col = df.columns[0]
    val_col = df.select_dtypes("number").columns.tolist()
    val_col = val_col[-1] if val_col else df.columns[1]

    start_d = date.fromisoformat(start)
    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        raw_d = str(row[date_col]).strip()
        try:
            if len(raw_d) == 7 and "-" in raw_d:  # 'YYYY-MM'
                y, m = map(int, raw_d.split("-"))
                # Pin to month-end for monthly cadence
                from calendar import monthrange
                d = date(y, m, monthrange(y, m)[1])
            else:
                d = pd.to_datetime(raw_d).date()
            v = float(row[val_col])
        except (ValueError, TypeError):
            continue
        if pd.isna(v) or d < start_d:
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'cepr' AND is_active = TRUE
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
            WHERE  s.source_id = 'cepr'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_cepr_ecoin(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("cepr", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "cepr", series_id)
            start = (latest[series_id] + timedelta(days=1)).isoformat() if (not full and series_id in latest) else "2000-01-01"
            try:
                obs = _fetch_csv(native_id, start)
            except Exception as exc:
                logger.warning("cepr_ecoin_ingest %s failed: %s", series_id, exc)
                mark_item_done(ctx, "cepr", series_id, status="failed", error=str(exc)[:4000])
                continue
            if obs:
                _upsert(series_id, obs)
                total += len(obs)
            mark_item_done(
                ctx, "cepr", series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest CEPR €-coin coincident indicator")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_cepr_ecoin(series_ids=args.series, full=args.full)
    print(f"cepr_ecoin_ingest: {n} rows")


if __name__ == "__main__":
    main()
