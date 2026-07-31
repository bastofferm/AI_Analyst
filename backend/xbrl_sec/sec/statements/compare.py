"""Golden-set comparison between current dashboard data and new assembly."""
from __future__ import annotations

import json
import contextlib
from decimal import Decimal
from io import StringIO
from typing import Any

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.statements.assembly import _cagr_eligible, normalize_statement_type
from xbrl_sec.sec.statements.data import assemble_statement_for_ticker


_DASH_STATEMENT = {
    "balance_sheet": "BalanceSheet",
    "income_statement": "IncomeStatement",
    "cash_flow_statement": "CashFlow",
}

_STATEMENTS = ["balance_sheet", "income_statement", "cash_flow_statement"]


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _default_golden_cases() -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT primary_ticker
            FROM dim_company_us_test
            WHERE include_in_pipeline
              AND COALESCE(mapping_sector, 'corp') = 'corp'
              AND primary_ticker = ANY(%s)
            ORDER BY array_position(%s::text[], primary_ticker)
            """,
            (["QCOM", "AAPL", "MSFT"], ["QCOM", "AAPL", "MSFT"]),
        )
        cases.extend({"jurisdiction": "US", "ticker": row[0], "reason": "golden_us_corp"} for row in cur.fetchall())
        cur.execute(
            """
            SELECT primary_ticker
            FROM dim_company_us_test
            WHERE include_in_pipeline
              AND COALESCE(mapping_sector, 'corp') = 'corp'
              AND primary_ticker <> ALL(%s)
            ORDER BY primary_ticker
            LIMIT 1
            """,
            (["QCOM", "AAPL", "MSFT"],),
        )
        row = cur.fetchone()
        if row:
            cases.append({"jurisdiction": "US", "ticker": row[0], "reason": "sparse_or_extra_us_corp_candidate"})
        cur.execute(
            """
            SELECT d.primary_ticker
            FROM dim_company_jp_test d
            WHERE d.include_in_pipeline
              AND COALESCE(d.mapping_sector, 'corp') = 'corp'
              AND d.primary_ticker IS NOT NULL
            ORDER BY d.primary_ticker
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row:
            cases.append({"jurisdiction": "JP", "ticker": row[0], "reason": "golden_jp_corp"})
        cur.execute(
            """
            SELECT d.primary_ticker
            FROM fact_statement_display_evidence_us e
            JOIN dim_company_us_test d ON d.cik = e.cik
            WHERE d.include_in_pipeline
              AND COALESCE(d.mapping_sector, 'corp') = 'corp'
              AND e.display_role IN ('NATURE_DISCLOSURE', 'OPERATING_EXPENSE_COMPONENT')
            GROUP BY d.primary_ticker
            ORDER BY COUNT(*) DESC, d.primary_ticker
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row and not any(c["jurisdiction"] == "US" and c["ticker"] == row[0] for c in cases):
            cases.append({"jurisdiction": "US", "ticker": row[0], "reason": "heavy_operating_expense_evidence"})
    return cases


def _legacy_dashboard_data(jurisdiction: str, ticker: str, statement_type: str, fiscal_period: str, n_periods: int):
    dash_stmt = _DASH_STATEMENT[normalize_statement_type(statement_type)]
    try:
        captured = StringIO()
        with contextlib.redirect_stdout(captured):
            from dashboard import ops_dashboard as dashboard  # noqa: PLC0415
            if jurisdiction.upper() == "JP":
                return dashboard.query_std_line_items_jp(ticker, dash_stmt, fiscal_period, n_periods=n_periods)
            return dashboard.query_std_line_items_us(ticker, dash_stmt, fiscal_period, n_periods=n_periods)
    except Exception as exc:
        return {"periods": [], "period_ends": {}, "rows": [], "_error": f"legacy_dashboard_import_or_query_failed: {exc}"}


def _legacy_rows_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in data.get("rows") or []:
        line_item_id = str(row.get("line_item_id") or row.get("concept_id") or "")
        if not line_item_id:
            continue
        out[line_item_id] = row
    return out


def _new_rows_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["line_item_id"]): row for row in data.get("rows") or []}


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _unit_family(unit: Any) -> str:
    text = str(unit or "").strip().upper()
    if text in {"USD", "EUR", "JPY", "GBP", "CCY"}:
        return "CCY"
    if text in {"%", "PCT", "PERCENT", "PERCENTAGE", "DEC", "DECIMAL"}:
        return "PCT"
    if text in {"SHARES", "SHARE", "COUNT"}:
        return "COUNT"
    if text in {"USD/SHARES", "USD/SHARE", "PER_SHARE"}:
        return "PER_SHARE"
    return text


def _compare_one(jurisdiction: str, ticker: str, statement_type: str, fiscal_period: str, n_periods: int) -> dict[str, Any]:
    legacy = _legacy_dashboard_data(jurisdiction, ticker, statement_type, fiscal_period, n_periods)
    assembled = assemble_statement_for_ticker(jurisdiction, ticker, statement_type, fiscal_period=fiscal_period, n_periods=n_periods)
    legacy_rows = _legacy_rows_by_id(legacy)
    new_rows = _new_rows_by_id(assembled)
    legacy_ids = set(legacy_rows)
    new_visible_ids = {
        row_id for row_id, row in new_rows.items()
        if str(row.get("display_policy") or "").upper() != "HIDE"
    }
    periods = sorted(set(int(y) for y in (legacy.get("periods") or assembled.get("periods") or [])), reverse=True)

    value_diffs = []
    explained_value_diffs = []
    unit_diffs = []
    cagr_diffs = []
    for row_id in sorted(legacy_ids & new_visible_ids):
        legacy_row = legacy_rows[row_id]
        new_row = new_rows[row_id]
        legacy_unit = legacy_row.get("unit") or legacy_row.get("currency") or ""
        new_unit = new_row.get("unit") or new_row.get("unit_type") or ""
        if _unit_family(legacy_unit) != _unit_family(new_unit):
            unit_diffs.append({"line_item_id": row_id, "legacy": legacy_unit, "assembled": new_unit})
        legacy_cagr = _cagr_eligible(legacy_unit, row_id)
        new_cagr = bool(new_row.get("cagr_eligible"))
        if legacy_cagr != new_cagr:
            cagr_diffs.append({"line_item_id": row_id, "legacy": legacy_cagr, "assembled": new_cagr})
        for year in periods:
            legacy_value = _as_decimal((legacy_row.get("values") or {}).get(year))
            new_value = _as_decimal((new_row.get("values") or {}).get(year))
            if legacy_value is None and new_value is None:
                continue
            if legacy_value is None or new_value is None:
                provenance = (new_row.get("provenance_by_year") or {}).get(year) or new_row.get("row_provenance")
                diff = {
                    "line_item_id": row_id,
                    "year": year,
                    "legacy": legacy_value,
                    "assembled": new_value,
                    "assembled_provenance": provenance,
                }
                if legacy_value is None and new_value is not None and provenance in {"derived", "display_only", "residual"}:
                    diff["explanation"] = "assembled value is provenance-tagged and legacy display was blank"
                    explained_value_diffs.append(diff)
                else:
                    value_diffs.append(diff)
                continue
            tolerance = max(abs(legacy_value), Decimal("1")) * Decimal("0.000001")
            if abs(legacy_value - new_value) > tolerance:
                provenance = (new_row.get("provenance_by_year") or {}).get(year) or new_row.get("row_provenance")
                metric_type = (new_row.get("metric_type_by_year") or {}).get(year)
                diff = {
                    "line_item_id": row_id,
                    "year": year,
                    "legacy": legacy_value,
                    "assembled": new_value,
                    "assembled_provenance": provenance,
                    "assembled_metric_type": metric_type,
                }
                if _is_explained_value_diff(provenance, metric_type):
                    diff["explanation"] = "assembler correction is provenance- or metric-tagged"
                    explained_value_diffs.append(diff)
                else:
                    value_diffs.append(diff)

    added_ids = sorted(new_visible_ids - legacy_ids)
    added_details = [_row_review_detail(new_rows[row_id], periods) for row_id in added_ids]
    return {
        "jurisdiction": jurisdiction.upper(),
        "ticker": ticker,
        "statement_type": normalize_statement_type(statement_type),
        "profile_key": assembled.get("profile_key"),
        "periods": assembled.get("periods"),
        "legacy_error": legacy.get("_error"),
        "legacy_row_count": len(legacy_ids),
        "assembled_row_count": len(new_visible_ids),
        "missing_in_assembled": sorted(legacy_ids - new_visible_ids),
        "added_by_assembled": added_ids,
        "added_row_details": added_details,
        "added_with_values_count": sum(1 for row in added_details if row.get("years_with_values")),
        "added_review_counts": _review_counts(new_rows[row_id] for row_id in added_ids),
        "value_diffs": value_diffs[:50],
        "value_diff_count": len(value_diffs),
        "explained_value_diffs": explained_value_diffs[:50],
        "explained_value_diff_count": len(explained_value_diffs),
        "unit_diffs": unit_diffs[:50],
        "cagr_eligibility_diffs": cagr_diffs[:50],
        "warnings": assembled.get("warnings") or [],
        "provenance_counts": _provenance_counts(assembled),
    }


def _row_review_detail(row: dict[str, Any], periods: list[int]) -> dict[str, Any]:
    values = row.get("values") or {}
    years_with_values = [
        int(year) for year in periods
        if values.get(year) is not None or values.get(str(year)) is not None
    ]
    return {
        "line_item_id": row.get("line_item_id"),
        "label": row.get("label"),
        "display_policy": row.get("display_policy"),
        "display_role": row.get("display_role"),
        "display_order": row.get("display_order"),
        "display_parent_id": row.get("display_parent_id"),
        "item_class": row.get("item_class"),
        "unit": row.get("unit") or row.get("unit_type"),
        "row_provenance": row.get("row_provenance"),
        "years_with_values": years_with_values,
        "display_ready": row.get("display_ready"),
        "diagnostic_only": row.get("diagnostic_only"),
        "diagnostic_reasons": row.get("diagnostic_reasons") or [],
        "cagr_eligible": row.get("cagr_eligible"),
        "source_line_item_ids": row.get("source_line_item_ids") or [],
    }


def _is_explained_value_diff(provenance: Any, metric_type: Any) -> bool:
    prov = str(provenance or "").lower()
    mt = str(metric_type or "").upper()
    if prov in {"derived", "display_only", "residual"}:
        return True
    return mt in {"DERIVED_BRIDGE", "DERIVED_FALLBACK"}


def _review_counts(rows: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        policy = str(row.get("display_policy") or "UNKNOWN").upper()
        role = str(row.get("display_role") or "UNKNOWN").upper()
        provenance = str(row.get("row_provenance") or "unknown")
        values = row.get("values") or {}
        value_key = "value:present" if any(value is not None for value in values.values()) else "value:empty"
        ready_key = "display:ready" if row.get("display_ready") else "display:diagnostic"
        for key in (f"policy:{policy}", f"role:{role}", f"provenance:{provenance}", value_key, ready_key):
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _provenance_counts(assembled: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in assembled.get("rows") or []:
        key = str(row.get("row_provenance") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def compare_golden_statements(
    tickers: list[str] | None = None,
    jurisdiction: str | None = None,
    statements: list[str] | None = None,
    fiscal_period: str = "FY",
    n_periods: int = 5,
) -> dict[str, Any]:
    if tickers:
        jur = (jurisdiction or "US").upper()
        cases = [{"jurisdiction": jur, "ticker": ticker, "reason": "explicit"} for ticker in tickers]
    else:
        cases = _default_golden_cases()
        if jurisdiction:
            cases = [case for case in cases if case["jurisdiction"] == jurisdiction.upper()]
    stmt_list = [normalize_statement_type(stmt) for stmt in (statements or _STATEMENTS)]
    results = []
    for case in cases:
        for stmt in stmt_list:
            results.append(_compare_one(case["jurisdiction"], case["ticker"], stmt, fiscal_period, n_periods))
    return {
        "cases": cases,
        "statements": stmt_list,
        "fiscal_period": fiscal_period,
        "n_periods": n_periods,
        "results": results,
        "summary": {
            "case_count": len(cases),
            "comparison_count": len(results),
            "total_value_diffs": sum(item["value_diff_count"] for item in results),
            "total_explained_value_diffs": sum(item["explained_value_diff_count"] for item in results),
            "total_missing_rows": sum(len(item["missing_in_assembled"]) for item in results),
            "total_added_rows": sum(len(item["added_by_assembled"]) for item in results),
            "total_added_with_values": sum(item["added_with_values_count"] for item in results),
            "total_warnings": sum(len(item["warnings"]) for item in results),
        },
    }


def compare_golden_statements_json(**kwargs: Any) -> str:
    return json.dumps(compare_golden_statements(**kwargs), indent=2, sort_keys=True, default=_json_default)


def compare_golden_statements_summary(**kwargs: Any) -> str:
    report = compare_golden_statements(**kwargs)
    return _format_summary(report)


def _format_summary(report: dict[str, Any]) -> str:
    lines = [
        "statement comparison summary",
        (
            f"cases={report['summary']['case_count']} "
            f"comparisons={report['summary']['comparison_count']} "
            f"value_diffs={report['summary']['total_value_diffs']} "
            f"explained={report['summary']['total_explained_value_diffs']} "
            f"missing={report['summary']['total_missing_rows']} "
            f"added={report['summary']['total_added_rows']} "
            f"added_with_values={report['summary']['total_added_with_values']} "
            f"warnings={report['summary']['total_warnings']}"
        ),
        "",
        "JUR  TICKER      STMT                  MISS  ADD  ADDV  DIFF  EXPL  WARN  PROFILE",
        "---  ----------  --------------------  ----  ---  ----  ----  ----  ----  ------------------------------",
    ]
    for item in report["results"]:
        lines.append(
            f"{item['jurisdiction']:<3}  "
            f"{item['ticker']:<10}  "
            f"{item['statement_type']:<20}  "
            f"{len(item['missing_in_assembled']):>4}  "
            f"{len(item['added_by_assembled']):>3}  "
            f"{item['added_with_values_count']:>4}  "
            f"{item['value_diff_count']:>4}  "
            f"{item['explained_value_diff_count']:>4}  "
            f"{len(item['warnings']):>4}  "
            f"{item['profile_key']}"
        )
    return "\n".join(lines)


def compare_golden_statements_review(max_rows: int = 20, **kwargs: Any) -> str:
    report = compare_golden_statements(**kwargs)
    lines = [_format_summary(report), "", "added row review"]
    for item in report["results"]:
        details = item.get("added_row_details") or []
        if not details:
            continue
        counts = " ".join(f"{key}={value}" for key, value in item.get("added_review_counts", {}).items())
        lines.extend([
            "",
            f"{item['jurisdiction']} {item['ticker']} {item['statement_type']} added={len(details)}",
            counts,
            "POLICY       ROLE         PROV          YEARS            LINE ITEM",
            "-----------  -----------  ------------  ---------------  ----------------------------------------",
        ])
        for row in details[:max_rows]:
            years = ",".join(str(year) for year in row.get("years_with_values") or []) or "-"
            line_item = f"{row.get('line_item_id')} | {row.get('label')}"
            lines.append(
                f"{str(row.get('display_policy') or '')[:11]:<11}  "
                f"{str(row.get('display_role') or '')[:11]:<11}  "
                f"{str(row.get('row_provenance') or '')[:12]:<12}  "
                f"{years[:15]:<15}  "
                f"{line_item[:80]}"
            )
        if len(details) > max_rows:
            lines.append(f"... {len(details) - max_rows} more")
    return "\n".join(lines)
