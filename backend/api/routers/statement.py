from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query

from ..db import acquire
from ..models.financials import (
    Period,
    StatementColumn,
    StatementDisplayGenerationRequest,
    StatementDisplayGenerationResponse,
    StatementFiling,
    StatementResponse,
    StatementRow,
)
from ..settings import get_settings


router = APIRouter()
logger = logging.getLogger(__name__)


_STATEMENT_CATEGORY = {
    "BS": "balance_sheet",
    "IS": "income_statement",
    "CF": "cash_flow",
}

_ACCOUNTING_STANDARD = {
    "US": "US_GAAP",
    "JP": "JP_GAAP",
}

_ASSEMBLER_STATEMENT = {
    "BS": "balance_sheet",
    "IS": "income_statement",
    "CF": "cash_flow_statement",
}


async def _resolve_entity_id(
    ticker: str,
    jurisdiction: Literal["US", "JP"],
) -> str | None:
    if jurisdiction == "US":
        sql = "SELECT cik::text AS entity_id FROM dim_company_us WHERE upper(primary_ticker)=upper($1) LIMIT 1"
    else:
        sql = "SELECT edinet_code AS entity_id FROM dim_company_jp WHERE upper(primary_ticker)=upper($1) LIMIT 1"
    async with acquire() as conn:
        row = await conn.fetchrow(sql, ticker)
    return str(row["entity_id"]) if row and row["entity_id"] else None


def _statement_display_sector(mapping_sector: str | None, gics_industry_group_code: object = None) -> str:
    sector = str(mapping_sector or "corp")
    gics = str(gics_industry_group_code or "")
    if sector == "bank_financial":
        return "bank_financial"
    if sector == "non_bank_financial":
        if gics == "4030":
            return "insurance"
        if gics == "6010":
            return "reit"
        return "asset_manager_other_financial"
    return "corp"


def _policy_sector_keys(sector_scope: str) -> list[str]:
    keys = {"", sector_scope}
    if sector_scope in {"insurance", "reit", "asset_manager_other_financial"}:
        keys.add("non_bank_financial")
    return sorted(keys)


def _cagr(values: dict[int, Optional[float]]) -> Optional[float]:
    if not values:
        return None
    yrs = sorted(v for v in values.keys())
    if len(yrs) < 2:
        return None
    first_v = values.get(yrs[0])
    last_v = values.get(yrs[-1])
    if first_v is None or last_v is None or first_v <= 0 or last_v <= 0:
        return None
    n = yrs[-1] - yrs[0]
    if n <= 0:
        return None
    return (float(last_v) / float(first_v)) ** (1.0 / n) - 1.0


def _assembler_cagr(row: dict, values: dict[int, Optional[float]]) -> Optional[float]:
    if not row.get("cagr_eligible", True):
        return None
    yrs = sorted(year for year, value in values.items() if value is not None)
    if len(yrs) < 2:
        return None
    first_v = values.get(yrs[0])
    last_v = values.get(yrs[-1])
    if first_v is None or last_v is None:
        return None
    if first_v == 0:
        return None
    if (first_v < 0 < last_v) or (first_v > 0 > last_v):
        return None
    n = yrs[-1] - yrs[0]
    if n <= 0:
        return None
    try:
        ratio = abs(float(last_v)) / abs(float(first_v))
        if ratio <= 0:
            return None
        growth = ratio ** (1.0 / n) - 1.0
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    return -growth if float(first_v) < 0 and float(last_v) < 0 else growth


def _unit_family(unit: object) -> str:
    text = str(unit or "").strip().upper()
    if text in {"USD", "EUR", "JPY", "GBP", "CCY", "MONETARY"}:
        return "CCY"
    if "/" in text and "SHARE" in text:
        return "PER_SHARE"
    if text in {"%", "PCT", "PERCENT", "PERCENTAGE", "DEC", "DECIMAL"}:
        return "PCT"
    if text in {"SHARES", "SHARE", "COUNT"}:
        return "shares"
    if text in {"X", "RATIO", "MULTIPLE"}:
        return "ratio"
    return text or "CCY"


async def _llm_raw_filing_statement_response(
    ticker: str,
    jurisdiction: Literal["US", "JP"],
    statement: Literal["BS", "IS", "CF"],
    period: Period,
    year_min: Optional[int],
    year_max: Optional[int],
    full: bool,
    display_depth: Optional[int],
) -> StatementResponse | None:
    max_depth = int(display_depth) if display_depth is not None else (2 if full else 1)
    max_depth = max(1, min(max_depth, 2))
    include_detail = max_depth >= 2 or full
    try:
        async with acquire() as conn:
            display = await conn.fetchrow(
                """
                SELECT llm_statement_display_id, llm_display_run_id, ticker, filing_id,
                       filing_form, filed_date, fiscal_year, fiscal_period, display_title,
                       statement_title, role_uri, diagnostics
                FROM fact_llm_raw_filing_statement_display
                WHERE status = 'succeeded'
                  AND jurisdiction = $1
                  AND upper(ticker) = upper($2)
                  AND api_statement = $3
                  AND fiscal_period = $4
                  AND ($5::int IS NULL OR fiscal_year >= $5)
                  AND ($6::int IS NULL OR fiscal_year <= $6)
                ORDER BY fiscal_year DESC NULLS LAST, filed_date DESC NULLS LAST,
                         created_at DESC, llm_statement_display_id DESC
                LIMIT 1
                """,
                jurisdiction,
                ticker,
                statement,
                period,
                year_min,
                year_max,
            )
            if display is None:
                return None

            columns_raw = await conn.fetch(
                """
                SELECT column_key, label, period_start, period_end, fiscal_year, fiscal_period
                FROM fact_llm_raw_filing_display_column
                WHERE llm_statement_display_id = $1
                ORDER BY column_order
                """,
                display["llm_statement_display_id"],
            )
            rows_raw = await conn.fetch(
                """
                SELECT r.row_key, r.parent_row_key, r.display_label, r.row_kind,
                       r.aggregation, r.visibility, r.display_depth, r.confidence,
                       r.rationale,
                       COALESCE(src.source_node_keys, ARRAY[]::text[]) AS source_node_keys,
                       COALESCE(src.source_concept_ids, ARRAY[]::text[]) AS source_concept_ids,
                       v.column_key, v.value, v.unit
                FROM fact_llm_raw_filing_display_row r
                LEFT JOIN LATERAL (
                    SELECT array_agg(s.source_node_key ORDER BY s.source_order) AS source_node_keys,
                           array_agg(s.source_concept_id ORDER BY s.source_order) AS source_concept_ids
                    FROM fact_llm_raw_filing_display_row_source s
                    WHERE s.row_id = r.row_id
                ) src ON TRUE
                LEFT JOIN fact_llm_raw_filing_display_value v ON v.row_id = r.row_id
                WHERE r.llm_statement_display_id = $1
                  AND r.visibility <> 'hidden'
                  AND r.display_depth <= $2
                  AND ($3::boolean OR r.visibility = 'default')
                ORDER BY r.display_order, v.column_key
                """,
                display["llm_statement_display_id"],
                max_depth,
                include_detail,
            )
            diag_raw = await conn.fetch(
                """
                SELECT severity, message
                FROM fact_llm_raw_filing_display_diagnostic
                WHERE llm_statement_display_id = $1
                  AND severity IN ('warning', 'error')
                ORDER BY diagnostic_id
                LIMIT 8
                """,
                display["llm_statement_display_id"],
            )
    except Exception as exc:
        logger.warning("llm raw filing statement fallback for %s %s %s: %s", ticker, statement, period, exc)
        return None

    if not columns_raw:
        return None

    columns = [
        StatementColumn(
            key=row["column_key"],
            label=row["label"],
            period_start=row["period_start"].isoformat() if row["period_start"] else None,
            period_end=row["period_end"].isoformat() if row["period_end"] else None,
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
        )
        for row in columns_raw
    ]

    grouped: dict[str, dict[str, object]] = {}
    row_order: list[str] = []
    for row in rows_raw:
        row_key = row["row_key"]
        if row_key not in grouped:
            source_concepts = [str(v) for v in (row["source_concept_ids"] or []) if v]
            grouped[row_key] = {
                "line_item_id": row_key,
                "label": row["display_label"],
                "category": "llm_raw_filing",
                "unit_type": None,
                "display_role": row["row_kind"],
                "parent_id": row["parent_row_key"],
                "depth": row["display_depth"],
                "values": {},
                "source_concept_id": source_concepts[0] if source_concepts else None,
                "source_node_keys": [str(v) for v in (row["source_node_keys"] or []) if v],
                "default_visibility": row["visibility"],
                "aggregation": row["aggregation"],
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                "rationale": row["rationale"],
            }
            row_order.append(row_key)
        if row["column_key"] is not None:
            values = grouped[row_key]["values"]
            assert isinstance(values, dict)
            values[str(row["column_key"])] = float(row["value"]) if row["value"] is not None else None
            if grouped[row_key]["unit_type"] is None and row["unit"]:
                grouped[row_key]["unit_type"] = _unit_family(row["unit"])

    visible = set(grouped)
    rows: list[StatementRow] = []
    currency = "USD"
    for row_key in row_order:
        item = grouped[row_key]
        parent_id = item["parent_id"]
        if parent_id not in visible:
            parent_id = None
        unit_type = item["unit_type"]
        if unit_type == "CCY":
            currency = "USD"
        rows.append(StatementRow(
            line_item_id=str(item["line_item_id"]),
            label=str(item["label"]),
            category=str(item["category"]),
            unit_type=str(unit_type) if unit_type else None,
            display_role=str(item["display_role"]),
            parent_id=str(parent_id) if parent_id else None,
            depth=int(item["depth"] or 0),
            values=item["values"],  # type: ignore[arg-type]
            cagr=None,
            source_concept_id=str(item["source_concept_id"]) if item["source_concept_id"] else None,
            default_visibility=str(item["default_visibility"]) if item["default_visibility"] else None,
            aggregation=str(item["aggregation"]) if item["aggregation"] else None,
            source_node_keys=item["source_node_keys"],  # type: ignore[arg-type]
            confidence=item["confidence"],  # type: ignore[arg-type]
            rationale=str(item["rationale"]) if item["rationale"] else None,
        ))

    fiscal_years = [int(row["fiscal_year"]) for row in columns_raw if row["fiscal_year"] is not None]
    y_min = min(fiscal_years) if fiscal_years else int(display["fiscal_year"] or year_min or 0)
    y_max = max(fiscal_years) if fiscal_years else int(display["fiscal_year"] or year_max or y_min)
    diagnostics = [f"{row['severity']}: {row['message']}" for row in diag_raw]
    return StatementResponse(
        ticker=display["ticker"] or ticker,
        statement=statement,
        period=period,
        currency=currency,
        year_min=y_min,
        year_max=y_max,
        rows=rows,
        display_mode="llm_raw_filing",
        columns=columns,
        filing=StatementFiling(
            filing_id=display["filing_id"],
            filing_form=display["filing_form"],
            filed_date=display["filed_date"].isoformat() if display["filed_date"] else None,
            statement_title=display["display_title"] or display["statement_title"],
            role_uri=display["role_uri"],
        ),
        diagnostics=diagnostics,
    )


async def _filing_native_statement_response(
    ticker: str,
    statement: Literal["BS", "IS", "CF"],
    period: Period,
    year_min: Optional[int],
    year_max: Optional[int],
    full: bool,
    display_depth: Optional[int],
) -> StatementResponse | None:
    if period not in {"Q1", "Q2", "Q3", "Q4"}:
        return None
    max_depth = int(display_depth) if display_depth is not None else (2 if full else 1)
    max_depth = max(1, min(max_depth, 2))
    include_detail = max_depth >= 2 or full
    try:
        async with acquire() as conn:
            display = await conn.fetchrow(
                """
                SELECT statement_display_id, ticker, filing_id, filing_form, filed_date,
                       fiscal_year, fiscal_period, statement_title, role_uri
                FROM fact_filing_statement_display
                WHERE jurisdiction = 'US'
                  AND upper(ticker) = upper($1)
                  AND api_statement = $2
                  AND fiscal_period = $3
                  AND ($4::int IS NULL OR fiscal_year >= $4)
                  AND ($5::int IS NULL OR fiscal_year <= $5)
                ORDER BY fiscal_year DESC, filed_date DESC NULLS LAST, filing_id DESC
                LIMIT 1
                """,
                ticker,
                statement,
                period,
                year_min,
                year_max,
            )
            if display is None:
                return None

            columns_raw = await conn.fetch(
                """
                SELECT column_key, label, period_start, period_end, fiscal_year, fiscal_period
                FROM fact_filing_statement_display_column
                WHERE statement_display_id = $1
                ORDER BY column_order
                """,
                display["statement_display_id"],
            )

            rows_raw = await conn.fetch(
                """
                SELECT n.node_key, n.parent_node_key, n.source_concept_id,
                       n.source_parent_concept_id, n.value_binding_concept_id,
                       n.raw_label, n.standardized_label,
                       n.display_label, n.display_role, n.display_depth,
                       n.presentation_depth,
                       n.std_line_item_id, n.default_visibility,
                       v.column_key, v.value, v.unit
                FROM fact_filing_statement_display_node n
                LEFT JOIN fact_filing_statement_display_value v ON v.node_id = n.node_id
                WHERE n.statement_display_id = $1
                  AND n.default_visibility <> 'hidden'
                  AND n.display_depth <= $2
                  AND ($3::boolean OR n.default_visibility = 'default')
                ORDER BY n.display_order
                """,
                display["statement_display_id"],
                max_depth,
                include_detail,
            )
    except Exception as exc:
        logger.warning("filing-native statement fallback for %s %s %s: %s", ticker, statement, period, exc)
        return None

    if not columns_raw:
        return None

    columns = [
        StatementColumn(
            key=row["column_key"],
            label=row["label"],
            period_start=row["period_start"].isoformat() if row["period_start"] else None,
            period_end=row["period_end"].isoformat() if row["period_end"] else None,
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
        )
        for row in columns_raw
    ]

    grouped: dict[str, dict[str, object]] = {}
    row_order: list[str] = []
    for row in rows_raw:
        node_key = row["node_key"]
        if node_key not in grouped:
            grouped[node_key] = {
                "line_item_id": node_key,
                "label": row["display_label"],
                "category": "filing_native",
                "unit_type": None,
                "display_role": str(row["display_role"] or "").lower(),
                "parent_id": row["parent_node_key"],
                "depth": row["display_depth"],
                "values": {},
                "source_concept_id": row["source_concept_id"],
                "source_parent_concept_id": row["source_parent_concept_id"],
                "value_binding_concept_id": row["value_binding_concept_id"],
                "std_line_item_id": row["std_line_item_id"],
                "raw_label": row["raw_label"],
                "standardized_label": row["standardized_label"],
                "default_visibility": row["default_visibility"],
                "presentation_depth": row["presentation_depth"],
            }
            row_order.append(node_key)
        if row["column_key"] is not None:
            values = grouped[node_key]["values"]
            assert isinstance(values, dict)
            values[str(row["column_key"])] = float(row["value"]) if row["value"] is not None else None
            if grouped[node_key]["unit_type"] is None and row["unit"]:
                grouped[node_key]["unit_type"] = _unit_family(row["unit"])

    visible = set(grouped)
    rows: list[StatementRow] = []
    currency = "USD"
    for node_key in row_order:
        item = grouped[node_key]
        parent_id = item["parent_id"]
        if parent_id not in visible:
            parent_id = None
        unit_type = item["unit_type"]
        if unit_type == "CCY":
            currency = "USD"
        rows.append(StatementRow(
            line_item_id=str(item["line_item_id"]),
            label=str(item["label"]),
            category=str(item["category"]),
            unit_type=str(unit_type) if unit_type else None,
            display_role=str(item["display_role"]),
            parent_id=str(parent_id) if parent_id else None,
            depth=int(item["depth"] or 0),
            values=item["values"],  # type: ignore[arg-type]
            cagr=None,
            source_concept_id=str(item["source_concept_id"]),
            source_parent_concept_id=str(item["source_parent_concept_id"]) if item["source_parent_concept_id"] else None,
            value_binding_concept_id=str(item["value_binding_concept_id"]) if item["value_binding_concept_id"] else None,
            std_line_item_id=str(item["std_line_item_id"]) if item["std_line_item_id"] else None,
            raw_label=str(item["raw_label"]) if item["raw_label"] else None,
            standardized_label=str(item["standardized_label"]) if item["standardized_label"] else None,
            default_visibility=str(item["default_visibility"]) if item["default_visibility"] else None,
            presentation_depth=int(item["presentation_depth"]) if item["presentation_depth"] is not None else None,
        ))

    fiscal_years = [int(row["fiscal_year"]) for row in columns_raw if row["fiscal_year"] is not None]
    y_min = min(fiscal_years) if fiscal_years else int(display["fiscal_year"] or year_min or 0)
    y_max = max(fiscal_years) if fiscal_years else int(display["fiscal_year"] or year_max or y_min)
    return StatementResponse(
        ticker=display["ticker"] or ticker,
        statement=statement,
        period=period,
        currency=currency,
        year_min=y_min,
        year_max=y_max,
        rows=rows,
        display_mode="filing_native",
        columns=columns,
        filing=StatementFiling(
            filing_id=display["filing_id"],
            filing_form=display["filing_form"],
            filed_date=display["filed_date"].isoformat() if display["filed_date"] else None,
            statement_title=display["statement_title"],
            role_uri=display["role_uri"],
        ),
    )


def _statement_response_from_assembler(
    ticker: str,
    statement: Literal["BS", "IS", "CF"],
    period: Period,
    assembled: dict,
    full: bool,
) -> StatementResponse:
    periods = [int(year) for year in assembled.get("periods") or []]
    if not periods:
        return StatementResponse(
            ticker=ticker,
            statement=statement,
            period=period,
            currency="USD",
            year_min=0,
            year_max=0,
            rows=[],
        )

    visible_ids: set[str] = set()
    for row in assembled.get("rows") or []:
        policy = str(row.get("display_policy") or "MAIN").upper()
        if not row.get("display_ready"):
            continue
        if policy == "SUPPLEMENTAL" and not full:
            continue
        visible_ids.add(str(row.get("line_item_id")))

    rows: list[StatementRow] = []
    currency = "USD"
    for row in assembled.get("rows") or []:
        line_item_id = str(row.get("line_item_id") or "")
        if line_item_id not in visible_ids:
            continue
        unit = row.get("unit") or row.get("unit_type")
        if str(unit).upper() in {"USD", "EUR", "JPY", "GBP"}:
            currency = str(unit).upper()
        values = {
            int(year): (float(value) if value is not None else None)
            for year, value in (row.get("values") or {}).items()
            if int(year) in periods
        }
        parent_id = row.get("display_parent_id")
        if parent_id not in visible_ids:
            parent_id = None
        rows.append(StatementRow(
            line_item_id=line_item_id,
            label=row.get("label") or line_item_id,
            category=assembled.get("statement_type"),
            unit_type=_unit_family(unit),
            display_role=row.get("item_class") or str(row.get("display_role") or "").lower(),
            parent_id=parent_id,
            depth=max(int(row.get("indent_level") or 1) - 1, 0),
            values=values,
            cagr=_assembler_cagr(row, values),
        ))

    return StatementResponse(
        ticker=ticker,
        statement=statement,
        period=period,
        currency=currency,
        year_min=min(periods),
        year_max=max(periods),
        rows=rows,
    )


async def _assembler_statement_response(
    ticker: str,
    jurisdiction: Literal["US", "JP"],
    statement: Literal["BS", "IS", "CF"],
    period: Period,
    year_min: Optional[int],
    year_max: Optional[int],
    full: bool,
) -> StatementResponse:
    if period != "FY":
        raise ValueError("assembler statement route currently supports FY only")
    from xbrl_sec.sec.statements.data import assemble_statement_for_ticker

    n_periods = (int(year_max) - int(year_min) + 1) if year_min is not None and year_max is not None else 5
    assembled = await asyncio.to_thread(
        assemble_statement_for_ticker,
        jurisdiction,
        ticker,
        _ASSEMBLER_STATEMENT[statement],
        fiscal_period=period,
        n_periods=n_periods,
        year_from=year_min,
        year_to=year_max,
        include_hidden=full,
    )
    return _statement_response_from_assembler(ticker, statement, period, assembled, full)


def _period_filter(alias: str, period: Period, jurisdiction: Literal["US", "JP"]) -> str:
    if period == "FY":
        return f"AND {alias}.fiscal_period IN ('FY','Annual')"
    if period == "H1":
        if jurisdiction == "JP":
            return f"AND {alias}.fiscal_period IN ('H1','SemiAnnual','Q')"
        return f"AND {alias}.fiscal_period = 'H1'"
    if period == "Q":
        return f"AND {alias}.fiscal_period IN ('Q1','Q2','Q3','Q4')"
    return f"AND {alias}.fiscal_period = '{period}'"


def _build_hierarchy(
    grouped: dict[str, dict],
    edges_raw: list,
) -> list[StatementRow]:
    """Attach parent_id + depth to each grouped item and sort in DFS order."""
    parent_of: dict[str, str] = {}
    children_of: dict[str, dict[str, int]] = {}  # parent -> {child: sibling_rank}

    for e in edges_raw:
        child_id = e["child_id"]
        parent_id = e["parent_id"]
        rank = e["sibling_rank"] or 0
        if child_id not in parent_of:
            parent_of[child_id] = parent_id
        children_of.setdefault(parent_id, {})
        if child_id not in children_of[parent_id]:
            children_of[parent_id][child_id] = rank

    children_sorted: dict[str, list[tuple[int, str]]] = {
        p: sorted((rank, cid) for cid, rank in children.items())
        for p, children in children_of.items()
    }

    depth_cache: dict[str, int] = {}

    def get_depth(lid: str) -> int:
        if lid in depth_cache:
            return depth_cache[lid]
        parent = parent_of.get(lid)
        if parent is None or parent not in grouped:
            depth_cache[lid] = 0
        else:
            depth_cache[lid] = 1 + get_depth(parent)
        return depth_cache[lid]

    for lid in grouped:
        get_depth(lid)

    visited: set[str] = set()

    def dfs(lid: str, result: list[str]) -> None:
        if lid not in grouped or lid in visited:
            return
        visited.add(lid)
        result.append(lid)
        for _, child_id in children_sorted.get(lid, []):
            dfs(child_id, result)

    roots = [
        lid for lid in grouped
        if parent_of.get(lid) is None or parent_of.get(lid) not in grouped
    ]
    roots.sort(key=lambda lid: grouped[lid].get("sort_order") or 9999)

    ordered: list[str] = []
    for lid in roots:
        dfs(lid, ordered)

    seen: set[str] = set(ordered)
    for lid in sorted(grouped.keys(), key=lambda l: grouped[l].get("sort_order") or 9999):
        if lid not in seen:
            ordered.append(lid)

    rows: list[StatementRow] = []
    for lid in ordered:
        g = grouped[lid]
        effective_parent = parent_of.get(lid)
        if effective_parent not in grouped:
            effective_parent = None
        rows.append(StatementRow(
            line_item_id=lid,
            label=g["label"],
            category=g["category"],
            unit_type=g["unit_type"],
            display_role=g["item_class"],
            parent_id=effective_parent,
            depth=depth_cache.get(lid, 0),
            values=g["values"],
            cagr=_cagr(g["values"]),
        ))

    return rows


@router.get("/{ticker}", response_model=StatementResponse)
async def get_statement(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    statement: Literal["BS", "IS", "CF"] = Query(...),
    period: Period = Query("FY"),
    year_min: Optional[int] = Query(None),
    year_max: Optional[int] = Query(None),
    full: bool = Query(False),
    display_depth: Optional[int] = Query(None, ge=1, le=2),
) -> StatementResponse:
    llm_raw = await _llm_raw_filing_statement_response(
        ticker=ticker,
        jurisdiction=jurisdiction,
        statement=statement,
        period=period,
        year_min=year_min,
        year_max=year_max,
        full=full,
        display_depth=display_depth,
    )
    if llm_raw is not None:
        return llm_raw

    if jurisdiction == "US":
        filing_native = await _filing_native_statement_response(
            ticker=ticker,
            statement=statement,
            period=period,
            year_min=year_min,
            year_max=year_max,
            full=full,
            display_depth=display_depth,
        )
        if filing_native is not None:
            return filing_native

    settings = get_settings()
    if settings.use_statement_assembler and period == "FY":
        try:
            return await _assembler_statement_response(
                ticker=ticker,
                jurisdiction=jurisdiction,
                statement=statement,
                period=period,
                year_min=year_min,
                year_max=year_max,
                full=full,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "statement assembler fallback for %s %s %s %s: %s",
                jurisdiction,
                ticker,
                statement,
                period,
                exc,
            )

    cat = _STATEMENT_CATEGORY[statement]
    std = _ACCOUNTING_STANDARD[jurisdiction]

    if jurisdiction == "US":
        resolve_sql = """
            SELECT cik::text AS eid,
                   COALESCE(mapping_sector, 'corp') AS mapping_sector,
                   COALESCE(gics_industry_group_code, '') AS gics_industry_group_code
            FROM dim_company_us
            WHERE primary_ticker=$1
            LIMIT 1
        """
        fact_tbl = "fact_fundamentals_std_us"
        eid_col = "cik"
        order_col = "display_order_us_gaap"
    else:
        resolve_sql = """
            SELECT edinet_code AS eid,
                   COALESCE(mapping_sector, 'corp') AS mapping_sector,
                   COALESCE(gics_industry_group_code, '') AS gics_industry_group_code
            FROM dim_company_jp
            WHERE primary_ticker=$1
            LIMIT 1
        """
        fact_tbl = "fact_fundamentals_std_jp"
        eid_col = "edinet_code"
        order_col = "display_order_jp_gaap"

    period_filter = _period_filter("s", period, jurisdiction)
    importance_filter = (
        "" if full
        else "AND (COALESCE(r.importance, 0) >= 2 OR r.item_class IN ('intermediate', 'catch_all'))"
    )

    sql = f"""
        WITH ranked AS (
            SELECT s.line_item_id,
                   r.label,
                   r.category,
                   r.unit_type,
                   r.item_class,
                   r.importance,
                   COALESCE(r.{order_col}, r.display_order) AS sort_order,
                   s.fiscal_year,
                   s.value,
                   s.currency,
                   BOOL_OR(
                       s.metric_type = 'RESIDUAL'
                       AND s.value < 0
                       AND s.line_item_id IN (
                           'other_current_assets',
                           'other_non_current_assets',
                           'other_current_liabilities',
                           'other_non_current_liabilities'
                       )
                   ) OVER (PARTITION BY s.line_item_id) AS has_unstable_negative_residual,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.line_item_id, s.fiscal_year
                       ORDER BY COALESCE(p.source_penalty, 0),
                                CASE s.metric_type
                                    WHEN 'RAW' THEN 0
                                    WHEN 'MARKET' THEN 0
                                    WHEN 'T2_SUM' THEN 1
                                    WHEN 'T2_COMPONENT' THEN 1
                                    WHEN 'DERIVED_BOTTOM_UP' THEN 2
                                    WHEN 'DERIVED_PARTIAL' THEN 3
                                    WHEN 'RESIDUAL' THEN 4
                                    ELSE 5
                                END,
                                s.period_end DESC NULLS LAST,
                                s.filed_date DESC NULLS LAST,
                                s.source_concept_id
                   ) AS rn
            FROM   {fact_tbl} s
            LEFT   JOIN ref_standardized_line_items r ON r.line_item_id = s.line_item_id
            LEFT JOIN LATERAL (
                SELECT policy_action,
                       default_visibility,
                       source_rank_penalty AS source_penalty,
                       reason_code
                FROM vw_concept_target_display_policy_active p
                WHERE p.jurisdiction = $5
                  AND p.normalized_concept_id = split_part(s.source_concept_id, ',', 1)
                  AND (p.target_variable = s.line_item_id OR p.target_variable = '')
                  AND COALESCE(p.mapping_sector, '') = ANY($6::text[])
                  AND (p.fiscal_year_from IS NULL OR s.fiscal_year >= p.fiscal_year_from)
                  AND (p.fiscal_year_to IS NULL OR s.fiscal_year <= p.fiscal_year_to)
                  AND (p.fiscal_period IS NULL OR p.fiscal_period = s.fiscal_period)
                ORDER BY
                  CASE WHEN p.target_variable = s.line_item_id THEN 0 ELSE 1 END,
                  p.specificity_rank DESC,
                  p.source_rank_penalty DESC,
                  p.policy_id DESC
                LIMIT 1
            ) p ON TRUE
            WHERE  s.{eid_col} = $1
              AND  r.category = $2
              AND  s.fiscal_year BETWEEN $3 AND $4
              AND  ($7::boolean OR COALESCE(p.default_visibility, 'default') = 'default')
              {period_filter}
              {importance_filter}
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
          AND ($7::boolean OR NOT COALESCE(has_unstable_negative_residual, FALSE))
        ORDER  BY sort_order NULLS LAST, line_item_id, fiscal_year
    """

    edge_sql = """
        SELECT parent_id, child_id, sibling_rank
        FROM   ref_std_item_edge
        WHERE  statement_type = $1
          AND  accounting_standard = $2
        ORDER  BY sibling_rank
    """

    async with acquire() as conn:
        row = await conn.fetchrow(resolve_sql, ticker)
        if row is None or not row["eid"]:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
        entity_id = row["eid"]
        sector_scope = _statement_display_sector(row["mapping_sector"], row["gics_industry_group_code"])
        policy_sector_keys = _policy_sector_keys(sector_scope)

        if year_min is None or year_max is None:
            yr_row = await conn.fetchrow(
                f"SELECT MAX(fiscal_year) AS ymax FROM {fact_tbl} WHERE {eid_col}=$1",
                entity_id,
            )
            ymax = int(yr_row["ymax"]) if yr_row and yr_row["ymax"] else None
            if ymax is None:
                return StatementResponse(
                    ticker=ticker, statement=statement, period=period,
                    currency="USD", year_min=0, year_max=0, rows=[],
                )
            year_max = year_max or ymax
            year_min = year_min or (ymax - 4)

        rows_raw = await conn.fetch(
            sql,
            entity_id,
            cat,
            year_min,
            year_max,
            jurisdiction,
            policy_sector_keys,
            full,
        )
        edges_raw = await conn.fetch(edge_sql, cat, std)

    grouped: dict[str, dict] = {}
    for r in rows_raw:
        lid = r["line_item_id"]
        if lid not in grouped:
            grouped[lid] = {
                "line_item_id": lid,
                "label": r["label"] or lid,
                "category": r["category"],
                "unit_type": r["unit_type"] or "CCY",
                "item_class": r["item_class"],
                "importance": r["importance"] or 0,
                "sort_order": r["sort_order"],
                "values": {},
                "currency": r["currency"],
            }
        try:
            grouped[lid]["values"][int(r["fiscal_year"])] = (
                float(r["value"]) if r["value"] is not None else None
            )
        except (TypeError, ValueError):
            grouped[lid]["values"][int(r["fiscal_year"])] = None

    currency = next((g["currency"] for g in grouped.values() if g["currency"]), "USD")
    rows = _build_hierarchy(grouped, edges_raw)

    return StatementResponse(
        ticker=ticker,
        statement=statement,
        period=period,
        currency=currency or "USD",
        year_min=year_min,
        year_max=year_max,
        rows=rows,
    )


@router.post("/{ticker}/llm-raw-filing-display", response_model=StatementDisplayGenerationResponse)
async def generate_llm_raw_filing_display(
    request: StatementDisplayGenerationRequest,
    ticker: str = Path(...),
) -> StatementDisplayGenerationResponse:
    entity_id = await _resolve_entity_id(ticker, request.jurisdiction)
    if entity_id is None:
        raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")

    try:
        from xbrl_sec.sec.sources.llm_raw_filing_display import build_llm_raw_filing_display

        result = await asyncio.to_thread(
            build_llm_raw_filing_display,
            request.jurisdiction,
            entity_id=entity_id,
            ticker=ticker,
            filing_id=request.filing_id,
            fiscal_period=request.period,
            year_min=request.year_min,
            year_max=request.year_max,
            statements=[request.statement] if request.statement else None,
            force=request.force,
            model=request.model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("llm raw filing display generation failed for %s %s", request.jurisdiction, ticker)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return StatementDisplayGenerationResponse(
        status=str(result.get("status") or "unknown"),
        ticker=ticker,
        jurisdiction=request.jurisdiction,
        run_id=int(result["run_id"]) if result.get("run_id") is not None else None,
        statements=int(result.get("statements") or 0),
        rows=int(result.get("rows") or 0),
        values=int(result.get("values") or 0),
        diagnostics=int(result.get("diagnostics") or 0),
        message=str(result.get("message")) if result.get("message") else None,
    )
