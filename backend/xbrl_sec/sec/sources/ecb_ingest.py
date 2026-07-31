"""ECB Data Portal native SDMX ingest.

Pulls observations for series registered in ``ref_macro_series`` with
``source_id='ecb'``. The ``native_id`` column holds the full SDMX series
key, e.g. ``MNA.Q.Y.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.GY``.

The ECB Data Portal API is public, requires no key, and returns CSV when
``?format=csvdata`` is appended. Pattern mirrors ``boj_ingest.py`` (state-
tracked via ``market_run``, idempotent, reuses ``_upsert`` for the
``fact_macro`` write).

Run:
    python -m xbrl_sec.sec.sources.ecb_ingest --full
    python -m xbrl_sec.sec.sources.ecb_ingest --series ECB:ICP_HICP_YOY
"""
from __future__ import annotations

import argparse
import calendar
import io
import urllib.request
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

# Reuse the canonical write path so all macro sources land in fact_macro the
# same way.
from xbrl_sec.sec.sources.boj_ingest import _upsert

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _parse_ecb_period(tp: str) -> date:
    """Parse ECB SDMX TIME_PERIOD values.

    Supports: 'YYYY', 'YYYY-MM', 'YYYY-MM-DD', 'YYYY-Qn'.
    """
    tp = tp.strip()
    if "Q" in tp:
        yr, q = tp.split("-Q")
        month_end = {1: 3, 2: 6, 3: 9, 4: 12}[int(q)]
        return _last_day_of_month(int(yr), month_end)
    if len(tp) == 4:
        return date(int(tp), 12, 31)
    if len(tp) == 7:
        y, m = map(int, tp.split("-"))
        return _last_day_of_month(y, m)
    return date.fromisoformat(tp)


def _fetch_via_ecb(native_id: str, start: str) -> list[tuple[date, float]]:
    """Hit the ECB Data Portal CSV endpoint and return (date, value) tuples.

    ``native_id`` is the full SDMX key. The dataflow is the first
    dot-separated segment, the rest is the series key.
    """
    import pandas as pd

    dataflow, _, key = native_id.partition(".")
    if not key:
        # Pure-dataflow request — caller passed a bare flow id. Unsupported.
        return []

    url = (
        f"{ECB_BASE}/{dataflow}/{key}"
        f"?format=csvdata&startPeriod={start}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/csv",
            "User-Agent": "mzqa-research/1.0 (macro-ingest)",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")

    if not raw.strip():
        return []

    df = pd.read_csv(io.StringIO(raw))
    if "TIME_PERIOD" not in df.columns or "OBS_VALUE" not in df.columns:
        return []

    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        tp = row["TIME_PERIOD"]
        val = row["OBS_VALUE"]
        if pd.isna(val):
            continue
        try:
            d = _parse_ecb_period(str(tp))
            v = float(val)
        except (ValueError, KeyError, TypeError):
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _active_series() -> list[tuple[str, str]]:
    """Return [(series_id, native_id), ...] for active ECB series."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'ecb' AND is_active = TRUE
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
            WHERE  s.source_id = 'ecb'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_ecb(series_ids: list[str] | None = None, full: bool = False) -> int:
    """Fetch & upsert ECB observations.

    Returns total rows written.
    """
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("ecb", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "ecb", series_id)
            if not full and series_id in latest:
                start = (latest[series_id] + timedelta(days=1)).isoformat()
            else:
                start = "2000-01-01"

            try:
                obs = _fetch_via_ecb(native_id, start)
            except Exception as exc:
                mark_item_done(
                    ctx, "ecb", series_id, status="failed", error=str(exc)[:4000]
                )
                continue

            if obs:
                _upsert(series_id, obs)

            mark_item_done(
                ctx,
                "ecb",
                series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs),
                rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
            total += len(obs)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest ECB macro series (native SDMX)")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_ecb(series_ids=args.series, full=args.full)
    print(f"ecb_ingest: {n} rows")


if __name__ == "__main__":
    main()
