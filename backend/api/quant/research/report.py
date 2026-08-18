"""The model validation report, and the leakage-safe packet the agents are shown.

Two audiences, two objects, deliberately not the same object:

* :func:`build_report` produces the full record for the UI, the PDF and the ledger. It may
  contain anything.
* :func:`agent_packet` produces what an LLM is allowed to see. It is built by an **allowlist**
  and then swept for ticker-shaped strings.

The separation exists because of a specific, documented failure mode. Papasotiriou, Sood,
Reynolds & Balch (arXiv:2411.00856, JPMorgan Chase AI Research) deliberately chose a model
whose training cutoff *preceded* their evaluation window, because an LLM that has memorized
market history can leak that knowledge into the analysis and produce results that look like
skill and are recall. Our agents steer a search over an evaluation window well inside every
current model's training data. If a packet named the top-performing tickers of 2023, an agent
could steer the spec toward them for reasons that have nothing to do with the diagnostics —
and the resulting rank-IC would be real, reproducible, and worthless out of sample.

So: aggregates and bucket statistics only. No instrument identifiers, no per-name returns, no
ranked name lists. ``assert_no_leakage`` enforces it and is asserted in the test suite.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence

from .spec import TrainingSpec, summarize

logger = logging.getLogger("mzqa.quant.research.report")

# The guard is EXACT, not heuristic: it looks for the run's actual instrument symbols rather
# than for things that look ticker-shaped. An earlier heuristic version (any 1-5 uppercase
# token) fired on "IG-B" bucket labels, on "LLM" in prose, and on FF factor names — endless
# false positives, each of which would have degraded a legitimate packet. Since the universe
# is known at run time, matching against it is both precise and cheaper.
_WORD_RE = re.compile(r"[A-Za-z0-9.]+")

# The only top-level keys an agent packet may carry.
_PACKET_ALLOWLIST = frozenset({
    "iteration", "market", "horizon", "spec", "spec_changes", "spec_rejected",
    "sample", "metrics", "robustness_rating", "breakdown_summary", "explainability",
    "consistency", "history", "incumbent", "critiques", "search_space",
})


# --------------------------------------------------------------------------- #
# The full report
# --------------------------------------------------------------------------- #
def build_report(
    *,
    iteration: int,
    spec: TrainingSpec,
    jurisdiction: str,
    provenance: dict[str, Any],
    metrics: dict[str, Any],
    rating: dict[str, Any],
    breakdowns: dict[str, Any],
    explain: dict[str, Any],
    consistency: dict[str, Any],
    spec_changes: Sequence[str] = (),
    spec_rejected: Sequence[str] = (),
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Assemble one iteration's validation report, sectioned by quality attribute.

    The sectioning follows Lewis et al.'s ML component quality model (arXiv:2602.05043) —
    functional correctness, ranking quality, economic value, robustness, explainability,
    consistency, monitorability — rather than listing metrics in the order they were
    computed. The point of that survey is that accuracy-only evaluation misses most defect
    classes; a report organized by attribute makes an empty section visible as a gap.
    """
    fc = (metrics or {}).get("functional_correctness", {})
    econ = (metrics or {}).get("economic_value", {})

    headline = {
        "robustness_rating": rating.get("rating"),
        "robustness_label": rating.get("rating_label"),
        "rank_ic": fc.get("rank_ic_mean"),
        "rank_ic_ci95": fc.get("rank_ic_ci95"),
        "rank_icir_annualized": fc.get("rank_icir_annualized"),
        "r2_oos": (fc.get("r2_oos") or {}).get("zero_benchmarked"),
        "long_short_sharpe": econ.get("long_short_sharpe"),
        "turnover": econ.get("turnover"),
        "n_months": fc.get("n_dates"),
    }

    return {
        "iteration": int(iteration),
        "market": jurisdiction,
        "horizon": spec.label,
        "horizon_months": spec.horizon_months,
        "spec": spec.to_dict(),
        "spec_hash": spec.hash(),
        "spec_changes": list(spec_changes),
        "spec_rejected": list(spec_rejected),
        "headline": headline,
        "sections": {
            "functional_correctness": fc,
            "ranking_quality": (metrics or {}).get("ranking_quality", {}),
            "economic_value": econ,
            "robustness": {
                **(metrics or {}).get("robustness", {}),
                "perturbation_rating": rating,
            },
            "explainability": explain,
            "factor_hygiene": {
                "factor_regression": econ.get("factor_regression", {}),
                "factor_neutral_ic": (metrics or {}).get("factor_neutral_ic", {}),
            },
            "consistency": consistency,
            "domain_adaptability": breakdowns,
            "monitorability": provenance,
        },
        "elapsed_seconds": elapsed_seconds,
    }


# --------------------------------------------------------------------------- #
# The agent packet
# --------------------------------------------------------------------------- #
def _bucket_summary(breakdowns: dict[str, Any], max_rows: int = 6) -> dict[str, Any]:
    """Best/worst buckets per cut — aggregate statistics, never the names inside them."""
    out: dict[str, Any] = {}
    for cut, rows in (breakdowns or {}).get("cuts", {}).items():
        usable = [r for r in rows if r.get("rank_ic") is not None]
        if not usable:
            continue
        ordered = sorted(usable, key=lambda r: -(r["rank_ic"] or -9))
        out[cut] = {
            "n_buckets": len(rows),
            "thin_buckets": sum(1 for r in rows if r.get("thin")),
            "best": [{"bucket": r["bucket"], "rank_ic": r["rank_ic"], "n_names": r["n_names"],
                      "thin": r["thin"]} for r in ordered[:max_rows // 2]],
            "worst": [{"bucket": r["bucket"], "rank_ic": r["rank_ic"], "n_names": r["n_names"],
                       "thin": r["thin"]} for r in ordered[-(max_rows // 2):]],
            "spread": (ordered[0]["rank_ic"] - ordered[-1]["rank_ic"]
                       if len(ordered) > 1 else None),
        }
    return out


def agent_packet(
    report: dict[str, Any],
    *,
    history: Sequence[dict[str, Any]] = (),
    incumbent: dict[str, Any] | None = None,
    critiques: dict[str, Any] | None = None,
    search_space: dict[str, Any] | None = None,
    max_history: int = 4,
) -> dict[str, Any]:
    """The allowlisted, leakage-swept view an agent is given.

    Also deliberately compact. Gupta, Sharma & Zhao (arXiv:2412.15386) show long-context
    performance degrading with context length, with task difficulty, and with how far the
    decision-critical information sits from the start of the prompt — including outright
    instruction-following collapse at length. So the packet leads with the headline and the
    spec, keeps only the last few rounds of history, and summarizes bucket tables to
    best/worst rather than shipping every row.
    """
    sections = report.get("sections", {})
    fc = sections.get("functional_correctness", {})
    econ = sections.get("economic_value", {})
    rob = sections.get("robustness", {})
    explain = sections.get("explainability", {})

    packet: dict[str, Any] = {
        # Decision-critical material first.
        "iteration": report.get("iteration"),
        "market": report.get("market"),
        "horizon": report.get("horizon"),
        "metrics": {
            "rank_ic": fc.get("rank_ic_mean"),
            "rank_ic_ci95": fc.get("rank_ic_ci95"),
            "rank_ic_t_stat": fc.get("rank_ic_t_stat"),
            "rank_icir_annualized": fc.get("rank_icir_annualized"),
            "ic_hit_rate": fc.get("ic_hit_rate"),
            "r2_oos_zero_benchmarked": (fc.get("r2_oos") or {}).get("zero_benchmarked"),
            "n_months": fc.get("n_dates"),
            "signal_autocorr": fc.get("signal_autocorr"),
            "decile_spread": sections.get("ranking_quality", {}).get("top_minus_bottom"),
            "decile_spread_tstat": sections.get("ranking_quality", {}).get(
                "top_minus_bottom_tstat"),
            "monotonicity": sections.get("ranking_quality", {}).get("monotonicity"),
            "long_short_sharpe": econ.get("long_short_sharpe"),
            "max_drawdown": econ.get("max_drawdown"),
            "turnover": econ.get("turnover"),
            "factor_regression": econ.get("factor_regression", {}),
            "train_oos_gap": rob.get("train_oos_gap"),
            "worst_year_rank_ic": rob.get("worst_year_rank_ic"),
            "positive_years": rob.get("positive_years"),
            "total_years": rob.get("total_years"),
            "regime_split": rob.get("regime_split", {}),
        },
        "robustness_rating": _rating_summary(rob.get("perturbation_rating", {})),
        "spec": summarize_spec(report.get("spec", {})),
        "spec_changes": report.get("spec_changes", []),
        "spec_rejected": report.get("spec_rejected", []),
        "sample": _sample_summary(sections.get("monitorability", {})),
        "breakdown_summary": _bucket_summary(sections.get("domain_adaptability", {})),
        "explainability": {
            "top_features_by_gain": list(explain.get("gain_importance", {}))[:8],
            "gain_concentration_top5": explain.get("gain_concentration_top5"),
            "importance_stability": explain.get("stability", {}),
        },
        "consistency": sections.get("consistency", {}),
        "history": [_history_row(h) for h in list(history)[-max_history:]],
    }
    if incumbent:
        packet["incumbent"] = incumbent
    if critiques:
        packet["critiques"] = critiques
    if search_space:
        packet["search_space"] = search_space

    packet = {k: v for k, v in packet.items() if k in _PACKET_ALLOWLIST}
    return scrub(packet)


def _rating_summary(rating: dict[str, Any]) -> dict[str, Any]:
    if not rating or not rating.get("available"):
        return {"available": False}
    return {
        "available": True,
        "rating": rating.get("rating"),
        "label": rating.get("rating_label"),
        "scale": rating.get("scale"),
        "worst_case": rating.get("worst_case"),
        "mean_degradation": rating.get("mean_degradation"),
        "per_perturbation": [
            {"id": p.get("id"), "label": p.get("label"),
             "rank_ic_degradation": p.get("rank_ic_degradation"),
             "confounding_share_pct": p.get("confounding_share_pct")}
            for p in rating.get("perturbations", []) if p.get("available")
        ],
    }


def _sample_summary(prov: dict[str, Any]) -> dict[str, Any]:
    return {k: prov.get(k) for k in
            ("rows_out", "features_out", "months", "names", "row_retention",
             "first_month", "last_month") if k in prov}


def _history_row(h: dict[str, Any]) -> dict[str, Any]:
    head = h.get("headline", {})
    return {
        "iteration": h.get("iteration"),
        "changes": h.get("spec_changes", []),
        "rank_ic": head.get("rank_ic"),
        "r2_oos": head.get("r2_oos"),
        "robustness_rating": head.get("robustness_rating"),
        "sharpe": head.get("long_short_sharpe"),
        "turnover": head.get("turnover"),
    }


def summarize_spec(spec_dict: dict[str, Any]) -> dict[str, Any]:
    """The searched knobs only — not the whole dataclass dump."""
    keys = ("model_family", "params", "normalization", "clip_sigma", "fill_missing",
            "winsorize_features", "winsorize_label", "neutralize", "families",
            "include_macro", "feature_selector", "max_features", "min_names_per_date",
            "min_market_cap_usd", "min_feature_coverage", "min_obs_per_name",
            "embargo_months", "valid_frac", "test_frac", "wf_refit_every",
            "wf_min_train_months", "eval_topk", "seed")
    return {k: spec_dict.get(k) for k in keys if k in spec_dict}


# --------------------------------------------------------------------------- #
# Leakage guard
# --------------------------------------------------------------------------- #
def scrub(obj: Any) -> Any:
    """Recursively drop anything that looks like instrument-level identity.

    Belt and braces on top of the allowlist: the allowlist controls which *keys* survive,
    this controls what can hide inside their values (a bucket label that happens to be a
    ticker, a free-text field echoing a name).
    """
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not _is_identity_key(k)}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def _is_identity_key(key: Any) -> bool:
    k = str(key).lower()
    return k in {"ticker", "tickers", "instrument", "instruments", "names_list",
                 "top_names", "holdings", "symbol", "symbols", "primary_ticker"}


def _identifier_values(obj: Any, out: set[str]) -> None:
    """Collect string values that occupy an IDENTIFIER position, never prose.

    A value counts as an identifier if it is a single bare token, or a delimited list of
    bare tokens. Sentences are never split into words.

    This distinction is what makes the guard usable on the real universe. US tickers include
    a lot of ordinary English — ALL, ON, BY, CARE, GAIN, GAP, MAX, REAL, FORM, OUT — so
    scanning the serialized packet for any word that matches a ticker fires on almost every
    sentence an agent writes. Restricting the scan to identifier positions keeps the check
    exact against real symbols while leaving prose alone: `{"best_name": "NVDA"}` is caught,
    "the spread is at its MAX in Q4" is not.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            _identifier_values(value, out)
        return
    if isinstance(obj, (list, tuple, set)):
        for value in obj:
            _identifier_values(value, out)
        return
    if not isinstance(obj, str):
        return
    s = obj.strip()
    if not s or len(s) > 200:
        return
    if _BARE_TOKEN_RE.fullmatch(s):
        out.add(s)
    elif _TOKEN_LIST_RE.fullmatch(s):
        out.update(p.strip() for p in re.split(r"\s*[,;|]\s*", s) if p.strip())


_BARE_TOKEN_RE = re.compile(r"[A-Za-z0-9.\-]{1,12}")
_TOKEN_LIST_RE = re.compile(r"[A-Za-z0-9.\-]{1,12}(\s*[,;|]\s*[A-Za-z0-9.\-]{1,12}){1,50}")


def _known_vocabulary() -> frozenset[str]:
    """Values the report legitimately carries that could collide with a ticker.

    Sourced from the spec's own enums rather than hard-coded, so the exclusion set cannot
    drift out of step with the search space.
    """
    from .spec import (
        VALID_FAMILIES, VALID_FAMILY_MODELS, VALID_FILL, VALID_NEUTRALIZE,
        VALID_NORMALIZATION, VALID_SELECTOR,
    )

    vocab = set()
    for group in (VALID_FAMILIES, VALID_FAMILY_MODELS, VALID_FILL, VALID_NEUTRALIZE,
                  VALID_NORMALIZATION, VALID_SELECTOR):
        vocab |= {str(v).upper() for v in group}
    vocab |= {"PASS", "WARN", "FAIL", "ACCEPT", "REJECT", "CONTINUE", "TRUE", "FALSE",
              "NONE", "NULL", "US", "JP", "INTL", "LLM", "DETERMINISTIC",
              "P1", "P2", "P3", "P4", "P5", "SINGLE", "ENSEMBLE"}
    return frozenset(vocab)


def find_leaks(packet: Any, universe: Iterable[str] | None = None) -> list[str]:
    """Instrument symbols from ``universe`` that survived into ``packet``. Empty means clean.

    Matching against the run's real universe makes this exact rather than heuristic. Called
    by the graph before every agent call, and asserted in the test suite.
    """
    if universe is None:
        return []                      # `not universe` is ambiguous for a pandas Index
    symbols = {str(s).strip().upper() for s in universe if str(s).strip()}
    if not symbols:
        return []
    found: set[str] = set()
    _identifier_values(packet, found)
    tokens = {t.upper() for t in found}
    # Also catch a JP-style bare code inside a suffixed token (e.g. "7203.T" -> "7203").
    tokens |= {t.split(".")[0] for t in tokens}
    return sorted((tokens & symbols) - _known_vocabulary())


def assert_no_leakage(packet: Any, universe: Iterable[str] | None = None) -> None:
    """Raise if instrument identity survived into an agent packet."""
    leaks = find_leaks(packet, universe)
    if leaks:
        raise ValueError(
            f"agent packet contains {len(leaks)} instrument symbol(s): {leaks[:10]} — "
            "instrument identity must never reach an LLM prompt (see module docstring)")
