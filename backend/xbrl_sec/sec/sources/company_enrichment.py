"""Enrich existing jurisdiction company masters without competing master tables."""
from __future__ import annotations

import io
import re
import time
import warnings
from datetime import date
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.jp_identifiers import normalize_jp_primary_ticker
from xbrl_sec.sec.sources.master_sync import sync_master_dimensions


_COMPANY_TABLE = {"US": ("dim_company_us", "cik"), "JP": ("dim_company_jp", "edinet_code")}
_BI_SUGGEST_URL = "https://markets.businessinsider.com/ajax/SearchController_Suggest?max_results=25&query={q}"
_JPX_LISTED_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
_JPX_SEARCH_URL = "https://www2.jpx.co.jp/tseHpFront/JJK020010Action.do"
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{10}")
_JP_ISIN_RE = re.compile(r"JP[A-Z0-9]{10}")
_DATE_RE = re.compile(r"\b(\d{4}/\d{2}/\d{2})\b")
_YF_SECTOR = {
    "Basic Materials": ("15", "Materials"),
    "Communication Services": ("50", "Communication Services"),
    "Consumer Cyclical": ("25", "Consumer Discretionary"),
    "Consumer Defensive": ("30", "Consumer Staples"),
    "Energy": ("10", "Energy"),
    "Financial Services": ("40", "Financials"),
    "Healthcare": ("35", "Health Care"),
    "Industrials": ("20", "Industrials"),
    "Real Estate": ("60", "Real Estate"),
    "Technology": ("45", "Information Technology"),
    "Utilities": ("55", "Utilities"),
}
_YF_INDUSTRY = {
    "banks-regional": ("40", "Financials", "4010", "Banks"),
    "banks-diversified": ("40", "Financials", "4010", "Banks"),
    "asset-management": ("40", "Financials", "4020", "Financial Services"),
    "capital-markets": ("40", "Financials", "4020", "Financial Services"),
    "financial-conglomerates": ("40", "Financials", "4020", "Financial Services"),
    "credit-services": ("40", "Financials", "4020", "Financial Services"),
    "mortgage-finance": ("40", "Financials", "4020", "Financial Services"),
    "shell-companies": ("40", "Financials", "4020", "Financial Services"),
    "insurance-life": ("40", "Financials", "4030", "Insurance"),
    "insurance-property-casualty": ("40", "Financials", "4030", "Insurance"),
    "insurance-diversified": ("40", "Financials", "4030", "Insurance"),
    "insurance-specialty": ("40", "Financials", "4030", "Insurance"),
    "insurance-reinsurance": ("40", "Financials", "4030", "Insurance"),
    "real-estate-diversified": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-diversified": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-healthcare-facilities": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-hotel-motel": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-industrial": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-mortgage": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-office": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-residential": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-retail": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
    "reit-specialty": ("60", "Real Estate", "6010", "Equity Real Estate Investment Trusts (REITs)"),
}
_US_ENRICHMENT_FILTER = """
    primary_ticker IS NOT NULL
    AND COALESCE(exchange, '') <> ''
    AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
    AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
    AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221', '6189')
    AND LOWER(COALESCE(entity_type, '')) <> 'investment'
    AND UPPER(COALESCE(name, '')) !~ '((^|[^A-Z0-9])(ETF|ETN)([^A-Z0-9]|$)|EXCHANGE[- ]TRADED|INVESCO QQQ TRUST|TRUST,\\s*SERIES|PPLUS TRUST|STRUCTURED PRODUCTS|STRATS|CORTS|SEC(?:URITIES)? BACKED|ABS CORP|DEPOSITOR INC|INDEXPLUS TRUST|PHYSICAL .* TRUST|CARBON ALLOWANCE TRUST|ETHEREUM FUND|BITCOIN FUND|CRYPTO|SPROTT PHYSICAL|ACQUISITION CORP)'
"""
_JP_ENGLISH_NAME_CONCEPTS = (
    "jpdei_cor/FilerNameInEnglishDEI",
    "jpcrp_cor/CompanyNameInEnglishCoverPage",
)
_JP_JAPANESE_NAME_CONCEPTS = (
    "jpdei_cor/FilerNameInJapaneseDEI",
    "jpcrp_cor/CompanyNameCoverPage",
)
_JP_SECURITY_CODE_CONCEPTS = ("jpdei_cor/SecurityCodeDEI",)


def enrich_mapping_sector(jurisdiction: str, full: bool = False) -> int:
    table, _id_col = _COMPANY_TABLE[jurisdiction]
    where = "TRUE" if full else "mapping_sector IS NULL"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table}
               SET mapping_sector = CASE
                   WHEN gics_industry_group_code = '4010' THEN 'bank_financial'
                   WHEN gics_industry_group_code IN ('4020', '4030', '4040')
                     OR gics_sector_code = '60'
                     OR (gics_sector_code = '40'
                         AND (gics_industry_group_code != '4010' OR gics_industry_group_code IS NULL))
                     THEN 'non_bank_financial'
                   ELSE 'corp'
               END,
               updated_at = now()
             WHERE {where}
            """
        )
        updated = cur.rowcount
    sync_master_dimensions(jurisdiction)
    return updated


def enrich_jp_identity_from_xbrl_metadata(full: bool = False) -> int:
    where = "TRUE" if full else "d.name_en IS NULL"
    updated = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT s.entity_id AS edinet_code,
                       NULLIF(trim(s.raw_payload #>> '{{jp_identity,name_en}}'), '') AS name_en,
                       row_number() OVER (
                           PARTITION BY s.entity_id
                           ORDER BY
                               s.filed_date DESC NULLS LAST,
                               s.period_end DESC NULLS LAST,
                               s.updated_at DESC
                       ) AS rn
                FROM source_filing_state s
                JOIN dim_company_jp d ON d.edinet_code = s.entity_id
                WHERE s.jurisdiction = 'JP'
                  AND s.raw_payload #>> '{{jp_identity,name_en}}' IS NOT NULL
                  AND {where}
            )
            UPDATE dim_company_jp d
               SET name_en = ranked.name_en,
                   updated_at = now()
              FROM ranked
             WHERE ranked.rn = 1
               AND ranked.name_en IS NOT NULL
               AND d.edinet_code = ranked.edinet_code
            """,
        )
        updated += cur.rowcount
        cur.execute(
            """
            WITH ranked AS (
                SELECT s.entity_id AS edinet_code,
                       NULLIF(trim(s.raw_payload #>> '{jp_identity,name}'), '') AS name_ja,
                       row_number() OVER (
                           PARTITION BY s.entity_id
                           ORDER BY
                               s.filed_date DESC NULLS LAST,
                               s.period_end DESC NULLS LAST,
                               s.updated_at DESC
                       ) AS rn
                FROM source_filing_state s
                JOIN dim_company_jp d ON d.edinet_code = s.entity_id
                WHERE s.jurisdiction = 'JP'
                  AND s.raw_payload #>> '{jp_identity,name}' IS NOT NULL
                  AND d.name IS NULL
            )
            UPDATE dim_company_jp d
               SET name = ranked.name_ja,
                   updated_at = now()
              FROM ranked
             WHERE ranked.rn = 1
               AND ranked.name_ja IS NOT NULL
               AND d.edinet_code = ranked.edinet_code
            """,
        )
        updated += cur.rowcount
        cur.execute(
            """
            WITH ranked AS (
                SELECT s.entity_id AS edinet_code,
                       NULLIF(trim(s.raw_payload #>> '{jp_identity,sec_code}'), '') AS sec_code,
                       row_number() OVER (
                           PARTITION BY s.entity_id
                           ORDER BY s.filed_date DESC NULLS LAST,
                                    s.period_end DESC NULLS LAST,
                                    s.updated_at DESC
                       ) AS rn
                FROM source_filing_state s
                JOIN dim_company_jp d ON d.edinet_code = s.entity_id
                WHERE s.jurisdiction = 'JP'
                  AND s.raw_payload #>> '{jp_identity,sec_code}' IS NOT NULL
            )
            SELECT edinet_code, sec_code
            FROM ranked
            WHERE rn = 1 AND sec_code IS NOT NULL
            """
        )
        repairs: list[tuple[str, str, str, str, str]] = []
        for edinet_code, sec_code in cur.fetchall():
            ticker = normalize_jp_primary_ticker(sec_code)
            if ticker:
                repairs.append((sec_code, ticker, edinet_code, sec_code, ticker))
        if repairs:
            cur.executemany(
                """
                UPDATE dim_company_jp
                   SET sec_code = %s,
                       primary_ticker = %s,
                       updated_at = now()
                 WHERE edinet_code = %s
                   AND (
                       sec_code IS DISTINCT FROM %s
                       OR primary_ticker IS DISTINCT FROM %s
                   )
                """,
                repairs,
            )
            updated += cur.rowcount
    sync_master_dimensions("JP")
    return updated


def enrich_jp_name_en_from_facts(full: bool = False) -> int:
    return enrich_jp_identity_from_xbrl_metadata(full=full)


def enrich_isin(jurisdiction: str, full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    if jurisdiction == "JP":
        return _enrich_jp_isin_from_jpx(full=full, max_tickers=max_tickers)
    table, id_col = _COMPANY_TABLE[jurisdiction]
    if jurisdiction == "US":
        where = _US_ENRICHMENT_FILTER if full else f"isin IS NULL AND {_US_ENRICHMENT_FILTER}"
    else:
        where = "primary_ticker IS NOT NULL" if full else "isin IS NULL AND primary_ticker IS NOT NULL"
    limit = "LIMIT %s" if max_tickers else ""
    params: list[Any] = [max_tickers] if max_tickers else []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {id_col}, primary_ticker, name
            FROM {table}
            WHERE {where}
            ORDER BY {id_col}
            {limit}
            """,
            params,
        )
        rows = cur.fetchall()
    found = missing = errors = 0
    for entity_id, ticker, name in rows:
        try:
            isin = _fetch_isin(jurisdiction, ticker, name)
        except Exception:
            errors += 1
            continue
        if isin:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET isin=%s, updated_at=now()
                    WHERE {id_col}=%s AND (isin IS NULL OR isin=%s)
                    """,
                    (isin, entity_id, isin),
                )
            found += 1
        else:
            missing += 1
        time.sleep(0.3)
    return {"candidates": len(rows), "found": found, "missing": missing, "errors": errors}


def _enrich_jp_isin_from_jpx(full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    try:
        driver = _make_jpx_driver()
    except Exception as exc:
        raise RuntimeError(
            "JP ISIN enrichment requires Selenium with a working Chrome/Chromedriver runtime"
        ) from exc

    where = (
        "primary_ticker IS NOT NULL"
        if full
        else """
             primary_ticker IS NOT NULL
             AND (
                  isin IS NULL
                  OR listing_date IS NULL
                  OR (is_active IS FALSE AND delisting_date IS NULL)
             )
             """
    )
    limit = "LIMIT %s" if max_tickers else ""
    params: list[Any] = [max_tickers] if max_tickers else []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT edinet_code, primary_ticker
            FROM dim_company_jp
            WHERE {where}
            ORDER BY primary_ticker
            {limit}
            """,
            params,
        )
        ticker_rows = cur.fetchall()
        if not max_tickers:
            cur.execute(
                """
                SELECT edinet_code, name
                FROM dim_company_jp
                WHERE primary_ticker IS NULL
                  AND isin IS NULL
                  AND name IS NOT NULL
                ORDER BY name
                """
            )
            name_rows = cur.fetchall()
        else:
            name_rows = []

    found = missing = delisted = name_found = errors = 0
    try:
        for edinet_code, ticker in ticker_rows:
            code = ticker.replace(".T", "")
            try:
                isin, listing_date, _ = _jpx_isin_for_code(driver, code, include_delisted=False)
                if not isin:
                    isin, listing_date, delisting_date = _jpx_isin_for_code(driver, code, include_delisted=True)
                    if isin or delisting_date:
                        _update_jp_isin(edinet_code, isin, listing_date, delisting_date, is_active=False)
                        delisted += 1
                    else:
                        missing += 1
                    continue
                _update_jp_isin(edinet_code, isin, listing_date, None, is_active=True)
                found += 1
            except Exception:
                errors += 1
            time.sleep(0.5)

        for edinet_code, name in name_rows:
            try:
                ticker, isin, listing_date, delisting_date = _jpx_isin_for_name(driver, name, include_delisted=False)
                if not isin:
                    ticker, isin, listing_date, delisting_date = _jpx_isin_for_name(driver, name, include_delisted=True)
                if not isin:
                    missing += 1
                    continue
                with connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE dim_company_jp
                           SET primary_ticker = COALESCE(%s, primary_ticker),
                               isin = %s,
                               listing_date = COALESCE(%s, listing_date),
                               delisting_date = COALESCE(%s, delisting_date),
                               is_active = CASE WHEN %s IS NOT NULL THEN false ELSE is_active END,
                               updated_at = now()
                         WHERE edinet_code = %s
                        """,
                        (ticker, isin, listing_date, delisting_date, delisting_date, edinet_code),
                    )
                name_found += 1
            except Exception:
                errors += 1
            time.sleep(0.5)
    finally:
        driver.quit()
    sync_master_dimensions("JP")
    return {
        "candidates": len(ticker_rows) + len(name_rows),
        "ticker_candidates": len(ticker_rows),
        "name_candidates": len(name_rows),
        "found": found,
        "name_found": name_found,
        "delisted": delisted,
        "missing": missing,
        "errors": errors,
    }


def _update_jp_isin(
    edinet_code: str,
    isin: str | None,
    listing_date: date | None,
    delisting_date: date | None,
    is_active: bool | None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dim_company_jp
               SET isin = COALESCE(%s, isin),
                   listing_date = COALESCE(%s, listing_date),
                   delisting_date = COALESCE(%s, delisting_date),
                   is_active = COALESCE(%s, is_active),
                   updated_at = now()
             WHERE edinet_code = %s
            """,
            (isin, listing_date, delisting_date, is_active, edinet_code),
        )


def enrich_gics(jurisdiction: str, full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    if jurisdiction == "JP":
        return _enrich_jp_gics_from_jpx(full=full, max_tickers=max_tickers)
    return _enrich_yfinance_gics(jurisdiction, full=full, max_tickers=max_tickers)


def _enrich_yfinance_gics(jurisdiction: str, full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    table, id_col = _COMPANY_TABLE[jurisdiction]
    primary_repairs = _repair_us_primary_tickers_from_alternates(max_tickers=max_tickers) if jurisdiction == "US" else 0
    if jurisdiction == "US":
        where = _US_ENRICHMENT_FILTER if full else f"gics_sector_code IS NULL AND {_US_ENRICHMENT_FILTER}"
    else:
        where = "primary_ticker IS NOT NULL" if full else "gics_sector_code IS NULL AND primary_ticker IS NOT NULL"
    limit = "LIMIT %s" if max_tickers else ""
    params: list[Any] = [max_tickers] if max_tickers else []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {id_col}, primary_ticker
            FROM {table}
            WHERE {where}
            ORDER BY {id_col}
            {limit}
            """,
            params,
        )
        rows = cur.fetchall()
    updated = unresolved = rate_limited = non_equity = errors = 0
    for entity_id, ticker in rows:
        try:
            gics = _get_gics(ticker)
        except Exception:
            errors += 1
            continue
        if gics == "RATE_LIMITED":
            rate_limited += 1
            time.sleep(5)
            continue
        if gics == "NON_EQUITY":
            non_equity += 1
            continue
        if not gics:
            unresolved += 1
            continue
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table}
                SET gics_sector_code=%s,
                    gics_sector_name=%s,
                    gics_industry_group_code=%s,
                    gics_industry_group_name=%s,
                    updated_at=now()
                WHERE {id_col}=%s
                """,
                (*gics, entity_id),
            )
        updated += 1
        time.sleep(0.2)
    mapping_updated = enrich_mapping_sector(jurisdiction, full=True)
    return {
        "candidates": len(rows),
        "updated": updated,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
        "non_equity": non_equity,
        "errors": errors,
        "mapping_sector_updated": mapping_updated,
        "primary_ticker_repairs": primary_repairs,
    }


def _ticker_repair_rank(ticker: str) -> tuple:
    text = ticker.upper()
    simple = "-" not in text and "." not in text and "^" not in text
    warrant_like = text.endswith(("W", "WS", "WT", "WTA", "WTB", "RT"))
    preferred_like = "-P" in text or "-PA" in text or "-PB" in text or "-PC" in text or "-PD" in text
    return (simple, not warrant_like, not preferred_like, -abs(len(text) - 4), text)


def _repair_us_primary_tickers_from_alternates(max_tickers: int | None = None) -> int:
    limit = "LIMIT %s" if max_tickers else ""
    params: list[Any] = [max_tickers] if max_tickers else []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.cik, d.primary_ticker, array_agg(r.ticker ORDER BY r.ticker) AS tickers
            FROM dim_company_us d
            JOIN ref_entity_ticker r
              ON r.entity_id = d.cik
             AND r.entity_id_type = 'CIK'
             AND r.jurisdiction = 'US'
            WHERE d.gics_sector_code IS NULL
              AND {_US_ENRICHMENT_FILTER}
            GROUP BY d.cik, d.primary_ticker
            HAVING COUNT(*) > 1
            ORDER BY d.cik
            {limit}
            """,
            params,
        )
        rows = cur.fetchall()
    repaired = 0
    for cik, primary_ticker, tickers in rows:
        candidates = sorted({t for t in tickers if t}, key=_ticker_repair_rank, reverse=True)
        for ticker in candidates:
            if ticker == primary_ticker:
                continue
            try:
                gics = _get_gics(ticker)
            except Exception:
                continue
            if not gics or gics in {"RATE_LIMITED", "NON_EQUITY"}:
                continue
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE dim_company_us
                       SET primary_ticker=%s,
                           gics_sector_code=%s,
                           gics_sector_name=%s,
                           gics_industry_group_code=%s,
                           gics_industry_group_name=%s,
                           updated_at=now()
                     WHERE cik=%s
                    """,
                    (ticker, *gics, cik),
                )
                cur.execute(
                    """
                    UPDATE ref_entity_ticker
                       SET is_primary = (ticker = %s),
                           updated_at = now()
                     WHERE jurisdiction = 'US'
                       AND entity_id_type = 'CIK'
                       AND entity_id = %s
                    """,
                    (ticker, cik),
                )
            repaired += 1
            break
        time.sleep(0.2)
    return repaired


def _enrich_jp_gics_from_jpx(full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    jpx_rows = _fetch_jpx_tse33_rows()
    candidate_where = "TRUE" if full else "gics_sector_code IS NULL"
    limit_sql = "LIMIT %s" if max_tickers else ""
    params: list[Any] = [max_tickers] if max_tickers else []
    ticker_repairs = _repair_jp_primary_tickers_from_sec_code()

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE tmp_jpx_tse33 (
                ticker TEXT PRIMARY KEY,
                tse33_code INTEGER NOT NULL,
                tse33_name_ja TEXT
            ) ON COMMIT DROP
            """
        )
        cur.executemany(
            """
            INSERT INTO tmp_jpx_tse33 (ticker, tse33_code, tse33_name_ja)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE SET
                tse33_code = EXCLUDED.tse33_code,
                tse33_name_ja = EXCLUDED.tse33_name_ja
            """,
            jpx_rows,
        )
        cur.execute(
            """
            UPDATE ref_jp_tse33_gics ref
               SET tse33_name_ja = src.tse33_name_ja,
                   updated_at = now()
              FROM (
                  SELECT tse33_code, MAX(tse33_name_ja) AS tse33_name_ja
                  FROM tmp_jpx_tse33
                  WHERE tse33_name_ja IS NOT NULL
                  GROUP BY tse33_code
              ) src
             WHERE ref.tse33_code = src.tse33_code
            """
        )
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_jp_gics_candidates ON COMMIT DROP AS
            SELECT edinet_code, primary_ticker, sec_code
            FROM dim_company_jp
            WHERE {candidate_where}
            ORDER BY edinet_code
            {limit_sql}
            """,
            params,
        )
        cur.execute("SELECT COUNT(*) FROM tmp_jp_gics_candidates")
        candidates = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM tmp_jp_gics_candidates c
            JOIN tmp_jpx_tse33 j ON j.ticker = c.primary_ticker
            """
        )
        active_true = cur.fetchone()[0]
        active_false = candidates - active_true
        cur.execute(
            """
            UPDATE dim_company_jp d
               SET is_active = (j.ticker IS NOT NULL),
                   updated_at = now()
              FROM tmp_jp_gics_candidates c
              LEFT JOIN tmp_jpx_tse33 j ON j.ticker = c.primary_ticker
             WHERE d.edinet_code = c.edinet_code
            """
        )
        cur.execute(
            """
            UPDATE dim_company_jp d
               SET gics_sector_code = ref.gics_sector_code,
                   gics_sector_name = ref.gics_sector_name,
                   gics_industry_group_code = ref.gics_industry_group_code,
                   gics_industry_group_name = ref.gics_industry_group_name,
                   updated_at = now()
              FROM tmp_jp_gics_candidates c
              JOIN tmp_jpx_tse33 j ON j.ticker = c.primary_ticker
              JOIN ref_jp_tse33_gics ref ON ref.tse33_code = j.tse33_code
             WHERE d.edinet_code = c.edinet_code
            """
        )
        updated = cur.rowcount
        cur.execute(
            """
            SELECT COUNT(*)
            FROM tmp_jp_gics_candidates c
            LEFT JOIN tmp_jpx_tse33 j ON j.ticker = c.primary_ticker
            LEFT JOIN ref_jp_tse33_gics ref ON ref.tse33_code = j.tse33_code
            WHERE ref.tse33_code IS NULL
            """
        )
        unresolved = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ref_jp_tse33_gics WHERE is_active")
        reference_codes = cur.fetchone()[0]

    mapping_updated = enrich_mapping_sector("JP", full=True)
    return {
        "candidates": candidates,
        "jpx_rows": len(jpx_rows),
        "reference_codes": reference_codes,
        "ticker_repairs": ticker_repairs,
        "updated": updated,
        "unresolved": unresolved,
        "active_true": active_true,
        "active_false": active_false,
        "mapping_sector_updated": mapping_updated,
    }


def _repair_jp_primary_tickers_from_sec_code() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT edinet_code, sec_code, primary_ticker
            FROM dim_company_jp
            WHERE sec_code IS NOT NULL
            """
        )
        repairs: list[tuple[str, str]] = []
        for edinet_code, sec_code, primary_ticker in cur.fetchall():
            normalized = normalize_jp_primary_ticker(sec_code)
            if normalized and normalized != primary_ticker:
                repairs.append((normalized, edinet_code))
        if not repairs:
            return 0
        cur.executemany(
            """
            UPDATE dim_company_jp
            SET primary_ticker=%s,
                updated_at=now()
            WHERE edinet_code=%s
            """,
            repairs,
        )
        return cur.rowcount


def enrich_company_master(
    jurisdiction: str,
    full: bool = False,
    max_tickers: int | None = None,
    isin: bool = False,
    gics: bool = False,
) -> dict[str, int]:
    if jurisdiction not in _COMPANY_TABLE:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")
    out: dict[str, int] = {}
    if isin:
        out.update({f"isin_{key}": value for key, value in enrich_isin(jurisdiction, full, max_tickers).items()})
    if gics:
        out.update({f"gics_{key}": value for key, value in enrich_gics(jurisdiction, full, max_tickers).items()})
    if not gics:
        out["mapping_sector_updated"] = enrich_mapping_sector(jurisdiction, full=full)
    return out


def _fetch_isin(jurisdiction: str, ticker: str, short_name: str | None) -> str | None:
    via_yf = _yf_fetch_isin(ticker)
    if via_yf:
        return via_yf
    if jurisdiction == "US":
        return _bi_fetch_isin(ticker, short_name)
    return None


def _yf_fetch_isin(ticker: str) -> str | None:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        obj = yf.Ticker(ticker)
        value = getattr(obj, "isin", None)
        if callable(value):
            value = value()
        if not value and hasattr(obj, "get_isin"):
            value = obj.get_isin()
        if isinstance(value, str) and _ISIN_RE.fullmatch(value):
            return value
    except Exception:
        return None
    return None


def _bi_fetch_isin(ticker: str, short_name: str | None) -> str | None:
    for query in (short_name, ticker):
        if not query:
            continue
        req = Request(_BI_SUGGEST_URL.format(q=quote(str(query))), headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as response:
            data = response.read().decode("utf-8", errors="ignore")
        search_str = f'"{ticker}|'
        if search_str in data:
            candidate = data.split(search_str)[1].split('"')[0].split("|")[0]
            if _ISIN_RE.fullmatch(candidate):
                return candidate
        pattern = re.compile(r'"[^"]*\|([A-Z]{2}[A-Z0-9]{10})\|[^"]*' + re.escape(ticker) + r'[^"]*"')
        match = pattern.search(data)
        if match and _ISIN_RE.fullmatch(match.group(1)):
            return match.group(1)
        for segment in data.split(f'"{ticker}'):
            if segment.startswith("|") or "|" + ticker + "|" in data:
                match2 = _ISIN_RE.search(segment[:30])
                if match2:
                    return match2.group(0)
    return None


def _get_gics(ticker: str):
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for GICS enrichment") from exc
    info = yf.Ticker(ticker).info
    quote_type = (info or {}).get("quoteType")
    if not info or not quote_type:
        return "RATE_LIMITED"
    if str(quote_type).upper() not in {"EQUITY", "ECNQUOTE"}:
        return "NON_EQUITY"
    industry_key = info.get("industryKey", "")
    sector = info.get("sector", "")
    if industry_key in _YF_INDUSTRY:
        return _YF_INDUSTRY[industry_key]
    if sector in _YF_SECTOR:
        sector_code, sector_name = _YF_SECTOR[sector]
        return (sector_code, sector_name, None, None)
    return None


def _make_jpx_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise RuntimeError("selenium is not installed") from exc
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def _jpx_isin_for_code(driver, code: str, include_delisted: bool = False) -> tuple[str | None, date | None, date | None]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(_JPX_SEARCH_URL)
    code_field = wait.until(EC.presence_of_element_located((By.NAME, "eqMgrCd")))
    if include_delisted:
        _try_click_delisted_checkbox(driver)
    code_field.clear()
    code_field.send_keys(code)
    driver.find_element(By.NAME, "searchButton").click()
    time.sleep(1.5)
    buttons = driver.find_elements(By.NAME, "detail_button")
    if not buttons:
        return None, None, None
    buttons[0].click()
    time.sleep(1.5)
    text = driver.find_element(By.TAG_NAME, "body").text
    return _parse_jpx_detail_text(text)


def _jpx_isin_for_name(
    driver,
    name: str,
    include_delisted: bool = False,
) -> tuple[str | None, str | None, date | None, date | None]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(_JPX_SEARCH_URL)
    if include_delisted:
        _try_click_delisted_checkbox(driver)
    name_field = wait.until(EC.presence_of_element_located((By.NAME, "mgrMiTxtBx")))
    name_field.clear()
    name_field.send_keys(name)
    driver.find_element(By.NAME, "searchButton").click()
    time.sleep(1.5)
    buttons = driver.find_elements(By.NAME, "detail_button")
    if not buttons:
        return None, None, None, None
    result_text = driver.find_element(By.TAG_NAME, "body").text
    ticker = _ticker_from_jpx_result_text(result_text)
    buttons[0].click()
    time.sleep(1.5)
    detail_text = driver.find_element(By.TAG_NAME, "body").text
    isin, listing_date, delisting_date = _parse_jpx_detail_text(detail_text)
    if ticker is None:
        ticker = _ticker_from_jpx_detail_text(detail_text)
    return ticker, isin, listing_date, delisting_date


def _try_click_delisted_checkbox(driver) -> None:
    try:
        from selenium.webdriver.common.by import By

        checkbox = driver.find_element(By.NAME, "jjHisiKbnChkbx")
        if not checkbox.is_selected():
            checkbox.click()
    except Exception:
        return


def _parse_jpx_detail_text(text: str) -> tuple[str | None, date | None, date | None]:
    match = _JP_ISIN_RE.search(text)
    isin = match.group(0) if match else None
    listing_date = delisting_date = None
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if "Date of listing" in line and idx + 1 < len(lines):
            listing_date = _parse_jpx_date_from_text(lines[idx + 1])
        if "Date of delisting" in line and idx + 1 < len(lines):
            delisting_date = _parse_jpx_date_from_text(lines[idx + 1])
    return isin, listing_date, delisting_date


def _parse_jpx_date_from_text(text: str) -> date | None:
    matches = _DATE_RE.findall(text)
    if not matches:
        return None
    try:
        return date.fromisoformat(matches[-1].replace("/", "-"))
    except ValueError:
        return None


def _ticker_from_jpx_result_text(text: str) -> str | None:
    passed_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not passed_header:
            if re.search(r"\bCode\b.*\bIssue name\b", stripped):
                passed_header = True
            continue
        token = stripped.split()[0]
        ticker = normalize_jp_primary_ticker(token)
        if ticker:
            return ticker
    return None


def _ticker_from_jpx_detail_text(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if re.search(r"\b(Code|Issue code)\b", line) and idx + 1 < len(lines):
            ticker = normalize_jp_primary_ticker(lines[idx + 1])
            if ticker:
                return ticker
    return None


def _fetch_jpx_tse33_rows() -> list[tuple[str, int, str | None]]:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required for JPX GICS enrichment") from exc

    req = Request(_JPX_LISTED_URL, headers={"User-Agent": "MZQA xbrl_sec pipeline"})
    with urlopen(req, timeout=60) as response:
        payload = response.read()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(io.BytesIO(payload), header=0)

    code_col = _find_jpx_column(df.columns, required=("コード",), excluded=("33",))
    tse33_code_col = _find_jpx_column(df.columns, required=("33", "コード"))
    try:
        tse33_name_col = _find_jpx_column(df.columns, required=("33", "区分"))
    except ValueError:
        tse33_name_col = None

    seen: dict[str, tuple[str, int, str | None]] = {}
    for _, row in df.iterrows():
        ticker_code = _clean_jpx_code(row.get(code_col))
        tse33_code = _clean_jpx_int(row.get(tse33_code_col))
        if not ticker_code or tse33_code is None:
            continue
        tse33_name = None
        if tse33_name_col is not None:
            value = row.get(tse33_name_col)
            if value is not None and str(value).strip().lower() not in {"", "nan", "-"}:
                tse33_name = str(value).strip()
        ticker = f"{ticker_code}.T"
        seen[ticker] = (ticker, tse33_code, tse33_name)
    return list(seen.values())


def _find_jpx_column(columns, required: tuple[str, ...], excluded: tuple[str, ...] = ()) -> str:
    for column in columns:
        text = str(column).replace(" ", "")
        if all(token in text for token in required) and not any(token in text for token in excluded):
            return column
    raise ValueError(f"Could not find JPX column with tokens {required}")


def _clean_jpx_code(value: Any) -> str | None:
    ticker = normalize_jp_primary_ticker(value)
    return ticker.replace(".T", "") if ticker else None


def _clean_jpx_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in {"", "nan", "-"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None
