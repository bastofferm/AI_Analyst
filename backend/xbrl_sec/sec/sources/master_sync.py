"""Synchronize ticker links from the jurisdiction-specific company masters."""
from __future__ import annotations

from xbrl_sec.sec.db.connection import connect


def sync_master_dimensions(jurisdiction: str | None = None) -> dict[str, int]:
    if jurisdiction and jurisdiction not in {"US", "JP"}:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")
    with connect() as conn, conn.cursor() as cur:
        return {
            "companies": _company_count(cur, jurisdiction),
            "ticker_links": _sync_ticker_links(cur, jurisdiction),
        }


def _company_count(cur, jurisdiction: str | None) -> int:
    total = 0
    if jurisdiction in (None, "US"):
        cur.execute("SELECT COUNT(*) FROM dim_company_us")
        total += cur.fetchone()[0]
    if jurisdiction in (None, "JP"):
        cur.execute("SELECT COUNT(*) FROM dim_company_jp")
        total += cur.fetchone()[0]
    return total


def _sync_ticker_links(cur, jurisdiction: str | None) -> int:
    total = 0
    if jurisdiction in (None, "US"):
        cur.execute(
            """
            DELETE FROM ref_entity_ticker r
             WHERE r.jurisdiction = 'US'
               AND r.entity_id_type = 'CIK'
               AND NOT EXISTS (
                   SELECT 1
                   FROM dim_company_us d
                   WHERE d.cik = r.entity_id
               )
            """
        )
        total += cur.rowcount
        cur.execute(
            """
            UPDATE ref_entity_ticker r
               SET is_primary = false,
                   updated_at = now()
             WHERE r.jurisdiction = 'US'
               AND r.entity_id_type = 'CIK'
               AND r.is_primary
               AND NOT EXISTS (
                   SELECT 1
                   FROM dim_company_us d
                   WHERE d.cik = r.entity_id
                     AND d.primary_ticker = r.ticker
               )
            """
        )
        total += cur.rowcount
        cur.execute(
            """
            INSERT INTO ref_entity_ticker
                (entity_id, entity_id_type, jurisdiction, ticker, is_primary)
            SELECT cik, 'CIK', 'US', primary_ticker, true
            FROM dim_company_us
            WHERE cik IS NOT NULL AND primary_ticker IS NOT NULL
            ON CONFLICT (entity_id, entity_id_type, ticker) DO UPDATE SET
                jurisdiction = EXCLUDED.jurisdiction,
                is_primary = ref_entity_ticker.is_primary OR EXCLUDED.is_primary,
                updated_at = now()
            """
        )
        total += cur.rowcount
    if jurisdiction in (None, "JP"):
        cur.execute(
            """
            DELETE FROM ref_entity_ticker r
             WHERE r.jurisdiction = 'JP'
               AND r.entity_id_type = 'EDINET_CODE'
               AND NOT EXISTS (
                   SELECT 1
                   FROM dim_company_jp d
                   WHERE d.edinet_code = r.entity_id
               )
            """
        )
        total += cur.rowcount
        cur.execute(
            """
            UPDATE ref_entity_ticker r
               SET is_primary = false,
                   updated_at = now()
             WHERE r.jurisdiction = 'JP'
               AND r.entity_id_type = 'EDINET_CODE'
               AND r.is_primary
               AND NOT EXISTS (
                   SELECT 1
                   FROM dim_company_jp d
                   WHERE d.edinet_code = r.entity_id
                     AND d.primary_ticker = r.ticker
               )
            """
        )
        total += cur.rowcount
        cur.execute(
            """
            INSERT INTO ref_entity_ticker
                (entity_id, entity_id_type, jurisdiction, ticker, is_primary)
            SELECT edinet_code, 'EDINET_CODE', 'JP', primary_ticker, true
            FROM dim_company_jp
            WHERE edinet_code IS NOT NULL AND primary_ticker IS NOT NULL
            ON CONFLICT (entity_id, entity_id_type, ticker) DO UPDATE SET
                jurisdiction = EXCLUDED.jurisdiction,
                is_primary = ref_entity_ticker.is_primary OR EXCLUDED.is_primary,
                updated_at = now()
            """
        )
        total += cur.rowcount
    return total
