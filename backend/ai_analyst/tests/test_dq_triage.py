"""Tests for the per-ticker DQ + mapping-check agent (dq_triage + committee node).

DB access is always mocked — these tests never touch Postgres.
"""
from __future__ import annotations

import contextlib

import pandas as pd
import pytest

from ai_analyst import dq_triage
from ai_analyst.committee import nodes


# --------------------------------------------------------------- proposal rows

def _mapping_proposal(**over):
    base = {
        "kind": "mapping_add",
        "concept_id": "us-gaap:AvailableForSaleSecurities",
        "target_variable": "investment_securities",
        "mapping_sector": "insurance",
        "proposed_action": "sector_scope",
        "confidence": 0.8,
        "reasoning": "insurer investment portfolio",
        "evidence_finding_ids": ["dq-1"],
        "next_step": "review queue",
    }
    base.update(over)
    return base


def test_proposal_rows_provenance_and_columns():
    rows = dq_triage._proposal_rows([_mapping_proposal()], jurisdiction="US", ticker="AIG", entity_id="0000005272")
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "US"                          # jurisdiction
    assert row[1] == "us-gaap:AvailableForSaleSecurities"  # normalized_concept_id
    assert row[2] == "non_bank_financial"          # mapping_sector (governed, normalized from 'insurance')
    assert row[3] == "generic"                     # gics_scope
    assert row[4] == "map_candidate"               # review_class (mapping_add)
    assert row[5] == "investment_securities"       # suggested_target_variable
    assert row[6] == "investment_securities"       # top_candidate_label (promote reads this)
    assert row[8] == "sector_scope"                # proposed_action
    assert row[9] == ["us-gaap:AvailableForSaleSecurities"]  # source_concept_ids
    # provenance tags isolate our rows from other producers on the unique index
    assert row[-4] == "committee_dq_agent"         # review_batch
    assert row[-3] == "committee_dq_agent_v1"      # prompt_version
    assert row[-1] == "committee_dq_agent_v1"      # mapping_source


def test_proposal_rows_retarget_is_special_case_review():
    rows = dq_triage._proposal_rows(
        [_mapping_proposal(kind="mapping_retarget")], jurisdiction="US", ticker="X", entity_id="1"
    )
    assert rows[0][4] == "special_case_review"


def test_proposal_rows_skips_non_mapping_and_empty_concept():
    rows = dq_triage._proposal_rows(
        [
            {"kind": "reparse_filing", "concept_id": "c"},
            {"kind": "no_action"},
            {"kind": "mapping_add", "concept_id": ""},  # missing concept
        ],
        jurisdiction="US",
        ticker="X",
        entity_id="1",
    )
    assert rows == []


def test_proposal_rows_dedupes_concept_sector():
    rows = dq_triage._proposal_rows(
        [_mapping_proposal(), _mapping_proposal(confidence=0.2)],
        jurisdiction="US",
        ticker="X",
        entity_id="1",
    )
    assert len(rows) == 1


def test_queue_insert_never_writes_versioned_table():
    """Governance guard: proposals only ever hit the review queue, never production."""
    assert "map_concept_to_taxonomy_review_queue" in dq_triage._QUEUE_INSERT_SQL
    assert "map_concept_to_taxonomy_versioned" not in dq_triage._QUEUE_INSERT_SQL
    import inspect

    source = inspect.getsource(dq_triage)
    assert "INSERT INTO map_concept_to_taxonomy_versioned" not in source
    assert "UPDATE map_concept_to_taxonomy_versioned" not in source


def test_governed_mapping_sector_normalizes_scopes():
    assert dq_triage.governed_mapping_sector("insurance") == "non_bank_financial"
    assert dq_triage.governed_mapping_sector("reit") == "non_bank_financial"
    assert dq_triage.governed_mapping_sector("bank_financial") == "bank_financial"
    assert dq_triage.governed_mapping_sector("BOTH") == "BOTH"
    assert dq_triage.governed_mapping_sector("corp") == "corp"
    assert dq_triage.governed_mapping_sector(None) == ""


# --------------------------------------------------------------- evidence pack

def test_unmapped_concepts_uses_latest_view_and_exclusions(monkeypatch):
    captured = {}

    def fake_read_sql(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame(
            [{"concept_id": "us-gaap:Foo", "max_abs_value": 1000.0, "fact_count": 3}]
        )

    monkeypatch.setattr(dq_triage, "read_sql", fake_read_sql)
    out = dq_triage.unmapped_concepts("US", "0000005272", "insurance")
    assert out and out[0]["concept_id"] == "us-gaap:Foo"
    assert "v_fact_fundamentals_us_latest" in captured["sql"]
    assert captured["params"]["entity_id"] == "0000005272"
    assert any("Abstract" in p for p in captured["params"]["patterns"])


def test_unmapped_concepts_returns_empty_without_entity(monkeypatch):
    monkeypatch.setattr(dq_triage, "read_sql", lambda *a, **k: pytest.fail("should not query"))
    assert dq_triage.unmapped_concepts("US", None, "corp") == []


def test_build_mapping_pack_is_empty(monkeypatch):
    monkeypatch.setattr(dq_triage, "unmapped_concepts", lambda *a, **k: [])
    monkeypatch.setattr(dq_triage, "sector_compatibility_gaps", lambda *a, **k: [])
    monkeypatch.setattr(dq_triage, "suspect_mappings", lambda *a, **k: [])
    pack = dq_triage.build_mapping_pack(ticker="X", jurisdiction="US", entity_id="1", sector_scope="corp")
    assert pack["is_empty"] is True


# --------------------------------------------------------------- compact

def test_compact_triage_shape():
    agent = {
        "available": True,
        "triage": {
            "narrative": "n",
            "way_forward": ["a", "b"],
            "proposals": [{"kind": "mapping_add", "concept_id": "c", "target_variable": "t", "confidence": 0.5}],
        },
        "queued_proposal_ids": ["c"],
    }
    compact = dq_triage.compact_triage(agent)
    assert compact["narrative"] == "n"
    assert compact["way_forward"] == ["a", "b"]
    assert compact["top_proposals"][0]["concept_id"] == "c"
    assert compact["queued_proposal_ids"] == ["c"]


def test_compact_triage_empty_when_unavailable():
    assert dq_triage.compact_triage({"available": False}) == {}
    assert dq_triage.compact_triage(None) == {}


# --------------------------------------------------------------- persistence

class _FakeCursor:
    def __init__(self, existing):
        self._existing = existing
        self.updates = []
        self.rowcounts = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "SELECT finding_id, status" in sql:
            self._fetch = list(self._existing)
        elif "UPDATE dq_finding_state" in sql:
            self.updates.append(params)

    def fetchall(self):
        return self._fetch


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_record_findings_computes_deltas(monkeypatch):
    cursor = _FakeCursor(existing=[("dq-A", "open"), ("dq-B", "open")])

    @contextlib.contextmanager
    def fake_connect():
        yield _FakeConn(cursor)

    upserts = {}

    def fake_execute_values(cur, sql, rows, page_size=1000):
        upserts["rows"] = list(rows)
        return len(rows)

    monkeypatch.setattr("xbrl_sec.sec.db.connection.connect", fake_connect)
    monkeypatch.setattr("xbrl_sec.sec.db.bulk.execute_values", fake_execute_values)

    report = {"findings": [{"finding_id": "dq-A", "layer": "raw", "severity": "low", "title": "A"},
                           {"finding_id": "dq-C", "layer": "recon", "severity": "high", "title": "C"}]}
    deltas = dq_triage.record_findings(report, ticker="aig", jurisdiction="US", entity_id="1")

    assert deltas["new"] == ["dq-C"]        # C is unseen
    assert deltas["resolved"] == ["dq-B"]   # B was open, absent this run
    assert deltas["still_open"] == 2
    assert len(upserts["rows"]) == 2


def test_record_findings_degrades_when_db_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr("xbrl_sec.sec.db.connection.connect", boom)
    deltas = dq_triage.record_findings({"findings": [{"finding_id": "dq-A"}]}, ticker="X", jurisdiction="US", entity_id="1")
    assert deltas["new"] == [] and deltas["resolved"] == []
    assert deltas.get("note") == "RuntimeError"


# --------------------------------------------------------------- escalation gate

def test_should_escalate_gate():
    assert nodes._dq_should_escalate({}, {"is_empty": True}) is False
    assert nodes._dq_should_escalate({"findings": [{"severity": "high"}]}, {"is_empty": True}) is True
    assert nodes._dq_should_escalate({"layer_scores": {"raw": 60}}, {"is_empty": True}) is True
    assert nodes._dq_should_escalate({}, {"is_empty": False}) is True
    assert nodes._dq_should_escalate({"findings": [{"severity": "low"}], "layer_scores": {"raw": 99}}, {"is_empty": True}) is False


# --------------------------------------------------------------- node behavior

def _base_state(**over):
    state = {
        "ticker": "AIG",
        "jurisdiction": "US",
        "cik": "0000005272",
        "data_quality_report": {"findings": [], "layer_scores": {}},
        "financial_ratios": {"company": {}},
        "config": {},
    }
    state.update(over)
    return state


def test_node_disabled_by_config():
    out = nodes.data_quality_agent_node(_base_state(config={"enable_data_quality_agent": False}))
    assert out["data_quality_agent"] == {"available": False, "note": "disabled"}


def test_node_offline_skips_llm(monkeypatch):
    monkeypatch.setenv("MZQA_COMMITTEE_DISABLE_LLM", "1")
    monkeypatch.setattr(dq_triage, "build_mapping_pack", lambda **k: {"is_empty": True})
    monkeypatch.setattr(dq_triage, "record_findings", lambda *a, **k: {"new": [], "resolved": []})
    out = nodes.data_quality_agent_node(_base_state())["data_quality_agent"]
    assert out["available"] is True
    assert out["triage_skipped_reason"] == "llm_disabled"
    assert "triage" not in out


def test_node_runs_triage_and_queues(monkeypatch):
    monkeypatch.setattr(nodes, "_resolve_key", lambda state: "key")
    monkeypatch.setattr(dq_triage, "build_mapping_pack", lambda **k: {"is_empty": False, "unmapped_concepts": [{"concept_id": "c"}]})
    monkeypatch.setattr(dq_triage, "record_findings", lambda *a, **k: {"new": [], "resolved": []})
    monkeypatch.setattr(
        nodes,
        "_run_dq_triage",
        lambda *a, **k: {
            "proposals": [{"kind": "mapping_add", "concept_id": "c", "target_variable": "t"}],
            "triage": [],
            "way_forward": [],
            "narrative": "",
        },
    )
    monkeypatch.setattr(dq_triage, "queue_proposals", lambda proposals, **k: ["c"])

    out = nodes.data_quality_agent_node(_base_state())["data_quality_agent"]
    assert out["triage"]["proposals"][0]["concept_id"] == "c"
    assert out["queued_proposal_ids"] == ["c"]


def test_node_llm_failure_degrades(monkeypatch):
    monkeypatch.setattr(nodes, "_resolve_key", lambda state: "key")
    monkeypatch.setattr(dq_triage, "build_mapping_pack", lambda **k: {"is_empty": False})
    monkeypatch.setattr(dq_triage, "record_findings", lambda *a, **k: {"new": [], "resolved": []})

    def boom(*a, **k):
        raise RuntimeError("deepseek down")

    monkeypatch.setattr(nodes, "_run_dq_triage", boom)
    out = nodes.data_quality_agent_node(_base_state())["data_quality_agent"]
    assert out["available"] is True
    assert out["triage_skipped_reason"].startswith("llm_error")


def test_parse_triage_tolerates_enum_drift():
    raw = {
        "triage": [{"finding_id": "dq-1", "root_cause": "unmapped_concept", "priority": 1}],
        "proposals": [
            {
                "kind": "mapping_add",
                "concept_id": "us-gaap/AvailableForSaleSecurities",
                "target_variable": "investment_securities",
                "mapping_sector": "insurance",
                "proposed_action": "global_mapping",
                "confidence": 0.9,
            },
            {"kind": "weird_kind"},  # kept by schema (str), filtered at the queue writer
            "not-a-dict",  # dropped
        ],
        "way_forward": ["step 1"],
        "narrative": "n",
    }
    triage = dq_triage.parse_triage(raw)
    assert triage.triage[0].root_cause == "unmapped_concept"  # synonym tolerated
    assert len(triage.proposals) == 2  # the string is dropped
    assert triage.way_forward == ["step 1"]
    # only the valid mapping proposal reaches the queue
    rows = dq_triage._proposal_rows(
        [p.model_dump() for p in triage.proposals], jurisdiction="US", ticker="AIG", entity_id="1"
    )
    assert len(rows) == 1 and rows[0][1] == "us-gaap/AvailableForSaleSecurities"
