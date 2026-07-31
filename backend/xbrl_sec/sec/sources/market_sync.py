"""Sync market support tables from legacy quant into xbrl_sec.sec.

Metric definitions are controlled by spec/line_item_metric_registry.json via
registry_sync.py. This module must not overwrite them from quant.
"""
from __future__ import annotations

import os
import site

site.addsitedir(site.getusersitepackages())
import psycopg2

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


def _quant_url() -> str:
    return os.environ.get("QUANT_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/quant")


def _fetch(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg2.connect(_quant_url()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def sync_metric_support() -> tuple[int, int]:
    tickers = _fetch(
        """
        SELECT entity_id, entity_id_type, jurisdiction, ticker, is_primary, updated_at
        FROM public.ref_entity_ticker
        """
    )
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO ref_entity_ticker
                (entity_id, entity_id_type, jurisdiction, ticker, is_primary, updated_at)
            VALUES %s
            ON CONFLICT (entity_id, entity_id_type, ticker) DO UPDATE SET
                jurisdiction = EXCLUDED.jurisdiction,
                is_primary = EXCLUDED.is_primary,
                updated_at = EXCLUDED.updated_at
            """,
            tickers,
            page_size=1000,
        )
    return 0, len(tickers)


# Note: sync_prices (the legacy "copy prices from the sister quant DB") was
# removed. Price acquisition now goes through fetch_prices in yfinance_ingest,
# which writes directly to fact_prices_us / fact_prices_jp from upstream
# yfinance. The remaining sync_metric_support above only mirrors ticker
# metadata, not price rows.
