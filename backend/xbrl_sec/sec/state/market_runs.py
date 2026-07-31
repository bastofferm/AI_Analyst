"""Market-data run and item-state helpers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
from typing import Iterator

from xbrl_sec.sec.state.store import RunContext, finish_run, start_run
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


def start_market_run(source: str, full: bool, scope: dict | None = None) -> RunContext:
    mode = "full_refresh" if full else "incremental"
    payload = {"source": source, **(scope or {})}
    return start_run("GLOBAL", f"{source}_download", mode, json.dumps(payload, default=str))


def mark_item_running(
    ctx: RunContext,
    source: str,
    source_key: str,
    *,
    source_url: str | None = None,
    source_hash: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_source_item_state
                (source, source_key, run_id, status, source_url, source_hash,
                 started_at, finished_at, rows_in, rows_out, error_message, updated_at)
            VALUES (%s, %s, %s, 'running', %s, %s, now(), NULL, 0, 0, NULL, now())
            ON CONFLICT (source, source_key) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                status = 'running',
                source_url = COALESCE(EXCLUDED.source_url, market_source_item_state.source_url),
                source_hash = COALESCE(EXCLUDED.source_hash, market_source_item_state.source_hash),
                started_at = now(),
                finished_at = NULL,
                rows_in = 0,
                rows_out = 0,
                error_message = NULL,
                updated_at = now()
            """,
            (source, source_key, str(ctx.run_id), source_url, source_hash),
        )


def mark_items_running(ctx: RunContext, source: str, source_keys: list[str]) -> int:
    rows = [(source, source_key, str(ctx.run_id)) for source_key in source_keys]
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO market_source_item_state
                (source, source_key, run_id, status, started_at, finished_at,
                 rows_in, rows_out, error_message, updated_at)
            SELECT v.source, v.source_key, v.run_id::uuid, 'running',
                   now(), NULL, 0, 0, NULL, now()
            FROM (VALUES %s) AS v(source, source_key, run_id)
            ON CONFLICT (source, source_key) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                status = 'running',
                started_at = now(),
                finished_at = NULL,
                rows_in = 0,
                rows_out = 0,
                error_message = NULL,
                updated_at = now()
            """,
            rows,
            page_size=5000,
        )


def mark_item_done(
    ctx: RunContext,
    source: str,
    source_key: str,
    *,
    status: str = "succeeded",
    rows_in: int = 0,
    rows_out: int = 0,
    min_date: date | None = None,
    max_date: date | None = None,
    source_url: str | None = None,
    source_hash: str | None = None,
    error: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_source_item_state
                (source, source_key, run_id, status, source_url, source_hash,
                 min_date, max_date, rows_in, rows_out, error_message,
                 started_at, finished_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE((SELECT started_at FROM market_source_item_state
                              WHERE source=%s AND source_key=%s), now()),
                    now(), now())
            ON CONFLICT (source, source_key) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                status = EXCLUDED.status,
                source_url = COALESCE(EXCLUDED.source_url, market_source_item_state.source_url),
                source_hash = COALESCE(EXCLUDED.source_hash, market_source_item_state.source_hash),
                min_date = EXCLUDED.min_date,
                max_date = EXCLUDED.max_date,
                rows_in = EXCLUDED.rows_in,
                rows_out = EXCLUDED.rows_out,
                error_message = EXCLUDED.error_message,
                finished_at = now(),
                updated_at = now()
            """,
            (
                source,
                source_key,
                str(ctx.run_id),
                status,
                source_url,
                source_hash,
                min_date,
                max_date,
                rows_in,
                rows_out,
                error,
                source,
                source_key,
            ),
        )


def previous_item_hash(source: str, source_key: str) -> str | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_hash
            FROM market_source_item_state
            WHERE source=%s
              AND source_key=%s
              AND status IN ('succeeded', 'skipped')
              AND source_hash IS NOT NULL
            """,
            (source, source_key),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def item_succeeded(source: str, source_key: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM market_source_item_state
            WHERE source=%s
              AND source_key=%s
              AND status='succeeded'
              AND rows_out > 0
            LIMIT 1
            """,
            (source, source_key),
        )
        return cur.fetchone() is not None


@contextmanager
def market_run(source: str, full: bool, scope: dict | None = None) -> Iterator[RunContext]:
    ctx = start_market_run(source, full, scope)
    rows_out = 0
    try:
        yield ctx
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(rows_out),0) FROM market_source_item_state WHERE run_id=%s",
                (str(ctx.run_id),),
            )
            rows_out = int(cur.fetchone()[0] or 0)
        finish_run(ctx, "succeeded", rows_in=0, rows_out=rows_out)
    except Exception as exc:
        finish_run(ctx, "failed", rows_in=0, rows_out=rows_out, error=str(exc)[:4000])
        raise
