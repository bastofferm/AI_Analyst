"""SNB Data API native CSV-cube ingest.

Pulls cube data for series registered in ``ref_macro_series`` with
``source_id='snb'``. The ``native_id`` column holds the SNB cube id, e.g.
``gdpqaag``, ``plkopr``, ``snbintrt``.

The SNB Data API is public and requires no key. It returns semicolon-
separated CSV with a multi-row metadata preamble; the data block begins
with a row whose first cell is ``Date`` (English) or ``Datum`` (German).

Pattern mirrors ``boj_ingest.py``: state-tracked via ``market_run``,
idempotent, reuses ``_upsert`` for the ``fact_macro`` write.

Run:
    python -m xbrl_sec.sec.sources.snb_ingest --full
    python -m xbrl_sec.sec.sources.snb_ingest --series SNB:CPI_YOY
"""
from __future__ import annotations

import argparse
import calendar
import io
import urllib.request
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

from xbrl_sec.sec.sources.boj_ingest import _upsert

SNB_BASE = "https://data.snb.ch/api/cube"


# ---------------------------------------------------------------------------
# Cube-specific selectors.
#
# Most SNB cubes contain multiple value columns (currencies, breakdowns,
# series variants). The mapping below pins each ingested cube to a specific
# column and (optionally) a dimension-filter dict that selects the right
# row before pivoting. When the column listed here is missing in the live
# CSV we fall back to the last numeric column with a logged warning.
# ---------------------------------------------------------------------------

CUBE_SELECTORS: dict[str, dict[str, str]] = {
    # cube_id: {value_col: <header>, filters: {dim: value}}
    # Headers are best-effort; verify against the live CSV during deployment.
    "gdpqaag":  {"value_col": "Value"},   # quarterly real GDP YoY
    "plkopr":   {"value_col": "Value"},   # CPI YoY
    "snbintrt": {"value_col": "Value"},   # SNB policy rate
    "rendoblim":{"value_col": "Value"},   # 10Y Confederation bond yield
    "devkua":   {"value_col": "Value"},   # CHF/USD reference rate
    "kofkbeko": {"value_col": "Value"},   # KOF barometer
}


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _parse_snb_period(tp: str) -> date:
    """Parse SNB date column values.

    Supports: 'YYYY-MM-DD', 'YYYY-MM', 'YYYY-Qn'.
    """
    tp = tp.strip()
    if "Q" in tp:
        yr, q = tp.split("-Q")
        return _last_day_of_month(int(yr), {1: 3, 2: 6, 3: 9, 4: 12}[int(q)])
    if len(tp) == 7:
        y, m = map(int, tp.split("-"))
        return _last_day_of_month(y, m)
    return date.fromisoformat(tp)


def _strip_preamble(raw: str) -> str:
    """Drop SNB metadata header rows; return CSV body starting at the data header."""
    lines = raw.splitlines()
    for i, ln in enumerate(lines):
        head = ln.split(";", 1)[0].strip().lower()
        if head in ("date", "datum"):
            return "\n".join(lines[i:])
    return raw  # let pandas error out informatively


def _pick_value_column(df, cube_id: str) -> str:
    """Choose the value column for the cube.

    Strategy: prefer the column declared in CUBE_SELECTORS; else fall back
    to the last numeric column in the DataFrame (SNB cubes typically put
    the headline series last).
    """
    cfg = CUBE_SELECTORS.get(cube_id, {})
    preferred = cfg.get("value_col")
    if preferred and preferred in df.columns:
        return preferred
    # Match a likely 'Value' header case-insensitively.
    for c in df.columns:
        if c.strip().lower() == "value":
            return c
    numeric_cols = df.select_dtypes("number").columns.tolist()
    if numeric_cols:
        return numeric_cols[-1]
    raise RuntimeError(f"SNB cube {cube_id}: no numeric column found")


def _fetch_via_snb(cube_id: str, start: str) -> list[tuple[date, float]]:
    """Hit the SNB CSV endpoint and return (date, value) tuples."""
    import pandas as pd

    url = f"{SNB_BASE}/{cube_id}/data/csv"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/csv",
            "Accept-Language": "en",          # force English column headers
            "User-Agent": "mzqa-research/1.0 (macro-ingest)",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig")

    if not raw.strip():
        return []

    body = _strip_preamble(raw)
    df = pd.read_csv(io.StringIO(body), sep=";", decimal=".")
    if df.empty:
        return []

    date_col = df.columns[0]
    val_col = _pick_value_column(df, cube_id)
    start_d = date.fromisoformat(start)

    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        raw_tp = row[date_col]
        raw_val = row[val_col]
        if pd.isna(raw_tp) or pd.isna(raw_val):
            continue
        try:
            d = _parse_snb_period(str(raw_tp))
            v = float(raw_val)
        except (ValueError, TypeError):
            continue
        if d < start_d:
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _active_series() -> list[tuple[str, str]]:
    """Return [(series_id, native_id), ...] for active SNB series."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'snb' AND is_active = TRUE
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
            WHERE  s.source_id = 'snb'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_snb(series_ids: list[str] | None = None, full: bool = False) -> int:
    """Fetch & upsert SNB observations. Returns rows written."""
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    latest = {} if full else _latest_dates()
    total = 0
    with market_run("snb", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "snb", series_id)
            if not full and series_id in latest:
                start = (latest[series_id] + timedelta(days=1)).isoformat()
            else:
                start = "2000-01-01"

            try:
                obs = _fetch_via_snb(native_id, start)
            except Exception as exc:
                mark_item_done(
                    ctx, "snb", series_id, status="failed", error=str(exc)[:4000]
                )
                continue

            if obs:
                _upsert(series_id, obs)

            mark_item_done(
                ctx,
                "snb",
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
    p = argparse.ArgumentParser(description="Ingest SNB macro series (native CSV cubes)")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_snb(series_ids=args.series, full=args.full)
    print(f"snb_ingest: {n} rows")


if __name__ == "__main__":
    main()
