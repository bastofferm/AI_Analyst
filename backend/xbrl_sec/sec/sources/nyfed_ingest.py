"""NY Fed Staff Nowcast + Weekly Economic Index (WEI) ingestion.

Both files are published as Excel workbooks (not CSV). The WEI was migrated
from the NY Fed to the Dallas Fed in 2024 — we hit the Dallas URL.

* ``NYFED:NOWCAST``  → NY Fed Staff Nowcast .xlsx (sheet ``Forecasts By Quarter``).
                      Each row is one forecast-date; columns are quarter labels.
                      The headline series is the most-recent (right-most non-NaN)
                      nowcast per row — i.e. the "current-quarter" forecast at
                      that vintage. We emit (forecast_date, current_q_value).
* ``NYFED:WEI``      → Dallas Fed WEI .xlsx (sheet ``2008-current``). Column
                      ``Date`` is the value-date; column ``WEI`` is the headline.

For pre-existing ``NYFED:*`` series whose ``native_id`` starts with ``fred:``
(e.g. ``NYFED:ACM_FIT10`` for the ACM term-premium decomposition served via
FRED), we route through FRED via the BOJ helper.

Run:
    python -m xbrl_sec.sec.sources.nyfed_ingest --full
    python -m xbrl_sec.sec.sources.nyfed_ingest --series NYFED:WEI
"""
from __future__ import annotations

import argparse
import io
import logging
from datetime import date, timedelta

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

from xbrl_sec.sec.sources import macro_cache
from xbrl_sec.sec.sources.boj_ingest import _fetch_via_fred, _fred_api_key, _upsert

logger = logging.getLogger("mzqa.nyfed_ingest")

# NYFED-XLSX endpoints. Keys are the ``native_id`` values stored in
# ``ref_macro_series``. Patch this dict when an upstream URL changes.
NATIVE_TO_URL: dict[str, str] = {
    "wei": "https://www.dallasfed.org/-/media/documents/research/wei/weekly-economic-index.xlsx",
    "staff_nowcast": (
        "https://www.newyorkfed.org/medialibrary/media/research/policy/"
        "nowcast/new-york-fed-staff-nowcast_data_2002-present.xlsx"
    ),
    "yc_recession_12m": "https://www.newyorkfed.org/medialibrary/media/research/capital_markets/allmonth.xls",
}


def _http_get(source: str, native_id: str, url: str, ext: str) -> bytes | None:
    """Download via the on-disk cache (D:/macroData). Returns bytes or None.

    Source label is what gets used in the cache layout; for WEI we tag as
    'dallasfed' since that's where the file now lives, and 'nyfed' for the
    Staff Nowcast which is still on the NY Fed site.
    """
    raw = macro_cache.fetch(source, native_id, url, ext=ext, attempts=3)
    if raw:
        return raw
    # Fall back to the most-recent successfully-cached copy if a previous
    # fetch worked but today's failed (e.g. transient 5xx).
    return macro_cache.read_latest(source, native_id, ext)


def _parse_wei_xlsx(raw: bytes, start: str) -> list[tuple[date, float]]:
    """Dallas Fed WEI xlsx — sheet ``2008-current``."""
    import pandas as pd

    df = pd.read_excel(io.BytesIO(raw), sheet_name="2008-current")
    if df.empty:
        return []
    # Headers are at row 0 — columns include 'Date' and 'WEI' (plus several
    # vintage 'WEI as of <date>' columns we ignore).
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    val_col = "WEI" if "WEI" in df.columns else df.select_dtypes("number").columns[0]
    start_d = date.fromisoformat(start)

    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        raw_d = row[date_col]
        raw_v = row[val_col]
        if pd.isna(raw_d) or pd.isna(raw_v):
            continue
        try:
            d = pd.to_datetime(raw_d).date()
            v = float(raw_v)
        except (ValueError, TypeError):
            continue
        if d < start_d:
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _parse_nowcast_xlsx(raw: bytes, start: str) -> list[tuple[date, float]]:
    """NY Fed Staff Nowcast xlsx — sheet ``Forecasts By Quarter``.

    Sheet has 13 preamble rows; row 13 holds the header (col 0 = 'Forecast
    Date', cols 1..N = quarter labels '2002Q1', '2002Q2', …). Rows 14+ are
    one observation per (forecast_date, quarter) cell. We emit
    (forecast_date, current_quarter_value) where current_quarter_value is
    the right-most non-NaN cell in that row — i.e. the nowcast for "the
    most recent quarter we have a number for as of that forecast date".
    """
    import pandas as pd

    df = pd.read_excel(io.BytesIO(raw), sheet_name="Forecasts By Quarter", header=13)
    if df.empty:
        return []

    date_col = df.columns[0]  # 'Forecast Date'
    quarter_cols = [c for c in df.columns[1:] if isinstance(c, str) and "Q" in c]
    if not quarter_cols:
        return []

    start_d = date.fromisoformat(start)
    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        raw_d = row[date_col]
        if pd.isna(raw_d):
            continue
        try:
            d = pd.to_datetime(raw_d).date()
        except (ValueError, TypeError):
            continue
        if d < start_d:
            continue
        # Right-most non-NaN quarter value = the current-quarter forecast.
        v = None
        for qcol in reversed(quarter_cols):
            val = row[qcol]
            if not pd.isna(val):
                try:
                    v = float(val)
                except (ValueError, TypeError):
                    v = None
                break
        if v is None:
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _parse_yc_recession_workbook(raw: bytes, start: str) -> list[tuple[date, float]]:
    """Parse NY Fed yield-curve recession probability workbook.

    The public monthly-data workbook has changed layout before, so we detect
    the strongest date column and the best recession-probability column rather
    than pinning to a brittle sheet/column name.
    """
    import pandas as pd

    def _read(header: int | None) -> dict[str, pd.DataFrame]:
        return pd.read_excel(io.BytesIO(raw), sheet_name=None, header=header)

    try:
        sheets = _read(0)
    except Exception:
        sheets = _read(None)

    start_d = date.fromisoformat(start)
    best: list[tuple[date, float]] = []

    for df in sheets.values():
        if df.empty:
            continue
        df = df.dropna(how="all").copy()
        if df.empty:
            continue

        date_col = None
        date_score = -1
        for col in df.columns:
            converted = pd.to_datetime(df[col], errors="coerce")
            score = int(converted.notna().sum())
            if "date" in str(col).lower():
                score += 1000
            if score > date_score:
                date_col = col
                date_score = score
        if date_col is None or date_score < 24:
            continue

        probability_col = None
        probability_score = -1
        for col in df.columns:
            if col == date_col:
                continue
            numeric = pd.to_numeric(df[col], errors="coerce")
            count = int(numeric.notna().sum())
            if count < 24:
                continue
            name = str(col).lower()
            score = count
            if "prob" in name:
                score += 1000
            if "recession" in name:
                score += 500
            if "12" in name or "twelve" in name:
                score += 100
            values = numeric.dropna()
            if not values.empty:
                vmax = float(values.max())
                vmin = float(values.min())
                if 0.0 <= vmin and vmax <= 1.0:
                    score += 50
                elif 0.0 <= vmin and vmax <= 100.0:
                    score += 25
            if score > probability_score:
                probability_col = col
                probability_score = score

        if probability_col is None or probability_score < 24:
            continue

        dates = pd.to_datetime(df[date_col], errors="coerce")
        values = pd.to_numeric(df[probability_col], errors="coerce")
        out: list[tuple[date, float]] = []
        for raw_d, raw_v in zip(dates, values):
            if pd.isna(raw_d) or pd.isna(raw_v):
                continue
            d = raw_d.date()
            if d < start_d:
                continue
            v = float(raw_v)
            if v > 1.5:
                v = v / 100.0
            if 0.0 <= v <= 1.0:
                out.append((d, v))
        out.sort(key=lambda r: r[0])
        if len(out) > len(best):
            best = out

    return best


def _fetch_xlsx(native_id: str, start: str) -> list[tuple[date, float]]:
    """Dispatch to the correct sheet parser for *native_id*.

    Routes WEI through the 'dallasfed' cache namespace (since the file
    actually lives at dallasfed.org now) and the Staff Nowcast through
    'nyfed'. This keeps the on-disk cache layout meaningful.
    """
    url = NATIVE_TO_URL.get(native_id)
    if not url:
        raise RuntimeError(f"nyfed_ingest: unknown native_id {native_id!r}")
    cache_source = "dallasfed" if native_id == "wei" else "nyfed"
    ext = "xls" if native_id == "yc_recession_12m" else "xlsx"
    raw = _http_get(cache_source, native_id, url, ext=ext)
    if not raw:
        return []
    if native_id == "wei":
        return _parse_wei_xlsx(raw, start)
    if native_id == "staff_nowcast":
        return _parse_nowcast_xlsx(raw, start)
    if native_id == "yc_recession_12m":
        return _parse_yc_recession_workbook(raw, start)
    raise RuntimeError(f"nyfed_ingest: no parser for native_id {native_id!r}")


def _active_series() -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT series_id, native_id
            FROM   ref_macro_series
            WHERE  source_id = 'nyfed' AND is_active = TRUE
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
            WHERE  s.source_id = 'nyfed'
            GROUP  BY f.series_id
            """
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_nyfed(series_ids: list[str] | None = None, full: bool = False) -> int:
    all_series = _active_series()
    if series_ids:
        wanted = set(series_ids)
        all_series = [(sid, nid) for sid, nid in all_series if sid in wanted]
    if not all_series:
        return 0

    fred_key: str | None = None
    latest = {} if full else _latest_dates()
    total = 0
    with market_run("nyfed", full, {"series": len(all_series)}) as ctx:
        for series_id, native_id in all_series:
            mark_item_running(ctx, "nyfed", series_id)
            start = (latest[series_id] + timedelta(days=1)).isoformat() if (not full and series_id in latest) else "2000-01-01"
            try:
                if native_id.startswith("fred:"):
                    if fred_key is None:
                        fred_key = _fred_api_key()
                    obs = _fetch_via_fred(native_id, start, fred_key)
                else:
                    obs = _fetch_xlsx(native_id, start)
            except Exception as exc:
                logger.warning("nyfed_ingest %s failed: %s", series_id, exc)
                mark_item_done(ctx, "nyfed", series_id, status="failed", error=str(exc)[:4000])
                continue
            if obs:
                _upsert(series_id, obs)
                total += len(obs)
            mark_item_done(
                ctx, "nyfed", series_id,
                status="succeeded" if obs else "skipped",
                rows_in=len(obs), rows_out=len(obs),
                min_date=min(d for d, _ in obs) if obs else None,
                max_date=max(d for d, _ in obs) if obs else None,
            )
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest NY Fed macro series (Nowcast + WEI)")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument("--series", nargs="*", help="Specific series_ids (default: all active)")
    args = p.parse_args()
    n = fetch_nyfed(series_ids=args.series, full=args.full)
    print(f"nyfed_ingest: {n} rows")


if __name__ == "__main__":
    main()
