"""Perturbation battery and the ordinal robustness rating.

Adapted from Lakkaraju et al., *Rating Multi-Modal Time-Series Forecasting Models for
Robustness Through a Causal Lens* (arXiv:2406.12908, JPMorgan Chase AI Research), and its
2025 follow-up on causally grounded rating methods. Four properties of that method are worth
keeping and are kept here:

1. **A perturbation taxonomy tied to real failures**, not arbitrary noise.
2. **Deconfounding against a sensitive attribute** — they use company and industry, and find
   inter-industry discrepancy dominates intra-industry. Industry is the confounder here too.
3. **Worst-case reporting.** They aggregate with MAX rather than mean, because a stakeholder
   choosing a model needs the floor, not the average.
4. **An ordinal rating** (1 best .. 3 worst) as the deliverable, so the output is a decision
   aid rather than a table of floats. It is also *third-party auditable*: nothing here needs
   access to training data or model internals, only the ability to score a perturbed panel.

The perturbations are chosen to mirror this warehouse's actual failure modes rather than the
image-domain ones in the paper (single-pixel, saturation). Each corresponds to something the
SEC/EDINET/Yahoo pipeline is known to do wrong on occasion — see ``docs/data_pipeline`` and
the mapping-reconciliation work in ``docs/sec_edinet_metrics_mapping``.

**On deconfounding method.** The paper uses propensity score matching. PSM estimates a
matching score because its confounder set is high-dimensional; here the confounder is a
single low-cardinality categorical (GICS industry), and for that case exact stratification
(direct standardization) is not an approximation of PSM — it is the exact version of what PSM
approximates. So we stratify, and report the difference between the raw and
industry-standardized effect as the confounding share, which is the same quantity their
``PIE%`` measures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from . import evaluate as ev
from .preprocess import FEATURE, LABEL, LABEL_COL, RawPanel
from .spec import TrainingSpec

logger = logging.getLogger("mzqa.quant.research.perturb")

# Degradation thresholds mapping worst-case relative rank-IC loss onto the 1..3 rating.
_RATING_BANDS = ((0.25, 1), (0.60, 2))          # < 25% -> 1, < 60% -> 2, else 3
_RATING_LABELS = {1: "robust", 2: "moderate", 3: "fragile"}

_DEFAULT_FRACTION = 0.10       # share of feature cells a perturbation touches


@dataclass
class Perturbation:
    """One named intervention on the panel."""

    id: str
    label: str
    stands_for: str
    apply: Callable[[pd.DataFrame, pd.Series, np.random.Generator, float],
                    tuple[pd.DataFrame, pd.Series]]
    target: str = "features"   # features | label | rows


def _drop_to_null(X: pd.DataFrame, y: pd.Series, rng: np.random.Generator,
                  frac: float) -> tuple[pd.DataFrame, pd.Series]:
    """P1 — a share of feature cells goes missing (and is imputed as the panel would)."""
    out = X.copy()
    mask = rng.random(out.shape) < frac
    arr = out.to_numpy(dtype=float)
    arr[mask] = 0.0            # post-normalization the imputed value IS the centre
    return pd.DataFrame(arr, index=out.index, columns=out.columns), y


def _halve(X: pd.DataFrame, y: pd.Series, rng: np.random.Generator,
           frac: float) -> tuple[pd.DataFrame, pd.Series]:
    """P2 — a share of feature cells is scaled by 0.5 (unit / mapping error)."""
    out = X.copy()
    mask = rng.random(out.shape) < frac
    arr = out.to_numpy(dtype=float)
    arr[mask] = arr[mask] * 0.5
    return pd.DataFrame(arr, index=out.index, columns=out.columns), y


def _stale_shift(X: pd.DataFrame, y: pd.Series, rng: np.random.Generator,
                 frac: float) -> tuple[pd.DataFrame, pd.Series]:
    """P3 — every feature is one month stale (reporting-lag slippage).

    Applied to all rows rather than a sample: a lag error in the point-in-time alignment is
    systematic, not sporadic, so a partial application would understate it.
    """
    shifted = X.groupby(level="instrument", group_keys=False).shift(1)
    return shifted.fillna(X), y


def _drop_newest_month(X: pd.DataFrame, y: pd.Series, rng: np.random.Generator,
                       frac: float) -> tuple[pd.DataFrame, pd.Series]:
    """P4 — the most recent month's features are unavailable, so the prior month is reused.

    The live-edge failure: fundamentals for the newest month have not landed yet, which is
    exactly the situation ``predict_cross_section`` is built to survive.
    """
    dts = X.index.get_level_values("datetime")
    newest = dts.max()
    out = X.copy()
    prev = X.groupby(level="instrument", group_keys=False).shift(1)
    rows = dts == newest
    out.loc[rows] = prev.loc[rows].fillna(X.loc[rows])
    return out, y


def _label_outliers(X: pd.DataFrame, y: pd.Series, rng: np.random.Generator,
                    frac: float) -> tuple[pd.DataFrame, pd.Series]:
    """P5 — fat-tail contamination of the LABEL (corporate actions, thin-name blowups).

    Tests the evaluation as much as the model: a spec that did not winsorize its label is
    scored against a target a handful of names now dominate.
    """
    out = y.copy()
    n = len(out)
    k = max(1, int(n * frac * 0.05))       # 0.5% of rows at 10% fraction
    idx = rng.choice(n, size=min(k, n), replace=False)
    scale = out.std(ddof=1) or 1.0
    out.iloc[idx] = out.iloc[idx] + rng.choice([-1.0, 1.0], size=len(idx)) * 20.0 * scale
    return X, out


BATTERY: tuple[Perturbation, ...] = (
    Perturbation("P1", "Feature values dropped",
                 "Missing or late filings, warehouse gaps", _drop_to_null),
    Perturbation("P2", "Feature values halved",
                 "Unit-scale and concept-mapping errors", _halve),
    Perturbation("P3", "Features one month stale",
                 "Point-in-time / reporting-lag slippage", _stale_shift),
    Perturbation("P4", "Newest month unavailable",
                 "Reporting delay at the live edge", _drop_newest_month),
    Perturbation("P5", "Label outlier contamination",
                 "Corporate actions and thin-name blowups", _label_outliers, "label"),
)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _score(model: Any, X: pd.DataFrame, y: pd.Series) -> tuple[float | None, float | None, pd.Series]:
    """(mean rank-IC, zero-benchmarked R2_oos, squared error per row)."""
    if X.empty or model is None:
        return None, None, pd.Series(dtype=float)
    pred = pd.Series(model.model.predict(X.to_numpy(dtype=float)), index=X.index)
    frame = pd.DataFrame({"alpha": pred, "ret": y.reindex(pred.index)})
    _ic, ric = ev._per_date_ic(frame)
    r2 = ev.r2_oos(frame["ret"], frame["alpha"])["zero_benchmarked"]
    resid = (frame["ret"] - frame["alpha"]) ** 2
    return (float(ric.mean()) if len(ric) else None), r2, resid


def _industry_standardized(effect: pd.Series, industry: pd.Series | None) -> float | None:
    """Mean effect after equalizing industry weights (direct standardization).

    With one categorical confounder this is the exact deconfounding that propensity-score
    matching approximates: average within each industry, then average those means with equal
    weight, so a perturbation that happens to concentrate in an over-represented industry no
    longer inherits that industry's share of the panel.
    """
    if effect.empty:
        return None
    if industry is None or industry.empty:
        return float(effect.mean())
    lab = industry.reindex(effect.index.get_level_values("instrument"))
    lab.index = effect.index
    joined = pd.DataFrame({"e": effect, "g": lab}).dropna()
    if joined.empty or joined["g"].nunique() < 2:
        return float(effect.mean())
    return float(joined.groupby("g")["e"].mean().mean())


def run_battery(
    wf: ev.WalkForward,
    panel: pd.DataFrame,
    raw: RawPanel,
    spec: TrainingSpec,
    *,
    feature_cols: Sequence[str] | None = None,
    fraction: float = _DEFAULT_FRACTION,
) -> dict[str, Any]:
    """Score the fitted model under every perturbation and aggregate to a rating.

    No refitting: the model is held fixed and the *data it is served* is degraded, which is
    what makes the result auditable by a third party who has the model but not the training
    pipeline. Cost is one prediction pass per perturbation.
    """
    model = wf.final_model
    if model is None or panel.empty:
        return {"available": False, "reason": "no fitted model to perturb"}

    cols = [str(c) for c in (feature_cols or panel[FEATURE].columns)]
    X = panel[FEATURE].reindex(columns=cols).astype(float).fillna(0.0)
    y = panel[(LABEL, LABEL_COL)].astype(float)

    base_ic, base_r2, base_resid = _score(model, X, y)
    if base_ic is None or abs(base_ic) < 1e-9:
        return {"available": False,
                "reason": "baseline rank-IC is ~0; relative degradation is undefined"}

    industry = None
    if not raw.sectors.empty and "industry_group" in raw.sectors.columns:
        industry = raw.sectors["industry_group"].dropna()
    elif not raw.sectors.empty and "sector" in raw.sectors.columns:
        industry = raw.sectors["sector"].dropna()

    results: list[dict[str, Any]] = []
    for p in BATTERY:
        rng = np.random.default_rng(spec.seed + hash(p.id) % 10_000)
        try:
            Xp, yp = p.apply(X, y, rng, fraction)
            ic, r2, resid = _score(model, Xp, yp)
        except Exception:  # noqa: BLE001 - one perturbation failing must not sink the rating
            logger.warning("perturbation %s failed", p.id, exc_info=True)
            results.append({"id": p.id, "label": p.label, "stands_for": p.stands_for,
                            "available": False})
            continue

        rel = (base_ic - ic) / abs(base_ic) if ic is not None else None
        delta_resid = (resid - base_resid).dropna()
        raw_effect = float(delta_resid.mean()) if len(delta_resid) else None
        std_effect = _industry_standardized(delta_resid, industry)
        # The share of the apparent effect attributable to industry composition rather than
        # to the perturbation itself — the paper's PIE%.
        confounding_pct = None
        if raw_effect is not None and std_effect is not None and abs(raw_effect) > 1e-12:
            confounding_pct = abs(abs(raw_effect) - abs(std_effect)) / abs(raw_effect) * 100.0

        results.append({
            "id": p.id, "label": p.label, "stands_for": p.stands_for, "target": p.target,
            "available": ic is not None,
            "rank_ic": ev._f(ic),
            "rank_ic_degradation": ev._f(rel),
            "r2_oos": ev._f(r2),
            "r2_oos_degradation": ev._f((base_r2 - r2) / abs(base_r2))
            if (base_r2 not in (None, 0) and r2 is not None) else None,
            "mean_error_increase": ev._f(raw_effect),
            "industry_standardized_error_increase": ev._f(std_effect),
            "confounding_share_pct": ev._f(confounding_pct),
            "industry_dispersion": ev._f(_industry_dispersion(delta_resid, industry)),
        })

    return _aggregate(results, base_ic, base_r2, fraction,
                      confounder=("GICS industry group" if industry is not None else None))


def _industry_dispersion(effect: pd.Series, industry: pd.Series | None) -> float | None:
    """Std of the per-industry mean effect.

    The paper's headline empirical finding is that inter-industry discrepancy exceeds
    intra-industry, i.e. a perturbation does not hurt every sector equally. A high dispersion
    here means the aggregate degradation number is hiding a sector that took most of it.
    """
    if industry is None or industry.empty or effect.empty:
        return None
    lab = industry.reindex(effect.index.get_level_values("instrument"))
    lab.index = effect.index
    joined = pd.DataFrame({"e": effect, "g": lab}).dropna()
    if joined["g"].nunique() < 3:
        return None
    return float(joined.groupby("g")["e"].mean().std(ddof=1))


def _aggregate(results: list[dict[str, Any]], base_ic: float, base_r2: float | None,
               fraction: float, *, confounder: str | None) -> dict[str, Any]:
    """Worst-case aggregation to the ordinal 1..3 rating."""
    usable = [r for r in results if r.get("available") and r.get("rank_ic_degradation") is not None]
    if not usable:
        return {"available": False, "reason": "no perturbation produced a usable score",
                "perturbations": results}

    worst = max(usable, key=lambda r: r["rank_ic_degradation"])
    worst_deg = float(worst["rank_ic_degradation"])

    rating = 3
    for threshold, value in _RATING_BANDS:
        if worst_deg < threshold:
            rating = value
            break

    return {
        "available": True,
        "rating": rating,
        "rating_label": _RATING_LABELS[rating],
        "scale": "1 = robust, 2 = moderate, 3 = fragile (worst-case across the battery)",
        "baseline_rank_ic": ev._f(base_ic),
        "baseline_r2_oos": ev._f(base_r2),
        "worst_case": {
            "id": worst["id"], "label": worst["label"],
            "rank_ic_degradation": ev._f(worst_deg),
        },
        "mean_degradation": ev._f(np.mean([r["rank_ic_degradation"] for r in usable])),
        "perturbation_fraction": fraction,
        "confounder": confounder,
        "deconfounding": ("exact stratification on GICS industry group" if confounder
                          else "not applied — no industry classification available"),
        "perturbations": results,
    }
