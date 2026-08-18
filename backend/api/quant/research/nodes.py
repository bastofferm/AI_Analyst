"""LangGraph nodes: the deterministic pipeline, the four agents, and the promotion gate.

Every node follows the committee's contract — take the state, return a partial dict, never
raise. A research run that hits a bad spec, a dead provider or an empty warehouse slice must
degrade to a reported error and keep going, because the whole point of the loop is that later
rounds learn from earlier ones.

LLM plumbing is lifted from ``ai_analyst/committee/nodes.py`` (``_make_structured`` /
``_invoke_structured``) so provider selection, key resolution and the retry-on-malformed
behaviour stay identical across the app. ``MZQA_COMMITTEE_DISABLE_LLM=1`` — or no key, or
``config["offline"]`` — puts the loop on a fully deterministic path in which the Researcher
walks a fixed ladder of spec changes and the critics apply rule-based versions of their
checklists. That path is not a stub: it is a usable no-token grid search, and it is what the
test suite exercises.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from typing import Any, Callable

import numpy as np
import pandas as pd

import llm_providers

from . import evaluate as ev
from . import models as models_mod
from . import perturb as perturb_mod
from . import preprocess as pp
from . import prompts, report as report_mod
from . import spec as spec_mod
from .schemas import (
    AdvisorNote, PMVerdict, SpecPatch, ValidationVerdict,
    normalize_decision, normalize_status,
)
from .spec import TrainingSpec, apply_patch, default_spec
from .state import AlphaResearchState, get_config, record_error

logger = logging.getLogger("mzqa.quant.research.nodes")

_DISABLE_ENV = ("MZQA_COMMITTEE_DISABLE_LLM", "MZQA_RESEARCH_DISABLE_LLM")


# --------------------------------------------------------------------------- #
# LLM plumbing (mirrors ai_analyst.committee.nodes)
# --------------------------------------------------------------------------- #
def _provider(state: AlphaResearchState, *, advisor: bool = False) -> str:
    key = "advisor_provider" if advisor else "provider"
    return llm_providers.normalize_id(state.get(key) or state.get("provider"))


def _resolve_key(state: AlphaResearchState, *, advisor: bool = False) -> str:
    for name in _DISABLE_ENV:
        if os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}:
            return ""
    if get_config(state).get("offline"):
        return ""
    explicit = state.get("advisor_api_key") if advisor else state.get("api_key")
    if advisor and not explicit and not state.get("advisor_provider"):
        explicit = state.get("api_key")      # advisor falls back to the primary credentials
    return (explicit or llm_providers.resolve_env_key(_provider(state, advisor=advisor)) or "").strip()


def _model_for(state: AlphaResearchState, *, advisor: bool = False) -> str:
    prov = _provider(state, advisor=advisor)
    explicit = state.get("advisor_model") if advisor else state.get("model")
    return llm_providers.chat_model(prov, explicit or get_config(state).get("structured_model"))


def _invoke_structured(
    state: AlphaResearchState, schema, system_prompt: str, user_prompt: str, *,
    advisor: bool = False, temperature: float = 0.2, max_tokens: int = 2400, attempts: int = 3,
):
    """Structured call with retries. Returns ``None`` when no key is available."""
    api_key = _resolve_key(state, advisor=advisor)
    if not api_key:
        return None
    from xbrl_sec.llm import make_chat_model, setup_llm_cache

    setup_llm_cache()
    prov = _provider(state, advisor=advisor)
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            llm = make_chat_model(prov, _model_for(state, advisor=advisor),
                                  temperature=temperature, max_tokens=max_tokens,
                                  api_key=api_key)
            result = llm.with_structured_output(schema).invoke(
                f"{system_prompt}\n\n{user_prompt}")
            if result is not None:
                return result
            last = ValueError("structured output returned None")
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("research structured call (%s) attempt %d/%d failed: %s",
                           getattr(schema, "__name__", schema), i + 1, attempts, exc)
        time.sleep(1.2 * (i + 1))
    logger.warning("research structured call gave up: %s", last)
    return None


def _dump(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(obj)


def _progress(state: AlphaResearchState, stage: str) -> None:
    """Report the live stage to the runner, so the UI's poller has something true to show."""
    cb: Callable[[str], None] | None = (state.get("config") or {}).get("_progress")
    if cb:
        try:
            cb(stage)
        except Exception:  # noqa: BLE001 - progress reporting must never break a run
            pass


# --------------------------------------------------------------------------- #
# 1. Prepare (runs once)
# --------------------------------------------------------------------------- #
def prepare_data_node(state: AlphaResearchState) -> dict[str, Any]:
    """Pull the panel and reference maps once; establish the incumbent baseline."""
    _progress(state, "prepare_data")
    jurisdiction = state.get("jurisdiction", "US")
    label = state.get("label", "forward_1m")
    cfg = get_config(state)

    base = default_spec(label, **(cfg.get("spec_overrides") or {}))
    injected = cfg.get("_raw_panel")     # test seam: run the whole graph without a warehouse
    try:
        raw = injected if injected is not None else pp.load_raw(jurisdiction, base)
    except Exception as exc:  # noqa: BLE001
        return {"errors": record_error(state, "prepare_data", exc),
                "stop_reason": f"panel load failed: {type(exc).__name__}: {exc}"}

    if raw.empty:
        return {"stop_reason": "the warehouse returned no usable panel for this market/horizon",
                "raw": raw}

    return {
        "raw": raw,
        "spec": base,
        "iteration": 0,
        "search_space": spec_mod.describe_search_space(),
        "incumbent": _incumbent_baseline(jurisdiction, label),
        "spec_changes": [],
        "spec_rejected": [],
    }


def _incumbent_baseline(jurisdiction: str, label: str) -> dict[str, Any]:
    """The live model's recorded metrics — what a champion must beat to be promoted."""
    try:
        from .. import alpha_signal

        meta = alpha_signal.model_meta(jurisdiction, label)
    except Exception:  # noqa: BLE001
        meta = None
    if not meta:
        return {"available": False, "reason": "no model is currently in production"}
    m = meta.get("metrics") or {}
    return {
        "available": True,
        "trained_at": meta.get("trained_at"),
        "train_range": meta.get("train_range"),
        "n_features": meta.get("n_features"),
        "rank_ic_mean": m.get("rank_ic_mean"),
        "rank_icir": m.get("rank_icir"),
        "n_dates": m.get("n_dates"),
        "note": ("the incumbent's figures come from a single held-out block with no embargo, "
                 "so they are optimistic relative to the purged walk-forward used here"),
    }


# --------------------------------------------------------------------------- #
# 2. Train + evaluate + perturb
# --------------------------------------------------------------------------- #
def train_node(state: AlphaResearchState) -> dict[str, Any]:
    """Build this round's matrix from the spec and run the purged walk-forward."""
    iteration = int(state.get("iteration", 0)) + 1
    _progress(state, f"iteration {iteration}: training")
    raw = state.get("raw")
    spec: TrainingSpec = state.get("spec")
    if raw is None or spec is None or raw.empty:
        return {"iteration": iteration,
                "stop_reason": "no panel available to train on"}

    t0 = time.perf_counter()
    try:
        panel, provenance = pp.apply_spec(raw, spec)
    except Exception as exc:  # noqa: BLE001
        return {"iteration": iteration, "errors": record_error(state, "apply_spec", exc),
                "panel": pd.DataFrame(), "provenance": {"reason": str(exc)}}

    if panel.empty:
        return {"iteration": iteration, "panel": panel, "provenance": provenance,
                "errors": record_error(
                    state, "apply_spec",
                    ValueError(provenance.get("reason", "spec produced an empty panel")))}

    segments = None
    try:
        segments = pp.purged_segments(panel, spec)
    except Exception:  # noqa: BLE001 - only needed to bound feature selection
        pass
    train_end = segments["train"][1] if segments else None
    feature_cols = pp.select_features(panel, spec, train_end=train_end)

    try:
        walk = ev.walk_forward(panel, spec, ret_1m=raw.ret_1m, feature_cols=feature_cols,
                               progress=lambda s: _progress(state, f"iteration {iteration}: {s}"))
    except Exception as exc:  # noqa: BLE001
        return {"iteration": iteration, "panel": panel, "provenance": provenance,
                "errors": record_error(state, "walk_forward", exc)}

    provenance["fit_seconds"] = walk.fit_seconds
    provenance["refits"] = walk.n_refits
    provenance["elapsed_seconds"] = round(time.perf_counter() - t0, 2)
    return {"iteration": iteration, "panel": panel, "provenance": provenance,
            "feature_cols": feature_cols, "walk": walk}


def evaluate_node(state: AlphaResearchState) -> dict[str, Any]:
    """The quality-attribute battery plus both sub-population breakdowns."""
    iteration = int(state.get("iteration", 0))
    _progress(state, f"iteration {iteration}: evaluating")
    walk = state.get("walk")
    panel = state.get("panel")
    raw = state.get("raw")
    spec: TrainingSpec = state.get("spec")
    if walk is None or walk.empty or panel is None or panel.empty:
        return {"metrics": {"available": False, "reason": "no out-of-sample predictions"},
                "breakdowns": {"available": False}, "explain": {"available": False},
                "consistency": {}}

    try:
        metrics = ev.core_metrics(walk, spec, ff=raw.ff_factors)
        metrics["factor_neutral_ic"] = ev.factor_neutral_ic(walk.predictions, raw.ff_betas)
    except Exception as exc:  # noqa: BLE001
        logger.warning("core metrics failed", exc_info=True)
        metrics = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    try:
        breakdowns = ev.all_breakdowns(walk.predictions, raw)
    except Exception:  # noqa: BLE001
        logger.warning("breakdowns failed", exc_info=True)
        breakdowns = {"available": False}

    try:
        explain = ev.explainability(walk, panel, state.get("feature_cols") or [], spec)
    except Exception:  # noqa: BLE001
        logger.warning("explainability failed", exc_info=True)
        explain = {"available": False}

    consistency = {
        "spec_hash": spec.hash(),
        "seed": spec.seed,
        "hyperparameter_stability": ev.hyperparameter_stability(walk),
        "refit_months": walk.refit_months,
    }
    return {"metrics": metrics, "breakdowns": breakdowns, "explain": explain,
            "consistency": consistency}


def perturb_node(state: AlphaResearchState) -> dict[str, Any]:
    """Degrade the data, hold the model fixed, and rate what survives."""
    iteration = int(state.get("iteration", 0))
    _progress(state, f"iteration {iteration}: robustness battery")
    walk, panel, raw, spec = (state.get("walk"), state.get("panel"),
                              state.get("raw"), state.get("spec"))
    if walk is None or walk.empty or panel is None or panel.empty:
        return {"rating": {"available": False, "reason": "nothing to perturb"}}
    try:
        rating = perturb_mod.run_battery(
            walk, panel, raw, spec, feature_cols=state.get("feature_cols"),
            fraction=float(get_config(state).get("perturbation_fraction", 0.10)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("perturbation battery failed", exc_info=True)
        rating = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"rating": rating}


def report_node(state: AlphaResearchState) -> dict[str, Any]:
    """Assemble the iteration report and register the round's candidate model."""
    iteration = int(state.get("iteration", 0))
    spec: TrainingSpec = state.get("spec")
    walk = state.get("walk")
    rep = report_mod.build_report(
        iteration=iteration, spec=spec, jurisdiction=state.get("jurisdiction", "US"),
        provenance=state.get("provenance") or {}, metrics=state.get("metrics") or {},
        rating=state.get("rating") or {}, breakdowns=state.get("breakdowns") or {},
        explain=state.get("explain") or {}, consistency=state.get("consistency") or {},
        spec_changes=state.get("spec_changes") or [],
        spec_rejected=state.get("spec_rejected") or [],
        elapsed_seconds=(state.get("provenance") or {}).get("elapsed_seconds"),
    )
    # Persist the round as soon as it exists: a run that dies in round 4 must not lose
    # rounds 1-3, and the UI's poller needs each round the moment it lands, not at the end.
    cfg = get_config(state)
    on_iteration = cfg.get("_on_iteration")
    if on_iteration:
        try:
            on_iteration(rep)
        except Exception:  # noqa: BLE001 - persistence failure must not sink the run
            logger.warning("iteration persistence hook failed", exc_info=True)

    out: dict[str, Any] = {"report": rep, "iterations": [rep]}
    should_cancel = cfg.get("_should_cancel")
    if should_cancel:
        try:
            if should_cancel():
                out["cancelled"] = True
        except Exception:  # noqa: BLE001
            pass

    if walk is not None and not walk.empty and walk.final_model is not None:
        out["candidates"] = [{
            "iteration": iteration,
            "spec": spec,
            "model": walk.final_model,
            "predictions": walk.predictions[["alpha", "ret"]],
            "headline": rep.get("headline", {}),
        }]
    return out


# --------------------------------------------------------------------------- #
# 3. The critics (parallel) and the researcher
# --------------------------------------------------------------------------- #
def _packet(state: AlphaResearchState, *, with_space: bool = False,
            critiques: dict[str, Any] | None = None) -> dict[str, Any]:
    packet = report_mod.agent_packet(
        state.get("report") or {},
        history=state.get("iterations") or [],
        incumbent=state.get("incumbent"),
        critiques=critiques,
        search_space=state.get("search_space") if with_space else None,
    )
    # Defensive: the allowlist should already guarantee this, but a leak here is the one
    # failure that silently invalidates an entire run's conclusions. Checked against the
    # run's REAL universe, so it cannot false-positive on a bucket label or a factor name.
    raw = state.get("raw")
    universe = None
    if raw is not None and not raw.features.empty:
        universe = raw.features.index.get_level_values("instrument").unique()
    try:
        report_mod.assert_no_leakage(packet, universe)
    except ValueError:
        logger.error("agent packet failed the leakage guard; scrubbing hard", exc_info=True)
        packet = report_mod.scrub(packet)
    return packet


def validation_node(state: AlphaResearchState) -> dict[str, Any]:
    _progress(state, f"iteration {state.get('iteration', 0)}: model validation")
    packet = _packet(state)
    result = _invoke_structured(state, ValidationVerdict, prompts.VALIDATION_SYSTEM,
                                prompts.validation_user(packet), temperature=0.1)
    verdict = _dump(result) if result is not None else _offline_validation(state)
    verdict["status"] = normalize_status(verdict.get("status"))
    verdict["blocking"] = bool(verdict.get("blocking")) or verdict["status"] == "fail"
    verdict["source"] = "llm" if result is not None else "deterministic"
    return {"validation": verdict,
            "agent_notes": [{"role": "validation", "iteration": state.get("iteration"),
                             "content": verdict.get("summary", ""), "verdict": verdict}]}


def pm_node(state: AlphaResearchState) -> dict[str, Any]:
    _progress(state, f"iteration {state.get('iteration', 0)}: portfolio manager")
    packet = _packet(state)
    result = _invoke_structured(state, PMVerdict, prompts.PM_SYSTEM,
                                prompts.pm_user(packet), temperature=0.2)
    verdict = _dump(result) if result is not None else _offline_pm(state)
    verdict["decision"] = normalize_decision(verdict.get("decision"))
    verdict["source"] = "llm" if result is not None else "deterministic"
    return {"pm": verdict,
            "agent_notes": [{"role": "portfolio_manager", "iteration": state.get("iteration"),
                             "content": verdict.get("reasoning", ""), "verdict": verdict}]}


def advisor_node(state: AlphaResearchState) -> dict[str, Any]:
    if not get_config(state).get("enable_advisor", True):
        return {"advisor": {"skipped": True}}
    _progress(state, f"iteration {state.get('iteration', 0)}: external advisor")
    packet = _packet(state)
    result = _invoke_structured(state, AdvisorNote, prompts.ADVISOR_SYSTEM,
                                prompts.advisor_user(packet), advisor=True, temperature=0.5)
    note = _dump(result) if result is not None else _offline_advisor(state)
    note["source"] = "llm" if result is not None else "deterministic"
    note["provider"] = _provider(state, advisor=True) if result is not None else None
    return {"advisor": note,
            "agent_notes": [{"role": "external_advisor", "iteration": state.get("iteration"),
                             "content": note.get("orthogonal_direction", ""), "note": note}]}


def researcher_node(state: AlphaResearchState) -> dict[str, Any]:
    """Synthesize the three critiques into the next spec, via the validated patch applier."""
    _progress(state, f"iteration {state.get('iteration', 0)}: researcher")
    critiques = {
        "model_validation": state.get("validation") or {},
        "portfolio_manager": state.get("pm") or {},
        "external_advisor": state.get("advisor") or {},
    }
    packet = _packet(state, with_space=True, critiques=critiques)
    result = _invoke_structured(state, SpecPatch, prompts.RESEARCHER_SYSTEM,
                                prompts.researcher_user(packet, state.get("search_space") or {}),
                                temperature=0.3, max_tokens=1800)
    proposal = _dump(result) if result is not None else _offline_researcher(state)
    proposal["source"] = "llm" if result is not None else "deterministic"

    spec: TrainingSpec = state.get("spec")
    new_spec, changes, rejected = apply_patch(spec, proposal.get("patch") or {})
    proposal["applied_changes"] = changes
    proposal["rejected"] = rejected

    return {
        "researcher": proposal,
        "spec": new_spec,
        "spec_changes": changes,
        "spec_rejected": rejected,
        "agent_notes": [{"role": "researcher", "iteration": state.get("iteration"),
                         "content": proposal.get("rationale", ""), "proposal": proposal}],
    }


# --------------------------------------------------------------------------- #
# Deterministic fallbacks — a real no-token path, not a stub
# --------------------------------------------------------------------------- #
# A fixed ladder of changes, ordered by how well-motivated they are by the known defects of
# the incumbent pipeline. Walked in order when no LLM is available.
_OFFLINE_LADDER: tuple[dict[str, Any], ...] = (
    {"winsorize_label": 0.01, "fill_missing": "cross_sectional_median"},
    {"normalization": "robust_zscore"},
    {"min_feature_coverage": 0.30, "winsorize_features": 0.01},
    {"model_family": "enet"},
    {"normalization": "rank", "model_family": "lgbm"},
    {"neutralize": ["sector"]},
    {"min_market_cap_usd": 2e8},
)


def _offline_researcher(state: AlphaResearchState) -> dict[str, Any]:
    i = int(state.get("iteration", 1)) - 1
    if i >= len(_OFFLINE_LADDER):
        return {"patch": {}, "rationale": "deterministic ladder exhausted", "stop": True}
    patch = dict(_OFFLINE_LADDER[i])
    return {
        "patch": patch,
        "rationale": ("deterministic ladder step %d — no LLM provider available, walking the "
                      "pre-registered sequence of changes motivated by the incumbent "
                      "pipeline's known defects" % (i + 1)),
        "hypothesis": "robustness-oriented preprocessing should not reduce out-of-sample rank-IC",
        "stop": False,
    }


def _offline_validation(state: AlphaResearchState) -> dict[str, Any]:
    """Rule-based version of the validation checklist. Same tripwires, no prose."""
    rep = state.get("report") or {}
    head = rep.get("headline", {})
    sections = rep.get("sections", {})
    rob = sections.get("robustness", {})
    econ = sections.get("economic_value", {})
    spec: TrainingSpec = state.get("spec")
    prov = sections.get("monitorability", {})
    findings: list[dict[str, Any]] = []

    ci = head.get("rank_ic_ci95") or [None, None]
    if ci[0] is not None and ci[1] is not None and ci[0] <= 0 <= ci[1]:
        findings.append({"category": "statistical_adequacy", "severity": "critical",
                         "detail": f"the rank-IC 95% interval {ci} spans zero",
                         "evidence": "rank_ic_ci95"})
    gap = rob.get("train_oos_gap")
    if gap is not None and gap > 0.05:
        findings.append({"category": "overfitting", "severity": "warn",
                         "detail": f"train-minus-OOS rank-IC gap is {gap:.3f}",
                         "evidence": "train_oos_gap"})
    if spec is not None and spec.resolved_embargo < spec.horizon_months:
        findings.append({"category": "leakage", "severity": "critical",
                         "detail": (f"embargo {spec.resolved_embargo}m is shorter than the "
                                    f"{spec.horizon_months}m label horizon"),
                         "evidence": "spec.embargo_months"})
    retention = prov.get("row_retention")
    if retention is not None and retention < 0.5:
        findings.append({"category": "sample_selection", "severity": "warn",
                         "detail": f"only {retention:.0%} of panel rows survived the filters",
                         "evidence": "sample.row_retention"})
    rating = (rob.get("perturbation_rating") or {}).get("rating")
    if rating == 3:
        findings.append({"category": "robustness", "severity": "warn",
                         "detail": "robustness rating 3 (fragile) under the perturbation battery",
                         "evidence": "robustness_rating"})
    fr = econ.get("factor_regression") or {}
    if fr.get("available") and fr.get("alpha_tstat") is not None and abs(fr["alpha_tstat"]) < 1.0 \
            and (fr.get("r2") or 0) > 0.5:
        findings.append({"category": "factor_mimicry", "severity": "warn",
                         "detail": (f"factor regression alpha t={fr['alpha_tstat']:.2f} with "
                                    f"R2={fr['r2']:.2f} — the spread is factor exposure"),
                         "evidence": "factor_regression"})
    stab = (sections.get("explainability", {}) or {}).get("stability", {})
    if stab.get("available") and not stab.get("stable"):
        findings.append({"category": "instability", "severity": "info",
                         "detail": "feature-importance ranking is not stable across refits",
                         "evidence": "importance_stability"})

    critical = [f for f in findings if f["severity"] == "critical"]
    status = "fail" if critical else ("warn" if findings else "pass")
    return {
        "status": status,
        "summary": (f"{len(findings)} finding(s); "
                    + ("blocking defect present" if critical else "no blocking defect")),
        "findings": findings,
        "blocking": bool(critical),
    }


def _offline_pm(state: AlphaResearchState) -> dict[str, Any]:
    head = (state.get("report") or {}).get("headline", {})
    history = state.get("iterations") or []
    ic = head.get("rank_ic")
    concerns: list[str] = []
    turnover = head.get("turnover")
    if turnover is not None and turnover > 0.8:
        concerns.append(f"turnover {turnover:.0%} is likely to consume the spread")
    if head.get("robustness_rating") == 3:
        concerns.append("fragile under routine data defects")

    decision = "continue"
    if ic is not None and ic <= 0:
        decision = "reject" if len(history) >= 2 else "continue"
    elif len(history) >= int(state.get("max_iterations", 4)):
        decision = "accept" if (ic or 0) > 0 else "reject"

    best = max(history, key=lambda h: (h.get("headline", {}).get("rank_ic") or -9),
               default=None)
    return {
        "decision": decision,
        "reasoning": (f"deterministic gate: rank-IC {ic}, turnover {turnover}, "
                      f"rating {head.get('robustness_rating')}"),
        "preferred_iteration": (best or {}).get("iteration"),
        "concerns": concerns,
    }


def _offline_advisor(state: AlphaResearchState) -> dict[str, Any]:
    i = int(state.get("iteration", 1))
    directions = [
        "The target may be the problem rather than the features — a shorter horizon has a "
        "higher signal-to-noise ratio even though it trades more.",
        "Consider whether the universe, not the model, is doing the work: restricting to "
        "names with a liquid, well-covered fundamental history changes what is being learned.",
        "A heavily regularized linear family would establish whether the GBDT's extra "
        "capacity is buying anything at all on this panel.",
        "Sector neutralization would separate genuine cross-sectional skill from an "
        "industry tilt the risk model already prices.",
    ]
    return {
        "contrarian_read": ("With no LLM available this is a mechanical reading: treat the "
                            "headline as unproven until the confidence interval excludes zero "
                            "and the robustness rating is better than 3."),
        "orthogonal_direction": directions[i % len(directions)],
        "suggested_patch": {},
        "reasoning": "deterministic advisor (no provider configured)",
    }


# --------------------------------------------------------------------------- #
# 4. Champion selection and promotion
# --------------------------------------------------------------------------- #
def select_champion_node(state: AlphaResearchState) -> dict[str, Any]:
    """Pick the best non-vetoed candidate, and test an ensemble of them against it."""
    _progress(state, "selecting champion")
    cfg = get_config(state)
    metric = cfg.get("guarded_metric", "rank_ic_mean")
    candidates = list(state.get("candidates") or [])
    reports = {r["iteration"]: r for r in (state.get("iterations") or [])}

    vetoed = {n["iteration"] for n in (state.get("agent_notes") or [])
              if n.get("role") == "validation" and (n.get("verdict") or {}).get("blocking")}
    def _score(c: dict[str, Any]) -> float:
        return (c.get("headline", {}).get("rank_ic") if metric == "rank_ic_mean"
                else c.get("headline", {}).get(metric)) or -9.0

    eligible = [c for c in candidates if c["iteration"] not in vetoed]
    if not eligible:
        if not candidates:
            return {"champion": {"available": False,
                                 "reason": "no candidate model was produced"}}
        # Every round was vetoed. Still name the best attempt: "the best we found was round 2
        # at rank-IC 0.017, and it cannot ship because its confidence interval spans zero" is
        # a far more useful answer than "no champion", and the report needs something to
        # point at. It is explicitly NOT promotable.
        best_blocked = max(candidates, key=_score)
        return {"champion": {
            "available": False, "promotable": False,
            "reason": "every iteration was vetoed by model validation",
            "best_blocked_iteration": best_blocked["iteration"],
            "best_blocked_score": _score(best_blocked),
            "metric": metric,
            "vetoed_iterations": sorted(vetoed),
        }}

    best = max(eligible, key=_score)
    champion = {
        "available": True, "kind": "single", "iteration": best["iteration"],
        "score": _score(best), "metric": metric,
        "model": best["model"], "spec": best["spec"],
        "vetoed_iterations": sorted(vetoed),
        "eligible_iterations": sorted(c["iteration"] for c in eligible),
    }

    # An ensemble of the survivors competes on the same OOS predictions. Huang et al. found
    # the combination beating every constituent; this checks whether it does so here rather
    # than assuming it.
    if cfg.get("enable_ensemble", True) and len(eligible) >= 2:
        try:
            combo_score, combo = _ensemble_score(eligible)
            if combo is not None and combo_score > champion["score"]:
                champion = {**champion, "kind": "ensemble", "score": combo_score,
                            "model": combo, "spec": best["spec"],
                            "members": [c["iteration"] for c in eligible],
                            "beat_single": True, "single_best_score": _score(best),
                            "single_best_iteration": best["iteration"]}
            else:
                champion["ensemble_score"] = combo_score
                champion["beat_single"] = False
        except Exception:  # noqa: BLE001
            logger.warning("ensemble construction failed", exc_info=True)

    return {"champion": champion}


def _ensemble_score(candidates: list[dict[str, Any]]) -> tuple[float, Any]:
    """Equal-weight, within-date rank-average of the candidates' OOS predictions.

    Rank-averaging *within each month* is the right combiner for a cross-sectional ranking —
    it is scale-free, so a GBDT and an elastic net contribute comparably. (The persisted
    ensemble artifact uses a standardized weighted mean instead, because the serving path
    scores a bare array with no date grouping available; the two agree on ordering closely
    enough that selection here remains meaningful.)
    """
    frames = []
    for c in candidates:
        p = c["predictions"]["alpha"]
        frames.append(p.groupby(level="datetime").rank(pct=True).rename(f"i{c['iteration']}"))
    joined = pd.concat(frames, axis=1).dropna()
    if joined.empty or joined.shape[1] < 2:
        return -9.0, None
    combined = joined.mean(axis=1)
    ret = candidates[0]["predictions"]["ret"].reindex(combined.index)
    frame = pd.DataFrame({"alpha": combined, "ret": ret}).dropna()
    _ic, ric = ev._per_date_ic(frame)
    score = float(ric.mean()) if len(ric) else -9.0
    model = models_mod.build_ensemble([c["model"] for c in candidates])
    return score, model


def promote_node(state: AlphaResearchState) -> dict[str, Any]:
    """Gate the champion against the incumbent, the PM and the validation verdict."""
    _progress(state, "promotion decision")
    cfg = get_config(state)
    champion = state.get("champion") or {}
    if not champion.get("available"):
        return {"promoted": False,
                "promotion_reason": champion.get("reason", "no champion available")}

    pm = state.get("pm") or {}
    reports = {r["iteration"]: r for r in (state.get("iterations") or [])}
    champ_report = reports.get(champion.get("iteration"), {})
    incumbent = state.get("incumbent") or {}
    margin = float(cfg.get("promotion_margin", 0.002))
    score = float(champion.get("score") or -9.0)

    reasons: list[str] = []
    ok = True

    if cfg.get("require_pm_accept", True) and normalize_decision(pm.get("decision")) != "accept":
        ok = False
        reasons.append(f"the portfolio manager did not accept (decision: "
                       f"{normalize_decision(pm.get('decision'))})")

    if incumbent.get("available"):
        base = incumbent.get("rank_ic_mean")
        if base is not None:
            if score <= float(base) + margin:
                ok = False
                reasons.append(f"champion {score:.4f} does not beat the incumbent "
                               f"{float(base):.4f} by the required margin {margin}")
            else:
                reasons.append(f"champion {score:.4f} beats the incumbent {float(base):.4f} "
                               f"by more than {margin}")
        # An incumbent whose figures came from an unpurged split is not a like-for-like
        # comparison; record that rather than pretending the numbers are commensurable.
        reasons.append("note: the incumbent's rank-IC was measured without an embargo, so "
                       "this comparison flatters it")
    else:
        reasons.append("no incumbent in production — promoting the champion establishes one")

    if cfg.get("require_rating_not_worse", True):
        champ_rating = ((champ_report.get("sections", {}).get("robustness", {})
                         .get("perturbation_rating", {})) or {}).get("rating")
        if champ_rating == 3:
            ok = False
            reasons.append("champion is rated 3 (fragile) under the perturbation battery")

    if not ok:
        return {"promoted": False, "promotion_reason": "; ".join(reasons)}

    # A run on an injected panel is by definition a test, and `dry_run` is the explicit
    # opt-out. Neither may write to the production model directory — a model fitted on
    # synthetic data must never become the artifact the optimizer and screener read.
    if cfg.get("dry_run") or cfg.get("_raw_panel") is not None:
        return {"promoted": False,
                "promotion_reason": "; ".join([*reasons,
                                               "promotion gate PASSED but suppressed "
                                               "(dry run / injected panel)"])}

    try:
        path = _persist_champion(state, champion)
    except Exception as exc:  # noqa: BLE001
        return {"promoted": False,
                "promotion_reason": f"promotion gate passed but persistence failed: {exc}",
                "errors": record_error(state, "promote", exc)}
    return {"promoted": True,
            "promotion_reason": "; ".join(reasons) or "promoted",
            "champion": {**champion, "artifact_path": str(path)}}


def _persist_champion(state: AlphaResearchState, champion: dict[str, Any]):
    """Write the champion as an ``AlphaArtifact`` through the existing persistence path."""
    from datetime import datetime, timezone

    from .. import alpha_signal, qlib_alpha

    spec: TrainingSpec = champion["spec"]
    model = champion["model"]
    jurisdiction = state.get("jurisdiction", "US")
    raw = state.get("raw")
    reports = {r["iteration"]: r for r in (state.get("iterations") or [])}
    rep = reports.get(champion.get("iteration"), {})
    head = rep.get("headline", {})

    artifact = qlib_alpha.AlphaArtifact(
        model=model,
        jurisdiction=jurisdiction.upper(),
        label=spec.label,
        families=tuple(spec.families),
        metric_ids=list(raw.metric_ids) if raw is not None else [],
        feature_cols=list(model.feature_cols),
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        train_range=((rep.get("sections", {}).get("monitorability", {}) or {}).get("first_month"),
                     (rep.get("sections", {}).get("monitorability", {}) or {}).get("last_month")),
        metrics={
            "rank_ic_mean": head.get("rank_ic"),
            "rank_icir": head.get("rank_icir_annualized"),
            "n_dates": head.get("n_months"),
            "r2_oos": head.get("r2_oos"),
            "robustness_rating": head.get("robustness_rating"),
            "source": "agentic_research",
            "run_id": state.get("run_id"),
        },
    )
    path = qlib_alpha.save(artifact)
    alpha_signal.clear_cache()
    return path
