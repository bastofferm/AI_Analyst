from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Path, Query

from ..db import acquire
from ..models.financials import (
    AnalyticsCoverageResponse,
    AnalyticsLineItemRow,
    AnalyticsLineItemSection,
    AnalyticsMetricGroup,
    AnalyticsMetricRow,
    AnalyticsResponse,
    AnalyticsRow,
    Period,
)


router = APIRouter()


def _period_filter(alias: str, period: Period) -> str:
    if period == "FY":
        return f"{alias}.fiscal_period IN ('FY','Annual')"
    if period == "H1":
        return f"{alias}.fiscal_period IN ('H1','SemiAnnual','Q')"
    if period == "Q":
        return f"{alias}.fiscal_period IN ('Q1','Q2','Q3','Q4')"
    return f"{alias}.fiscal_period = '{period}'"


def _line_item_label(line_item_id: str) -> str:
    return line_item_id.replace("_", " ").title()


def _metric_label(metric_id: str, name: str | None) -> str:
    return name or metric_id.replace("_", " ").title()


def _metric_relevance(metric_id: str, defn: dict, ctx: dict) -> tuple[str, str]:
    scope = str(defn.get("sector_scope") or "universal").lower()
    gics = str(defn.get("gics_sector") or "").lower()
    category = str(defn.get("category") or "").lower()
    jurisdiction = str(ctx.get("jurisdiction") or "").upper()
    company_sector = str(ctx.get("gics_sector_code") or "").lower()
    mapping_sector = str(ctx.get("mapping_sector") or "").lower()

    if category == "regional":
        if scope in {"jp_gaap", "japan_gaap"}:
            if jurisdiction == "JP":
                return "relevant", "Relevant for Japanese GAAP reporting."
            return "irrelevant", "Japan GAAP regional metric; not applicable to US filers."
        if scope == "ifrs":
            return "irrelevant", "IFRS-specific regional metric; show only when an IFRS reporting profile is available."
    if scope in {"universal", "", "none"} and not gics:
        return "relevant", "Universal metric."
    if company_sector and (
        gics == company_sector
        or gics.startswith(company_sector + "_")
        or scope == f"gics_{company_sector}"
    ):
        return "relevant", f"Relevant for GICS sector {company_sector}."
    if mapping_sector == "bank_financial" and ("bank" in scope or "bank" in gics):
        return "relevant", "Relevant for bank financials."
    if mapping_sector == "non_bank_financial" and any(
        token in scope + " " + gics for token in ("insurance", "reit", "asset")
    ):
        return "relevant", "Relevant for this non-bank financial sector family."
    return "irrelevant", f"Sector-scoped metric ({defn.get('sector_scope') or defn.get('gics_sector')}) not applicable to this company."


def _metric_tooltip(defn: dict, relevance_note: str = "", missing_note: str = "") -> str:
    parts: list[str] = []
    if defn.get("name"):
        parts.append(str(defn["name"]))
    if defn.get("formula_symbolic"):
        parts.append(f"Formula: {defn['formula_symbolic']}")
    elif defn.get("formula"):
        parts.append(f"Formula: {defn['formula']}")
    if defn.get("note"):
        parts.append(str(defn["note"]))
    if defn.get("interpretation"):
        parts.append(str(defn["interpretation"]))
    if relevance_note:
        parts.append(f"Relevance: {relevance_note}")
    if missing_note:
        parts.append(missing_note)
    return "\n\n".join(parts)


def _trace_tooltip(trace: dict | None) -> str:
    if not trace:
        return ""
    parts: list[str] = []
    if trace.get("fiscal_year"):
        parts.append(f"AS OF: FY{trace.get('fiscal_year')} {trace.get('fiscal_period') or ''} ({trace.get('period_end') or ''})".strip())
    for label, key in (
        ("TYPE", "metric_type"),
        ("FORM", "filing_form"),
        ("SOURCE", "source_concept_id"),
        ("DOC", "doc_id"),
        ("PATH", "concept_path"),
        ("STD", "std_concept_path"),
    ):
        if trace.get(key):
            parts.append(f"{label}: {trace[key]}")
    return "\n".join(parts)


def _cagr(values: dict[int, Optional[float]]) -> Optional[float]:
    if not values:
        return None
    yrs = sorted(v for v in values.keys() if values.get(v) is not None)
    if len(yrs) < 2:
        return None
    first_v = values[yrs[0]]
    last_v = values[yrs[-1]]
    if first_v is None or last_v is None or first_v <= 0 or last_v <= 0:
        return None
    n = yrs[-1] - yrs[0]
    if n <= 0:
        return None
    return (float(last_v) / float(first_v)) ** (1.0 / n) - 1.0


@router.get("/coverage/{ticker}", response_model=AnalyticsCoverageResponse)
async def get_analytics_coverage(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    period: Period = Query("FY"),
    year_min: int = Query(...),
    year_max: int = Query(...),
) -> AnalyticsCoverageResponse:
    if year_min > year_max:
        year_min, year_max = year_max, year_min

    metric_tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    recon_tbl = "fact_metrics_recon_us" if jurisdiction == "US" else "fact_metrics_recon_jp"
    fact_tbl = "fact_fundamentals_std_us" if jurisdiction == "US" else "fact_fundamentals_std_jp"
    dim_tbl = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    eid_col = "cik" if jurisdiction == "US" else "edinet_code"

    metric_period = _period_filter("m", period)
    std_period = _period_filter("s", period)

    async with acquire() as conn:
        ctx_sql = f"""
            SELECT primary_ticker,
                   COALESCE(mapping_sector, '') AS mapping_sector,
                   COALESCE(gics_sector_code, '') AS gics_sector_code,
                   COALESCE(gics_sector_name, '') AS gics_sector_name,
                   COALESCE(gics_industry_group_code, '') AS gics_industry_group_code
            FROM {dim_tbl}
            WHERE primary_ticker = $1
            LIMIT 1
        """
        ctx_row = await conn.fetchrow(ctx_sql, ticker)
        metric_ctx = dict(ctx_row) if ctx_row else {}
        metric_ctx["jurisdiction"] = jurisdiction

        def_rows = await conn.fetch("""
            SELECT metric_id, name, unit_type, category, formula, formula_symbolic,
                   note, interpretation, sector_scope, gics_sector
            FROM ref_metric_definitions
            ORDER BY category NULLS LAST, metric_id
        """)

        metrics_sql = f"""
            SELECT m.metric_id, m.category, m.fiscal_year, m.value, r.formula_with_values
            FROM {metric_tbl} m
            LEFT JOIN {recon_tbl} r
                   ON r.ticker = m.ticker
                  AND r.{eid_col} = m.{eid_col}
                  AND r.fiscal_year = m.fiscal_year
                  AND r.fiscal_period = m.fiscal_period
                  AND r.metric_id = m.metric_id
            WHERE m.ticker = $1
              AND m.fiscal_year BETWEEN $2 AND $3
              AND {metric_period}
            ORDER BY m.metric_id, m.fiscal_year
        """
        metric_rows_raw = await conn.fetch(metrics_sql, ticker, year_min, year_max)

        supplemental_rows = []
        if jurisdiction == "US":
            try:
                supplemental_rows = await conn.fetch(f"""
                    SELECT m.metric_id,
                           COALESCE(r.category, 'solvency_liquidity') AS category,
                           m.fiscal_year,
                           m.value,
                           m.formula_with_values
                    FROM fact_metrics_supplemental_us m
                    LEFT JOIN ref_metric_definitions r ON r.metric_id = m.metric_id
                    WHERE m.ticker = $1
                      AND m.fiscal_year BETWEEN $2 AND $3
                      AND {metric_period}
                    ORDER BY m.metric_id, m.fiscal_year
                """, ticker, year_min, year_max)
            except Exception:
                supplemental_rows = []

        line_sql = f"""
            SELECT s.line_item_id, s.fiscal_year, s.fiscal_period, s.period_end, s.value,
                   COALESCE(r.category, 'uncategorised') AS category,
                   COALESCE(r.label, s.line_item_id) AS label,
                   COALESCE(r.unit_type, 'CCY') AS unit_type,
                   s.metric_type, s.filing_form, s.source_concept_id,
                   s.filing_id AS doc_id, s.concept_path, s.std_concept_path
            FROM {fact_tbl} s
            JOIN {dim_tbl} d ON d.{eid_col} = s.{eid_col}
            LEFT JOIN ref_standardized_line_items r ON r.line_item_id = s.line_item_id
            WHERE d.primary_ticker = $1
              AND s.fiscal_year BETWEEN $2 AND $3
              AND {std_period}
            ORDER BY category, s.line_item_id, s.fiscal_year DESC
        """
        line_rows_raw = await conn.fetch(line_sql, ticker, year_min, year_max)

    metric_data: dict[str, dict] = {}
    for r in list(metric_rows_raw) + list(supplemental_rows):
        mid = r["metric_id"]
        entry = metric_data.setdefault(
            mid,
            {"category": r["category"], "values": {}, "formulas": {}},
        )
        try:
            entry["values"][int(r["fiscal_year"])] = (
                float(r["value"]) if r["value"] is not None else None
            )
        except (TypeError, ValueError):
            entry["values"][int(r["fiscal_year"])] = None
        if r["formula_with_values"]:
            entry["formulas"][int(r["fiscal_year"])] = str(r["formula_with_values"])

    by_cat: dict[str, list[AnalyticsMetricRow]] = {}
    metrics_defined = 0
    for r in def_rows:
        mid = r["metric_id"]
        defn = dict(r)
        relevance, relevance_note = _metric_relevance(mid, defn, metric_ctx)
        if relevance != "relevant":
            continue
        metrics_defined += 1
        computed = metric_data.get(mid, {})
        values = computed.get("values", {})
        formulas = computed.get("formulas", {})
        display_relevance: Literal["relevant", "missing"] = "relevant" if values else "missing"
        missing_note = "" if values else "Missing required source data for the selected period."
        category = str(defn.get("category") or computed.get("category") or "Uncategorised")
        by_cat.setdefault(category, []).append(
            AnalyticsMetricRow(
                metric_id=mid,
                name=_metric_label(mid, defn.get("name")),
                category=category,
                unit_type=defn.get("unit_type"),
                relevance=display_relevance,
                tooltip=_metric_tooltip(defn, relevance_note, missing_note),
                values=values,
                formulas=formulas,
            )
        )

    metric_groups = [
        AnalyticsMetricGroup(
            category=cat,
            computed_count=sum(1 for row in rows if row.values),
            defined_count=len(rows),
            rows=rows,
        )
        for cat, rows in sorted(by_cat.items())
    ]

    line_by_id: dict[str, dict] = {}
    for r in line_rows_raw:
        lid = r["line_item_id"]
        entry = line_by_id.setdefault(
            lid,
            {
                "line_item_id": lid,
                "name": r["label"] or _line_item_label(lid),
                "category": r["category"] or "uncategorised",
                "unit_type": r["unit_type"] or "CCY",
                "values": {},
                "trace": {
                    "metric_type": r["metric_type"],
                    "filing_form": r["filing_form"],
                    "source_concept_id": r["source_concept_id"],
                    "doc_id": r["doc_id"],
                    "concept_path": r["concept_path"],
                    "std_concept_path": r["std_concept_path"],
                    "fiscal_year": r["fiscal_year"],
                    "fiscal_period": r["fiscal_period"],
                    "period_end": r["period_end"],
                },
            },
        )
        try:
            entry["values"][int(r["fiscal_year"])] = (
                float(r["value"]) if r["value"] is not None else None
            )
        except (TypeError, ValueError):
            entry["values"][int(r["fiscal_year"])] = None

    line_by_cat: dict[str, list[AnalyticsLineItemRow]] = {}
    for item in line_by_id.values():
        category = str(item["category"])
        line_by_cat.setdefault(category, []).append(
            AnalyticsLineItemRow(
                line_item_id=item["line_item_id"],
                name=item["name"],
                category=category,
                unit_type=item["unit_type"],
                tooltip=_trace_tooltip(item.get("trace")),
                values=item["values"],
            )
        )

    line_item_sections = [
        AnalyticsLineItemSection(
            category=cat,
            rows=sorted(rows, key=lambda row: row.line_item_id),
        )
        for cat, rows in sorted(line_by_cat.items())
    ]

    return AnalyticsCoverageResponse(
        ticker=ticker,
        jurisdiction=jurisdiction,
        period=period,
        year_min=year_min,
        year_max=year_max,
        metric_table=metric_tbl,
        line_item_table=fact_tbl,
        metrics_defined=metrics_defined,
        metrics_computed=len(metric_data),
        metric_groups=metric_groups,
        line_item_sections=line_item_sections,
    )


@router.get("/{ticker}", response_model=AnalyticsResponse)
async def get_analytics(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
    year_min: Optional[int] = Query(None),
    year_max: Optional[int] = Query(None),
) -> AnalyticsResponse:
    tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"

    async with acquire() as conn:
        if year_min is None or year_max is None:
            ymax_row = await conn.fetchrow(
                f"SELECT MAX(fiscal_year) AS ymax FROM {tbl} WHERE ticker=$1",
                ticker,
            )
            ymax = int(ymax_row["ymax"]) if ymax_row and ymax_row["ymax"] else None
            if ymax is None:
                return AnalyticsResponse(ticker=ticker, year_min=0, year_max=0, rows=[])
            year_max = year_max or ymax
            year_min = year_min or (ymax - 4)

        sql = f"""
            SELECT m.metric_id,
                   m.category,
                   m.fiscal_year,
                   m.value,
                   d.name,
                   d.unit_type
            FROM   {tbl} m
            LEFT   JOIN ref_metric_definitions d ON d.metric_id = m.metric_id
            WHERE  m.ticker = $1
              AND  m.fiscal_period IN ('FY','Annual')
              AND  m.fiscal_year BETWEEN $2 AND $3
            ORDER  BY m.category, m.metric_id, m.fiscal_year
        """
        rows_raw = await conn.fetch(sql, ticker, year_min, year_max)

    grouped: dict[str, dict] = {}
    for r in rows_raw:
        mid = r["metric_id"]
        if mid not in grouped:
            grouped[mid] = {
                "metric_id": mid,
                "name": r["name"] or mid,
                "category": r["category"],
                "unit_type": r["unit_type"] or "x",
                "values": {},
            }
        try:
            grouped[mid]["values"][int(r["fiscal_year"])] = float(r["value"]) if r["value"] is not None else None
        except (TypeError, ValueError):
            grouped[mid]["values"][int(r["fiscal_year"])] = None

    rows: list[AnalyticsRow] = []
    for g in grouped.values():
        years_with_vals = [y for y in sorted(g["values"]) if g["values"][y] is not None]
        latest_y = years_with_vals[-1] if years_with_vals else None
        latest_v = g["values"].get(latest_y) if latest_y is not None else None
        rows.append(AnalyticsRow(
            metric_id=g["metric_id"],
            name=g["name"],
            category=g["category"],
            unit_type=g["unit_type"],
            values=g["values"],
            latest_value=latest_v,
            latest_year=latest_y,
            cagr=_cagr(g["values"]),
        ))

    return AnalyticsResponse(ticker=ticker, year_min=year_min, year_max=year_max, rows=rows)
