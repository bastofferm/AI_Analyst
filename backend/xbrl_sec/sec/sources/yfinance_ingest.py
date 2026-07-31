"""Yahoo Finance price ingestion and market metric derivation.

Populates fact_prices_us/fact_prices_jp from yfinance and derives market
metrics into fact_market_metrics. Standardized fundamentals remain XBRL-only.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running, mark_items_running


def _configure_yfinance_cache(yf) -> None:
    cache_dir = load_settings().project_root / ".cache" / "yfinance"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(str(cache_dir))


def _active_us_tickers() -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ticker
            FROM ref_entity_ticker
            WHERE jurisdiction = 'US'
              AND is_primary = TRUE
            ORDER BY ticker
            """
        )
        return [row[0] for row in cur.fetchall()]


def _active_jp_tickers() -> list[str]:
    """Return yfinance-formatted JP tickers (appends .T suffix for TSE stocks).

    TSE 4-digit codes (e.g. 7203) must be requested as '7203.T' in yfinance.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ticker
            FROM ref_entity_ticker
            WHERE jurisdiction = 'JP'
              AND is_primary = TRUE
            ORDER BY ticker
            """
        )
        raw = [row[0] for row in cur.fetchall()]
    return [f"{t}.T" if t.isdigit() and len(t) <= 4 else t for t in raw]


def _strip_jp_suffix(ticker: str) -> str:
    """Remove .T suffix for storage (fact_prices_jp uses bare codes)."""
    return ticker[:-2] if ticker.endswith(".T") else ticker


def _entity_tickers(jurisdiction: str) -> dict[str, str]:
    """Map stored tickers to their primary entity id for the jurisdiction."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, entity_id
            FROM ref_entity_ticker
            WHERE jurisdiction = %s
              AND is_primary = TRUE
            """,
            (jurisdiction,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _dedupe_market_metric_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Keep one row per fact_market_metrics primary key."""
    keyed: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in rows:
        keyed[(row[0], row[1], row[2], row[3], row[4], row[7])] = row
    return list(keyed.values())


def _price_date_spans(table_name: str) -> dict[str, tuple[date, date]]:
    if table_name not in {"fact_prices_us", "fact_prices_jp"}:
        raise ValueError(f"Unsupported price table: {table_name}")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT ticker, MIN(date), MAX(date) FROM {table_name} GROUP BY ticker"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _latest_price_dates() -> dict[str, date]:
    return {ticker: span[1] for ticker, span in _price_date_spans("fact_prices_us").items()}


def _frame_for_yf_ticker(raw: Any, ticker: str, ticker_count: int) -> Any | None:
    """Return one ticker frame from yfinance single- or multi-ticker output."""
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
    df: Any,
    currency: str,
    jurisdiction: str,
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
                abs_diff = adj_v - prev
            else:
                ret = log_ret = abs_diff = None
        else:
            ret = log_ret = abs_diff = None

        rows.append((d, ticker, close_v, adj_v, ret, log_ret, abs_diff, vol_v, currency, jurisdiction))
        written_dates.append(d)

    return written_dates


def _print_price_progress(done: int, total: int) -> None:
    if done == total or done % 25 == 0:
        print(f"{done} / {total} tickers processed")


_PRICES_CFG = {
    "US": {
        "table": "fact_prices_us",
        "source": "prices_us",
        "currency": "USD",
    },
    "JP": {
        "table": "fact_prices_jp",
        "source": "prices_jp",
        "currency": "JPY",
    },
}


def fetch_prices(
    tickers: list[str],
    start_date: str = "2000-01-01",
    end_date: str | None = None,
    incremental: bool = False,
    jurisdiction: str = "US",
) -> int:
    """Download daily OHLCV from yfinance and upsert into fact_prices_{us|jp}.

    For JP, callers pass yfinance-formatted tickers (with .T suffix). Storage
    in fact_prices_jp uses the bare code (suffix stripped), matching the
    existing JP price-table convention.

    Returns total row count written.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError(
            "yfinance is not installed. Run: pip install yfinance"
        )
    _configure_yfinance_cache(yf)

    jurisdiction = jurisdiction.upper()
    if jurisdiction not in _PRICES_CFG:
        raise ValueError(f"jurisdiction must be one of {list(_PRICES_CFG)}; got {jurisdiction!r}")
    cfg = _PRICES_CFG[jurisdiction]
    table = cfg["table"]
    source_name = cfg["source"]
    currency = cfg["currency"]
    label = f"Prices {jurisdiction}"

    if not tickers:
        return 0

    # Storage ticker = bare code (no .T) for JP, identical to query ticker for US.
    # We do all internal lookups keyed by the storage form so latest/spans align
    # with the table layout.
    def storage_form(t: str) -> str:
        return _strip_jp_suffix(t) if jurisdiction == "JP" else t

    spans = _price_date_spans(table)
    latest = {ticker: span[1] for ticker, span in spans.items()}
    default_start = date.fromisoformat(start_date)
    end_str = end_date or date.today().isoformat()
    end_day = date.fromisoformat(end_str)

    if incremental:
        ticker_starts = {
            ticker: (
                latest[storage_form(ticker)] + timedelta(days=1)
                if storage_form(ticker) in latest
                else default_start
            )
            for ticker in tickers
        }
    else:
        ticker_starts = {ticker: default_start for ticker in tickers}

    grouped_starts: dict[date, list[str]] = {}
    for ticker, start in ticker_starts.items():
        grouped_starts.setdefault(start, []).append(ticker)

    # yfinance batch download — multi-ticker returns MultiIndex columns
    rows: list[tuple[Any, ...]] = []
    by_ticker: dict[str, list[date]] = {}
    total = len(tickers)
    done = 0
    existing_count = sum(1 for ticker in tickers if storage_form(ticker) in latest)
    new_count = total - existing_count
    earliest_start = min(ticker_starts.values())
    latest_start = max(ticker_starts.values())
    scope = {
        "tickers": total,
        "existing_tickers": existing_count,
        "new_tickers": new_count,
        "start_groups": len(grouped_starts),
        "earliest_start": earliest_start,
        "latest_start": latest_start,
        "end_date": end_str,
    }

    with market_run(source_name, not incremental, scope) as ctx:
        run_id = str(ctx.run_id)
        mark_items_running(ctx, source_name, [storage_form(t) for t in tickers])

        if incremental:
            print(
                f"{label}: incremental download for "
                f"{total} tickers; {existing_count} existing, {new_count} new; "
                f"{len(grouped_starts)} start-date groups; earliest {earliest_start.isoformat()}, "
                f"latest {latest_start.isoformat()}"
            )
        else:
            print(f"{label}: full download for {total} tickers from {default_start.isoformat()}")

        for start, group in sorted(grouped_starts.items()):
            group_total = len(group)
            if start >= end_day:
                print(f"{label}: skipping {group_total} current tickers with start {start.isoformat()} >= end {end_str}")
                done += group_total
                _print_price_progress(done, total)
                continue

            print(f"{label}: downloading {group_total} tickers from {start.isoformat()}")
            raw = yf.download(
                group,
                start=start.isoformat(),
                end=end_str,
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
            )

            if raw is None or raw.empty:
                print(f"{label}: no data returned from yfinance for start {start.isoformat()}")
                done += group_total
                _print_price_progress(done, total)
                continue

            for ticker in group:
                df = _frame_for_yf_ticker(raw, ticker, group_total)
                if df is None:
                    done += 1
                    _print_price_progress(done, total)
                    continue

                store_ticker = storage_form(ticker)
                if incremental and store_ticker in latest:
                    cutoff = latest[store_ticker] + timedelta(days=1)
                    df = df[df.index.date >= cutoff]  # type: ignore[operator]

                dates = _append_price_rows(rows, store_ticker, df, currency, jurisdiction)
                if dates:
                    by_ticker[store_ticker] = dates

                done += 1
                _print_price_progress(done, total)

        if rows:
            stage_rows = [(run_id, source_name, row[1], *row) for row in rows]
            with connect() as conn, conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO stage_prices
                        (run_id, source, source_key, date, ticker, close, adj_close,
                         return, log_return, abs_diff, volume, currency, jurisdiction)
                    VALUES %s
                    ON CONFLICT (run_id, source, ticker, date) DO UPDATE SET
                        close=EXCLUDED.close,
                        adj_close=EXCLUDED.adj_close,
                        return=EXCLUDED.return,
                        log_return=EXCLUDED.log_return,
                        abs_diff=EXCLUDED.abs_diff,
                        volume=EXCLUDED.volume,
                        currency=EXCLUDED.currency
                    """,
                    stage_rows,
                    page_size=5000,
                )
                cur.execute(
                    f"""
                    INSERT INTO {table}
                        (date, ticker, close, adj_close, return, log_return, abs_diff, volume, currency, jurisdiction)
                    SELECT date, ticker, close, adj_close, return, log_return, abs_diff, volume, currency, jurisdiction
                    FROM stage_prices
                    WHERE run_id=%s AND source=%s
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        close      = EXCLUDED.close,
                        adj_close  = EXCLUDED.adj_close,
                        return     = EXCLUDED.return,
                        log_return = EXCLUDED.log_return,
                        abs_diff   = EXCLUDED.abs_diff,
                        volume     = EXCLUDED.volume,
                        currency   = EXCLUDED.currency
                    """,
                    (run_id, source_name),
                )

        for ticker in tickers:
            store_ticker = storage_form(ticker)
            dates = by_ticker.get(store_ticker, [])
            span = spans.get(store_ticker)
            mark_item_done(
                ctx,
                source_name,
                store_ticker,
                status="succeeded" if dates else "skipped",
                rows_in=len(dates),
                rows_out=len(dates),
                min_date=min(dates) if dates else (span[0] if span else None),
                max_date=max(dates) if dates else (span[1] if span else None),
            )

    # Forward-fill shares_outstanding on the fresh rows. Narrowed to the date
    # range we just touched so this is a cheap UPDATE.
    if rows:
        try:
            from xbrl_sec.sec.sources.shares_backfill import backfill as _shares_backfill
            since = min(r[0] for r in rows)
            _shares_backfill(jurisdiction, since_date=since)
        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).warning("%s shares forward-fill skipped: %s", jurisdiction, exc)

    return len(rows)


def fetch_stock_splits(
    jurisdiction: str = "US",
    tickers: list[str] | None = None,
    start_date: str = "2008-01-01",
    full: bool = False,
) -> int:
    """Download stock split events from yfinance into fact_stock_split_event.

    Split ratios use new-shares / old-share convention: a 4-for-1 split is 4.0,
    while a 1-for-10 reverse split is 0.1.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")
    _configure_yfinance_cache(yf)

    jurisdiction = jurisdiction.upper()
    if jurisdiction not in {"US", "JP"}:
        raise ValueError("jurisdiction must be US or JP")

    raw_tickers = tickers or (_active_jp_tickers() if jurisdiction == "JP" else _active_us_tickers())
    if not raw_tickers:
        return 0

    entity_by_ticker = _entity_tickers(jurisdiction)
    cutoff = date.fromisoformat(start_date)
    source = f"stock_splits_{jurisdiction.lower()}"

    def _yf_ticker(ticker: str) -> str:
        if jurisdiction != "JP":
            return ticker
        return ticker if ticker.endswith(".T") else (f"{ticker}.T" if ticker.isdigit() and len(ticker) <= 4 else ticker)

    def _stored_ticker(yf_ticker: str) -> str:
        return _strip_jp_suffix(yf_ticker) if jurisdiction == "JP" else yf_ticker

    def _flush(run_id: str, batch_rows: list[tuple[Any, ...]]) -> None:
        if not batch_rows:
            return
        with connect() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO stage_stock_splits
                    (run_id, source, source_key, jurisdiction, entity_id, ticker,
                     event_date, effective_date, split_ratio, source_type,
                     source_filing_id, confidence, notes)
                VALUES %s
                ON CONFLICT (run_id, jurisdiction, ticker, effective_date, source_type)
                DO UPDATE SET
                    entity_id=EXCLUDED.entity_id,
                    event_date=EXCLUDED.event_date,
                    split_ratio=EXCLUDED.split_ratio,
                    source_filing_id=EXCLUDED.source_filing_id,
                    confidence=EXCLUDED.confidence,
                    notes=EXCLUDED.notes
                """,
                batch_rows,
                page_size=1000,
            )
            cur.execute(
                """
                INSERT INTO fact_stock_split_event
                    (jurisdiction, entity_id, ticker, event_date, effective_date,
                     split_ratio, source_type, source_filing_id, confidence, notes)
                SELECT jurisdiction, entity_id, ticker, event_date, effective_date,
                       split_ratio, source_type, source_filing_id, confidence, notes
                FROM stage_stock_splits
                WHERE run_id=%s AND source=%s
                ON CONFLICT (jurisdiction, ticker, effective_date, source_type)
                DO UPDATE SET
                    entity_id=EXCLUDED.entity_id,
                    event_date=EXCLUDED.event_date,
                    split_ratio=EXCLUDED.split_ratio,
                    source_filing_id=EXCLUDED.source_filing_id,
                    confidence=EXCLUDED.confidence,
                    notes=EXCLUDED.notes,
                    updated_at=now()
                """,
                (run_id, source),
            )

    with market_run(source, full, {"tickers": len(raw_tickers), "start_date": start_date}) as ctx:
        run_id = str(ctx.run_id)
        total_rows = 0
        yf_tickers = [_yf_ticker(ticker) for ticker in raw_tickers]
        for start in range(0, len(yf_tickers), 100):
            batch = yf_tickers[start:start + 100]
            for yf_ticker in batch:
                mark_item_running(ctx, source, _stored_ticker(yf_ticker))
            try:
                raw = yf.download(
                    batch,
                    start=start_date,
                    end=date.today().isoformat(),
                    actions=True,
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                )
            except Exception as exc:
                for yf_ticker in batch:
                    mark_item_done(ctx, source, _stored_ticker(yf_ticker), status="failed", error=str(exc))
                continue

            if raw is None or raw.empty:
                for yf_ticker in batch:
                    mark_item_done(ctx, source, _stored_ticker(yf_ticker), status="skipped")
                continue

            batch_rows: list[tuple[Any, ...]] = []
            single = len(batch) == 1
            for yf_ticker in batch:
                stored_ticker = _stored_ticker(yf_ticker)
                ticker_rows: list[tuple[Any, ...]] = []
                try:
                    df = raw.copy() if single else raw[yf_ticker].copy()
                except (KeyError, TypeError):
                    mark_item_done(ctx, source, stored_ticker, status="skipped")
                    continue
                if df.empty or "Stock Splits" not in df.columns:
                    mark_item_done(ctx, source, stored_ticker, status="skipped")
                    continue
                splits = df["Stock Splits"].dropna()
                splits = splits[splits.astype(float) != 0.0]
                for idx, value in splits.items():
                    effective = idx.date() if hasattr(idx, "date") else idx
                    if effective < cutoff:
                        continue
                    ratio = float(value)
                    if ratio <= 0 or math.isnan(ratio) or math.isinf(ratio):
                        continue
                    ticker_rows.append(
                        (
                            run_id,
                            source,
                            stored_ticker,
                            jurisdiction,
                            entity_by_ticker.get(stored_ticker),
                            stored_ticker,
                            effective,
                            effective,
                            ratio,
                            "YFINANCE",
                            None,
                            0.95,
                            "Downloaded from yfinance ticker actions.",
                        )
                    )
                batch_rows.extend(ticker_rows)
                mark_item_done(
                    ctx,
                    source,
                    stored_ticker,
                    status="succeeded" if ticker_rows else "skipped",
                    rows_in=len(ticker_rows),
                    rows_out=len(ticker_rows),
                    min_date=min(row[7] for row in ticker_rows) if ticker_rows else None,
                    max_date=max(row[7] for row in ticker_rows) if ticker_rows else None,
                )
            _flush(run_id, batch_rows)
            total_rows += len(batch_rows)

    return total_rows


def _latest_jp_price_dates() -> dict[str, date]:
    return {ticker: span[1] for ticker, span in _price_date_spans("fact_prices_jp").items()}


def fetch_jp_prices(
    tickers: list[str] | None = None,
    full: bool = False,
    start_date: str = "2008-01-01",
) -> int:
    """Download JP equity prices from yfinance and upsert into fact_prices_jp.

    Tickers should be bare TSE codes (e.g. '7203'); the .T suffix is added
    internally for the yfinance query and stripped before storage.
    Returns rows written.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")
    _configure_yfinance_cache(yf)

    raw_tickers = tickers or _active_jp_tickers()
    if not raw_tickers:
        return 0

    # Ensure .T suffix for yfinance
    yf_tickers = [t if t.endswith(".T") else (f"{t}.T" if t.isdigit() and len(t) <= 4 else t)
                  for t in raw_tickers]

    spans = _price_date_spans("fact_prices_jp")
    latest = {ticker: span[1] for ticker, span in spans.items()}
    default_start = date.fromisoformat(start_date)
    end_date = date.today().isoformat()
    end_day = date.fromisoformat(end_date)

    if full:
        ticker_starts = {_strip_jp_suffix(t): default_start for t in yf_tickers}
    else:
        ticker_starts = {
            _strip_jp_suffix(t): (
                latest[_strip_jp_suffix(t)] + timedelta(days=1)
                if _strip_jp_suffix(t) in latest
                else default_start
            )
            for t in yf_tickers
        }

    grouped_starts: dict[date, list[str]] = {}
    for yf_ticker in yf_tickers:
        grouped_starts.setdefault(ticker_starts[_strip_jp_suffix(yf_ticker)], []).append(yf_ticker)

    rows: list[tuple[Any, ...]] = []
    by_ticker: dict[str, list[date]] = {}
    total = len(yf_tickers)
    done = 0
    existing_count = sum(1 for yf_ticker in yf_tickers if _strip_jp_suffix(yf_ticker) in latest)
    new_count = total - existing_count
    earliest_start = min(ticker_starts.values())
    latest_start = max(ticker_starts.values())
    scope = {
        "tickers": total,
        "existing_tickers": existing_count,
        "new_tickers": new_count,
        "start_groups": len(grouped_starts),
        "earliest_start": earliest_start,
        "latest_start": latest_start,
        "end_date": end_date,
    }

    with market_run("prices_jp", full, scope) as ctx:
        run_id = str(ctx.run_id)
        mark_items_running(ctx, "prices_jp", [_strip_jp_suffix(ticker) for ticker in raw_tickers])

        if full:
            print(f"Prices JP: full download for {total} tickers from {default_start.isoformat()}")
        else:
            print(
                "Prices JP: incremental download for "
                f"{total} tickers; {existing_count} existing, {new_count} new; "
                f"{len(grouped_starts)} start-date groups; earliest {earliest_start.isoformat()}, "
                f"latest {latest_start.isoformat()}"
            )

        for start, group in sorted(grouped_starts.items()):
            group_total = len(group)
            if start >= end_day:
                print(f"Prices JP: skipping {group_total} current tickers with start {start.isoformat()} >= end {end_date}")
                done += group_total
                _print_price_progress(done, total)
                continue

            print(f"Prices JP: downloading {group_total} tickers from {start.isoformat()}")
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
                print(f"Prices JP: no data returned from yfinance for start {start.isoformat()}")
                done += group_total
                _print_price_progress(done, total)
                continue

            for yf_ticker in group:
                bare = _strip_jp_suffix(yf_ticker)
                df = _frame_for_yf_ticker(raw, yf_ticker, group_total)
                if df is None:
                    done += 1
                    _print_price_progress(done, total)
                    continue

                if not full and bare in latest:
                    cutoff = latest[bare] + timedelta(days=1)
                    df = df[df.index.date >= cutoff]  # type: ignore[operator]

                dates = _append_price_rows(rows, bare, df, "JPY", "JP")
                if dates:
                    by_ticker[bare] = dates

                done += 1
                _print_price_progress(done, total)

        if rows:
            stage_rows = [(run_id, "prices_jp", row[1], *row) for row in rows]
            with connect() as conn, conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO stage_prices
                        (run_id, source, source_key, date, ticker, close, adj_close,
                         return, log_return, abs_diff, volume, currency, jurisdiction)
                    VALUES %s
                    ON CONFLICT (run_id, source, ticker, date) DO UPDATE SET
                        close=EXCLUDED.close,
                        adj_close=EXCLUDED.adj_close,
                        return=EXCLUDED.return,
                        log_return=EXCLUDED.log_return,
                        abs_diff=EXCLUDED.abs_diff,
                        volume=EXCLUDED.volume,
                        currency=EXCLUDED.currency
                    """,
                    stage_rows,
                    page_size=5000,
                )
                cur.execute(
                    """
                    INSERT INTO fact_prices_jp
                        (date, ticker, close, adj_close, return, log_return, abs_diff, volume, currency, jurisdiction)
                    SELECT date, ticker, close, adj_close, return, log_return, abs_diff, volume, currency, jurisdiction
                    FROM stage_prices
                    WHERE run_id=%s AND source='prices_jp'
                    ON CONFLICT (ticker, date) DO UPDATE SET
                        close      = EXCLUDED.close,
                        adj_close  = EXCLUDED.adj_close,
                        return     = EXCLUDED.return,
                        log_return = EXCLUDED.log_return,
                        abs_diff   = EXCLUDED.abs_diff,
                        volume     = EXCLUDED.volume,
                        currency   = EXCLUDED.currency
                    """,
                    (run_id,),
                )

        for ticker in raw_tickers:
            key = _strip_jp_suffix(ticker)
            dates = by_ticker.get(key, [])
            span = spans.get(key)
            mark_item_done(
                ctx,
                "prices_jp",
                key,
                status="succeeded" if dates else "skipped",
                rows_in=len(dates),
                rows_out=len(dates),
                min_date=min(dates) if dates else (span[0] if span else None),
                max_date=max(dates) if dates else (span[1] if span else None),
            )

    # Forward-fill shares_outstanding on the fresh rows from XBRL fundamentals.
    if rows:
        try:
            from xbrl_sec.sec.sources.shares_backfill import backfill as _shares_backfill
            since = min(r[0] for r in rows)
            _shares_backfill("JP", since_date=since)
        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).warning("JP shares forward-fill skipped: %s", exc)

    return len(rows)


def derive_market_metrics(
    tickers: list[str] | None = None,
    full: bool = False,
) -> int:
    """Derive stock_price and market_capitalization using filed shares + prices.

    Writes rows to fact_market_metrics, not standardized fundamentals.
    Returns total row count written.
    """
    ticker_filter = ""
    params: list[Any] = []
    if tickers:
        ticker_filter = "AND t.ticker = ANY(%s)"
        params.append(tickers)

    full_filter = ""
    if not full:
        full_filter = """
            AND NOT EXISTS (
                SELECT 1 FROM fact_market_metrics mx
                WHERE mx.jurisdiction = 'US'
                  AND mx.entity_id = f.cik
                  AND mx.ticker = t.ticker
                  AND mx.fiscal_year = f.fiscal_year
                  AND mx.fiscal_period = f.fiscal_period
                  AND mx.metric_id IN ('stock_price', 'market_capitalization')
            )
        """

    query = f"""
        SELECT f.cik, t.ticker, f.fiscal_year, f.fiscal_period, f.period_end,
               f.value AS shares_diluted
        FROM fact_fundamentals_std_us f
        JOIN ref_entity_ticker t
          ON t.entity_id = f.cik
         AND t.jurisdiction = 'US'
         AND t.is_primary = TRUE
        WHERE f.line_item_id = 'shares_outstanding_diluted'
          AND f.period_end IS NOT NULL
          AND f.value > 0
          {ticker_filter}
          {full_filter}
        ORDER BY f.cik, f.fiscal_year, f.fiscal_period
    """

    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params or None)
        rows = cur.fetchall()

    if not rows:
        return 0

    # Load prices once for all relevant tickers
    needed_tickers = list({r[1] for r in rows})
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, date, close
            FROM fact_prices_us
            WHERE ticker = ANY(%s)
              AND close IS NOT NULL
            ORDER BY ticker, date
            """,
            (needed_tickers,),
        )
        price_rows = cur.fetchall()

    # Build per-ticker sorted list of (date, close) for binary search
    from bisect import bisect_right
    ticker_dates: dict[str, list[date]] = {}
    ticker_closes: dict[str, list[float]] = {}
    for ticker, d, close in price_rows:
        ticker_dates.setdefault(ticker, []).append(d)
        ticker_closes.setdefault(ticker, []).append(float(close))

    def _price_at(ticker: str, period_end: date) -> tuple[date, float] | None:
        dates = ticker_dates.get(ticker)
        if not dates:
            return None
        idx = bisect_right(dates, period_end) - 1
        if idx < 0:
            return None
        return dates[idx], ticker_closes[ticker][idx]

    out_rows: list[tuple[Any, ...]] = []

    for cik, ticker, fiscal_year, fiscal_period, period_end, shares in rows:
        priced = _price_at(ticker, period_end)
        if priced is None:
            continue
        market_date, px = priced
        mkt_cap = px * float(shares)

        base = ("US", cik, ticker, fiscal_year, fiscal_period, period_end, market_date)
        out_rows.append((*base, "stock_price", px, "USD", "yfinance"))
        out_rows.append((*base, "market_capitalization", mkt_cap, "USD", "yfinance"))

    if not out_rows:
        return 0
    out_rows = _dedupe_market_metric_rows(out_rows)

    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_market_metrics
                (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, period_end,
                 market_date, metric_id, value, currency, source)
            VALUES %s
            ON CONFLICT (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, metric_id)
            DO UPDATE SET
                period_end  = EXCLUDED.period_end,
                market_date = EXCLUDED.market_date,
                value       = EXCLUDED.value,
                currency    = EXCLUDED.currency,
                source      = EXCLUDED.source,
                updated_at  = now()
            """,
            out_rows,
            page_size=2000,
        )
    return len(out_rows)


def derive_market_fundamentals(
    tickers: list[str] | None = None,
    full: bool = False,
) -> int:
    """Backward-compatible wrapper; writes to fact_market_metrics."""
    return derive_market_metrics(tickers=tickers, full=full)


def fetch_shares_outstanding(
    jurisdiction: str,
    tickers: list[str] | None = None,
) -> int:
    """Pull `sharesOutstanding` from yfinance Ticker.info for the given
    jurisdiction (US|JP). Writes one snapshot per ticker into
    dim_company_{us,jp}.shares_outstanding.

    yfinance occasionally returns empty info dicts (thinly traded names) —
    those are silently skipped. Returns count of rows updated.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")
    _configure_yfinance_cache(yf)

    if jurisdiction == "JP":
        raw_tickers = tickers or _active_jp_tickers()
        table = "dim_company_jp"
        yf_tickers = [
            t if t.endswith(".T") else (f"{t}.T" if t.isdigit() and len(t) <= 4 else t)
            for t in raw_tickers
        ]
        # dim_company_jp.primary_ticker stores tickers WITH .T suffix (e.g. '7203.T')
        # so we keep the yf_ticker as-is for the UPDATE WHERE clause
        to_storage_key = lambda t: t  # noqa: E731
    elif jurisdiction == "US":
        raw_tickers = tickers or _active_us_tickers()
        table = "dim_company_us"
        yf_tickers = list(raw_tickers)
        to_storage_key = lambda t: t  # noqa: E731
    else:
        raise ValueError(f"Unknown jurisdiction: {jurisdiction}")

    if not raw_tickers:
        return 0

    updates: list[tuple[int, str]] = []
    for yf_ticker in yf_tickers:
        bare = to_storage_key(yf_ticker)
        try:
            info = yf.Ticker(yf_ticker).info
        except Exception:
            continue
        if not info:
            continue
        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if not shares or not isinstance(shares, (int, float)) or shares <= 0:
            continue
        try:
            updates.append((int(shares), bare))
        except (TypeError, ValueError):
            continue

    if not updates:
        return 0

    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            f"""
            UPDATE {table}
               SET shares_outstanding = %s,
                   shares_source      = 'yfinance',
                   shares_updated_at  = now()
             WHERE primary_ticker = %s
            """,
            updates,
        )
    return len(updates)


def fetch_jp_shares_outstanding(tickers: list[str] | None = None) -> int:
    """Backward-compatible alias."""
    return fetch_shares_outstanding("JP", tickers)


def compute_betas(
    tickers: list[str] | None = None,
    benchmark: str = "SPY",
    window_months: int = 60,
    min_obs: int = 24,
) -> int:
    """Compute trailing beta vs. benchmark for each ticker-period.

    Writes rows to fact_market_metrics, not standardized fundamentals.
    Returns row count written.
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        raise RuntimeError("pandas and numpy required for beta computation.")

    active = tickers or _active_us_tickers()
    all_tickers = list(set(active + [benchmark]))

    # Ensure benchmark prices exist
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fact_prices_us WHERE ticker = %s", (benchmark,))
        cnt = cur.fetchone()[0]
    if cnt == 0:
        fetch_prices([benchmark], start_date="2000-01-01")

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, date, adj_close
            FROM fact_prices_us
            WHERE ticker = ANY(%s)
              AND adj_close IS NOT NULL
            ORDER BY ticker, date
            """,
            (all_tickers,),
        )
        price_rows = cur.fetchall()

    if not price_rows:
        return 0

    df = pd.DataFrame(price_rows, columns=["ticker", "date", "adj_close"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.pivot(index="date", columns="ticker", values="adj_close")

    # Resample to month-end
    monthly = df.resample("ME").last()
    monthly_ret = monthly.pct_change().dropna(how="all")

    bench_ret = monthly_ret.get(benchmark)
    if bench_ret is None or bench_ret.empty:
        return 0

    # Load (cik, ticker, fiscal_year, fiscal_period, period_end) for beta attachment
    ticker_filter = "AND t.ticker = ANY(%s)" if tickers else ""
    params: list[Any] = [tickers] if tickers else []

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT f.cik, t.ticker, f.fiscal_year, f.fiscal_period, f.period_end
            FROM fact_fundamentals_std_us f
            JOIN ref_entity_ticker t
              ON t.entity_id = f.cik
             AND t.jurisdiction = 'US'
             AND t.is_primary = TRUE
            WHERE f.period_end IS NOT NULL
              {ticker_filter}
            """,
            params or None,
        )
        entity_periods = cur.fetchall()

    out_rows: list[tuple[Any, ...]] = []

    for cik, ticker, fiscal_year, fiscal_period, period_end in entity_periods:
        if ticker not in monthly_ret.columns:
            continue
        stock_ret = monthly_ret[ticker]

        end_dt = pd.Timestamp(period_end)
        start_dt = end_dt - pd.DateOffset(months=window_months)

        mask = (monthly_ret.index >= start_dt) & (monthly_ret.index <= end_dt)
        s = stock_ret[mask].dropna()
        b = bench_ret[mask].dropna()

        common = s.index.intersection(b.index)
        if len(common) < min_obs:
            continue

        s_vals = s.loc[common].values
        b_vals = b.loc[common].values
        cov = float(((s_vals - s_vals.mean()) * (b_vals - b_vals.mean())).mean())
        var = float(((b_vals - b_vals.mean()) ** 2).mean())
        if var == 0:
            continue
        beta = cov / var

        out_rows.append((
            "US", cik, ticker, fiscal_year, fiscal_period, period_end, period_end,
            "market_beta_5_year", beta, None, "yfinance",
        ))

    if not out_rows:
        return 0
    out_rows = _dedupe_market_metric_rows(out_rows)

    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_market_metrics
                (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, period_end,
                 market_date, metric_id, value, currency, source)
            VALUES %s
            ON CONFLICT (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, metric_id)
            DO UPDATE SET
                period_end  = EXCLUDED.period_end,
                market_date = EXCLUDED.market_date,
                value       = EXCLUDED.value,
                currency    = EXCLUDED.currency,
                source      = EXCLUDED.source,
                updated_at  = now()
            """,
            out_rows,
            page_size=2000,
        )
    return len(out_rows)
