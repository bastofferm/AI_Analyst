"""Explicit promotion from review queue into the protected versioned table."""
from __future__ import annotations

import re
from typing import Any

import psycopg2
import psycopg2.extensions

_TAX_SPECIAL_PATTERNS = (
    "tax reconciliation",
    "effective income tax rate",
    "effective tax rate reconciliation",
    "tax benefit from compensation expense",
    "nondeductible expense",
    "unrecognized tax benefits",
    "income tax effect",
)


def _load_promotable_rows(
    cur,
    jurisdiction: str,
    limit: int | None = None,
    namespace_prefix: str | None = None,
    review_class: str | None = None,
    min_fact_count: int | None = None,
    min_confidence: float | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    where = [
        "q.jurisdiction = %s",
        "q.review_status = 'llm_scored'",
        "q.suggested_target_variable IS NOT NULL",
    ]
    params: list[Any] = [jurisdiction]
    if namespace_prefix:
        where.append("q.normalized_concept_id LIKE %s")
        params.append(f"{namespace_prefix}%")
    if review_class:
        where.append("q.review_class = %s")
        params.append(review_class)
    if min_fact_count is not None:
        where.append("q.fact_count >= %s")
        params.append(min_fact_count)
    if min_confidence is not None:
        where.append("q.confidence >= %s")
        params.append(min_confidence)
    if decision:
        where.append("q.decision = %s")
        params.append(decision)

    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)

    cur.execute(
        f"""
        WITH ranked AS (
            SELECT q.queue_id,
                   q.normalized_concept_id,
                   q.suggested_target_variable,
                   q.suggested_tier,
                   q.suggested_multiplier,
                   q.reasoning,
                   q.mapping_sector,
                   q.jurisdiction,
                   q.fiscal_year_min,
                   q.taxonomies,
                   q.accounting_standards,
                   q.confidence,
                   q.gics_scope,
                   q.gics_sector,
                   q.gics_industry_group,
                   q.review_class,
                   q.fact_count,
                   q.updated_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY
                           q.normalized_concept_id,
                           q.jurisdiction,
                           q.mapping_sector,
                           CASE WHEN q.gics_scope = 'gics_conflict' THEN COALESCE(q.gics_sector, '') ELSE '' END,
                           CASE WHEN q.gics_scope = 'gics_conflict' THEN COALESCE(q.gics_industry_group, '') ELSE '' END
                       ORDER BY
                           q.fact_count DESC,
                           q.confidence DESC NULLS LAST,
                           q.updated_at DESC,
                           q.queue_id DESC
                   ) AS scope_rank
            FROM sec.map_concept_to_taxonomy_review_queue q
            WHERE {" AND ".join(where)}
        )
        SELECT r.queue_id,
               r.normalized_concept_id,
               r.suggested_target_variable,
               r.suggested_tier,
               r.suggested_multiplier,
               r.reasoning,
               r.mapping_sector,
               r.jurisdiction,
               r.fiscal_year_min,
               r.taxonomies,
               r.accounting_standards,
               r.confidence,
               r.gics_scope,
               r.gics_sector,
               r.gics_industry_group,
               r.review_class,
               r.fact_count
        FROM ranked r
        WHERE r.scope_rank = 1
          AND NOT EXISTS (
              SELECT 1
              FROM sec.map_concept_to_taxonomy_versioned v
              WHERE v.concept_id = r.normalized_concept_id
                AND (v.jurisdiction = r.jurisdiction OR v.jurisdiction = 'BOTH' OR r.jurisdiction = 'BOTH')
                AND v.mapping_sector IS NOT DISTINCT FROM r.mapping_sector
                AND v.gics_sector IS NOT DISTINCT FROM CASE WHEN r.gics_scope = 'gics_conflict' THEN r.gics_sector ELSE NULL END
                AND v.gics_industry_group IS NOT DISTINCT FROM CASE WHEN r.gics_scope = 'gics_conflict' THEN r.gics_industry_group ELSE NULL END
                AND v.gics_industry IS NULL
                AND v.gics_sub_industry IS NULL
                AND COALESCE(r.fiscal_year_min, 1900) <= COALESCE(v.effective_to_year, 9999)
          )
        ORDER BY r.fact_count DESC, r.confidence DESC NULLS LAST, r.queue_id
        {limit_sql}
        """,
        params,
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _first_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def _text_blob(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            parts.extend(_text_blob(item) for item in value if item is not None)
            continue
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(value))
        parts.append(text.replace("_", " ").replace("/", " ").lower())
    return " ".join(part for part in parts if part)


def _is_tax_special_mapping(row: dict[str, Any]) -> bool:
    concept_text = _text_blob(
        row.get("normalized_concept_id"),
        row.get("reasoning"),
    )
    return any(pattern in concept_text for pattern in _TAX_SPECIAL_PATTERNS)


def _is_tax_target(row: dict[str, Any]) -> bool:
    return "tax" in _text_blob(row.get("suggested_target_variable"))


def _promotion_allowed(row: dict[str, Any]) -> bool:
    if row.get("review_class") == "mapped_anomaly":
        return False
    if _is_tax_special_mapping(row) and not _is_tax_target(row):
        return False
    return True


def promote_review_queue_rows(
    conn: psycopg2.extensions.connection,
    jurisdiction: str,
    limit: int | None = None,
    namespace_prefix: str | None = None,
    review_class: str | None = None,
    min_fact_count: int | None = None,
    min_confidence: float | None = None,
    decision: str | None = "READY_FOR_REVIEW",
    approved_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Insert approved review-queue rows into the protected versioned table.

    This never truncates or bulk replaces production mappings. It only inserts
    queue rows that currently have no overlapping open mapping.
    """
    with conn.cursor() as cur:
        rows = _load_promotable_rows(
            cur,
            jurisdiction,
            limit=limit,
            namespace_prefix=namespace_prefix,
            review_class=review_class,
            min_fact_count=min_fact_count,
            min_confidence=min_confidence,
            decision=decision,
        )
    rows = [row for row in rows if _promotion_allowed(row)]
    if dry_run:
        return {"selected": len(rows), "promoted": 0, "skipped": 0}

    promoted = 0
    skipped = 0
    approver = approved_by or "codex"
    for row in rows:
        effective_from_year = int(row.get("fiscal_year_min") or 1900)
        gics_sector = row.get("gics_sector") if row.get("gics_scope") == "gics_conflict" else None
        gics_industry_group = row.get("gics_industry_group") if row.get("gics_scope") == "gics_conflict" else None
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT promote_review_queue_row")
            try:
                cur.execute(
                    """
                    INSERT INTO sec.map_concept_to_taxonomy_versioned (
                        concept_id,
                        target_variable,
                        tier,
                        multiplier,
                        reasoning,
                        mapping_sector,
                        jurisdiction,
                        effective_from_year,
                        effective_to_year,
                        taxonomy_version,
                        accounting_standard,
                        review_status,
                        mapping_source,
                        confidence,
                        gics_sector,
                        gics_industry_group,
                        gics_industry,
                        gics_sub_industry,
                        suggestion_id,
                        source_method,
                        source_confidence,
                        approved_at,
                        approved_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, now(), %s)
                    """,
                    (
                        row["normalized_concept_id"],
                        row["suggested_target_variable"],
                        row.get("suggested_tier"),
                        row.get("suggested_multiplier") or 1,
                        row.get("reasoning"),
                        row.get("mapping_sector") or "",
                        row["jurisdiction"],
                        effective_from_year,
                        _first_text(row.get("taxonomies")),
                        _first_text(row.get("accounting_standards")),
                        "reviewed",
                        "review_queue_promotion",
                        row.get("confidence"),
                        gics_sector,
                        gics_industry_group,
                        row["queue_id"],
                        "llm_rerank",
                        row.get("confidence"),
                        approver,
                    ),
                )
                cur.execute(
                    """
                    UPDATE sec.map_concept_to_taxonomy_review_queue
                       SET review_status = 'reviewed',
                           reviewed_at = now(),
                           reviewed_by = %s,
                           updated_at = now()
                     WHERE queue_id = %s
                    """,
                    (approver, row["queue_id"]),
                )
                promoted += 1
                cur.execute("RELEASE SAVEPOINT promote_review_queue_row")
            except psycopg2.Error:
                skipped += 1
                cur.execute("ROLLBACK TO SAVEPOINT promote_review_queue_row")
                cur.execute("RELEASE SAVEPOINT promote_review_queue_row")
    return {"selected": len(rows), "promoted": promoted, "skipped": skipped}
