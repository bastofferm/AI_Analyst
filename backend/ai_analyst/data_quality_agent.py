from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import services
from ._db import read_sql


QualityLayer = Literal["raw", "standardized", "metrics", "recon", "yahoo_cross_check"]
QualitySeverity = Literal["info", "low", "medium", "high", "blocker"]
FindingStatus = Literal["open", "explained", "resolved"]

_LAYER_ORDER: tuple[QualityLayer, ...] = ("raw", "standardized", "metrics", "recon", "yahoo_cross_check")
_ENTITY_COL = {"US": "cik", "JP": "edinet_code"}
_RAW_TABLE = {"US": "fact_fundamentals_us", "JP": "fact_fundamentals_jp"}
_STD_TABLE = {"US": "fact_fundamentals_std_us", "JP": "fact_fundamentals_std_jp"}
_METRIC_TABLE = {"US": "fact_metrics_us", "JP": "fact_metrics_jp"}
_RECON_TABLE = {"US": "fact_metrics_recon_us", "JP": "fact_metrics_recon_jp"}
_ANNUAL_PERIODS = {"FY", "Annual"}
_SEVERITY_PENALTY = {"info": 0.0, "low": 4.0, "medium": 10.0, "high": 22.0, "blocker": 45.0}
_YAHOO_SEVERITY_TO_FINDING = {
    "ok": None,
    "informational": "info",
    "watch": "medium",
    "material": "high",
    "currency_mismatch": "high",
}


class DataQualityFinding(BaseModel):
    finding_id: str
    layer: QualityLayer
    severity: QualitySeverity
    status: FindingStatus = "open"
    title: str
    message: str
    jurisdiction: str | None = None
    ticker: str | None = None
    entity_id: str | None = None
    fiscal_year: int | None = None
    period_end: str | None = None
    metric_id: str | None = None
    line_item_id: str | None = None
    absolute_delta: float | None = None
    pct_delta: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_action: str | None = None


class MetricReconciliation(BaseModel):
    reconciliation_id: str
    metric_id: str
    label: str | None = None
    fiscal_year: int | None = None
    period_end: str | None = None
    standardized_value: float | None = None
    standardized_currency: str | None = None
    yahoo_value: float | None = None
    yahoo_currency: str | None = None
    absolute_delta: float | None = None
    pct_delta: float | None = None
    severity: str | None = None
    likely_driver: str
    source_relation: str | None = None
    source_line_items: list[str] = Field(default_factory=list)
    source_concept_ids: list[str] = Field(default_factory=list)
    source_filing_ids: list[str] = Field(default_factory=list)
    raw_trace: list[dict[str, Any]] = Field(default_factory=list)
    formula_with_values: str | None = None


class DataQualityAgentReport(BaseModel):
    ticker: str
    jurisdiction: str
    entity_id: str | None = None
    as_of: str
    overall_score: float
    layer_scores: dict[str, float]
    counts: dict[str, int]
    findings: list[DataQualityFinding] = Field(default_factory=list)
    metric_reconciliations: list[MetricReconciliation] = Field(default_factory=list)
    coverage_gaps: dict[str, Any] = Field(default_factory=dict)
    repair_suggestions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def stable_dq_id(*parts: object) -> str:
    joined = "|".join("" if part is None else str(part) for part in parts)
    return "dq-" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def build_data_quality_report(
    *,
    ticker: str,
    jurisdiction: str | None = None,
    entity_id: str | None = None,
    packet: dict[str, Any] | None = None,
    completeness_report: dict[str, Any] | None = None,
    dq_errors: list[str] | None = None,
) -> DataQualityAgentReport:
    """Build a read-only quality report over the committee's existing fact packet."""
    ticker = ticker.upper().strip()
    packet = packet or {}
    company = packet.get("company") if isinstance(packet.get("company"), dict) else {}
    if not company:
        try:
            company = services.company_overview(ticker)
        except Exception:
            company = {}
    jurisdiction = (jurisdiction or company.get("jurisdiction") or "US").upper()
    entity_id = entity_id or _entity_from_company(company, jurisdiction)

    findings: list[DataQualityFinding] = []
    warnings: list[str] = []
    coverage_gaps: dict[str, Any] = {}
    metric_reconciliations: list[MetricReconciliation] = []

    raw_findings, raw_cov, raw_warnings = _raw_layer(ticker, jurisdiction, entity_id)
    findings.extend(raw_findings)
    warnings.extend(raw_warnings)
    coverage_gaps["raw"] = raw_cov

    std_findings, std_cov = _standardized_layer(
        ticker, jurisdiction, entity_id, packet, completeness_report or {}, dq_errors or []
    )
    findings.extend(std_findings)
    coverage_gaps["standardized"] = std_cov

    metric_findings, metric_cov = _metrics_layer(ticker, jurisdiction, entity_id, packet)
    findings.extend(metric_findings)
    coverage_gaps["metrics"] = metric_cov

    recon_rows, recon_warnings = _detailed_recon_rows(ticker, jurisdiction, entity_id)
    warnings.extend(recon_warnings)
    recon_findings, recon_cov = _recon_layer(ticker, jurisdiction, entity_id, packet, recon_rows)
    findings.extend(recon_findings)
    coverage_gaps["recon"] = recon_cov

    yahoo_findings, yahoo_cov, yahoo_reconciliations = _yahoo_layer(
        ticker, jurisdiction, entity_id, packet, recon_rows
    )
    findings.extend(yahoo_findings)
    coverage_gaps["yahoo_cross_check"] = yahoo_cov
    metric_reconciliations.extend(yahoo_reconciliations)

    layer_scores = _score_layers(findings)
    overall = round(sum(layer_scores.values()) / max(1, len(layer_scores)), 1)
    counts = {
        "findings": len(findings),
        "high_or_blocker": sum(1 for finding in findings if finding.severity in {"high", "blocker"}),
        "reconciliations": len(metric_reconciliations),
    }
    for severity in ("blocker", "high", "medium", "low", "info"):
        counts[severity] = sum(1 for finding in findings if finding.severity == severity)

    return DataQualityAgentReport(
        ticker=ticker,
        jurisdiction=jurisdiction,
        entity_id=entity_id,
        as_of=datetime.now(timezone.utc).date().isoformat(),
        overall_score=overall,
        layer_scores=layer_scores,
        counts=counts,
        findings=findings[:80],
        metric_reconciliations=metric_reconciliations[:30],
        coverage_gaps=coverage_gaps,
        repair_suggestions=_repair_suggestions(findings, jurisdiction, entity_id),
        warnings=list(dict.fromkeys(warnings))[:20],
    )


def compact_data_quality_report(
    report: DataQualityAgentReport | dict[str, Any] | None,
    *,
    max_findings: int = 8,
    max_reconciliations: int = 5,
) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, dict):
        try:
            parsed = DataQualityAgentReport.model_validate(report)
        except Exception:
            return {
                "overall_score": report.get("overall_score"),
                "layer_scores": report.get("layer_scores") or {},
                "warnings": report.get("warnings") or [],
            }
    else:
        parsed = report
    return {
        "ticker": parsed.ticker,
        "jurisdiction": parsed.jurisdiction,
        "as_of": parsed.as_of,
        "overall_score": parsed.overall_score,
        "layer_scores": parsed.layer_scores,
        "counts": parsed.counts,
        "findings": [
            {
                "finding_id": finding.finding_id,
                "layer": finding.layer,
                "severity": finding.severity,
                "title": finding.title,
                "message": _truncate(finding.message, 240),
                "metric_id": finding.metric_id,
                "line_item_id": finding.line_item_id,
                "fiscal_year": finding.fiscal_year,
                "pct_delta": finding.pct_delta,
                "suggested_action": finding.suggested_action,
            }
            for finding in parsed.findings[:max_findings]
        ],
        "metric_reconciliations": [
            {
                "reconciliation_id": item.reconciliation_id,
                "metric_id": item.metric_id,
                "fiscal_year": item.fiscal_year,
                "severity": item.severity,
                "pct_delta": item.pct_delta,
                "likely_driver": item.likely_driver,
                "source_relation": _truncate(item.source_relation, 220),
            }
            for item in parsed.metric_reconciliations[:max_reconciliations]
        ],
        "repair_suggestions": parsed.repair_suggestions[:5],
        "warnings": parsed.warnings[:5],
    }


def _entity_from_company(company: dict[str, Any], jurisdiction: str) -> str | None:
    if jurisdiction == "US":
        cik = company.get("cik")
        return str(cik).zfill(10) if cik not in (None, "") else None
    code = company.get("edinet_code")
    return str(code) if code not in (None, "") else None


def _sector_scope(packet: dict[str, Any]) -> str:
    modeled = packet.get("modeled_statements") if isinstance(packet.get("modeled_statements"), dict) else {}
    if modeled.get("sector_scope"):
        return str(modeled.get("sector_scope"))
    company = packet.get("company") if isinstance(packet.get("company"), dict) else {}
    return services.sector_scope_from_company(company)


def _latest_modeled_year(packet: dict[str, Any]) -> int | None:
    modeled = packet.get("modeled_statements") if isinstance(packet.get("modeled_statements"), dict) else {}
    rows = [row for row in ((modeled or {}).get("rows") or []) if isinstance(row, dict)]
    years = [_int(row.get("fiscal_year")) for row in rows]
    years = [year for year in years if year is not None]
    return max(years) if years else None


def _raw_layer(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
) -> tuple[list[DataQualityFinding], dict[str, Any], list[str]]:
    layer: QualityLayer = "raw"
    findings: list[DataQualityFinding] = []
    warnings: list[str] = []
    coverage: dict[str, Any] = {"entity_id": entity_id, "source_filings": 0, "parsed_filings": 0}
    if not entity_id:
        return [
            _finding(layer, "high", "Missing entity identifier", "No CIK/EDINET code is available for raw filing checks.",
                     ticker=ticker, jurisdiction=jurisdiction, suggested_action="Refresh company master and ticker mapping.")
        ], coverage, warnings

    try:
        filings = read_sql(
            """
            SELECT filing_type, filed_date, period_end,
                   EXTRACT(YEAR FROM period_end)::int AS fiscal_year,
                   COALESCE(parsed, FALSE) AS parsed
            FROM source_filing_state
            WHERE jurisdiction = %(jurisdiction)s AND entity_id = %(entity_id)s
            ORDER BY period_end DESC NULLS LAST, filed_date DESC NULLS LAST
            LIMIT 40
            """,
            {"jurisdiction": jurisdiction, "entity_id": entity_id},
        )
        filing_rows = _records(filings)
    except Exception as exc:
        filing_rows = []
        warnings.append(f"source_filing_state unavailable for DQ raw layer: {exc.__class__.__name__}")
    parsed_rows = [row for row in filing_rows if bool(row.get("parsed"))]
    coverage.update(
        {
            "source_filings": len(filing_rows),
            "parsed_filings": len(parsed_rows),
            "filing_years": sorted({int(row["fiscal_year"]) for row in filing_rows if row.get("fiscal_year")}, reverse=True),
            "parsed_years": sorted({int(row["fiscal_year"]) for row in parsed_rows if row.get("fiscal_year")}, reverse=True),
            "latest_period_end": str(filing_rows[0].get("period_end"))[:10] if filing_rows else None,
        }
    )
    if not filing_rows:
        findings.append(
            _finding(layer, "high", "No source filings tracked",
                     "No source_filing_state rows were found for the entity.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action=_run_suggestion(jurisdiction, entity_id))
        )
    elif not parsed_rows:
        findings.append(
            _finding(layer, "high", "Tracked filings are not parsed",
                     "The filing index has rows, but none are marked parsed for the entity.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Run extract and parse for the entity, then standardize/metrics/recon.")
        )

    raw_count = _entity_count(_RAW_TABLE[jurisdiction], _ENTITY_COL[jurisdiction], entity_id)
    coverage["raw_fact_rows"] = raw_count
    if raw_count == 0:
        findings.append(
            _finding(layer, "high", "No raw fact rows",
                     f"{_RAW_TABLE[jurisdiction]} has no rows for the entity.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Run source download/extract/parse for the entity.")
        )
    return findings, coverage, warnings


def _standardized_layer(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    packet: dict[str, Any],
    completeness_report: dict[str, Any],
    dq_errors: list[str],
) -> tuple[list[DataQualityFinding], dict[str, Any]]:
    layer: QualityLayer = "standardized"
    modeled = packet.get("modeled_statements") if isinstance(packet.get("modeled_statements"), dict) else {}
    rows = [row for row in ((modeled or {}).get("rows") or []) if isinstance(row, dict)]
    sector_scope = _sector_scope(packet)
    expected_items = services.line_items_for_sector(sector_scope)
    findings: list[DataQualityFinding] = []
    years = sorted({_int(row.get("fiscal_year")) for row in rows if _int(row.get("fiscal_year")) is not None}, reverse=True)
    latest_year = years[0] if years else None
    coverage = {
        "standardized_rows_in_packet": len(rows),
        "standardized_years": years,
        "latest_year": latest_year,
        "sector_scope": sector_scope,
        "expected_line_items": list(expected_items),
        "missing_target_years": completeness_report.get("missing_fundamental_years") or [],
    }
    if not rows:
        findings.append(
            _finding(layer, "blocker", "No standardized facts in packet",
                     "The committee packet has no standardized statement rows.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Run standardize, metrics, and recon for this entity.")
        )
        return findings, coverage

    latest = [row for row in rows if _int(row.get("fiscal_year")) == latest_year]
    present = {str(row.get("line_item_id")) for row in latest if row.get("line_item_id")}
    missing_core = [item for item in expected_items if item not in present]
    coverage["missing_core_latest_year"] = missing_core
    if missing_core:
        severity: QualitySeverity = "high" if len(missing_core) >= 5 else "medium"
        findings.append(
            _finding(layer, severity, "Missing standardized core line items",
                     "Latest FY packet is missing: " + ", ".join(missing_core[:10]),
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     fiscal_year=latest_year, suggested_action="Review raw-to-standard mapping coverage for the latest filing.")
        )

    identity_errors = [err for err in dq_errors if "CORE identity break" in str(err)]
    for err in identity_errors[:10]:
        fy = _extract_fy(err)
        findings.append(
            _finding(layer, "high", "Accounting identity break", str(err),
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     fiscal_year=fy, suggested_action="Inspect standardized line-item mapping and XBRL rollup signs.")
        )
    rollup_warnings = [err for err in dq_errors if "rollup warning" in str(err)]
    if rollup_warnings:
        findings.append(
            _finding(layer, "medium", "Rollup warnings present",
                     f"{len(rollup_warnings)} non-core rollup warning(s) were reported by the gate.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Inspect non-core statement hierarchy warnings before relying on secondary line items.")
        )
    return findings, coverage


def _metrics_layer(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    packet: dict[str, Any],
) -> tuple[list[DataQualityFinding], dict[str, Any]]:
    layer: QualityLayer = "metrics"
    rows = [row for row in ((packet.get("metrics") or {}).get("rows") or []) if isinstance(row, dict)]
    sector_scope = _sector_scope(packet)
    expected_metrics = services.peer_metric_ids_for_sector(sector_scope)
    findings: list[DataQualityFinding] = []
    years = sorted({_int(row.get("fiscal_year")) for row in rows if _int(row.get("fiscal_year")) is not None}, reverse=True)
    latest_year = _latest_modeled_year(packet) or (years[0] if years else None)
    latest_ids = {str(row.get("metric_id")) for row in rows if _int(row.get("fiscal_year")) == latest_year and row.get("metric_id")}
    missing = [metric for metric in expected_metrics if metric not in latest_ids]
    coverage = {
        "metric_rows_in_packet": len(rows),
        "metric_years": years,
        "latest_year": latest_year,
        "sector_scope": sector_scope,
        "expected_derived_metrics": list(expected_metrics),
        "missing_derived_metrics_latest_year": missing,
    }
    if not rows:
        findings.append(
            _finding(layer, "high", "No derived metric rows",
                     f"{_METRIC_TABLE[jurisdiction]} appears empty for the committee packet.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Run metrics and recon for this entity.")
        )
    elif missing:
        findings.append(
            _finding(layer, "medium", "Derived metric coverage gap",
                     "Latest FY metrics are missing: " + ", ".join(missing),
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     fiscal_year=latest_year,
                     suggested_action="Recompute metrics after confirming sector-appropriate standardized inputs and formulas.")
        )
    return findings, coverage


def _detailed_recon_rows(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not entity_id:
        return [], warnings
    table = _RECON_TABLE[jurisdiction]
    try:
        df = read_sql(
            f"""
            SELECT fiscal_year, fiscal_period, period_end, metric_id, formula,
                   formula_with_values, value::double precision AS value, currency,
                   input_values, source_line_items, source_concept_ids,
                   source_filing_ids, raw_trace, trace_quality
            FROM {table}
            WHERE UPPER(ticker) = UPPER(%(ticker)s)
            ORDER BY fiscal_year DESC, fiscal_period, metric_id
            LIMIT 160
            """,
            {"ticker": ticker},
        )
    except Exception as exc:
        warnings.append(f"Detailed recon rows unavailable: {exc.__class__.__name__}")
        return [], warnings
    rows = _records(df)
    for row in rows:
        row["input_values"] = _jsonish(row.get("input_values"))
        row["raw_trace"] = _jsonish(row.get("raw_trace"))
        row["source_line_items"] = _listish(row.get("source_line_items"))
        row["source_concept_ids"] = _listish(row.get("source_concept_ids"))
        row["source_filing_ids"] = _listish(row.get("source_filing_ids"))
    return rows, warnings


def _recon_layer(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    packet: dict[str, Any],
    detailed_rows: list[dict[str, Any]],
) -> tuple[list[DataQualityFinding], dict[str, Any]]:
    layer: QualityLayer = "recon"
    packet_rows = [row for row in ((packet.get("recon_flags") or {}).get("rows") or []) if isinstance(row, dict)]
    rows = detailed_rows or packet_rows
    findings: list[DataQualityFinding] = []
    years = sorted({_int(row.get("fiscal_year")) for row in rows if _int(row.get("fiscal_year")) is not None}, reverse=True)
    bad = [
        row for row in rows
        if str(row.get("trace_quality") or "").strip().lower() in {"broken", "bad", "fail", "failed", "red"}
    ]
    missing_trace = [
        row for row in detailed_rows
        if not row.get("raw_trace") and not row.get("source_line_items") and row.get("formula")
    ]
    coverage = {
        "recon_rows": len(rows),
        "recon_years": years,
        "bad_trace_rows": len(bad),
        "missing_trace_rows": len(missing_trace),
    }
    if not rows:
        findings.append(
            _finding(layer, "medium", "No recon trace rows",
                     f"{_RECON_TABLE[jurisdiction]} has no visible rows in the committee packet.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Run recon after metrics so formulas have traceable input rows.")
        )
    for row in bad[:12]:
        findings.append(
            _finding(layer, "high", "Broken metric trace",
                     f"{row.get('metric_id')} FY{row.get('fiscal_year')} trace_quality={row.get('trace_quality')}.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     fiscal_year=_int(row.get("fiscal_year")), metric_id=_clean(row.get("metric_id")),
                     suggested_action="Inspect formula inputs, source line items and raw_trace for this metric.")
        )
    if missing_trace and not bad:
        findings.append(
            _finding(layer, "low", "Recon formulas lack full trace detail",
                     f"{len(missing_trace)} recon row(s) have formulas but no raw_trace/source_line_items.",
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Rebuild recon with traceability migrations applied.")
        )
    return findings, coverage


def _yahoo_layer(
    ticker: str,
    jurisdiction: str,
    entity_id: str | None,
    packet: dict[str, Any],
    recon_rows: list[dict[str, Any]],
) -> tuple[list[DataQualityFinding], dict[str, Any], list[MetricReconciliation]]:
    layer: QualityLayer = "yahoo_cross_check"
    check = packet.get("yahoo_cross_check") if isinstance(packet, dict) else {}
    findings: list[DataQualityFinding] = []
    reconciliations: list[MetricReconciliation] = []
    rows = [row for row in ((check or {}).get("rows") or []) if isinstance(row, dict)]
    coverage = {
        "available": bool((check or {}).get("available")),
        "snapshot_date": (check or {}).get("snapshot_date"),
        "compared_line_items": len(rows),
        "material_count": (check or {}).get("material_count"),
        "watch_count": (check or {}).get("watch_count"),
    }
    if not check or not check.get("available"):
        findings.append(
            _finding(layer, "low", "Yahoo cross-check unavailable",
                     str((check or {}).get("note") or "No overlapping Yahoo annual snapshot rows were available."),
                     ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
                     suggested_action="Refresh Yahoo fundamental snapshots and rerun the cross-check.")
        )
        return findings, coverage, reconciliations

    std_rows = [row for row in ((packet.get("modeled_statements") or {}).get("rows") or []) if isinstance(row, dict)]
    std_by_item = _latest_by_line_item(std_rows)
    ranked = sorted(rows, key=lambda row: _yahoo_rank(str(row.get("severity") or "")))
    for row in ranked[:18]:
        severity = str(row.get("severity") or "informational")
        finding_severity = _YAHOO_SEVERITY_TO_FINDING.get(severity)
        line_item = _clean(row.get("line_item_id"))
        if finding_severity:
            findings.append(
                _finding(
                    layer,
                    finding_severity,  # type: ignore[arg-type]
                    "Yahoo vs filing discrepancy",
                    _yahoo_message(row),
                    ticker=ticker,
                    jurisdiction=jurisdiction,
                    entity_id=entity_id,
                    fiscal_year=_int(row.get("standardized_fiscal_year")),
                    line_item_id=line_item,
                    absolute_delta=_float(row.get("absolute_delta")),
                    pct_delta=_float(row.get("pct_delta")),
                    suggested_action="Trace the standardized filing source and compare the Yahoo definition/period before using the metric."
                )
            )
        if finding_severity in {"medium", "high"} or line_item in {
            "earnings_before_interest_taxes",
            "earnings_before_interest_taxes_depreciation_amortization",
        }:
            trace = _trace_for_line_item(line_item, std_by_item.get(line_item), recon_rows)
            reconciliations.append(_metric_reconciliation(row, trace))
    return findings, coverage, reconciliations


def _metric_reconciliation(row: dict[str, Any], trace: dict[str, Any]) -> MetricReconciliation:
    line_item = _clean(row.get("line_item_id")) or "metric"
    driver = _likely_driver(row, trace)
    return MetricReconciliation(
        reconciliation_id=stable_dq_id("recon", line_item, row.get("standardized_fiscal_year"), row.get("yahoo_metric_id")),
        metric_id=line_item,
        label=_clean(row.get("label")) or None,
        fiscal_year=_int(row.get("standardized_fiscal_year")),
        period_end=_date_text(row.get("standardized_period_end")),
        standardized_value=_float(row.get("standardized_value")),
        standardized_currency=_clean(row.get("standardized_currency")) or None,
        yahoo_value=_float(row.get("yahoo_value")),
        yahoo_currency=_clean(row.get("yahoo_currency")) or None,
        absolute_delta=_float(row.get("absolute_delta")),
        pct_delta=_float(row.get("pct_delta")),
        severity=_clean(row.get("severity")) or None,
        likely_driver=driver,
        source_relation=trace.get("source_relation"),
        source_line_items=trace.get("source_line_items") or [],
        source_concept_ids=trace.get("source_concept_ids") or [],
        source_filing_ids=trace.get("source_filing_ids") or [],
        raw_trace=trace.get("raw_trace") or [],
        formula_with_values=trace.get("formula_with_values"),
    )


def _trace_for_line_item(
    line_item: str,
    std_row: dict[str, Any] | None,
    recon_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    tokens = {_token(line_item)}
    if line_item == "earnings_before_interest_taxes_depreciation_amortization":
        tokens.update({"ebitda", "depreciationamortization"})
    elif line_item == "earnings_before_interest_taxes":
        tokens.update({"ebit", "operatingincome", "operatingprofit"})

    best: dict[str, Any] | None = None
    for row in recon_rows:
        haystack = " ".join(
            [
                str(row.get("metric_id") or ""),
                str(row.get("formula") or ""),
                str(row.get("formula_with_values") or ""),
                " ".join(_listish(row.get("source_line_items"))),
            ]
        )
        hay = _token(haystack)
        if any(token and token in hay for token in tokens):
            best = row
            break
    source_concepts = _listish((best or {}).get("source_concept_ids"))
    source_line_items = _listish((best or {}).get("source_line_items"))
    source_filing_ids = _listish((best or {}).get("source_filing_ids"))
    raw_trace = _jsonish((best or {}).get("raw_trace"))
    if std_row:
        if std_row.get("source_concept_id"):
            source_concepts = list(dict.fromkeys([*source_concepts, str(std_row.get("source_concept_id"))]))
        if std_row.get("filing_id"):
            source_filing_ids = list(dict.fromkeys([*source_filing_ids, str(std_row.get("filing_id"))]))
    relation_bits = []
    if best and best.get("formula_with_values"):
        relation_bits.append(_clean(best.get("formula_with_values")))
    if std_row and std_row.get("concept_path"):
        relation_bits.append(f"standardized from {std_row.get('source_concept_id') or line_item}; path {std_row.get('concept_path')}")
    return {
        "source_relation": _truncate("; ".join(relation_bits), 500) or None,
        "source_line_items": source_line_items,
        "source_concept_ids": source_concepts,
        "source_filing_ids": source_filing_ids,
        "raw_trace": raw_trace if isinstance(raw_trace, list) else [],
        "formula_with_values": _clean((best or {}).get("formula_with_values")) or None,
    }


def _likely_driver(row: dict[str, Any], trace: dict[str, Any]) -> str:
    if row.get("currency_mismatch"):
        return "currency_or_unit_mismatch"
    std_year = _int(row.get("standardized_fiscal_year"))
    yahoo_year = _int(row.get("yahoo_fiscal_year"))
    if std_year is not None and yahoo_year is not None and std_year != yahoo_year:
        return "period_mismatch"
    line_item = _clean(row.get("line_item_id"))
    if line_item == "capital_expenditures":
        return "sign_convention_or_absolute_value_basis"
    pct = abs(_float(row.get("pct_delta")) or 0.0)
    source_relation = _clean(trace.get("source_relation"))
    if not source_relation:
        return "filing_mapping_gap_or_missing_trace"
    if line_item in {"earnings_before_interest_taxes_depreciation_amortization", "earnings_before_interest_taxes"}:
        return "definition_difference_or_component_scope"
    if pct >= 25:
        return "definition_difference_or_mapping_gap"
    return "minor_snapshot_or_rounding_difference"


def _score_layers(findings: list[DataQualityFinding]) -> dict[str, float]:
    out: dict[str, float] = {}
    for layer in _LAYER_ORDER:
        penalty = sum(_SEVERITY_PENALTY[f.severity] for f in findings if f.layer == layer)
        out[layer] = round(max(0.0, 100.0 - penalty), 1)
    return out


def _repair_suggestions(findings: list[DataQualityFinding], jurisdiction: str, entity_id: str | None) -> list[str]:
    suggestions = [finding.suggested_action for finding in findings if finding.suggested_action]
    if any(f.layer == "raw" and f.severity in {"high", "blocker"} for f in findings):
        suggestions.append(_run_suggestion(jurisdiction, entity_id))
    if any(f.layer in {"standardized", "metrics", "recon"} and f.severity in {"high", "blocker"} for f in findings):
        suggestions.append("Run targeted standardize -> metrics -> recon for the entity; avoid global reset.")
    if any(f.layer == "yahoo_cross_check" and f.severity in {"medium", "high"} for f in findings):
        suggestions.append("Refresh Yahoo fundamentals, then compare the filing trace before changing canonical metrics.")
    return list(dict.fromkeys(suggestions))[:8]


def _run_suggestion(jurisdiction: str, entity_id: str | None) -> str:
    entity = f" --entity {entity_id}" if entity_id else ""
    if jurisdiction == "US":
        return f"Run fundamentals.run US with download=true, lookback_days=120{entity}."
    return f"Run fundamentals.run JP with download=true{entity}."


def _entity_count(table: str, entity_col: str, entity_id: str) -> int | None:
    try:
        df = read_sql(
            f"SELECT COUNT(*)::bigint AS rows FROM {table} WHERE {entity_col} = %(entity_id)s",
            {"entity_id": entity_id},
        )
    except Exception:
        return None
    rows = _records(df)
    if not rows:
        return None
    return int(rows[0].get("rows") or 0)


def _latest_by_line_item(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        line_item = _clean(row.get("line_item_id"))
        if not line_item:
            continue
        current = out.get(line_item)
        if current is None or (_int(row.get("fiscal_year")) or -1) > (_int(current.get("fiscal_year")) or -1):
            out[line_item] = row
    return out


def _finding(
    layer: QualityLayer,
    severity: QualitySeverity,
    title: str,
    message: str,
    *,
    ticker: str | None = None,
    jurisdiction: str | None = None,
    entity_id: str | None = None,
    fiscal_year: int | None = None,
    period_end: str | None = None,
    metric_id: str | None = None,
    line_item_id: str | None = None,
    absolute_delta: float | None = None,
    pct_delta: float | None = None,
    suggested_action: str | None = None,
) -> DataQualityFinding:
    return DataQualityFinding(
        finding_id=stable_dq_id(layer, severity, ticker, entity_id, fiscal_year, metric_id, line_item_id, title, message[:80]),
        layer=layer,
        severity=severity,
        title=title,
        message=_truncate(message, 520),
        ticker=ticker,
        jurisdiction=jurisdiction,
        entity_id=entity_id,
        fiscal_year=fiscal_year,
        period_end=period_end,
        metric_id=metric_id,
        line_item_id=line_item_id,
        absolute_delta=absolute_delta,
        pct_delta=pct_delta,
        suggested_action=suggested_action,
    )


def _yahoo_message(row: dict[str, Any]) -> str:
    pct = _float(row.get("pct_delta"))
    pct_text = f"{pct:+.1f}%" if pct is not None and math.isfinite(pct) else "n/a"
    return (
        f"{row.get('line_item_id')} FY{row.get('standardized_fiscal_year')}: "
        f"standardized {row.get('standardized_value')} {row.get('standardized_currency')} vs "
        f"Yahoo {row.get('yahoo_value')} {row.get('yahoo_currency')} ({pct_text}, {row.get('severity')})."
    )


def _yahoo_rank(severity: str) -> tuple[int, str]:
    return (
        {"material": 0, "currency_mismatch": 1, "watch": 2, "informational": 3, "ok": 4}.get(severity, 9),
        severity,
    )


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return df.to_dict(orient="records")
    if isinstance(df, list):
        return [row for row in df if isinstance(row, dict)]
    return []


def _jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    text = str(value)
    try:
        return json.loads(text)
    except Exception:
        return text


def _listish(value: Any) -> list[str]:
    value = _jsonish(value)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, set):
        return [str(item) for item in value if item not in (None, "")]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    return [part.strip().strip('"') for part in text.split(",") if part.strip()]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _truncate(value: Any, limit: int = 300) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if hasattr(value, "item"):
            value = value.item()
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _extract_fy(text: str) -> int | None:
    match = re.search(r"FY\s*(\d{4})", str(text))
    return int(match.group(1)) if match else None


def _date_text(value: Any) -> str | None:
    text = _clean(value)
    return text[:10] if text else None
