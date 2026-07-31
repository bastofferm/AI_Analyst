"""Compact validation summaries for the MZQA data layer."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.connection import connect


_RAW_TABLE = {"US": "fact_fundamentals_us", "JP": "fact_fundamentals_jp"}
_STD_TABLE = {"US": "fact_fundamentals_std_us", "JP": "fact_fundamentals_std_jp"}
_METRIC_TABLE = {"US": "fact_metrics_us", "JP": "fact_metrics_jp"}
_RECON_TABLE = {"US": "fact_metrics_recon_us", "JP": "fact_metrics_recon_jp"}
_ID_COL = {"US": "cik", "JP": "edinet_code"}
_STRUCTURAL_COLS = {
    "US": (
        "statement_type", "parent_id", "root_id", "concept_path", "concept_id_level",
        "weight", "effective_weight", "pre_parent_id", "pre_order", "pre_level", "pre_position",
    ),
    "JP": (
        "context_id", "dimension_signature", "statement_type", "parent_id", "root_id",
        "concept_path", "concept_id_level", "weight", "effective_weight",
        "pre_parent_id", "pre_order", "pre_level", "pre_position",
    ),
}


def _us_master_quality(cur) -> dict[str, Any]:
    rows = _rows(
        cur,
        """
        SELECT issue, COUNT(*)
        FROM (
            SELECT 'otc_exchange' AS issue
            FROM dim_company_us
            WHERE UPPER(COALESCE(exchange, '')) IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
            UNION ALL
            SELECT 'missing_exchange'
            FROM dim_company_us
            WHERE COALESCE(exchange, '') = ''
            UNION ALL
            SELECT 'fund_or_trust_class'
            FROM dim_company_us
            WHERE entity_class IN ('FUND', 'TRUST')
            UNION ALL
            SELECT 'excluded_sic'
            FROM dim_company_us
            WHERE sic IN ('6770', '6722', '6726', '6221', '6189')
            UNION ALL
            SELECT 'investment_entity_type'
            FROM dim_company_us
            WHERE LOWER(COALESCE(entity_type, '')) = 'investment'
            UNION ALL
            SELECT 'instrument_name'
            FROM dim_company_us
            WHERE UPPER(COALESCE(name, '')) ~
                  '((^|[^A-Z0-9])(ETF|ETN)([^A-Z0-9]|$)|EXCHANGE[- ]TRADED|INVESCO QQQ TRUST|TRUST,\\s*SERIES|PPLUS TRUST|STRUCTURED PRODUCTS|STRATS|CORTS|SEC(?:URITIES)? BACKED|ABS CORP|DEPOSITOR INC|INDEXPLUS TRUST|PHYSICAL .* TRUST|CARBON ALLOWANCE TRUST|ETHEREUM FUND|BITCOIN FUND|CRYPTO|SPROTT PHYSICAL|ACQUISITION CORP)'
            UNION ALL
            SELECT 'active_without_primary_ticker'
            FROM dim_company_us
            WHERE include_in_pipeline AND primary_ticker IS NULL
        ) issues
        GROUP BY issue
        ORDER BY issue
        """,
    )
    samples = _rows(
        cur,
        """
        SELECT COALESCE(pipeline_sample_group, '<NULL>'), COUNT(*)
        FROM dim_company_us
        WHERE include_in_pipeline
        GROUP BY 1
        ORDER BY 1
        """,
    )
    issues = {row[0]: row[1] for row in rows}
    return {
        "passed": not issues,
        "issues": issues,
        "active_sample_groups": {row[0]: row[1] for row in samples},
    }


def assert_us_master_quality() -> None:
    with connect() as conn, conn.cursor() as cur:
        quality = _us_master_quality(cur)
    if quality["passed"]:
        return
    details = ", ".join(f"{key}={value}" for key, value in sorted(quality["issues"].items()))
    raise RuntimeError(f"US master quality gate failed: {details}")


def _jp_master_quality(cur) -> dict[str, Any]:
    blocking_rows = _rows(
        cur,
        """
        SELECT issue, COUNT(*)
        FROM (
            SELECT 'active_without_primary_ticker' AS issue
            FROM dim_company_jp
            WHERE include_in_pipeline AND primary_ticker IS NULL
            UNION ALL
            SELECT 'active_without_name'
            FROM dim_company_jp
            WHERE include_in_pipeline AND NULLIF(trim(COALESCE(name, '')), '') IS NULL
            UNION ALL
            SELECT 'invalid_primary_ticker_format'
            FROM dim_company_jp
            WHERE primary_ticker IS NOT NULL
              AND primary_ticker !~ '^[0-9A-Z]{4}\\.T$'
            UNION ALL
            SELECT 'invalid_isin_format'
            FROM dim_company_jp
            WHERE isin IS NOT NULL
              AND isin !~ '^JP[A-Z0-9]{10}$'
            UNION ALL
            SELECT 'sec_code_ticker_mismatch'
            FROM dim_company_jp
            WHERE sec_code IS NOT NULL
              AND primary_ticker IS NOT NULL
              AND left(upper(regexp_replace(sec_code, '[^0-9A-Z]', '', 'g')), 4) <> replace(primary_ticker, '.T', '')
            UNION ALL
            SELECT 'missing_is_active'
            FROM dim_company_jp
            WHERE include_in_pipeline AND is_active IS NULL
            UNION ALL
            SELECT 'stale_ticker_links'
            FROM ref_entity_ticker r
            WHERE r.jurisdiction = 'JP'
              AND r.entity_id_type = 'EDINET_CODE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM dim_company_jp d
                  WHERE d.edinet_code = r.entity_id
              )
            UNION ALL
            SELECT 'dim_without_primary_ticker_link'
            FROM dim_company_jp d
            WHERE d.primary_ticker IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM ref_entity_ticker r
                  WHERE r.jurisdiction = 'JP'
                    AND r.entity_id_type = 'EDINET_CODE'
                    AND r.entity_id = d.edinet_code
                    AND r.ticker = d.primary_ticker
                    AND r.is_primary
              )
            UNION ALL
            SELECT 'duplicate_primary_ticker_link'
            FROM (
                SELECT entity_id
                FROM ref_entity_ticker
                WHERE jurisdiction = 'JP'
                  AND entity_id_type = 'EDINET_CODE'
                  AND is_primary
                GROUP BY entity_id
                HAVING COUNT(*) > 1
            ) dup
            UNION ALL
            SELECT 'raw_not_in_dim_company_jp'
            FROM (
                SELECT DISTINCT edinet_code
                FROM fact_fundamentals_jp
            ) f
            WHERE NOT EXISTS (
                SELECT 1
                FROM dim_company_jp d
                WHERE d.edinet_code = f.edinet_code
            )
        ) issues
        GROUP BY issue
        ORDER BY issue
        """,
    )
    warning_rows = _rows(
        cur,
        """
        SELECT issue, COUNT(*)
        FROM (
            SELECT 'active_missing_name_en' AS issue
            FROM dim_company_jp
            WHERE include_in_pipeline AND NULLIF(trim(COALESCE(name_en, '')), '') IS NULL
            UNION ALL
            SELECT 'pipeline_missing_isin'
            FROM dim_company_jp
            WHERE include_in_pipeline AND isin IS NULL
            UNION ALL
            SELECT 'pipeline_missing_gics'
            FROM dim_company_jp
            WHERE include_in_pipeline
              AND (gics_sector_code IS NULL OR gics_industry_group_code IS NULL)
            UNION ALL
            SELECT 'pipeline_missing_mapping_sector'
            FROM dim_company_jp
            WHERE include_in_pipeline AND mapping_sector IS NULL
            UNION ALL
            SELECT 'pipeline_missing_listing_date'
            FROM dim_company_jp
            WHERE include_in_pipeline AND listing_date IS NULL
            UNION ALL
            SELECT 'inactive_missing_delisting_date'
            FROM dim_company_jp
            WHERE is_active = false AND delisting_date IS NULL
            UNION ALL
            SELECT 'inactive_missing_is_active'
            FROM dim_company_jp
            WHERE NOT include_in_pipeline AND is_active IS NULL
            UNION ALL
            SELECT 'pipeline_without_raw_facts'
            FROM dim_company_jp d
            WHERE d.include_in_pipeline
              AND NOT EXISTS (
                  SELECT 1
                  FROM fact_fundamentals_jp f
                  WHERE f.edinet_code = d.edinet_code
              )
        ) warnings
        GROUP BY issue
        ORDER BY issue
        """,
    )
    samples = _rows(
        cur,
        """
        SELECT COALESCE(pipeline_sample_group, '<NULL>'), COUNT(*)
        FROM dim_company_jp
        WHERE include_in_pipeline
        GROUP BY 1
        ORDER BY 1
        """,
    )
    issues = {row[0]: row[1] for row in blocking_rows}
    warnings = {row[0]: row[1] for row in warning_rows}
    return {
        "passed": not issues,
        "issues": issues,
        "warnings": warnings,
        "active_sample_groups": {row[0]: row[1] for row in samples},
    }


def assert_jp_master_quality() -> None:
    with connect() as conn, conn.cursor() as cur:
        quality = _jp_master_quality(cur)
    if quality["passed"]:
        return
    details = ", ".join(f"{key}={value}" for key, value in sorted(quality["issues"].items()))
    raise RuntimeError(f"JP master quality gate failed: {details}")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _single(cur, sql: str, params: tuple = ()) -> tuple:
    cur.execute(sql, params)
    return cur.fetchone()


def _rows(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _table_counts(cur, jurisdiction: str) -> dict[str, Any]:
    raw_table = _RAW_TABLE[jurisdiction]
    std_table = _STD_TABLE[jurisdiction]
    metric_table = _METRIC_TABLE[jurisdiction]
    recon_table = _RECON_TABLE[jurisdiction]
    id_col = _ID_COL[jurisdiction]
    raw = _single(
        cur,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT {id_col}), COUNT(DISTINCT filing_id),
               COUNT(DISTINCT concept_id)
        FROM {raw_table}
        """,
    )
    std = _single(
        cur,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT {id_col}), COUNT(DISTINCT line_item_id)
        FROM {std_table}
        """,
    )
    metrics = _single(
        cur,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT {id_col}), COUNT(DISTINCT metric_id)
        FROM {metric_table}
        """,
    )
    recon = _single(
        cur,
        f"""
        SELECT COUNT(*), COUNT(DISTINCT {id_col}), COUNT(DISTINCT metric_id)
        FROM {recon_table}
        """,
    )
    return {
        "raw_rows": raw[0],
        "raw_entities": raw[1],
        "raw_filings": raw[2],
        "raw_concepts": raw[3],
        "std_rows": std[0],
        "std_entities": std[1],
        "std_line_items": std[2],
        "metric_rows": metrics[0],
        "metric_entities": metrics[1],
        "metric_ids": metrics[2],
        "recon_rows": recon[0],
        "recon_entities": recon[1],
        "recon_metric_ids": recon[2],
    }


def _source_state(cur, jurisdiction: str) -> list[dict[str, Any]]:
    rows = _rows(
        cur,
        """
        SELECT source_kind, downloaded, extracted, parsed, COUNT(*), COUNT(parse_error)
        FROM source_filing_state
        WHERE jurisdiction=%s
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, 3, 4
        """,
        (jurisdiction,),
    )
    return [
        {
            "source_kind": row[0],
            "downloaded": row[1],
            "extracted": row[2],
            "parsed": row[3],
            "rows": row[4],
            "parse_errors": row[5],
        }
        for row in rows
    ]


def _master_counts(cur, jurisdiction: str) -> dict[str, Any]:
    company_table = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    id_col = "cik" if jurisdiction == "US" else "edinet_code"
    id_type = "CIK" if jurisdiction == "US" else "EDINET_CODE"
    companies = _single(
        cur,
        f"""
        SELECT COUNT(*), COUNT(primary_ticker), COUNT(isin),
               COUNT(gics_sector_code), COUNT(gics_industry_group_code),
               COUNT(mapping_sector),
               COUNT(*) FILTER (WHERE include_in_pipeline),
               COUNT(*) FILTER (WHERE include_in_pipeline AND primary_ticker IS NOT NULL),
               COUNT(DISTINCT pipeline_sample_group) FILTER (WHERE pipeline_sample_group IS NOT NULL)
        FROM {company_table}
        """,
    )
    tickers = _single(
        cur,
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE is_primary)
        FROM ref_entity_ticker
        WHERE jurisdiction=%s AND entity_id_type=%s
        """,
        (jurisdiction, id_type),
    )
    filings = _single(
        cur,
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE parsed), COUNT(*) FILTER (WHERE parse_error IS NOT NULL)
        FROM source_filing_state
        WHERE jurisdiction=%s
        """,
        (jurisdiction,),
    )
    return {
        "company_master_table": company_table,
        "company_id_column": id_col,
        "companies": companies[0],
        "companies_with_primary_ticker": companies[1],
        "companies_with_isin": companies[2],
        "companies_with_gics_sector": companies[3],
        "companies_with_gics_industry_group": companies[4],
        "companies_with_mapping_sector": companies[5],
        "active_pipeline_companies": companies[6],
        "active_pipeline_companies_with_primary_ticker": companies[7],
        "pipeline_sample_groups": companies[8],
        "ticker_links": tickers[0],
        "primary_ticker_links": tickers[1],
        "source_filings": filings[0],
        "parsed_filings": filings[1],
        "filing_parse_errors": filings[2],
    }


def _us_xbrl_state(cur) -> list[dict[str, Any]]:
    rows = _rows(
        cur,
        """
        SELECT xbrl_acquisition_status, xbrl_download_attempted, xbrl_downloaded,
               xbrl_extracted, xbrl_cal_extracted, xbrl_pre_extracted,
               xbrl_def_extracted, xbrl_lab_extracted, COUNT(*)
        FROM source_filing_state
        WHERE jurisdiction='US'
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
        ORDER BY 1, 2, 3, 4, 5, 6, 7, 8
        """,
    )
    return [
        {
            "status": row[0],
            "attempted": row[1],
            "xbrl_downloaded": row[2],
            "xbrl_extracted": row[3],
            "cal": row[4],
            "pre": row[5],
            "def": row[6],
            "lab": row[7],
            "rows": row[8],
        }
        for row in rows
    ]


def _us_non_core_forms(cur) -> list[dict[str, Any]]:
    rows = _rows(
        cur,
        """
        SELECT COALESCE(filing_type, '<NULL>') AS form, COUNT(*)
        FROM fact_fundamentals_us
        WHERE filing_type IS NULL
           OR filing_type NOT IN ('10-K','10-K/A','10-Q','10-Q/A','20-F','20-F/A','40-F','40-F/A')
        GROUP BY 1
        ORDER BY 2 DESC, 1
        """,
    )
    return [{"form": row[0], "raw_rows": row[1]} for row in rows]


def _sparsity(cur, jurisdiction: str) -> list[dict[str, Any]]:
    table = _RAW_TABLE[jurisdiction]
    cols = _STRUCTURAL_COLS[jurisdiction]
    total = _single(cur, f"SELECT COUNT(*) FROM {table}")[0]
    if total == 0:
        return [{"column": col, "non_null_rows": 0, "coverage_pct": 0.0} for col in cols]
    checks = ", ".join(f"COUNT({col})" for col in cols)
    row = _single(cur, f"SELECT {checks} FROM {table}")
    return [
        {
            "column": col,
            "non_null_rows": row[idx],
            "coverage_pct": round((row[idx] / total) * 100, 2),
        }
        for idx, col in enumerate(cols)
    ]


def _recent_runs(cur, jurisdiction: str) -> list[dict[str, Any]]:
    rows = _rows(
        cur,
        """
        SELECT stage, mode, status, rows_in, rows_out, error_message, finished_at
        FROM pipeline_stage_run
        WHERE jurisdiction=%s
        ORDER BY started_at DESC
        LIMIT 12
        """,
        (jurisdiction,),
    )
    return [
        {
            "stage": row[0],
            "mode": row[1],
            "status": row[2],
            "rows_in": row[3],
            "rows_out": row[4],
            "error": row[5],
            "finished_at": row[6],
        }
        for row in rows
    ]


def validate_jurisdiction(jurisdiction: str) -> dict[str, Any]:
    if jurisdiction not in _RAW_TABLE:
        raise ValueError(f"Unsupported jurisdiction: {jurisdiction}")
    with connect() as conn, conn.cursor() as cur:
        return {
            "jurisdiction": jurisdiction,
            "counts": _table_counts(cur, jurisdiction),
            "master": _master_counts(cur, jurisdiction),
            "master_quality": _us_master_quality(cur) if jurisdiction == "US" else _jp_master_quality(cur),
            "source_state": _source_state(cur, jurisdiction),
            "us_xbrl_state": _us_xbrl_state(cur) if jurisdiction == "US" else [],
            "us_non_core_forms": _us_non_core_forms(cur) if jurisdiction == "US" else [],
            "sparsity": _sparsity(cur, jurisdiction),
            "recent_runs": _recent_runs(cur, jurisdiction),
        }


def validation_json(jurisdiction: str) -> str:
    return json.dumps(validate_jurisdiction(jurisdiction), indent=2, default=_json_default)
