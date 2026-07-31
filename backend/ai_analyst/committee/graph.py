r"""Investment-committee LangGraph assembly + CLI.

Topology (mirrors the approved plan):

    START -> completeness_check -> dq_validation
      -[route_data_validation]-> terminate_error -> END      (governance failure)
                              \-> financial_analysis_engine
      -(fan-out)-> {advocate_analyst, challenger_analyst, auditor}
      -(fan-in)-> lead_analyst
      -[route_lead_analyst_review]-> re-run tribunal (loop, capped at 3)
                                 \-> memo_generator -> END

The same topology is also available split in two — ``build_prepare_graph`` (gate →
engine → evidence) and ``build_debate_graph`` (tribunal → lead → memo). The prepare
phase is provider-independent, so one prepared state can feed several debates on
different LLM providers concurrently without re-doing the deterministic work.

Run:
    python -m ai_analyst.committee.graph --ticker AAPL --years 2022 2023 2024 2025
"""
from __future__ import annotations

import argparse
import copy
import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from .. import services
from . import nodes
from .state import InvestmentCommitteeState, default_config, get_config

_AGENT_NODES = ["advocate_analyst", "challenger_analyst", "auditor"]
_MAX_ITERATIONS = nodes._MAX_ITERATIONS


def route_data_validation(state: InvestmentCommitteeState) -> str:
    # Completeness is always a hard stop — with no fundamentals the engine cannot run.
    # The accounting-identity DQ check is advisory by default (dq_enforce=False): the
    # committee proceeds and surfaces is_dq_passed / dq_errors as a warning banner. Set
    # config["dq_enforce"]=True (the UI "strict" toggle) to restore the hard block.
    if not state.get("is_data_complete"):
        return "terminate_error"
    if get_config(state).get("dq_enforce") and not state.get("is_dq_passed"):
        return "terminate_error"
    return "analyze_data"


def _make_lead_router(agent_nodes: list[str]):
    """Lead-review router bound to the *actual* tribunal roster (built-ins + any
    user-added analysts), so re-run rounds fan back out to everyone."""
    def route_lead_analyst_review(state: InvestmentCommitteeState):
        if state.get("decision_ready") or int(state.get("iteration_count") or 0) >= _MAX_ITERATIONS:
            return "generate_memo"
        return list(agent_nodes)  # fan back out to the whole tribunal for another round
    return route_lead_analyst_review


_EVIDENCE_NODES = ["news_macro", "institutional", "dq_mapping_agent", "qlib_signals"]


def _add_prepare_nodes(workflow: StateGraph) -> None:
    """Deterministic gate + engine + evidence layer.

    Everything here is either pure data/maths or, in the single case of
    ``dq_mapping_agent``, LLM *triage* of data quality rather than investment
    judgment — so this whole phase is shared across providers (see
    ``run_prepare``) instead of being re-run per debate.
    """
    workflow.add_node("completeness_check", nodes.completeness_check_node)
    workflow.add_node("dq_validation", nodes.dq_validation_node)
    workflow.add_node("financial_analysis_engine", nodes.financial_analysis_engine_node)
    workflow.add_node("news_macro", nodes.news_macro_node)
    workflow.add_node("institutional", nodes.institutional_node)
    workflow.add_node("dq_mapping_agent", nodes.data_quality_agent_node)
    workflow.add_node("qlib_signals", nodes.qlib_signals_node)
    workflow.add_node("error_terminator", nodes.error_terminator_node)


def _wire_prepare_edges(workflow: StateGraph) -> None:
    workflow.add_edge(START, "completeness_check")
    workflow.add_edge("completeness_check", "dq_validation")
    workflow.add_conditional_edges(
        "dq_validation",
        route_data_validation,
        {"terminate_error": "error_terminator", "analyze_data": "financial_analysis_engine"},
    )
    workflow.add_edge("error_terminator", END)
    # Engine → parallel evidence gathering (macro/news + 13F + DQ/mapping agent).
    for evidence in _EVIDENCE_NODES:
        workflow.add_edge("financial_analysis_engine", evidence)


def _add_debate_nodes(workflow: StateGraph, extra_analysts: Any | None) -> list[str]:
    """Tribunal + lead + memo. Returns the resolved agent roster."""
    workflow.add_node("advocate_analyst", nodes.advocate_analyst_node)
    workflow.add_node("challenger_analyst", nodes.challenger_analyst_node)
    workflow.add_node("auditor", nodes.auditor_node)
    workflow.add_node("lead_analyst", nodes.lead_analyst_node)
    workflow.add_node("memo_generator", nodes.memo_generator_node)

    # Specialist and user-defined analysts join the roster as additional agent nodes.
    agent_nodes = list(_AGENT_NODES)
    for a in nodes.normalize_extra_analysts(extra_analysts):
        node_name = f"{a['key']}_analyst"
        workflow.add_node(
            node_name,
            nodes.make_extra_analyst_node(
                a["key"], a["name"], a["mandate"], emit_structured=bool(a.get("emit_structured"))
            ),
        )
        agent_nodes.append(node_name)
    return agent_nodes


def _wire_debate_tail(workflow: StateGraph, agent_nodes: list[str]) -> None:
    for agent in agent_nodes:
        workflow.add_edge(agent, "lead_analyst")
    workflow.add_conditional_edges(
        "lead_analyst",
        _make_lead_router(agent_nodes),
        {"generate_memo": "memo_generator", **{n: n for n in agent_nodes}},
    )
    workflow.add_edge("memo_generator", END)


def build_committee_graph(checkpointer: Any | None = None, extra_analysts: Any | None = None):
    """The whole pipeline as one graph — the original topology, unchanged.

    Still used by the CLI and by ``run_committee`` when no split is needed.
    """
    workflow = StateGraph(InvestmentCommitteeState)
    _add_prepare_nodes(workflow)
    agent_nodes = _add_debate_nodes(workflow, extra_analysts)
    _wire_prepare_edges(workflow)
    # Each agent joins on all three evidence nodes (LangGraph waits for every
    # incoming edge in the superstep), so the tribunal never starts before the
    # DQ/mapping triage is available.
    for agent in agent_nodes:
        for evidence in _EVIDENCE_NODES:
            workflow.add_edge(evidence, agent)
    _wire_debate_tail(workflow, agent_nodes)
    return workflow.compile(checkpointer=checkpointer)


def build_prepare_graph(checkpointer: Any | None = None):
    """Phase 1: gate → engine → evidence, then stop.

    The resulting state is the shared, provider-independent evidence base that one
    or more debate phases run on top of.
    """
    workflow = StateGraph(InvestmentCommitteeState)
    _add_prepare_nodes(workflow)
    _wire_prepare_edges(workflow)
    for evidence in _EVIDENCE_NODES:
        workflow.add_edge(evidence, END)
    return workflow.compile(checkpointer=checkpointer)


def build_debate_graph(extra_analysts: Any | None = None, checkpointer: Any | None = None):
    """Phase 2: the tribunal debate over an already-prepared state."""
    workflow = StateGraph(InvestmentCommitteeState)
    agent_nodes = _add_debate_nodes(workflow, extra_analysts)
    for agent in agent_nodes:
        workflow.add_edge(START, agent)
    _wire_debate_tail(workflow, agent_nodes)
    return workflow.compile(checkpointer=checkpointer)


def _resolve_roster(ticker: str, config: dict[str, Any] | None) -> tuple[dict[str, Any], Any]:
    """Resolve the specialist + user-added analyst roster and fold it into the config.

    Done once, in ``run_prepare``, and carried through the prepared state, so that
    parallel debates on different providers argue with an identical roster.
    """
    input_config = dict(config or {})
    effective_config = default_config()
    effective_config.update(input_config)
    try:
        company = services.company_overview(ticker)
    except Exception:  # noqa: BLE001 - roster can still run without sector metadata
        company = {}
    specialist_analysts = nodes.default_specialist_analysts(company, effective_config)
    extra_analysts = nodes.merge_analyst_rosters(specialist_analysts, input_config.get("extra_analysts"))
    graph_config = dict(input_config)
    if extra_analysts:
        graph_config["extra_analysts"] = extra_analysts
    return graph_config, extra_analysts


def _recursion_budget(extra_analysts: Any | None) -> int:
    # gate(3) + engine + up to 3 tribunal rounds (N agents + lead) + memo. Extra
    # analysts add nodes per round, so scale the ceiling with the roster size.
    return 40 + len(nodes.normalize_extra_analysts(extra_analysts)) * _MAX_ITERATIONS


def gate_passed(state: dict[str, Any]) -> bool:
    """Did the deterministic governance gate let the run proceed?

    Mirrors ``route_data_validation``: incomplete data is always fatal, a DQ failure
    only when the caller opted into strict mode.
    """
    if not state.get("is_data_complete"):
        return False
    if get_config(state).get("dq_enforce") and not state.get("is_dq_passed"):
        return False
    return True


def run_prepare(
    ticker: str,
    target_years: list[int] | None = None,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 1 — the shared, provider-independent evidence base.

    Check ``gate_passed()`` on the result before handing it to ``run_debate``.
    """
    graph_config, extra_analysts = _resolve_roster(ticker, config)
    app = build_prepare_graph()
    initial: InvestmentCommitteeState = {
        "ticker": ticker,
        "target_years": target_years or [],
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "config": graph_config,
        "iteration_count": 0,
    }
    return app.invoke(initial, config={"recursion_limit": _recursion_budget(extra_analysts)})


def run_debate(
    prepared: dict[str, Any],
    *,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Phase 2 — one provider's debate over an already-prepared state.

    The prepared state is deep-copied because ``committee_chat_history`` and
    ``specialist_verdicts`` are ``operator.add`` reducer channels: sharing one dict
    across concurrent debates would let one provider's argument leak into another's
    transcript.
    """
    state = copy.deepcopy(dict(prepared))
    state["provider"] = provider
    state["api_key"] = api_key
    state["model"] = model
    state.setdefault("iteration_count", 0)
    extra_analysts = (state.get("config") or {}).get("extra_analysts")
    app = build_debate_graph(extra_analysts=extra_analysts)
    return app.invoke(state, config={"recursion_limit": _recursion_budget(extra_analysts)})


def run_committee(
    ticker: str,
    target_years: list[int] | None = None,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    checkpointer: Any | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """The full pipeline, prepare → debate.

    With a checkpointer this still runs the single combined graph, since resuming a
    thread expects one topology.
    """
    if checkpointer is not None:
        graph_config, extra_analysts = _resolve_roster(ticker, config)
        app = build_committee_graph(checkpointer=checkpointer, extra_analysts=extra_analysts)
        initial: InvestmentCommitteeState = {
            "ticker": ticker,
            "target_years": target_years or [],
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "config": graph_config,
            "iteration_count": 0,
        }
        cfg: dict[str, Any] = {"recursion_limit": _recursion_budget(extra_analysts)}
        cfg["configurable"] = {"thread_id": thread_id or f"committee-{ticker}"}
        return app.invoke(initial, config=cfg)

    prepared = run_prepare(
        ticker, target_years, provider=provider, api_key=api_key, model=model, config=config
    )
    if not gate_passed(prepared):
        return prepared  # governance stop — the tribunal never convenes
    return run_debate(prepared, provider=provider, api_key=api_key, model=model)


def _summary(state: dict[str, Any]) -> str:
    lines = [f"Ticker: {state.get('ticker')}  jurisdiction={state.get('jurisdiction')}"]
    lines.append(f"complete={state.get('is_data_complete')} dq_passed={state.get('is_dq_passed')} "
                 f"iterations={state.get('iteration_count')}")
    if not state.get("is_data_complete") or not state.get("is_dq_passed"):
        lines.append("STOPPED at governance gate.")
        lines.append("dq_errors: " + json.dumps((state.get("dq_errors") or [])[:5]))
        lines.append("completeness: " + json.dumps(state.get("completeness_report") or {}, default=str))
        return "\n".join(lines)
    a = state.get("analytics") or {}
    w = a.get("wacc") or {}
    lines.append(f"WACC={w.get('wacc_pct')}% (Re={w.get('cost_of_equity_capm_pct')}% Rd={w.get('cost_of_debt_pct')}% "
                 f"beta={(w.get('betas') or {}).get('mkt')})")
    rd = state.get("reverse_dcf") or {}
    lines.append(f"reverse DCF: market implies {rd.get('implied_growth_pct')}% growth")
    tri = state.get("triangulation") or {}
    lines.append(f"PRIMARY fair value (SOTP) = {tri.get('primary_fair_value')} "
                 f"(price {tri.get('current_price')}, upside {tri.get('implied_upside_pct')}%)")
    for m in (tri.get("methods") or []):
        lines.append(f"  {m['label']}: {m.get('low')}-{m.get('high')} (mid {m.get('mid')})"
                     + (" PRIMARY" if m.get("primary") else ""))
    macro = (state.get("macro") or {}).get("signal") or {}
    lines.append(f"macro: regime={macro.get('regime_quadrant')} tilt={macro.get('tilt')}")
    own = state.get("ownership") or {}
    lines.append(f"13F: {own.get('net_direction')} (passive {own.get('passive_share_of_reported_pct')}%)")
    memo = state.get("memo") or {}
    if memo.get("en"):
        lines.append("memo(en) first 400 chars:\n" + memo["en"][:400])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the multi-agent investment committee for a ticker.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--years", type=int, nargs="*", default=[])
    parser.add_argument("--model", default=None)
    parser.add_argument("--json", action="store_true", help="Dump the full final state as JSON.")
    args = parser.parse_args(argv)

    final = run_committee(args.ticker, args.years, model=args.model)
    if args.json:
        print(json.dumps(final, indent=2, default=str))
    else:
        print(_summary(final))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
