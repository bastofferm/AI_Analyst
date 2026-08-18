"""Graph assembly, the run entrypoint, and the CLI.

Topology, one round per loop:

    START -> prepare_data -> train -> evaluate -> perturb -> report
                                ^                              |
                                |            +-----------------+-----------------+
                                |            v                 v                 v
                                |       validation      portfolio_mgr      ext_advisor
                                |            +-----------------+-----------------+
                                |                              v
                                +--------- continue ------- researcher ---- finish ----+
                                                                                       v
                                                            select_champion -> promote -> END

The three critics fan out in one superstep and the researcher joins on all of them, so it
synthesizes a complete set of critiques rather than reacting to whichever finished first.
That is the same join the committee uses for its evidence nodes; the reducer channels on
``agent_notes`` and ``candidates`` exist because three nodes write them concurrently.

Run it directly::

    python -m api.quant.research.graph --jurisdiction US --label forward_12m --max-iterations 2
    python -m api.quant.research.graph --jurisdiction US --offline    # no tokens spent
"""
from __future__ import annotations

import argparse
import json
import logging
import uuid
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import AlphaResearchState, default_config, get_config
from .schemas import normalize_decision

logger = logging.getLogger("mzqa.quant.research.graph")

_CRITICS = ("model_validation", "portfolio_manager", "external_advisor")


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def route_after_prepare(state: AlphaResearchState) -> str:
    """A run with no panel ends immediately rather than looping over emptiness."""
    if state.get("stop_reason"):
        return "finish_early"
    return "train"


def route_iteration(state: AlphaResearchState) -> str:
    """Continue the loop, or stop and select a champion.

    Five independent stopping conditions, in priority order. Each is recorded in
    ``stop_reason`` so the report can say why the run ended rather than leaving the user to
    infer it from the iteration count.
    """
    cfg = get_config(state)
    iteration = int(state.get("iteration", 0))
    max_iters = int(state.get("max_iterations") or cfg.get("max_iterations", 4))

    if state.get("cancelled"):
        return "finish"
    if iteration >= max_iters:
        return "finish"
    if normalize_decision((state.get("pm") or {}).get("decision")) in ("accept", "reject"):
        return "finish"
    if (state.get("researcher") or {}).get("stop"):
        return "finish"
    if not (state.get("researcher") or {}).get("applied_changes"):
        # The researcher proposed nothing the validator would accept; another identical
        # round would produce an identical result.
        return "finish"
    if _stalled(state, patience=int(cfg.get("patience", 2))):
        return "finish"
    return "train"


def _stalled(state: AlphaResearchState, patience: int) -> bool:
    """True when the guarded metric has not improved for ``patience`` consecutive rounds."""
    history = [h.get("headline", {}).get("rank_ic") for h in (state.get("iterations") or [])]
    scores = [s for s in history if s is not None]
    if len(scores) <= patience:
        return False
    best_before = max(scores[:-patience])
    return all(s <= best_before for s in scores[-patience:])


def stop_reason(state: AlphaResearchState) -> str:
    cfg = get_config(state)
    iteration = int(state.get("iteration", 0))
    max_iters = int(state.get("max_iterations") or cfg.get("max_iterations", 4))
    if state.get("stop_reason"):
        return state["stop_reason"]
    if state.get("cancelled"):
        return "cancelled by the user"
    decision = normalize_decision((state.get("pm") or {}).get("decision"))
    if decision == "accept":
        return "the portfolio manager accepted a candidate"
    if decision == "reject":
        return "the portfolio manager rejected the line of research"
    if (state.get("researcher") or {}).get("stop"):
        return "the researcher judged the search converged"
    if iteration >= max_iters:
        return f"reached the iteration budget ({max_iters})"
    if _stalled(state, patience=int(cfg.get("patience", 2))):
        return "the guarded metric stopped improving"
    return "loop ended"


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_research_graph(checkpointer: Any | None = None):
    workflow = StateGraph(AlphaResearchState)

    # NB: node names must not collide with state keys — LangGraph rejects the graph outright.
    # Hence build_report / model_validation / quant_researcher rather than the obvious names,
    # which are taken by the `report`, `validation` and `researcher` channels.
    workflow.add_node("prepare_data", nodes.prepare_data_node)
    workflow.add_node("train", nodes.train_node)
    workflow.add_node("evaluate", nodes.evaluate_node)
    workflow.add_node("perturb", nodes.perturb_node)
    workflow.add_node("build_report", nodes.report_node)
    workflow.add_node("model_validation", nodes.validation_node)
    workflow.add_node("portfolio_manager", nodes.pm_node)
    workflow.add_node("external_advisor", nodes.advisor_node)
    workflow.add_node("quant_researcher", nodes.researcher_node)
    workflow.add_node("select_champion", nodes.select_champion_node)
    workflow.add_node("promote", nodes.promote_node)

    workflow.add_edge(START, "prepare_data")
    workflow.add_conditional_edges(
        "prepare_data", route_after_prepare,
        {"train": "train", "finish_early": END},
    )
    workflow.add_edge("train", "evaluate")
    workflow.add_edge("evaluate", "perturb")
    workflow.add_edge("perturb", "build_report")

    # Fan out to the three critics, then join them all on the researcher.
    for critic in _CRITICS:
        workflow.add_edge("build_report", critic)
        workflow.add_edge(critic, "quant_researcher")

    workflow.add_conditional_edges(
        "quant_researcher", route_iteration,
        {"train": "train", "finish": "select_champion"},
    )
    workflow.add_edge("select_champion", "promote")
    workflow.add_edge("promote", END)
    return workflow.compile(checkpointer=checkpointer)


def _recursion_budget(max_iterations: int) -> int:
    """Nodes per round is ~8; give the loop generous headroom plus the tail."""
    return max(40, int(max_iterations) * 12 + 20)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def run_research(
    jurisdiction: str = "US",
    label: str = "forward_1m",
    *,
    max_iterations: int = 4,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    advisor_provider: str | None = None,
    advisor_api_key: str | None = None,
    advisor_model: str | None = None,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
    progress: Callable[[str], None] | None = None,
    on_iteration: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the loop synchronously and return the final state.

    ``progress`` receives a short stage label for the UI's poller. ``on_iteration`` is called
    with each completed report so the runner can persist rounds as they finish — a run that
    dies in round 4 should not lose rounds 1 to 3. ``should_cancel`` is polled between rounds
    for cooperative cancellation.
    """
    cfg = {**default_config(), **(config or {})}
    if progress:
        cfg["_progress"] = progress
    if on_iteration:
        cfg["_on_iteration"] = on_iteration
    if should_cancel:
        cfg["_should_cancel"] = should_cancel

    graph = build_research_graph()
    initial: AlphaResearchState = {
        "run_id": run_id or uuid.uuid4().hex,
        "jurisdiction": jurisdiction,
        "label": label,
        "max_iterations": int(max_iterations),
        "config": cfg,
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "advisor_provider": advisor_provider,
        "advisor_api_key": advisor_api_key,
        "advisor_model": advisor_model,
        "iterations": [],
        "agent_notes": [],
        "candidates": [],
        "errors": [],
    }
    final = graph.invoke(
        initial, {"recursion_limit": _recursion_budget(max_iterations)})
    final["stop_reason"] = stop_reason(final)
    return final


def summarize(final: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe summary of a finished run (the shape the REST layer returns)."""
    champion = final.get("champion") or {}
    return {
        "run_id": final.get("run_id"),
        "market": final.get("jurisdiction"),
        "horizon": final.get("label"),
        "iterations_done": len(final.get("iterations") or []),
        "stop_reason": final.get("stop_reason"),
        "champion": {k: v for k, v in champion.items()
                     if k not in ("model", "spec")},
        "promoted": bool(final.get("promoted")),
        "promotion_reason": final.get("promotion_reason"),
        "incumbent": final.get("incumbent"),
        "errors": final.get("errors") or [],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="agentic alpha-model research loop")
    p.add_argument("--jurisdiction", default="US")
    p.add_argument("--label", default="forward_1m")
    p.add_argument("--max-iterations", type=int, default=2)
    p.add_argument("--provider", default=None, help="llm_providers id (default: DeepSeek)")
    p.add_argument("--advisor-provider", default=None,
                   help="a DIFFERENT provider for the external advisor, if you have a key")
    p.add_argument("--offline", action="store_true",
                   help="run the deterministic ladder with no LLM calls")
    p.add_argument("--json", action="store_true", help="print the full summary as JSON")
    args = p.parse_args(argv)

    final = run_research(
        args.jurisdiction, args.label, max_iterations=args.max_iterations,
        provider=args.provider, advisor_provider=args.advisor_provider,
        config={"offline": bool(args.offline)},
        progress=lambda s: print(f"  ... {s}", flush=True),
    )
    summary = summarize(final)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"\nrun {summary['run_id']} — {summary['market']} {summary['horizon']}")
    print(f"stopped: {summary['stop_reason']}")
    for rep in final.get("iterations") or []:
        h = rep.get("headline", {})
        changes = ", ".join(rep.get("spec_changes") or []) or "baseline"
        print(f"  [{rep['iteration']}] rank_ic={_n(h.get('rank_ic'))} "
              f"r2_oos={_n(h.get('r2_oos'), 5)} rating={h.get('robustness_rating')} "
              f"sharpe={_n(h.get('long_short_sharpe'), 2)} | {changes[:90]}")
    champ = summary["champion"]
    if champ.get("available"):
        print(f"champion: iteration {champ.get('iteration')} ({champ.get('kind')}) "
              f"score={_n(champ.get('score'))}")
    print(f"promoted: {summary['promoted']} — {summary['promotion_reason']}")
    if summary["errors"]:
        print(f"errors: {summary['errors']}")
    return 0


def _n(v: Any, d: int = 4) -> str:
    try:
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
