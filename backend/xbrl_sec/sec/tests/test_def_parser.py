"""Tests for parse_def_xml and iter_def_arcs.

Synthesizes a minimal XBRL definition linkbase in a temp file and verifies
that the parser extracts dimension/axis/member arcs correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xbrl_sec.sec.parsers.xbrl_linkbase import iter_def_arcs, parse_def_xml


_DEF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<linkbase xmlns="http://www.xbrl.org/2003/linkbase"
          xmlns:xlink="http://www.w3.org/1999/xlink"
          xmlns:xbrldt="http://xbrl.org/2005/xbrldt">
  <definitionLink xlink:type="extended"
                  xlink:role="http://example.com/role/SegmentDisclosure">
    <loc xlink:type="locator"
         xlink:href="us-gaap-2024.xsd#us-gaap_SegmentTable"
         xlink:label="loc_table"/>
    <loc xlink:type="locator"
         xlink:href="us-gaap-2024.xsd#us-gaap_SegmentAxis"
         xlink:label="loc_axis"/>
    <loc xlink:type="locator"
         xlink:href="us-gaap-2024.xsd#us-gaap_SegmentDomain"
         xlink:label="loc_domain"/>
    <loc xlink:type="locator"
         xlink:href="us-gaap-2024.xsd#us-gaap_RetailMember"
         xlink:label="loc_member_a"/>
    <loc xlink:type="locator"
         xlink:href="us-gaap-2024.xsd#us-gaap_WholesaleMember"
         xlink:label="loc_member_b"/>

    <definitionArc xlink:type="arc"
                   xlink:arcrole="http://xbrl.org/int/dim/arcrole/hypercube-dimension"
                   xlink:from="loc_table" xlink:to="loc_axis" order="1"/>
    <definitionArc xlink:type="arc"
                   xlink:arcrole="http://xbrl.org/int/dim/arcrole/dimension-domain"
                   xlink:from="loc_axis" xlink:to="loc_domain" order="1"/>
    <definitionArc xlink:type="arc"
                   xlink:arcrole="http://xbrl.org/int/dim/arcrole/domain-member"
                   xlink:from="loc_domain" xlink:to="loc_member_a"
                   order="1" xbrldt:usable="true"/>
    <definitionArc xlink:type="arc"
                   xlink:arcrole="http://xbrl.org/int/dim/arcrole/domain-member"
                   xlink:from="loc_domain" xlink:to="loc_member_b"
                   order="2" xbrldt:usable="false"/>
  </definitionLink>
</linkbase>
"""


@pytest.fixture
def def_xml_path(tmp_path: Path) -> Path:
    path = tmp_path / "test_def.xml"
    path.write_text(_DEF_XML, encoding="utf-8")
    return path


def test_iter_def_arcs_returns_all_arcs(def_xml_path: Path):
    arcs = list(iter_def_arcs(def_xml_path))
    assert len(arcs) == 4
    arcroles = [a["arcrole"] for a in arcs]
    assert "http://xbrl.org/int/dim/arcrole/hypercube-dimension" in arcroles
    assert "http://xbrl.org/int/dim/arcrole/dimension-domain" in arcroles
    assert arcroles.count("http://xbrl.org/int/dim/arcrole/domain-member") == 2


def test_hypercube_dimension_arc_emits_axis(def_xml_path: Path):
    arcs = list(iter_def_arcs(def_xml_path))
    hd = next(a for a in arcs if a["arcrole"].endswith("hypercube-dimension"))
    assert hd["dimension_axis"] == "us-gaap/SegmentAxis"
    assert hd["dimension_member"] is None


def test_dimension_domain_arc_emits_axis_and_member(def_xml_path: Path):
    arcs = list(iter_def_arcs(def_xml_path))
    dd = next(a for a in arcs if a["arcrole"].endswith("dimension-domain"))
    assert dd["dimension_axis"] == "us-gaap/SegmentAxis"
    assert dd["dimension_member"] == "us-gaap/SegmentDomain"


def test_domain_member_arc_usable_flag(def_xml_path: Path):
    arcs = list(iter_def_arcs(def_xml_path))
    members = [a for a in arcs if a["arcrole"].endswith("domain-member")]
    by_member = {a["child_concept_id"]: a for a in members}
    assert by_member["us-gaap/RetailMember"]["usable"] is True
    assert by_member["us-gaap/WholesaleMember"]["usable"] is False


def test_parse_def_xml_indexes_by_concept(def_xml_path: Path):
    by_concept = parse_def_xml(def_xml_path)
    assert "us-gaap/RetailMember" in by_concept
    assert "us-gaap/WholesaleMember" in by_concept
    # In a domain-member arc, the immediate parent of the member is the domain
    # (not the axis directly). Walking to the axis requires following
    # dimension-domain arcs upward.
    assert by_concept["us-gaap/RetailMember"][0]["axis"] == "us-gaap/SegmentDomain"


def test_missing_def_file_returns_empty(tmp_path: Path):
    assert parse_def_xml(tmp_path / "nope.xml") == {}
    assert list(iter_def_arcs(tmp_path / "nope.xml")) == []


def test_corrupt_xml_returns_empty(tmp_path: Path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<not actually xml", encoding="utf-8")
    assert parse_def_xml(bad) == {}
    assert list(iter_def_arcs(bad)) == []


def test_role_uri_propagated(def_xml_path: Path):
    arcs = list(iter_def_arcs(def_xml_path))
    for arc in arcs:
        assert arc["role_uri"] == "http://example.com/role/SegmentDisclosure"
        assert arc["linkbase_type"] == "definition"
