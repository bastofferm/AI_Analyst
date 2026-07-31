"""The prepare/debate split of the committee graph.

The LLM nodes are stubbed, so these run offline and fast. What they protect is the
contract that lets several providers debate one shared evidence base: the prepared
state must survive the hand-off intact, and concurrent debates must not bleed into
each other through the ``operator.add`` reducer channels.
"""
import concurrent.futures as cf

import pytest

from ai_analyst.committee import graph, nodes


def _stub_agent(stance):
    def node(state):
        return {
            f"{stance}_analysis": f"{stance} on {state['ticker']} via {state.get('provider')}",
            "committee_chat_history": [
                {"role": stance, "content": f"{stance}/{state.get('provider')}"}
            ],
        }

    return node


@pytest.fixture
def stub_debate_nodes(monkeypatch):
    monkeypatch.setattr(nodes, "advocate_analyst_node", _stub_agent("advocate"))
    monkeypatch.setattr(nodes, "challenger_analyst_node", _stub_agent("challenger"))
    monkeypatch.setattr(nodes, "auditor_node", _stub_agent("auditor"))
    monkeypatch.setattr(
        nodes, "lead_analyst_node", lambda s: {"lead_synthesis": "synth", "decision_ready": True}
    )
    monkeypatch.setattr(
        nodes, "memo_generator_node", lambda s: {"memo": {"en": f"memo/{s.get('provider')}"}}
    )


@pytest.fixture
def prepared():
    """A prepared state shaped like what run_prepare leaves behind."""
    return {
        "ticker": "MSFT",
        "target_years": [2024],
        "jurisdiction": "US",
        "config": {},
        "iteration_count": 0,
        "is_data_complete": True,
        "is_dq_passed": True,
        "financial_ratios": {"revenue": 245_000},
        "analytics": {"wacc": {"wacc_pct": 8.4}},
        "ownership": {"net_direction": "accumulating"},
        "committee_chat_history": [],
    }


def test_prepare_and_debate_graphs_split_the_topology():
    prepare_nodes = set(graph.build_prepare_graph().get_graph().nodes)
    debate_nodes = set(graph.build_debate_graph().get_graph().nodes)

    assert {"completeness_check", "dq_validation", "financial_analysis_engine"} <= prepare_nodes
    assert {"news_macro", "institutional", "dq_mapping_agent"} <= prepare_nodes
    assert {"advocate_analyst", "challenger_analyst", "auditor", "lead_analyst"} <= debate_nodes
    # The two phases share no working node — only START/END.
    assert (prepare_nodes & debate_nodes) <= {"__start__", "__end__"}


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, True),
        ({"is_data_complete": False}, False),
        # A DQ failure is advisory unless the caller opted into strict mode.
        ({"is_dq_passed": False}, True),
        ({"is_dq_passed": False, "config": {"dq_enforce": True}}, False),
    ],
)
def test_gate_passed_mirrors_the_routing_rule(prepared, overrides, expected):
    assert graph.gate_passed({**prepared, **overrides}) is expected


def test_debate_carries_the_prepared_evidence_through(stub_debate_nodes, prepared):
    out = graph.run_debate(prepared, provider="deepseek")

    assert out["financial_ratios"] == {"revenue": 245_000}
    assert out["analytics"]["wacc"]["wacc_pct"] == 8.4
    assert out["ownership"]["net_direction"] == "accumulating"
    assert out["jurisdiction"] == "US"
    assert "advocate on MSFT" in out["advocate_analysis"]
    assert len(out["committee_chat_history"]) == 3


def test_debate_does_not_mutate_the_prepared_state(stub_debate_nodes, prepared):
    graph.run_debate(prepared, provider="deepseek")

    assert prepared["committee_chat_history"] == []
    assert "advocate_analysis" not in prepared


def test_concurrent_debates_do_not_cross_contaminate(stub_debate_nodes, prepared):
    """Without the deep copy in run_debate, one provider's argument shows up in
    another's transcript — the whole point of the shared prepare phase."""
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(graph.run_debate, prepared, provider=p) for p in ("deepseek", "gemini")]
        results = [f.result() for f in futures]

    for res, provider in zip(results, ("deepseek", "gemini")):
        lines = [m["content"] for m in res["committee_chat_history"]]
        assert len(lines) == 3, lines
        assert all(line.endswith(f"/{provider}") for line in lines), lines
