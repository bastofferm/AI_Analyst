"""Tests for the per-proposal promote path (mocked DB — never touches Postgres)."""
from __future__ import annotations

import contextlib

import pytest

from ai_analyst import mapping_promote


class _FakeCursor:
    def __init__(self, queue_row):
        self._queue_row = queue_row
        self._next = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "FROM map_concept_to_taxonomy_review_queue" in sql and "WHERE mapping_source" in sql:
            self._next = self._queue_row
        elif "FROM map_concept_to_taxonomy_versioned" in sql:  # find_existing_mapping
            self._next = None
        elif "INSERT INTO map_concept_to_taxonomy_versioned" in sql:
            self._next = (4242,)
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _patch_connect(monkeypatch, cursor):
    @contextlib.contextmanager
    def fake_connect():
        yield _FakeConn(cursor)

    monkeypatch.setattr(mapping_promote, "connect", fake_connect)


def test_promote_inserts_new_mapping(monkeypatch):
    # queued row: (queue_id, top_candidate_label, suggested_target_variable, agg_type, sign_policy)
    cursor = _FakeCursor((7, "investment_securities", None, None, None))
    _patch_connect(monkeypatch, cursor)

    res = mapping_promote.promote_proposal(
        jurisdiction="US",
        concept_id="us-gaap:AvailableForSaleSecurities",
        mapping_sector="insurance",  # normalized to non_bank_financial
    )
    assert res["status"] == "promoted"
    assert res["action"] == "inserted"
    assert res["mapping_id"] == 4242
    assert res["mapping_sector"] == "non_bank_financial"
    assert res["target_variable"] == "investment_securities"
    # the governed table was written and the queue row stamped approved
    sqls = " ".join(sql for sql, _ in cursor.executed)
    assert "INSERT INTO map_concept_to_taxonomy_versioned" in sqls
    assert "review_status = 'approved'" in sqls


def test_promote_errors_when_no_queued_row(monkeypatch):
    cursor = _FakeCursor(None)  # SELECT returns nothing
    _patch_connect(monkeypatch, cursor)
    with pytest.raises(mapping_promote.PromoteError):
        mapping_promote.promote_proposal(jurisdiction="US", concept_id="us-gaap:Foo", mapping_sector="corp")


def test_promote_rejects_bad_jurisdiction(monkeypatch):
    _patch_connect(monkeypatch, _FakeCursor(None))
    with pytest.raises(mapping_promote.PromoteError):
        mapping_promote.promote_proposal(jurisdiction="XX", concept_id="c")
