"""Versioned concept mapping lookup for standardized fundamentals."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.connection import connect


_GICS_FIELDS = (
    ("gics_sub_industry", 4),
    ("gics_industry", 3),
    ("gics_industry_group", 2),
    ("gics_sector", 1),
)


def _add_aliases(mapping: dict[str, list[dict[str, Any]]], concept_id: str, rec: dict[str, Any]) -> None:
    mapping[concept_id].append(rec)
    colon = concept_id.replace("/", ":", 1)
    if colon != concept_id:
        mapping[colon].append(rec)


def load_versioned_mapping(jurisdiction: str) -> dict[str, list[dict[str, Any]]]:
    """Load governed mappings for one jurisdiction, including slash/colon aliases."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.mapping_id, m.concept_id, m.mapping_sector, m.target_variable, m.tier,
                   m.multiplier, m.jurisdiction, m.effective_from_year, m.effective_to_year,
                   m.gics_sector, m.gics_industry_group, m.gics_industry, m.gics_sub_industry,
                   m.confidence, m.accounting_standard, m.taxonomy_version,
                   m.aggregation_type, m.aggregation_priority, m.sign_policy,
                   m.normal_balance
            FROM map_concept_to_taxonomy_versioned m
            WHERE m.target_variable IS NOT NULL
              AND m.target_variable <> 'UNMAPPED'
              AND m.tier IS NOT NULL
              AND m.jurisdiction IN (%s, 'BOTH')
            """,
            (jurisdiction,),
        )
        mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for (
            mapping_id,
            concept_id,
            mapping_sector,
            target,
            tier,
            multiplier,
            row_jurisdiction,
            effective_from_year,
            effective_to_year,
            gics_sector,
            gics_industry_group,
            gics_industry,
            gics_sub_industry,
            confidence,
            accounting_standard,
            taxonomy_version,
            aggregation_type,
            aggregation_priority,
            sign_policy,
            normal_balance,
        ) in cur.fetchall():
            rec = {
                "mapping_id": mapping_id,
                "mapping_exception_id": None,
                "mapping_sector": mapping_sector or "",
                "target_variable": target,
                "tier": int(tier),
                "multiplier": Decimal(str(multiplier or 1)),
                "jurisdiction": row_jurisdiction,
                "effective_from_year": int(effective_from_year),
                "effective_to_year": int(effective_to_year) if effective_to_year is not None else None,
                "gics_sector": gics_sector,
                "gics_industry_group": gics_industry_group,
                "gics_industry": gics_industry,
                "gics_sub_industry": gics_sub_industry,
                "confidence": float(confidence) if confidence is not None else None,
                "accounting_standard": accounting_standard,
                "taxonomy_version": taxonomy_version,
                "aggregation_type": aggregation_type,
                "aggregation_priority": int(aggregation_priority) if aggregation_priority is not None else None,
                "sign_policy": sign_policy,
                "normal_balance": normal_balance,
                "mapping_source": "versioned",
            }
            _add_aliases(mapping, concept_id, rec)
        return mapping


def load_mapping_exceptions(jurisdiction: str) -> dict[str, list[dict[str, Any]]]:
    """Load approved company-period mapping exceptions for one jurisdiction."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT exception_id, entity_id, concept_id, fiscal_year_from, fiscal_year_to,
                   fiscal_period, target_variable, tier, multiplier, mapping_sector,
                   accounting_standard, taxonomy_version, reason_code, approved_at,
                   expires_at, current_mapping_id, aggregation_type, sign_policy
            FROM map_concept_to_taxonomy_exception
            WHERE jurisdiction = %s
              AND review_status = 'approved'
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (jurisdiction,),
        )
        mapping: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for (
            exception_id,
            entity_id,
            concept_id,
            fiscal_year_from,
            fiscal_year_to,
            fiscal_period,
            target_variable,
            tier,
            multiplier,
            mapping_sector,
            accounting_standard,
            taxonomy_version,
            reason_code,
            approved_at,
            expires_at,
            current_mapping_id,
            aggregation_type,
            sign_policy,
        ) in cur.fetchall():
            rec = {
                "mapping_id": current_mapping_id,
                "mapping_exception_id": exception_id,
                "entity_id": entity_id,
                "mapping_sector": mapping_sector or "",
                "target_variable": target_variable,
                "tier": int(tier),
                "multiplier": Decimal(str(multiplier or 1)),
                "jurisdiction": jurisdiction,
                "effective_from_year": int(fiscal_year_from),
                "effective_to_year": int(fiscal_year_to) if fiscal_year_to is not None else None,
                "fiscal_period": fiscal_period,
                "gics_sector": None,
                "gics_industry_group": None,
                "gics_industry": None,
                "gics_sub_industry": None,
                "confidence": 1.0,
                "accounting_standard": accounting_standard,
                "taxonomy_version": taxonomy_version,
                "aggregation_type": aggregation_type,
                "sign_policy": sign_policy,
                "reason_code": reason_code,
                "approved_at": approved_at,
                "expires_at": expires_at,
                "mapping_source": "exception",
            }
            _add_aliases(mapping, concept_id, rec)
        return mapping


def _valid_for_year(rec: dict[str, Any], fiscal_year: int) -> bool:
    end = rec["effective_to_year"] if rec["effective_to_year"] is not None else 9999
    return rec["effective_from_year"] <= fiscal_year <= end


def _normal_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _taxonomy_year(value: Any) -> str | None:
    text = _normal_text(value)
    for token in text.replace("-", " ").replace("_", " ").replace("/", " ").split():
        if len(token) == 4 and token.isdigit():
            return token
    return None


def _row_entity_id(raw_row: dict[str, Any]) -> str | None:
    return raw_row.get("entity_id") or raw_row.get("cik") or raw_row.get("edinet_code")


def _sector_order(entity_sector: str | None) -> list[str]:
    """Return allowed mapping sectors from most specific to universal.

    The old selector used exact sector -> corp -> universal for every filer.
    This version keeps corporate fallback for corporate filers only. Banks and
    non-bank financials can use their own scopes plus universal mappings.
    """
    sector = (entity_sector or "corp").strip() or "corp"
    aliases: dict[str, list[str]] = {
        "bank": ["bank_financial", "bank", ""],
        "bank_financial": ["bank_financial", "bank", ""],
        "corp": ["corp", ""],
        "corporate": ["corp", ""],
        "insurance": ["insurance", "non_bank_financial", ""],
        "reit": ["reit", "non_bank_financial", ""],
        "asset_manager": ["asset_manager_other_financial", "asset_manager", "non_bank_financial", ""],
        "asset_manager_other_financial": ["asset_manager_other_financial", "asset_manager", "non_bank_financial", ""],
        "other_financial": ["asset_manager_other_financial", "other_financial", "non_bank_financial", ""],
        "non_bank_financial": ["non_bank_financial", ""],
        "": [""],
    }
    order = aliases.get(sector, [sector, ""])
    out: list[str] = []
    for item in order:
        if item not in out:
            out.append(item)
    return out


def _accounting_standard_score(rec: dict[str, Any], raw_row: dict[str, Any]) -> int | None:
    rec_standard = _normal_text(rec.get("accounting_standard"))
    row_standard = _normal_text(raw_row.get("accounting_standard"))
    if not rec_standard:
        return 1
    if not row_standard:
        return 0
    return 3 if rec_standard == row_standard else None


def _taxonomy_score(rec: dict[str, Any], raw_row: dict[str, Any]) -> int | None:
    rec_taxonomy = _normal_text(rec.get("taxonomy_version"))
    row_taxonomy = _normal_text(raw_row.get("taxonomy_version") or raw_row.get("taxonomy"))
    if not rec_taxonomy:
        return 1
    if not row_taxonomy:
        return 0
    if rec_taxonomy == row_taxonomy:
        return 3
    rec_year = _taxonomy_year(rec_taxonomy)
    row_year = _taxonomy_year(row_taxonomy)
    if rec_year and row_year and rec_year == row_year:
        return 2
    return None


def _gics_score(rec: dict[str, Any], row: dict[str, Any]) -> int | None:
    score = 0
    for field, weight in _GICS_FIELDS:
        constraint = rec.get(field)
        if constraint is None:
            continue
        if row.get(field) != constraint:
            return None
        score = max(score, weight)
    return score


def _exception_sort_key(rec: dict[str, Any]) -> tuple:
    approved_at = rec.get("approved_at")
    if isinstance(approved_at, datetime):
        approved_value = approved_at.astimezone(timezone.utc).timestamp() if approved_at.tzinfo else approved_at.timestamp()
    else:
        approved_value = 0
    return (
        1 if rec.get("fiscal_period") else 0,
        rec["effective_from_year"],
        approved_value,
        int(rec.get("mapping_exception_id") or 0),
    )


def _select_exception(
    jurisdiction: str,
    raw_row: dict[str, Any],
    sector_order: list[str],
    exceptions: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not exceptions:
        return None
    entity_id = _row_entity_id(raw_row)
    if not entity_id:
        return None
    fiscal_year = int(raw_row["fiscal_year"])
    fiscal_period = raw_row.get("fiscal_period")
    candidates = []
    for rec in exceptions:
        if rec.get("review_status") and rec.get("review_status") != "approved":
            continue
        expires_at = rec.get("expires_at")
        if isinstance(expires_at, datetime):
            now = datetime.now(expires_at.tzinfo or timezone.utc)
            if expires_at <= now:
                continue
        if rec.get("jurisdiction") != jurisdiction:
            continue
        if str(rec.get("entity_id")) != str(entity_id):
            continue
        if rec.get("mapping_sector", "") not in sector_order:
            continue
        if not _valid_for_year(rec, fiscal_year):
            continue
        rec_period = rec.get("fiscal_period")
        if rec_period and rec_period != fiscal_period:
            continue
        if _accounting_standard_score(rec, raw_row) is None:
            continue
        if _taxonomy_score(rec, raw_row) is None:
            continue
        candidates.append(rec)
    if not candidates:
        return None
    return max(candidates, key=_exception_sort_key)


def select_versioned_mapping(
    jurisdiction: str,
    entity_sector: str | None,
    raw_row: dict[str, Any],
    mappings: list[dict[str, Any]],
    exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve concept mapping by governed specificity and approved exceptions."""
    sector_order = _sector_order(entity_sector)
    exception = _select_exception(jurisdiction, raw_row, sector_order, exceptions)
    if exception is not None:
        return exception

    if not mappings:
        return None
    fiscal_year = int(raw_row["fiscal_year"])
    valid = []
    for rec in mappings:
        if not _valid_for_year(rec, fiscal_year):
            continue
        accounting_score = _accounting_standard_score(rec, raw_row)
        if accounting_score is None:
            continue
        taxonomy_score = _taxonomy_score(rec, raw_row)
        if taxonomy_score is None:
            continue
        valid.append((accounting_score, taxonomy_score, rec))
    if not valid:
        return None

    for sector in sector_order:
        candidates = []
        for accounting_score, taxonomy_score, rec in valid:
            if rec["mapping_sector"] != sector:
                continue
            score = _gics_score(rec, raw_row)
            if score is None:
                continue
            candidates.append((score, accounting_score, taxonomy_score, rec))
        if not candidates:
            continue
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3]["jurisdiction"] == jurisdiction,
                item[3]["effective_from_year"],
                item[3]["confidence"] if item[3]["confidence"] is not None else -1,
                -int(item[3]["mapping_id"] or 0),
            ),
            reverse=True,
        )
        return candidates[0][3]
    return None
