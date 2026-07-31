"""Multi-agent investment committee (Tribunal).

A LangGraph pipeline that layers a dialectical Advocate/Challenger/Auditor debate plus a
Lead-Analyst synthesizer on top of the existing deterministic ai_analyst engine
(``services``, ``dcf_engine``) and produces a probability-weighted fair value with
explicit thesis-falsification KPIs.

No financial logic is duplicated here: every number originates from the
deterministic services/metrics/DCF layer; the LLM agents only *argue over* and
*tilt assumptions on* that data.
"""
from __future__ import annotations

__all__ = ["build_committee_graph", "run_committee"]


def __getattr__(name: str):
    if name in ("build_committee_graph", "run_committee"):
        from ai_analyst.committee.graph import build_committee_graph, run_committee

        return {"build_committee_graph": build_committee_graph, "run_committee": run_committee}[name]
    raise AttributeError(name)
