"""Cross-asset ETF proxy price ingestion.

Downloads daily OHLCV for a cross-asset universe defined in dim_cross_asset
(sec schema). Writes into fact_cross_asset.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_items_running

# ---------------------------------------------------------------------------
# Universe — loaded from sec.dim_cross_asset at runtime
# ---------------------------------------------------------------------------

def _load_cross_asset_universe() -> list[tuple[str, str]]:
    """Return [(ticker, asset_class), ...] from sec.dim_cross_asset."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT ticker, asset_class FROM dim_cross_asset ORDER BY ticker")
        return cur.fetchall()


def _date_spans() -> dict[str, tuple[date, date]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, MIN(date), MAX(date) FROM fact_cross_asset GROUP BY ticker"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _frame_for_ticker(raw: Any, ticker: str, ticker_count: int) -> Any | None:
    """Return the yfinance frame for one ticker from single or grouped output."""
    try:
        columns = raw.columns
        if getattr(columns, "nlevels", 1) > 1:
            level0 = columns.get_level_values(0)
            if ticker in level0:
                return raw[ticker].copy()
            level1 = columns.get_level_values(1)
            if ticker in level1:
                return raw.xs(ticker, axis=1, level=1).copy()
            return None
        if ticker_count == 1:
            return raw.copy()
    except (KeyError, TypeError, AttributeError):
        return None
    return None


def _append_price_rows(
    rows: list[tuple[Any, ...]],
    ticker: str,
    asset_class: str,
    df: Any,
) -> list[date]:
    if df.empty or "Close" not in df.columns:
        return []

    df = df.dropna(subset=["Close"])
    if df.empty:
        return []

    adj_closes = df["Adj Close"].astype(float) if "Adj Close" in df.columns else df["Close"].astype(float)
    raw_closes = df["Close"].astype(float)
    volumes = df.get("Volume")
    written_dates: list[date] = []

    for i, (idx, _row) in enumerate(df.iterrows()):
        d = idx.date() if hasattr(idx, "date") else idx
        close_v = float(raw_closes.iloc[i])
        adj_v = float(adj_closes.iloc[i])
        vol_v = None
        if volumes is not None:
            try:
                volume = float(volumes.iloc[i])
                vol_v = int(volume) if not math.isnan(volume) else None
            except (TypeError, ValueError):
                vol_v = None

        if math.isnan(close_v):
            continue

        if i > 0 and not math.isnan(adj_v):
            prev = float(adj_closes.iloc[i - 1])
            if not math.isnan(prev) and prev != 0:
                ret = (adj_v - prev) / prev
                log_ret = math.log(adj_v / prev) if adj_v > 0 and prev > 0 else None
            else:
                ret = log_ret = None
        else:
            ret = log_ret = None

        rows.append((d, ticker, asset_class, close_v, adj_v, ret, log_ret, vol_v, "USD"))
        written_dates.append(d)

    return written_dates


def _print_progress(done: int, total: int) -> None:
    if done == total or done % 10 == 0:
        print(f"{done} / {total} tickers processed")


def fetch_cross_asset(full: bool = False) -> int:
    """Download cross-asset ETF prices and upsert into fact_cross_asset.

    Universe is loaded at runtime from dim_cross_asset in the quant DB.
    Returns total rows written.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")

    universe = _load_cross_asset_universe()
    ticker_class: dict[str, str] = {t: cls for t, cls in universe}
    tickers = [t for t, _ in universe]
    total = len(tickers)

    spans = _date_spans()
    latest = {ticker: span[1] for ticker, span in spans.items()}
    default_start = date(2000, 1, 1)
    end_day = date.today()
    end_date = end_day.isoformat()

    if not tickers:
        print("Cross-asset: no tickers configured in dim_cross_asset")
        return 0

    if full:
        starts = {t: default_start for t in tickers}
    else:
        starts = {
            t: (latest[t] + timedelta(days=1) if t in latest else default_start)
            for t in tickers
        }

    grouped_starts: dict[date, list[str]] = {}
    for ticker, start in starts.items():
        grouped_starts.setdefault(start, []).append(ticker)

    rows: list[tuple[Any, ...]] = []
    by_ticker: dict[str, list[date]] = {}
    done = 0
    existing_count = sum(1 for ticker in tickers if ticker in latest)
    new_count = total - existing_count
    earliest_start = min(starts.values())
    latest_start = max(starts.values())
    scope = {
        "tickers": total,
        "existing_tickers": existing_count,
        "new_tickers": new_count,
        "start_groups": len(grouped_starts),
        "earliest_start": earliest_start,
        "latest_start": latest_start,
        "end_date": end_date,
    }

    with market_run("cross_asset", full, scope) as ctx:
        run_id = str(ctx.run_id)
        mark_items_running(ctx, "cross_asset", tickers)

        if full:
            print(f"Cross-asset: full download for {total} tickers from {default_start.isoformat()}")
        else:
            print(
                "Cross-asset: incremental download for "
                f"{total} tickers; {existing_count} existing, {new_count} new; "
                f"{len(grouped_starts)} start-date groups; earliest {earliest_start.isoformat()}, "
                f"latest {latest_start.isoformat()}"
            )

        for start, group in sorted(grouped_starts.items()):
            group_total = len(group)
            if start >= end_day:
                print(
                    "Cross-asset: skipping "
                    f"{group_total} current tickers with start {start.isoformat()} >= end {end_date}"
                )
                done += group_total
                _print_progress(done, total)
                continue

            print(f"Cross-asset: downloading {group_total} tickers from {start.isoformat()}")
            raw = yf.download(
                group,
                start=start.isoformat(),
                end=end_date,
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
            )

            if raw is None or raw.empty:
                print(f"Cross-asset: no data returned from yfinance for start {start.isoformat()}")
                done += group_total
                _print_progress(done, total)
                continue

            for ticker in group:
                df = _frame_for_ticker(raw, ticker, group_total)
                if df is None:
                    done += 1
                    _print_progress(done, total)
                    continue

                if not full and ticker in latest:
                    cutoff = latest[ticker] + timedelta(days=1)
                    df = df[df.index.date >= cutoff]  # type: ignore[operator]

                dates = _append_price_rows(
                    rows,
                    ticker,
                    ticker_class.get(ticker, "Other"),
                    df,
                )
                if dates:
                    by_ticker[ticker] = dates

                done += 1
                _print_progress(done, total)

        if not rows:
            for ticker in tickers:
                span = spans.get(ticker)
                mark_item_done(
                    ctx,
                    "cross_asset",
                    ticker,
                    status="skipped",
                    rows_in=0,
                    rows_out=0,
                    min_date=span[0] if span else None,
                    max_date=span[1] if span else None,
                )
            return 0

        stage_rows = [(run_id, "cross_asset", row[1], *row) for row in rows]
        with connect() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO stage_cross_asset
                    (run_id, source, source_key, date, ticker, asset_class, close,
                     adj_close, return, log_return, volume, currency)
                VALUES %s
                ON CONFLICT (run_id, ticker, date) DO UPDATE SET
                    asset_class=EXCLUDED.asset_class,
                    close=EXCLUDED.close,
                    adj_close=EXCLUDED.adj_close,
                    return=EXCLUDED.return,
                    log_return=EXCLUDED.log_return,
                    volume=EXCLUDED.volume,
                    currency=EXCLUDED.currency
                """,
                stage_rows,
                page_size=5000,
            )
            cur.execute(
                """
                INSERT INTO fact_cross_asset
                    (date, ticker, asset_class, close, adj_close, return, log_return, volume, currency)
                SELECT date, ticker, asset_class, close, adj_close, return, log_return, volume, currency
                FROM stage_cross_asset
                WHERE run_id=%s
                ON CONFLICT (ticker, date) DO UPDATE SET
                    close      = EXCLUDED.close,
                    adj_close  = EXCLUDED.adj_close,
                    return     = EXCLUDED.return,
                    log_return = EXCLUDED.log_return,
                    volume     = EXCLUDED.volume
                """,
                (run_id,),
            )

        for ticker in tickers:
            dates = by_ticker.get(ticker, [])
            span = spans.get(ticker)
            mark_item_done(
                ctx,
                "cross_asset",
                ticker,
                status="succeeded" if dates else "skipped",
                rows_in=len(dates),
                rows_out=len(dates),
                min_date=min(dates) if dates else (span[0] if span else None),
                max_date=max(dates) if dates else (span[1] if span else None),
            )

    return len(rows)
