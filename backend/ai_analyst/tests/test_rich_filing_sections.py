from ai_analyst import rich_filing_sections as rich


def _segment_html() -> str:
    return """
    <html><body>
      <ix:nonNumeric name="us-gaap:ScheduleOfSegmentReportingInformationBySegmentTextBlock" id="seg">
        <div>Segment reporting</div>
        <p>Management reports revenue and operating income by reportable segment for the current period.</p>
        <table>
          <tr><th>Segment</th><th>Revenue</th><th>Operating Income</th></tr>
          <tr><td>Cloud</td><td>1200</td><td>420</td></tr>
          <tr><td>Devices</td><td>500</td><td>80</td></tr>
        </table>
      </ix:nonNumeric>
      <ix:nonNumeric name="us-gaap:SummaryOfSignificantAccountingPoliciesTextBlock" id="policy">
        <p>This policy text is deliberately generic and should not outrank operating disclosures.</p>
      </ix:nonNumeric>
    </body></html>
    """


def _filing():
    return {
        "cik": "0000001234",
        "ticker": "TST",
        "accession_no": "0000001234-26-000001",
        "filing_id": "0000001234-26-000001",
        "form_type": "10-Q",
        "filing_date": "2026-05-01",
        "fiscal_year": 2026,
        "fiscal_period": "Q1",
        "filing_rank": 0,
    }


def test_extract_rich_sections_classifies_segment_table():
    sections = rich.extract_rich_sections_from_html(
        _segment_html(),
        _filing(),
        {
            "ticker": "TST",
            "mapping_sector": "corp",
            "gics_sector_name": "Information Technology",
        },
    )

    assert sections
    top = sections[0]
    assert top["section_family"] == "segment_reporting"
    assert top["table_count"] == 1
    assert top["metrics_preview_jsonb"]["sample_rows"]
    assert top["quality_score"] >= 60
    assert "ScheduleOfSegmentReportingInformationBySegmentTextBlock" in top["concept_name"]


def test_extract_rich_sections_classifies_insurance_specific_textblock():
    html = """
    <html><body>
      <ix:nonNumeric name="us-gaap:ReinsuranceRecoverablesTextBlock" id="re">
        <div>Reinsurance recoverables</div>
        <p>The company analyzes recoverables, reserves, premiums, losses, and allowance activity.</p>
        <table>
          <tr><th>Counterparty group</th><th>Recoverable</th><th>Allowance</th></tr>
          <tr><td>A rated reinsurers</td><td>2000</td><td>40</td></tr>
        </table>
      </ix:nonNumeric>
    </body></html>
    """

    sections = rich.extract_rich_sections_from_html(
        html,
        _filing(),
        {
            "ticker": "CB",
            "mapping_sector": "non_bank_financial",
            "gics_industry_group_name": "Insurance",
        },
    )

    assert sections
    assert sections[0]["section_family"] == "industry_specific"
    assert sections[0]["sector_scope"] == "insurance"


def test_fetch_rich_sections_uses_local_html_fallback_and_persists(tmp_path, monkeypatch):
    filing = _filing()
    html_path = tmp_path / "CIK0000001234_0000001234-26-000001.htm"
    html_path.write_text(_segment_html(), encoding="utf-8")
    persisted = []

    monkeypatch.setattr(
        rich,
        "_company_profile",
        lambda ticker: {
            "found": True,
            "ticker": ticker,
            "jurisdiction": "US",
            "cik": "0000001234",
            "mapping_sector": "corp",
            "gics_sector_name": "Information Technology",
            "sector_scope": "corp",
        },
    )
    monkeypatch.setattr(rich, "_target_filings", lambda cik, years, limit_filings: [filing])
    monkeypatch.setattr(rich, "_read_cached_sections", lambda cik, filing_ids: [])
    monkeypatch.setattr(rich, "_persist_sections", lambda sections: persisted.extend(sections) or len(sections))
    monkeypatch.setattr(rich, "html_dir_from_env", lambda: tmp_path)

    packet = rich.fetch_rich_filing_sections("TST", years=[2026])

    assert packet["available"] is True
    assert packet["sections"][0]["section_family"] == "segment_reporting"
    assert packet["compact"]["sections"][0]["family"] == "segment_reporting"
    assert persisted
