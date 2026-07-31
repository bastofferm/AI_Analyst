from ai_analyst import data_quality_agent
from ai_analyst import services


def _full_core_rows():
    rows = []
    for idx, line_item in enumerate(services.CORE_LINE_ITEMS):
        rows.append(
            {
                "fiscal_year": 2025,
                "period_end": "2025-06-30",
                "line_item_id": line_item,
                "label": line_item.replace("_", " ").title(),
                "value": 1000.0 + idx,
                "currency": "USD",
                "source_concept_id": f"us-gaap:{line_item}",
                "concept_path": f"Statement > {line_item}",
                "filing_id": "0000789019-25-000001",
            }
        )
    return rows


def _full_insurance_rows():
    rows = []
    for idx, line_item in enumerate(services.INSURANCE_LINE_ITEMS):
        rows.append(
            {
                "fiscal_year": 2025,
                "period_end": "2025-12-31",
                "line_item_id": line_item,
                "label": line_item.replace("_", " ").title(),
                "value": 1000.0 + idx,
                "currency": "USD",
            }
        )
    return rows


def test_stable_dq_id_is_deterministic():
    first = data_quality_agent.stable_dq_id("MSFT", "raw", 2025)
    second = data_quality_agent.stable_dq_id("MSFT", "raw", 2025)
    other = data_quality_agent.stable_dq_id("AAPL", "raw", 2025)

    assert first == second
    assert first.startswith("dq-")
    assert first != other


def test_report_builds_yahoo_reconciliation_with_trace(monkeypatch):
    monkeypatch.setattr(
        data_quality_agent,
        "_raw_layer",
        lambda *args: ([], {"source_filings": 1, "parsed_filings": 1, "raw_fact_rows": 100}, []),
    )
    monkeypatch.setattr(
        data_quality_agent,
        "_detailed_recon_rows",
        lambda *args: (
            [
                {
                    "fiscal_year": 2025,
                    "metric_id": "earnings_before_interest_taxes_depreciation_amortization_margin",
                    "formula": "ebitda / revenue",
                    "formula_with_values": "EBITDA 1002 / revenue 1000",
                    "source_line_items": ["earnings_before_interest_taxes_depreciation_amortization", "revenue"],
                    "source_concept_ids": ["us-gaap:NormalizedEBITDA"],
                    "source_filing_ids": ["0000789019-25-000001"],
                    "raw_trace": [{"source_concept_id": "us-gaap:NormalizedEBITDA"}],
                    "trace_quality": "direct",
                }
            ],
            [],
        ),
    )

    packet = {
        "company": {"jurisdiction": "US", "cik": "0000789019", "ticker": "MSFT"},
        "modeled_statements": {"rows": _full_core_rows()},
        "metrics": {
            "rows": [
                {"fiscal_year": 2025, "metric_id": metric, "value": 1.0}
                for metric in services.PEER_METRIC_IDS
            ]
        },
        "recon_flags": {"rows": []},
        "yahoo_cross_check": {
            "available": True,
            "snapshot_date": "2026-07-07",
            "material_count": 1,
            "watch_count": 0,
            "rows": [
                {
                    "line_item_id": "earnings_before_interest_taxes_depreciation_amortization",
                    "label": "EBITDA",
                    "standardized_fiscal_year": 2025,
                    "standardized_period_end": "2025-06-30",
                    "standardized_value": 1002.0,
                    "standardized_currency": "USD",
                    "yahoo_fiscal_year": 2025,
                    "yahoo_value": 1500.0,
                    "yahoo_currency": "USD",
                    "yahoo_metric_id": "ebitda",
                    "absolute_delta": 498.0,
                    "pct_delta": 49.7,
                    "severity": "material",
                    "currency_mismatch": False,
                }
            ],
        },
    }

    report = data_quality_agent.build_data_quality_report(
        ticker="MSFT",
        jurisdiction="US",
        entity_id="0000789019",
        packet=packet,
    )

    assert report.counts["high"] == 1
    assert report.layer_scores["yahoo_cross_check"] < 100
    assert report.metric_reconciliations
    recon = report.metric_reconciliations[0]
    assert recon.metric_id == "earnings_before_interest_taxes_depreciation_amortization"
    assert recon.likely_driver == "definition_difference_or_component_scope"
    assert "us-gaap:NormalizedEBITDA" in recon.source_concept_ids


def test_insurance_packet_uses_sector_expected_items_and_modeled_metric_year(monkeypatch):
    monkeypatch.setattr(
        data_quality_agent,
        "_raw_layer",
        lambda *args: ([], {"source_filings": 1, "parsed_filings": 1, "raw_fact_rows": 100}, []),
    )
    monkeypatch.setattr(data_quality_agent, "_detailed_recon_rows", lambda *args: ([], []))

    packet = {
        "company": {
            "jurisdiction": "US",
            "cik": "0000896159",
            "ticker": "CB",
            "mapping_sector": "non_bank_financial",
            "gics_industry_group_name": "Insurance",
        },
        "modeled_statements": {"rows": _full_insurance_rows()},
        "metrics": {
            "rows": [
                {"fiscal_year": 2026, "metric_id": "market_capitalization", "value": 1.0},
                *[
                    {"fiscal_year": 2025, "metric_id": metric, "value": 1.0}
                    for metric in services.INSURANCE_PEER_METRIC_IDS
                ],
            ]
        },
        "recon_flags": {"rows": []},
        "yahoo_cross_check": {"available": False, "note": "not loaded", "rows": []},
    }

    report = data_quality_agent.build_data_quality_report(
        ticker="CB",
        jurisdiction="US",
        entity_id="0000896159",
        packet=packet,
    )

    standardized = report.coverage_gaps["standardized"]
    metrics = report.coverage_gaps["metrics"]
    assert standardized["sector_scope"] == "insurance"
    assert "revenue" not in standardized["expected_line_items"]
    assert standardized["missing_core_latest_year"] == []
    assert metrics["latest_year"] == 2025
    assert metrics["missing_derived_metrics_latest_year"] == []


def test_compact_report_caps_findings():
    report = data_quality_agent.DataQualityAgentReport(
        ticker="MSFT",
        jurisdiction="US",
        as_of="2026-07-07",
        overall_score=80.0,
        layer_scores={"raw": 100.0},
        counts={"findings": 20},
        findings=[
            data_quality_agent.DataQualityFinding(
                finding_id=f"dq-{i:016x}",
                layer="raw",
                severity="low",
                title="Finding",
                message="message",
            )
            for i in range(20)
        ],
    )

    compact = data_quality_agent.compact_data_quality_report(report, max_findings=3)

    assert len(compact["findings"]) == 3
    assert compact["findings"][0]["finding_id"].startswith("dq-")
