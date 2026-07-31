"""Upsert helpers for the ETF tables. Only place that writes dim_etf / dim_etf_listing /
fact_prices_etf / pipeline_firds_run (WA0006 §3)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from .models import EtfRecord, ListingRecord, PriceRecord


def upsert_etfs(records: Iterable[EtfRecord]) -> int:
    rows = [
        (r.isin, r.full_name, r.short_name, r.issuer_lei, r.fund_currency, r.cfi, r.termination_date)
        for r in records
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO sec.dim_etf
            (isin, full_name, short_name, issuer_lei, fund_currency, index_tracked, termination_date)
        VALUES %s
        ON CONFLICT (isin) DO UPDATE SET
            full_name        = EXCLUDED.full_name,
            short_name       = COALESCE(EXCLUDED.short_name, sec.dim_etf.short_name),
            issuer_lei       = COALESCE(EXCLUDED.issuer_lei, sec.dim_etf.issuer_lei),
            fund_currency    = COALESCE(EXCLUDED.fund_currency, sec.dim_etf.fund_currency),
            termination_date = EXCLUDED.termination_date,
            updated_at       = NOW()
    """
    # index_tracked is enriched later (Xetra); insert NULL placeholder here.
    payload = [(isin, full, short, lei, ccy, None, term) for (isin, full, short, lei, ccy, _cfi, term) in rows]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, payload)


def upsert_listings(records: Iterable[ListingRecord]) -> int:
    rows = [(r.isin, r.mic, r.trading_currency, r.country) for r in records]
    if not rows:
        return 0
    sql = """
        INSERT INTO sec.dim_etf_listing (isin, mic, trading_currency, country)
        VALUES %s
        ON CONFLICT (isin, mic) DO UPDATE SET
            trading_currency = COALESCE(EXCLUDED.trading_currency, sec.dim_etf_listing.trading_currency),
            country          = EXCLUDED.country
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows)


def recompute_primary_listings() -> int:
    """Mark exactly one primary listing per ISIN. Prefer XETR, then alpha by MIC.

    FIRDS does not carry a primary-venue marker, so we derive one. Called after
    every FIRDS upsert; safe to run repeatedly.
    """
    sql = """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY isin
                ORDER BY (mic = 'XETR') DESC, mic
            ) AS rn
            FROM sec.dim_etf_listing
        )
        UPDATE sec.dim_etf_listing l
        SET is_primary_listing = (r.rn = 1)
        FROM ranked r
        WHERE l.id = r.id
          AND l.is_primary_listing IS DISTINCT FROM (r.rn = 1)
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return max(cur.rowcount, 0)


def upsert_prices(records: Iterable[PriceRecord]) -> int:
    rows = [
        (
            r.isin,
            r.mic,
            r.price_date,
            r.open,
            r.high,
            r.low,
            r.close,
            r.volume,
            r.currency,
            r.source,
            r.history_kind,
            r.source_symbol,
        )
        for r in records
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO sec.fact_prices_etf
            (isin, mic, price_date, open, high, low, close, volume, currency,
             source, history_kind, source_symbol)
        VALUES %s
        ON CONFLICT (isin, mic, price_date) DO UPDATE SET
            open   = EXCLUDED.open,
            high   = EXCLUDED.high,
            low    = EXCLUDED.low,
            close  = EXCLUDED.close,
            volume = EXCLUDED.volume,
            currency = COALESCE(EXCLUDED.currency, sec.fact_prices_etf.currency),
            source = EXCLUDED.source,
            history_kind = EXCLUDED.history_kind,
            source_symbol = EXCLUDED.source_symbol
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows)


def set_etf_price_state(isin: str, stage: str, last_price_date: date | None, error: str | None = None) -> None:
    sql = """
        INSERT INTO sec.pipeline_etf_state (isin, price_stage, last_price_date, last_run_at, error_message)
        VALUES (%s, %s, %s, NOW(), %s)
        ON CONFLICT (isin) DO UPDATE SET
            price_stage     = EXCLUDED.price_stage,
            last_price_date = COALESCE(EXCLUDED.last_price_date, sec.pipeline_etf_state.last_price_date),
            last_run_at     = NOW(),
            error_message   = EXCLUDED.error_message,
            retry_count     = sec.pipeline_etf_state.retry_count + (CASE WHEN EXCLUDED.price_stage = 'failed' THEN 1 ELSE 0 END)
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (isin, stage, last_price_date, error))


def record_firds_run(
    file_type: str,
    file_date: date,
    file_url: str,
    file_md5: str | None,
    status: str,
    instruments_parsed: int | None,
    etfs_upserted: int | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    error: str | None = None,
) -> None:
    sql = """
        INSERT INTO sec.pipeline_firds_run
            (file_type, file_date, file_url, file_md5, status, instruments_parsed,
             etfs_upserted, run_started_at, run_completed_at, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            sql,
            (file_type, file_date, file_url, file_md5, status, instruments_parsed,
             etfs_upserted, started_at, completed_at, error),
        )


def active_etfs(
    limit: int | None = None,
    *,
    only_missing_prices: bool = False,
    isins: Iterable[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Return (isin, primary_mic) for active ETFs, primary listing preferred."""
    where = ["COALESCE(d.is_active, TRUE)"]
    params: list[object] = []
    isin_list = sorted({isin.strip().upper() for isin in (isins or []) if isin})
    if isin_list:
        where.append("d.isin = ANY(%s)")
        params.append(isin_list)
    if only_missing_prices:
        where.append("NOT EXISTS (SELECT 1 FROM sec.fact_prices_etf fp WHERE fp.isin = d.isin)")
    sql = f"""
        SELECT d.isin,
               (SELECT l.mic FROM sec.dim_etf_listing l
                 WHERE l.isin = d.isin
                 ORDER BY l.is_primary_listing DESC, (l.mic = 'XETR') DESC, l.mic
                 LIMIT 1) AS mic
        FROM sec.dim_etf d
        WHERE {" AND ".join(where)}
        ORDER BY d.isin
    """
    if limit is not None:
        sql += f"\n        LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        return [(row[0], row[1]) for row in cur.fetchall()]
