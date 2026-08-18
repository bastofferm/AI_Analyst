"""GraphState for the alpha-research loop.

Mirrors the convention in ``ai_analyst/committee/state.py``: a ``TypedDict(total=False)`` with
``Annotated[..., operator.add]`` on exactly the channels that fan in. Everything else is
last-write-wins, which is what you want for a loop that overwrites its working spec each
round.

Only two channels accumulate — ``iterations`` (one validation report per round) and
``agent_notes`` (the four personas' output, which arrives from three parallel nodes in the
same superstep and would otherwise clobber itself). The committee learned this the hard way;
see the deep-copy note in ``committee/graph.run_debate``.

Heavyweight objects — the raw panel, the fitted candidates — live on the state as ordinary
Python references. The graph runs in one process on one thread, so there is no serialization
boundary to cross, and re-deriving a 60-second warehouse pull every round to satisfy a purity
principle would be the wrong trade.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AlphaResearchState(TypedDict, total=False):
    # --- Inputs ---
    run_id: str
    jurisdiction: str                  # US | JP | INTL:<cc>
    label: str                         # forward_1m | forward_3m | forward_6m | forward_12m
    max_iterations: int
    config: dict[str, Any]

    provider: str | None               # llm_providers id for the researcher/validation/PM
    api_key: str | None
    model: str | None
    advisor_provider: str | None       # deliberately a DIFFERENT provider when available
    advisor_api_key: str | None
    advisor_model: str | None

    # --- Prepared once ---
    raw: Any                           # preprocess.RawPanel
    incumbent: dict[str, Any]          # the live model's metrics, for the promotion gate
    search_space: dict[str, Any]

    # --- Current iteration ---
    iteration: int
    spec: Any                          # spec.TrainingSpec
    spec_changes: list[str]
    spec_rejected: list[str]
    panel: Any                         # processed pd.DataFrame
    provenance: dict[str, Any]
    feature_cols: list[str]
    walk: Any                          # evaluate.WalkForward
    metrics: dict[str, Any]
    rating: dict[str, Any]
    breakdowns: dict[str, Any]
    explain: dict[str, Any]
    consistency: dict[str, Any]
    report: dict[str, Any]

    # --- Agent output for the current round ---
    validation: dict[str, Any]
    pm: dict[str, Any]
    advisor: dict[str, Any]
    researcher: dict[str, Any]

    # --- Accumulating channels (fan-in) ---
    iterations: Annotated[list[dict[str, Any]], operator.add]
    agent_notes: Annotated[list[dict[str, Any]], operator.add]

    # --- Candidates kept for champion selection / ensembling ---
    candidates: Annotated[list[dict[str, Any]], operator.add]

    # --- Control / outcome ---
    stop_reason: str
    champion: dict[str, Any]
    promoted: bool
    promotion_reason: str
    errors: list[dict[str, str]]
    cancelled: bool


def default_config() -> dict[str, Any]:
    """Run configuration. Overridable per run by the REST layer."""
    return {
        # DeepSeek is the server default and the tested path; any registry provider works.
        "reasoning_model": None,          # None -> the provider's registry default
        "structured_model": None,
        "max_iterations": 4,
        # Promotion gate.
        "guarded_metric": "rank_ic_mean",
        "promotion_margin": 0.002,
        "require_pm_accept": True,
        "require_rating_not_worse": True,
        # Loop control.
        "patience": 2,                    # rounds without improvement before stopping
        "enable_ensemble": True,
        "enable_advisor": True,
        # Evaluation cost controls.
        "perturbation_fraction": 0.10,
        # Set true to run the whole loop deterministically with no LLM calls.
        "offline": False,
    }


def get_config(state: "AlphaResearchState") -> dict[str, Any]:
    cfg = default_config()
    cfg.update(state.get("config") or {})
    return cfg


def record_error(state: "AlphaResearchState", stage: str, exc: Exception) -> list[dict[str, str]]:
    """Append a structured error, mirroring ``committee.state.record_error``."""
    errors = list(state.get("errors") or [])
    errors.append({"stage": stage, "type": exc.__class__.__name__, "message": str(exc)[:300]})
    return errors
