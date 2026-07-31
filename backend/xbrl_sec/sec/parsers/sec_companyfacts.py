"""SEC companyfacts parser for the unified US raw fact table."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.sec_filings import CompanyFactsFile, load_companyfacts
from xbrl_sec.sec.sources.sec_forms import CORE_ANNUAL_FORMS, CORE_QUARTERLY_FORMS, is_core_fundamental_form, normalize_form
from xbrl_sec.sec.parsers.xbrl_linkbase import parse_cal_xml, parse_pre_xml
from xbrl_sec.sec.writers.raw_facts import upsert_us_facts


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _normalize_fp(fp: Any, form: str | None, start: date | None, end: date | None) -> str | None:
    raw = (str(fp).strip().upper() if fp else "")
    if raw in {"FY", "Q1", "Q2", "Q3", "Q4"}:
        return raw
    if form in CORE_ANNUAL_FORMS:
        return "FY"
    if form in CORE_QUARTERLY_FORMS:
        if raw in {"Q1", "Q2", "Q3"}:
            return raw
        if start and end:
            days = (end - start).days
            if 70 <= days <= 110:
                quarter = ((end.month - 1) // 3) + 1
                return f"Q{quarter}"
    if end:
        return "FY" if form not in CORE_QUARTERLY_FORMS else f"Q{((end.month - 1) // 3) + 1}"
    return None


def _value_type(form: str | None) -> str:
    return "REST" if form and form.endswith("/A") else "ORIG"


def _is_annual_duration(start: date | None, end: date) -> bool:
    if start is None:
        return True
    days = (end - start).days
    return 300 <= days <= 380


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per unified-table key, preferring latest filed date then non-null value.

    The key includes filing_id so each filing's view of a period is retained
    (bitemporal): a later 10-K's comparative for an earlier period does NOT
    overwrite that period's originally-filed row. Dedup only collapses true
    duplicates within a single filing.
    """
    best: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (
            row["cik"], row.get("filing_id") or "", row["concept_id"], row["period_end"],
            row["fiscal_period"], row["context_tier"], row["value_type"],
        )
        existing = best.get(key)
        if existing is None:
            best[key] = row
            continue
        filed = row.get("filed_date") or date.min
        existing_filed = existing.get("filed_date") or date.min
        if filed > existing_filed or (filed == existing_filed and row.get("value") is not None):
            best[key] = row
    return list(best.values())


def _linkbase_path(cik: str, accn: str | None, kind: str) -> Path | None:
    if not accn:
        return None
    root = load_settings().market_data_root / "us_sec" / f"xbrl_{kind}"
    path = root / f"CIK{cik}_{accn}_{kind}.xml"
    return path if path.exists() else None


def _linkbase_for(
    cache: dict[str, tuple[dict[str, tuple], dict[str, tuple]]],
    cik: str,
    accn: str | None,
) -> tuple[dict[str, tuple], dict[str, tuple]]:
    if not accn:
        return {}, {}
    cached = cache.get(accn)
    if cached is not None:
        return cached
    cal = parse_cal_xml(_linkbase_path(cik, accn, "cal"))
    pre = parse_pre_xml(_linkbase_path(cik, accn, "pre"))
    cache[accn] = (cal, pre)
    return cal, pre


def parse_companyfacts_file(
    item: CompanyFactsFile,
    annual_10k_periods: dict[str, date] | None = None,
    filing_types: set[str] | frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    payload = load_companyfacts(item.path)
    cik = str(payload.get("cik") or item.cik).zfill(10)
    rows: list[dict[str, Any]] = []
    stats = {
        "kept": 0,
        "dropped_not_annual_10k": 0,
        "dropped_unselected_filing_type": 0,
        "dropped_without_current_period": 0,
        "dropped_comparison_period": 0,
        "dropped_non_annual_duration": 0,
    }
    linkbase_cache: dict[str, tuple[dict[str, tuple], dict[str, tuple]]] = {}
    for taxonomy, concepts in (payload.get("facts") or {}).items():
        if not isinstance(concepts, dict):
            continue
        for local_name, concept in concepts.items():
            concept_id = f"{taxonomy}/{local_name}"
            units = concept.get("units") or {}
            for unit, facts in units.items():
                if not isinstance(facts, list):
                    continue
                for fact in facts:
                    accn = fact.get("accn") or fact.get("accession")
                    end = _parse_date(fact.get("end"))
                    if not end:
                        continue
                    start = _parse_date(fact.get("start"))
                    form = normalize_form(fact.get("form"))
                    if not is_core_fundamental_form(form):
                        continue
                    if filing_types is not None and form not in filing_types:
                        stats["dropped_unselected_filing_type"] += 1
                        continue
                    if annual_10k_periods is not None:
                        if not accn or accn not in annual_10k_periods:
                            if form in CORE_ANNUAL_FORMS:
                                stats["dropped_not_annual_10k"] += 1
                            else:
                                stats["dropped_without_current_period"] += 1
                            continue
                        if annual_10k_periods[accn] != end:
                            stats["dropped_comparison_period"] += 1
                            continue
                        if form in CORE_ANNUAL_FORMS and not _is_annual_duration(start, end):
                            stats["dropped_non_annual_duration"] += 1
                            continue
                    fiscal_period = _normalize_fp(fact.get("fp"), form, start, end)
                    if not fiscal_period:
                        continue
                    if annual_10k_periods is not None and form in CORE_ANNUAL_FORMS:
                        fiscal_period = "FY"
                    value = _to_decimal(fact.get("val"))
                    if value is None:
                        continue
                    stats["kept"] += 1
                    cal_map, pre_map = _linkbase_for(linkbase_cache, cik, accn)
                    cal = cal_map.get(concept_id)
                    pre = pre_map.get(concept_id)
                    rows.append({
                        "cik": cik,
                        "concept_id": concept_id,
                        "period_end": end,
                        "fiscal_period": fiscal_period,
                        "value_type": _value_type(form),
                        "filing_id": fact.get("accn"),
                        "filing_type": form,
                        "period_start": start,
                        "fiscal_year": fact.get("fy"),
                        "source_fp": fact.get("fp"),
                        "value": value,
                        "unit": unit,
                        "filed_date": _parse_date(fact.get("filed")),
                        "taxonomy": taxonomy,
                        "context_tier": 0,
                        "statement_type": cal[4] if cal else pre[3] if pre else None,
                        "parent_id": cal[0] if cal else None,
                        "root_id": cal[1] if cal else concept_id if pre else None,
                        "concept_path": cal[2] if cal else f"[{local_name}]" if pre else None,
                        "concept_id_level": cal[5] if cal else pre[2] if pre else None,
                        "weight": cal[3] if cal else None,
                        "effective_weight": cal[6] if cal else None,
                        "pre_parent_id": pre[0] if pre else None,
                        "pre_order": pre[1] if pre else None,
                        "pre_level": pre[2] if pre else None,
                        "pre_position": pre[4] if pre else None,
                    })
    return _dedupe(rows), stats


def parse_and_write_us(item: CompanyFactsFile) -> int:
    rows, _stats = parse_companyfacts_file(item)
    return upsert_us_facts(rows)
