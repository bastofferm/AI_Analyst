from ai_analyst import evidence


def _disable_db(monkeypatch):
    monkeypatch.setattr(evidence, "_filing_section_evidence", lambda *args: [])


def test_stable_evidence_id_is_deterministic():
    first = evidence.stable_evidence_id("news", "US", "MSFT", "2026-06-30")
    second = evidence.stable_evidence_id("news", "US", "MSFT", "2026-06-30")
    other = evidence.stable_evidence_id("news", "US", "AAPL", "2026-06-30")

    assert first == second
    assert first.startswith("ev-")
    assert first != other


def test_truncate_excerpt_compacts_and_limits():
    text = ("alpha   beta\n" * 80).strip()
    compact = evidence.truncate_excerpt(text, limit=80)

    assert len(compact) <= 80
    assert "\n" not in compact
    assert "  " not in compact
    assert compact.endswith("...")


def test_bundle_serializes_and_compacts(monkeypatch):
    _disable_db(monkeypatch)

    bundle = evidence.build_evidence_bundle(
        ticker="MSFT",
        jurisdiction="US",
        entity_id="0000789019",
        mda_text="Management described durable cloud demand and disciplined capital allocation. " * 12,
        packet={
            "modeled_statements": {
                "rows": [
                    {
                        "fiscal_year": 2025,
                        "label": "Revenue",
                        "value": 1000,
                        "source_concept_id": "us-gaap:Revenues",
                    }
                ]
            },
            "recon_flags": {
                "rows": [
                    {
                        "fiscal_year": 2025,
                        "metric_id": "revenue_growth",
                        "trace_quality": "direct",
                        "formula_with_values": "reported value",
                    }
                ]
            },
        },
    )

    dumped = bundle.model_dump(mode="json")
    parsed = evidence.EvidenceBundle.model_validate(dumped)
    compact = evidence.compact_evidence_bundle(parsed, max_cards=2)

    assert parsed.counts["mda"] == 1
    assert parsed.counts["statement"] == 1
    assert compact["cards"][0]["evidence_id"].startswith("ev-")
    assert len(compact["cards"]) == 2


def test_bundle_includes_rich_filing_sections(monkeypatch):
    _disable_db(monkeypatch)

    bundle = evidence.build_evidence_bundle(
        ticker="CB",
        jurisdiction="US",
        entity_id="0000896159",
        mda_text="Management discussed underwriting discipline.",
        packet={},
        rich_filing_sections={
            "sections": [
                {
                    "filing_id": "0000896159-26-000011",
                    "accession_no": "0000896159-26-000011",
                    "form_type": "10-Q",
                    "filing_date": "2026-04-28",
                    "concept_name": "us-gaap:ScheduleOfSegmentReportingInformationBySegmentTextBlock",
                    "section_family": "segment_reporting",
                    "sector_scope": "insurance",
                    "section_title": "Segment Reporting",
                    "summary": "10-Q segment reporting: net premiums and underwriting income by segment.",
                    "excerpt": "Segment table shows premiums, losses, underwriting income, and investment income.",
                    "table_count": 1,
                    "quality_score": 72.0,
                    "source_html_path": "D:/market_data/us_sec/xbrl_html/sample.htm",
                    "text_hash": "abc",
                }
            ]
        },
    )

    assert bundle.counts["rich_filing_section"] == 1
    rich_card = next(card for card in bundle.cards if card.kind == "rich_filing_section")
    assert rich_card.source.source_path.endswith("sample.htm")
    assert "segment_reporting" in rich_card.tags


def test_missing_sources_add_warnings(monkeypatch):
    _disable_db(monkeypatch)

    bundle = evidence.build_evidence_bundle(
        ticker="MSFT",
        jurisdiction="US",
        entity_id="0000789019",
        mda_text="",
        packet={},
    )

    assert "mda source empty" in bundle.warnings
    assert "filing section source missing or empty: sec.filing_section_extract" in bundle.warnings
    assert "statement metadata empty in report packet" in bundle.warnings
    assert "reconciliation evidence empty in report packet" in bundle.warnings


class _FakeDf:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient="records"):
        assert orient == "records"
        return self._rows


def test_filing_section_falls_back_to_xbrl_mda(monkeypatch):
    calls = []

    def fake_read_sql(sql, params):
        calls.append(sql)
        if "filing_section_extract" in sql:
            return _FakeDf([])
        return _FakeDf([
            {
                "filing_id": "0000896159-26-000011",
                "section_id": "item_2",
                "form_type": "10-Q",
                "filed_date": "2026-04-28",
                "section_text": "ITEM 2. Management discussed underwriting discipline and investment income.",
                "char_count": 72,
                "extraction_method": "html_regex",
                "extraction_quality": "clean",
            }
        ])

    monkeypatch.setattr(evidence, "_relation_exists", lambda name: True)
    monkeypatch.setattr(evidence, "read_sql", fake_read_sql)

    bundle = evidence.build_evidence_bundle(
        ticker="CB",
        jurisdiction="US",
        entity_id="0000896159",
        mda_text="Management discussed underwriting discipline.",
        packet={},
    )

    assert bundle.counts["filing_section"] == 1
    assert bundle.cards[1].source.source_path == "sec.fact_mda_sections_us"
    assert "filing section source missing or empty: sec.filing_section_extract" not in bundle.warnings
