"""FRED macro series ingestion.

Fetches observations for registered series from the St. Louis Fed FRED API
and upserts them into fact_macro.

API key required: set FRED_API_KEY environment variable (Windows user env var).
Free key: https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


def _fred_api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FRED_API_KEY environment variable not set. "
            "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    return key


def _fetch_series_requests(
    series_id: str,
    start_date: str,
    api_key: str,
) -> list[tuple[date, float]]:
    import urllib.request
    import json

    url = (
        f"{_FRED_BASE}?series_id={series_id}"
        f"&observation_start={start_date}"
        f"&api_key={api_key}"
        f"&file_type=json"
        f"&sort_order=asc"
        f"&limit=100000"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    result: list[tuple[date, float]] = []
    for obs in data.get("observations", []):
        val_str = obs.get("value", ".")
        if val_str == "." or val_str is None:
            continue
        try:
            result.append((date.fromisoformat(obs["date"]), float(val_str)))
        except (ValueError, KeyError):
            continue
    return result


def _fetch_series_fredapi(
    series_id: str,
    start_date: str,
    api_key: str,
) -> list[tuple[date, float]]:
    import fredapi  # type: ignore[import]

    fred = fredapi.Fred(api_key=api_key)
    series = fred.get_series(series_id, observation_start=start_date)
    result: list[tuple[date, float]] = []
    for d, v in series.items():
        if v is None or (hasattr(v, "__class__") and v.__class__.__name__ == "float" and v != v):
            continue
        try:
            result.append((d.date() if hasattr(d, "date") else d, float(v)))
        except (TypeError, ValueError):
            continue
    return result


def _fetch_series(
    series_id: str,
    start_date: str,
    api_key: str,
) -> list[tuple[date, float]]:
    try:
        return _fetch_series_fredapi(series_id, start_date, api_key)
    except ImportError:
        pass
    return _fetch_series_requests(series_id, start_date, api_key)


def _date_spans() -> dict[str, tuple[date, date]]:
    """Return date span per FRED series, keyed by raw native code (no FRED: prefix)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT
                   CASE WHEN series_id LIKE 'FRED:%%' THEN substr(series_id, 6) ELSE series_id END,
                   MIN(date),
                   MAX(date)
               FROM fact_macro
               WHERE series_id LIKE 'FRED:%%' OR series_id NOT LIKE '%%:%%'
               GROUP BY 1"""
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _latest_dates() -> dict[str, date]:
    return {series_id: span[1] for series_id, span in _date_spans().items()}


def _checked_today() -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_key
            FROM market_source_item_state
            WHERE source='macro'
              AND status IN ('succeeded', 'skipped')
              AND finished_at::date = CURRENT_DATE
            """
        )
        return {row[0] for row in cur.fetchall()}


def _active_series() -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT series_id FROM ref_fred_series WHERE is_active = TRUE ORDER BY series_id")
        return [row[0] for row in cur.fetchall()]


def fetch_fred(
    series_ids: list[str] | None = None,
    start_date: str = "2000-01-01",
    full: bool = False,
) -> int:
    """Fetch and upsert FRED observations for the given (or all active) series.

    If not full, resumes from the last loaded date for each series.
    Returns total row count written.
    """
    api_key = _fred_api_key()
    ids = series_ids or _active_series()
    if not ids:
        return 0

    spans: dict[str, tuple[date, date]] = {} if full else _date_spans()
    latest: dict[str, date] = {series_id: span[1] for series_id, span in spans.items()}
    checked_today = set() if full else _checked_today()
    today = date.today()

    total = 0
    with market_run("macro", full, {"series": len(ids)}) as ctx:
        run_id = str(ctx.run_id)
        for series_id in ids:
            mark_item_running(ctx, "macro", series_id)

            span = spans.get(series_id)
            if not full and series_id in checked_today:
                print(f"FRED: {series_id} already checked today; skipping")
                mark_item_done(
                    ctx,
                    "macro",
                    series_id,
                    status="skipped",
                    rows_in=0,
                    rows_out=0,
                    min_date=span[0] if span else None,
                    max_date=span[1] if span else None,
                )
                continue

            if not full and series_id in latest:
                next_date = latest[series_id] + timedelta(days=1)
                if next_date >= today:
                    print(f"FRED: {series_id} current through {latest[series_id].isoformat()}; skipping")
                    mark_item_done(
                        ctx,
                        "macro",
                        series_id,
                        status="skipped",
                        rows_in=0,
                        rows_out=0,
                        min_date=span[0] if span else None,
                        max_date=span[1] if span else None,
                    )
                    continue
                series_start = next_date.isoformat()
            else:
                series_start = start_date

            try:
                observations = _fetch_series(series_id, series_start, api_key)
            except Exception as exc:
                import warnings
                warnings.warn(f"FRED fetch failed for {series_id}: {exc}")
                mark_item_done(ctx, "macro", series_id, status="failed", error=str(exc)[:4000])
                continue

            rows: list[tuple[Any, ...]] = [
                (series_id, d, v) for d, v in observations
            ]

            if rows:
                ns_id = f"FRED:{series_id}"
                stage_rows = [(run_id, "macro", series_id, *row) for row in rows]
                with connect() as conn, conn.cursor() as cur:
                    execute_values(
                        cur,
                        """
                        INSERT INTO stage_macro
                            (run_id, source, source_key, series_id, date, value)
                        VALUES %s
                        ON CONFLICT (run_id, series_id, date) DO UPDATE SET
                            value=EXCLUDED.value
                        """,
                        stage_rows,
                        page_size=2000,
                    )
                    cur.execute(
                        """
                        INSERT INTO fact_macro (series_id, date, value)
                        SELECT %s::text, date, value
                        FROM stage_macro
                        WHERE run_id=%s AND series_id=%s
                        ON CONFLICT (series_id, date) DO UPDATE SET
                            value      = EXCLUDED.value,
                            updated_at = now()
                        """,
                        (ns_id, run_id, series_id),
                    )
            dates = [row[1] for row in rows]
            mark_item_done(
                ctx,
                "macro",
                series_id,
                status="succeeded" if rows else "skipped",
                rows_in=len(rows),
                rows_out=len(rows),
                min_date=min(dates) if dates else (span[0] if span else None),
                max_date=max(dates) if dates else (span[1] if span else None),
            )
            total += len(rows)

    return total
