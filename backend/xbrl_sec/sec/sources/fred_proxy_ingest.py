"""Generic FRED-mirror ingest for non-FRED sources.

For sources whose ``native_id`` in ``ref_macro_series`` is prefixed ``fred:``,
this pulls observations from FRED and writes to ``fact_macro``. Works for
ECB, SNB, BEA, BLS, RBA, HKMA, MAS — wherever a credible FRED mirror exists.

Run:
    python -m xbrl_sec.sec.sources.fred_proxy_ingest --source ecb --full
    python -m xbrl_sec.sec.sources.fred_proxy_ingest --source all  --full
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running
from xbrl_sec.sec.sources.boj_ingest import _fetch_via_fred, _upsert


SOURCES = ["ecb", "snb", "bea", "bls", "rba", "hkma", "mas", "atlfed", "nyfed", "oecd"]


def _fred_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY environment variable not set")
    return key


def _series(source: str) -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT series_id, native_id FROM ref_macro_series
               WHERE source_id=%s AND is_active=TRUE AND native_id LIKE 'fred:%%'
               ORDER BY series_id""",
            (source,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _latest_dates(source: str) -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.series_id, MAX(f.date)
               FROM fact_macro f JOIN ref_macro_series s ON s.series_id=f.series_id
               WHERE s.source_id=%s GROUP BY f.series_id""",
            (source,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def fetch_source(source: str, full: bool = False) -> int:
    key = _fred_api_key()
    series = _series(source)
    if not series:
        return 0
    latest = {} if full else _latest_dates(source)
    total = 0
    with market_run(source, full, {"series": len(series)}) as ctx:
        for series_id, native_id in series:
            mark_item_running(ctx, source, series_id)
            start = (
                (latest[series_id] + timedelta(days=1)).isoformat()
                if not full and series_id in latest else "2000-01-01"
            )
            try:
                obs = _fetch_via_fred(native_id, start, key)
            except Exception as exc:
                mark_item_done(ctx, source, series_id, status="failed", error=str(exc)[:4000])
                continue
            if obs:
                _upsert(series_id, obs)
            mark_item_done(
                ctx, source, series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
            total += len(obs)
    return total


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, choices=SOURCES + ["all"])
    p.add_argument("--full", action="store_true")
    args = p.parse_args()
    sources = SOURCES if args.source == "all" else [args.source]
    totals: dict[str, int] = {}
    for s in sources:
        totals[s] = fetch_source(s, full=args.full)
    import json
    print(json.dumps(totals))


if __name__ == "__main__":
    main()
