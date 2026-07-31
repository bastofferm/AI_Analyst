"""Unified raw fact writers for sec.fact_fundamentals_us and _jp."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


_US_COLUMNS = (
    "cik", "concept_id", "period_end", "fiscal_period", "value_type",
    "filing_id", "filing_type", "period_start", "fiscal_year", "source_fp",
    "value", "unit", "filed_date", "taxonomy", "context_tier",
    "statement_type", "parent_id", "root_id", "concept_path", "concept_id_level",
    "weight", "effective_weight", "pre_parent_id", "pre_order", "pre_level", "pre_position",
)

_JP_COLUMNS = (
    "edinet_code", "concept_id", "period_end", "fiscal_period", "value_type",
    "filing_id", "filing_type", "period_start", "fiscal_year", "source_fp",
    "value", "unit", "decimals", "filed_date", "taxonomy", "context_tier",
    "context_id", "dimension_signature",
    "statement_type", "parent_id", "root_id", "concept_path", "concept_id_level",
    "weight", "effective_weight", "pre_parent_id", "pre_order", "pre_level", "pre_position",
)


def _get(row: dict[str, Any], key: str) -> Any:
    return row.get(key)


def upsert_us_facts(rows: Iterable[dict[str, Any]], page_size: int = 5000) -> int:
    # filing_id is part of the bitemporal key (see migration 113): each filing's
    # view of a period is its own row. Coalesce NULL filing_id -> '' in both the
    # dedup key and the inserted value to satisfy the NOT NULL key.
    deduped = {
        (r.get("cik"), r.get("filing_id") or "", r.get("concept_id"), r.get("period_end"), r.get("fiscal_period"), r.get("context_tier"), r.get("value_type")): r
        for r in rows
    }
    values = [tuple(("" if c == "filing_id" and _get(r, c) is None else _get(r, c)) for c in _US_COLUMNS) for r in deduped.values()]
    if not values:
        return 0
    cols = ", ".join(_US_COLUMNS)
    sql = f"""
        INSERT INTO fact_fundamentals_us ({cols}) VALUES %s
        ON CONFLICT (cik, filing_id, concept_id, period_end, fiscal_period, context_tier, value_type)
        DO UPDATE SET
            filing_type = COALESCE(EXCLUDED.filing_type, fact_fundamentals_us.filing_type),
            period_start = COALESCE(EXCLUDED.period_start, fact_fundamentals_us.period_start),
            fiscal_year = COALESCE(EXCLUDED.fiscal_year, fact_fundamentals_us.fiscal_year),
            source_fp = COALESCE(EXCLUDED.source_fp, fact_fundamentals_us.source_fp),
            value = EXCLUDED.value,
            unit = COALESCE(EXCLUDED.unit, fact_fundamentals_us.unit),
            filed_date = COALESCE(EXCLUDED.filed_date, fact_fundamentals_us.filed_date),
            taxonomy = COALESCE(EXCLUDED.taxonomy, fact_fundamentals_us.taxonomy),
            statement_type = COALESCE(EXCLUDED.statement_type, fact_fundamentals_us.statement_type),
            parent_id = COALESCE(EXCLUDED.parent_id, fact_fundamentals_us.parent_id),
            root_id = COALESCE(EXCLUDED.root_id, fact_fundamentals_us.root_id),
            concept_path = COALESCE(EXCLUDED.concept_path, fact_fundamentals_us.concept_path),
            concept_id_level = COALESCE(EXCLUDED.concept_id_level, fact_fundamentals_us.concept_id_level),
            weight = COALESCE(EXCLUDED.weight, fact_fundamentals_us.weight),
            effective_weight = COALESCE(EXCLUDED.effective_weight, fact_fundamentals_us.effective_weight),
            pre_parent_id = COALESCE(EXCLUDED.pre_parent_id, fact_fundamentals_us.pre_parent_id),
            pre_order = COALESCE(EXCLUDED.pre_order, fact_fundamentals_us.pre_order),
            pre_level = COALESCE(EXCLUDED.pre_level, fact_fundamentals_us.pre_level),
            pre_position = COALESCE(EXCLUDED.pre_position, fact_fundamentals_us.pre_position),
            restatement_counter = fact_fundamentals_us.restatement_counter +
                CASE WHEN EXCLUDED.value IS DISTINCT FROM fact_fundamentals_us.value THEN 1 ELSE 0 END,
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, values, page_size=page_size)


def upsert_jp_facts(rows: Iterable[dict[str, Any]], page_size: int = 5000) -> int:
    deduped = {
        (
            r.get("edinet_code"),
            r.get("filing_id"),
            r.get("concept_id"),
            r.get("period_end"),
            r.get("fiscal_period"),
            r.get("context_id") or "",
            r.get("value_type"),
        ): r
        for r in rows
    }
    values = [tuple(_get(r, c) for c in _JP_COLUMNS) for r in deduped.values()]
    if not values:
        return 0
    cols = ", ".join(_JP_COLUMNS)
    sql = f"""
        INSERT INTO fact_fundamentals_jp ({cols}) VALUES %s
        ON CONFLICT (edinet_code, filing_id, concept_id, period_end, fiscal_period, context_id, value_type)
        DO UPDATE SET
            filing_id = COALESCE(EXCLUDED.filing_id, fact_fundamentals_jp.filing_id),
            filing_type = COALESCE(EXCLUDED.filing_type, fact_fundamentals_jp.filing_type),
            period_start = COALESCE(EXCLUDED.period_start, fact_fundamentals_jp.period_start),
            fiscal_year = COALESCE(EXCLUDED.fiscal_year, fact_fundamentals_jp.fiscal_year),
            source_fp = COALESCE(EXCLUDED.source_fp, fact_fundamentals_jp.source_fp),
            value = EXCLUDED.value,
            unit = COALESCE(EXCLUDED.unit, fact_fundamentals_jp.unit),
            decimals = COALESCE(EXCLUDED.decimals, fact_fundamentals_jp.decimals),
            filed_date = COALESCE(EXCLUDED.filed_date, fact_fundamentals_jp.filed_date),
            taxonomy = COALESCE(EXCLUDED.taxonomy, fact_fundamentals_jp.taxonomy),
            context_tier = EXCLUDED.context_tier,
            dimension_signature = COALESCE(NULLIF(EXCLUDED.dimension_signature, ''), fact_fundamentals_jp.dimension_signature),
            statement_type = COALESCE(EXCLUDED.statement_type, fact_fundamentals_jp.statement_type),
            parent_id = COALESCE(EXCLUDED.parent_id, fact_fundamentals_jp.parent_id),
            root_id = COALESCE(EXCLUDED.root_id, fact_fundamentals_jp.root_id),
            concept_path = COALESCE(EXCLUDED.concept_path, fact_fundamentals_jp.concept_path),
            concept_id_level = COALESCE(EXCLUDED.concept_id_level, fact_fundamentals_jp.concept_id_level),
            weight = COALESCE(EXCLUDED.weight, fact_fundamentals_jp.weight),
            effective_weight = COALESCE(EXCLUDED.effective_weight, fact_fundamentals_jp.effective_weight),
            pre_parent_id = COALESCE(EXCLUDED.pre_parent_id, fact_fundamentals_jp.pre_parent_id),
            pre_order = COALESCE(EXCLUDED.pre_order, fact_fundamentals_jp.pre_order),
            pre_level = COALESCE(EXCLUDED.pre_level, fact_fundamentals_jp.pre_level),
            pre_position = COALESCE(EXCLUDED.pre_position, fact_fundamentals_jp.pre_position),
            restatement_counter = fact_fundamentals_jp.restatement_counter +
                CASE WHEN EXCLUDED.value IS DISTINCT FROM fact_fundamentals_jp.value THEN 1 ELSE 0 END,
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, values, page_size=page_size)
