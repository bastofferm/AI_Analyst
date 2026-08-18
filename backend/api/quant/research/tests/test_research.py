"""Tests for the agentic alpha-research loop.

Pure numpy/pandas throughout — no database, no network, no LLM. Follows the style of
``api/quant/tests/test_quant.py``: build a synthetic panel with a *known* structure, then
assert the machinery recovers it.

The fixtures matter as much as the assertions here. Two properties of real fundamental data
are reproduced deliberately, because getting either wrong makes a test that passes on data
the production system never sees:

* **Features are persistent.** A company's book-to-market does not resample independently
  each month. An earlier version of these fixtures used i.i.d. features, which made the
  "stale by one month" perturbation destroy all signal for every model and left the
  robustness rating unable to discriminate anything.
* **The signal is weak.** Rank-IC around 0.03 is a genuine monthly-equity edge, so tests
  assert *ordering* (this model beats that one) rather than absolute thresholds that only
  hold for an unrealistically strong planted signal.
"""
from __future__ import annotations

import os
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from api.quant.research import (
    evaluate as ev,
    models as models_mod,
    perturb as perturb_mod,
    preprocess as pp,
    report as report_mod,
)
from api.quant.research.spec import (
    VALID_FAMILY_MODELS, TrainingSpec, apply_patch, default_spec, family_defaults,
)


# --------------------------------------------------------------------------- fixtures
def make_panel(*, seed: int = 11, rho: float = 0.92, n_features: int = 8,
               months: int = 90, names: int = 90, noise: float = 1.0,
               tickers: list[str] | None = None) -> pp.RawPanel:
    """A persistent (AR(1)) feature panel with a planted signal in f0 and f1."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2015-01-31", periods=months, freq="ME")
    inst = tickers or [f"N{i:03d}" for i in range(names)]
    names = len(inst)

    F = np.zeros((months, names, n_features))
    F[0] = rng.normal(size=(names, n_features))
    for t in range(1, months):
        F[t] = rho * F[t - 1] + np.sqrt(1 - rho ** 2) * rng.normal(size=(names, n_features))

    idx = pd.MultiIndex.from_product([dates, inst], names=["datetime", "instrument"])
    X = pd.DataFrame(F.reshape(months * names, n_features), index=idx,
                     columns=[f"f{i}" for i in range(n_features)])
    y = (0.28 * X["f0"] + 0.14 * X["f1"]
         + rng.normal(scale=noise, size=len(idx))).rename("y")
    sectors = pd.DataFrame(
        {"sector": ["Industrials" if i % 3 == 0 else
                    ("Financials" if i % 3 == 1 else "Information Technology")
                    for i in range(names)],
         "industry_group": ["Capital Goods" if i % 4 < 2 else "Banks" for i in range(names)]},
        index=pd.Index(inst, name="instrument"))
    return pp.RawPanel(features=X, label=y, metric_ids=list(X.columns),
                       jurisdiction="US", label_name="forward_1m", sectors=sectors)


def base_spec(**kw) -> TrainingSpec:
    return replace(default_spec("forward_1m"), min_names_per_date=20,
                   wf_min_train_months=24, wf_refit_every=12, **kw)


REGULARIZED = {"num_leaves": 15, "max_depth": 4, "lambda_l1": 5.0, "lambda_l2": 5.0,
               "min_child_samples": 100, "learning_rate": 0.03, "num_boost_round": 300,
               "early_stopping_rounds": 40, "loss": "mse"}
OVERFIT = {"num_leaves": 255, "max_depth": 16, "lambda_l1": 0.0, "lambda_l2": 0.0,
           "min_child_samples": 2, "learning_rate": 0.3, "num_boost_round": 400,
           "early_stopping_rounds": 0, "loss": "mse"}


# --------------------------------------------------------------------------- spec
def test_apply_patch_clamps_and_rejects():
    spec = default_spec("forward_12m")
    new, changes, rejected = apply_patch(spec, {
        "min_names_per_date": 99999,          # above the bound -> clamped
        "normalization": "robust_zscore",     # valid enum
        "fill_missing": "telepathy",          # invalid enum -> rejected
        "os.system": "rm -rf /",              # not a field -> rejected
        "winsorize_label": 0.02,
    })
    assert new.min_names_per_date == 500, "out-of-range value must be clamped, not accepted"
    assert new.normalization == "robust_zscore"
    assert new.fill_missing == spec.fill_missing, "invalid enum must leave the field untouched"
    assert new.winsorize_label == 0.02
    assert any("os.system" in r for r in rejected)
    assert any("fill_missing" in r for r in rejected)
    assert any("min_names_per_date" in r for r in rejected)
    assert changes, "a valid patch must report what it changed"


def test_apply_patch_rejects_unknown_family_and_foreign_hyperparameters():
    spec = default_spec()
    new, _changes, rejected = apply_patch(spec, {"model_family": "transformer"})
    assert new.model_family == "lgbm"
    assert any("model_family" in r for r in rejected)

    # Switching family must swap in that family's defaults and drop foreign keys.
    new2, _c, rejected2 = apply_patch(spec, {"model_family": "enet",
                                             "params": {"alpha": 0.01, "num_leaves": 64}})
    assert new2.model_family == "enet"
    assert "num_leaves" not in new2.params, "a LightGBM key must not reach an elastic net"
    assert new2.params["alpha"] == 0.01
    assert any("num_leaves" in r for r in rejected2)


def test_embargo_defaults_to_the_label_horizon():
    assert default_spec("forward_12m").resolved_embargo == 12
    assert default_spec("forward_1m").resolved_embargo == 1
    assert replace(default_spec("forward_12m"), embargo_months=0).resolved_embargo == 0


def test_spec_hash_is_stable_and_sensitive():
    a = default_spec("forward_1m")
    assert a.hash() == default_spec("forward_1m").hash()
    assert a.hash() != replace(a, winsorize_label=0.01).hash()


# --------------------------------------------------------------------------- preprocessing
def test_robust_normalization_bounds_outlier_influence():
    """The defect this whole knob exists for: one extreme value distorts a z-scored month."""
    raw = make_panel(months=30, names=60)
    raw.features.iloc[3, 0] = 1e6           # one absurd value in month 0

    centres = {}
    for method in ("zscore", "robust_zscore", "rank"):
        panel, _prov = pp.apply_spec(raw, base_spec(normalization=method))
        f0 = panel[("feature", "f0")]
        centres[method] = abs(float(f0.groupby(level="datetime").mean().iloc[0]))

    assert centres["zscore"] > centres["robust_zscore"] * 3, (
        "mean/std normalization should be visibly dragged off centre by the outlier")
    assert centres["robust_zscore"] < 0.05
    assert centres["rank"] < 0.05


def test_label_winsorization_bounds_the_target():
    raw = make_panel(months=30, names=60)
    raw.label.iloc[7] = 500.0
    plain, _ = pp.apply_spec(raw, base_spec())
    clipped, _ = pp.apply_spec(raw, base_spec(winsorize_label=0.01))
    assert plain[("label", "y")].max() > 100
    assert clipped[("label", "y")].max() < 20


def test_coverage_filter_drops_sparse_features_and_median_imputation_runs():
    raw = make_panel(months=30, names=60)
    raw.features["f7"] = np.nan
    raw.features.iloc[:100, raw.features.columns.get_loc("f7")] = 1.0   # ~5% coverage

    kept, _ = pp.apply_spec(raw, base_spec())
    filtered, prov = pp.apply_spec(
        raw, base_spec(min_feature_coverage=0.5, fill_missing="cross_sectional_median"))
    assert kept["feature"].shape[1] == 8
    assert filtered["feature"].shape[1] == 7, "the sparse feature should have been dropped"
    assert not filtered["feature"].isna().any().any()
    assert any(s["stage"] == "min_feature_coverage" for s in prov["stages"])


def test_purged_split_leaves_a_real_gap():
    raw = make_panel(months=90, names=40)
    spec = base_spec(embargo_months=12)
    panel, _ = pp.apply_spec(raw, spec)
    seg = pp.purged_segments(panel, spec)
    train_end = pd.Timestamp(seg["train"][1])
    valid_start = pd.Timestamp(seg["valid"][0])
    gap_months = (valid_start.year - train_end.year) * 12 + (valid_start.month - train_end.month)
    assert gap_months >= 12, f"expected a >=12-month purge, got {gap_months}"


def test_walk_forward_never_trains_on_the_month_it_predicts():
    """The embargo is the whole point; assert it holds at the schedule level."""
    raw = make_panel(months=90, names=40)
    spec = base_spec(embargo_months=12)
    panel, _ = pp.apply_spec(raw, spec)
    for month, cutoff in pp.walk_forward_dates(panel, spec):
        gap = (month.year - cutoff.year) * 12 + (month.month - cutoff.month)
        assert gap == 12, f"train cutoff {cutoff} is not 12 months before {month}"


# --------------------------------------------------------------------------- models
@pytest.mark.parametrize("family", sorted(VALID_FAMILY_MODELS))
def test_every_family_satisfies_the_serving_contract(family):
    """``qlib_alpha.predict`` does ``artifact.model.model.predict(X.values)`` — all six must."""
    raw = make_panel(months=36, names=50)
    spec = base_spec(model_family=family, params=family_defaults(family))
    panel, _ = pp.apply_spec(raw, spec)
    X = panel["feature"]
    y = panel[("label", "y")]
    fitted = models_mod.fit(spec, X, y)

    scores = fitted.model.predict(X.to_numpy(dtype=float))
    assert scores.ndim == 1, "the contract requires flat scores"
    assert scores.shape[0] == len(X)
    assert np.isfinite(scores).all()


def test_component_count_is_clamped_to_available_rank():
    """An agent may propose pcr/pls with more components than the feature set has."""
    raw = make_panel(months=36, names=50, n_features=4)
    spec = base_spec(model_family="pcr", params={"n_components": 64})
    panel, _ = pp.apply_spec(raw, spec)
    fitted = models_mod.fit(spec, panel["feature"], panel[("label", "y")])
    assert fitted.resolved_params["n_components"] <= 4


def test_ensemble_beats_its_anticorrelated_members():
    """The Huang et al. property the champion selector relies on."""
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-31", periods=40, freq="ME"), [f"N{i:02d}" for i in range(60)]],
        names=["datetime", "instrument"])
    rng = np.random.default_rng(3)
    truth = pd.Series(rng.normal(size=len(idx)), index=idx)
    # Two noisy views of the same truth with independent errors.
    a = truth + rng.normal(scale=1.5, size=len(idx))
    b = truth + rng.normal(scale=1.5, size=len(idx))

    def rank_ic(pred):
        frame = pd.DataFrame({"alpha": pred, "ret": truth})
        _ic, ric = ev._per_date_ic(frame)
        return float(ric.mean())

    combo = (a.groupby(level="datetime").rank(pct=True)
             + b.groupby(level="datetime").rank(pct=True)) / 2.0
    assert rank_ic(combo) > max(rank_ic(a), rank_ic(b))


# --------------------------------------------------------------------------- metrics
def test_r2_oos_both_conventions():
    y = pd.Series([0.10, -0.05, 0.02, 0.07])
    perfect = ev.r2_oos(y, y)
    assert perfect["zero_benchmarked"] == pytest.approx(1.0)
    assert perfect["mean_benchmarked"] == pytest.approx(1.0)

    zero = ev.r2_oos(y, pd.Series([0.0, 0.0, 0.0, 0.0]))
    # Predicting zero scores exactly 0 on the zero-benchmarked convention, and NEGATIVE on
    # the mean-benchmarked one. Conflating the two is how implausible R2 figures get quoted.
    assert zero["zero_benchmarked"] == pytest.approx(0.0)
    assert zero["mean_benchmarked"] < 0.0


def test_icir_annualization_is_sqrt_12():
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-31", periods=48, freq="ME"), [f"N{i:02d}" for i in range(40)]],
        names=["datetime", "instrument"])
    rng = np.random.default_rng(1)
    ret = pd.Series(rng.normal(size=len(idx)), index=idx)
    preds = pd.DataFrame({"alpha": ret * 0.3 + rng.normal(size=len(idx)), "ret": ret})
    wf = ev.WalkForward(predictions=preds)
    fc = ev.core_metrics(wf, base_spec())["functional_correctness"]
    assert fc["rank_icir_annualized"] == pytest.approx(
        fc["rank_icir"] * np.sqrt(12.0), rel=1e-6)


def test_breakdown_recovers_known_per_group_skill():
    """One group carries the signal, the other is noise; the table must say so."""
    dates = pd.date_range("2020-01-31", periods=36, freq="ME")
    good = [f"G{i:02d}" for i in range(40)]
    bad = [f"B{i:02d}" for i in range(40)]
    idx = pd.MultiIndex.from_product([dates, good + bad], names=["datetime", "instrument"])
    rng = np.random.default_rng(5)
    alpha = pd.Series(rng.normal(size=len(idx)), index=idx)
    is_good = idx.get_level_values("instrument").str.startswith("G")
    ret = pd.Series(np.where(is_good, alpha * 2.0, 0.0), index=idx) + \
        pd.Series(rng.normal(scale=0.5, size=len(idx)), index=idx)
    preds = pd.DataFrame({"alpha": alpha, "ret": ret})
    groups = pd.Series(["skilled" if g else "noise" for g in is_good],
                       index=idx.get_level_values("instrument")).groupby(level=0).first()

    rows = {r["bucket"]: r for r in ev.breakdown(preds, groups, name="synthetic")}
    assert rows["skilled"]["rank_ic"] > 0.5
    assert abs(rows["noise"]["rank_ic"]) < 0.15
    assert rows["skilled"]["thin"] is False


def test_thin_buckets_are_flagged_not_hidden():
    dates = pd.date_range("2020-01-31", periods=24, freq="ME")
    inst = [f"N{i:02d}" for i in range(40)]
    idx = pd.MultiIndex.from_product([dates, inst], names=["datetime", "instrument"])
    rng = np.random.default_rng(7)
    preds = pd.DataFrame({"alpha": rng.normal(size=len(idx)),
                          "ret": rng.normal(size=len(idx))}, index=idx)
    # 5 names in one bucket, 35 in the other.
    groups = pd.Series(["tiny" if i < 5 else "big" for i in range(40)],
                       index=pd.Index(inst, name="instrument"))
    rows = {r["bucket"]: r for r in ev.breakdown(preds, groups, name="synthetic")}
    assert rows["tiny"]["thin"] is True
    assert rows["big"]["thin"] is False


def test_overfitting_shows_up_as_a_train_oos_gap():
    raw = make_panel(months=90, names=90)
    gaps = {}
    for name, params in (("regularized", REGULARIZED), ("overfit", OVERFIT)):
        spec = base_spec(params=params)
        panel, _ = pp.apply_spec(raw, spec)
        wf = ev.walk_forward(panel, spec)
        gaps[name] = ev.core_metrics(wf, spec)["robustness"]["train_oos_gap"]
    assert gaps["overfit"] > gaps["regularized"] * 3, (
        f"the overfit model should show a much larger train-OOS gap; got {gaps}")


# --------------------------------------------------------------------------- robustness rating
def test_rating_prefers_the_regularized_model_over_the_overfit_one():
    raw = make_panel(months=90, names=90)
    out = {}
    for name, params in (("regularized", REGULARIZED), ("overfit", OVERFIT)):
        spec = base_spec(params=params)
        panel, _ = pp.apply_spec(raw, spec)
        wf = ev.walk_forward(panel, spec)
        out[name] = perturb_mod.run_battery(wf, panel, raw, spec)

    assert out["regularized"]["rating"] <= out["overfit"]["rating"]
    assert out["regularized"]["mean_degradation"] < out["overfit"]["mean_degradation"]
    assert len(out["overfit"]["perturbations"]) == len(perturb_mod.BATTERY)
    assert out["regularized"]["confounder"] == "GICS industry group"


def test_rating_is_reproducible_for_a_fixed_seed():
    raw = make_panel(months=60, names=60)
    spec = base_spec(params=REGULARIZED)
    panel, _ = pp.apply_spec(raw, spec)
    wf = ev.walk_forward(panel, spec)
    a = perturb_mod.run_battery(wf, panel, raw, spec)
    b = perturb_mod.run_battery(wf, panel, raw, spec)
    assert a["rating"] == b["rating"]
    assert a["mean_degradation"] == pytest.approx(b["mean_degradation"], rel=1e-9)


# --------------------------------------------------------------------------- leakage guard
def test_agent_packet_carries_no_instrument_identity():
    """The guard that keeps memorized market knowledge out of the search."""
    tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "JPM", "XOM"] + \
              [f"N{i:03d}" for i in range(40)]
    raw = make_panel(months=60, names=len(tickers), tickers=tickers)
    spec = base_spec()
    panel, prov = pp.apply_spec(raw, spec)
    wf = ev.walk_forward(panel, spec)
    metrics = ev.core_metrics(wf, spec)
    rating = perturb_mod.run_battery(wf, panel, raw, spec)
    breakdowns = ev.all_breakdowns(wf.predictions, raw)

    rep = report_mod.build_report(
        iteration=1, spec=spec, jurisdiction="US", provenance=prov, metrics=metrics,
        rating=rating, breakdowns=breakdowns, explain={}, consistency={})
    packet = report_mod.agent_packet(rep, history=[rep])

    universe = raw.features.index.get_level_values("instrument").unique()
    assert report_mod.find_leaks(packet, universe) == []
    report_mod.assert_no_leakage(packet, universe)

    # ...and the guard must actually be capable of catching one.
    poisoned = {**packet, "metrics": {"best_name": "NVDA"}}
    assert "NVDA" in report_mod.find_leaks(poisoned, universe)
    with pytest.raises(ValueError):
        report_mod.assert_no_leakage(poisoned, universe)


def test_leakage_guard_tolerates_prose_containing_real_ticker_words():
    """Real US symbols include ordinary English; agent prose must not trip the guard.

    ALL, ON, BY, CARE, GAIN, GAP, MAX, REAL, FORM and OUT are all live tickers. A guard that
    scans serialized text for any word matching the universe fires on essentially every
    sentence an agent writes, which is how the first version of this behaved against the
    real 5,000-name universe.
    """
    universe = ["ALL", "ON", "BY", "CARE", "GAIN", "GAP", "MAX", "REAL", "FORM", "OUT", "NVDA"]
    prose = {
        "critiques": {
            "portfolio_manager": {
                "reasoning": "Turnover is at its MAX in Q4 and the GAIN is driven BY a GAP "
                             "in coverage; ALL sectors need CARE before we act ON this.",
                "concerns": ["the spread does not look REAL after costs"],
            },
        },
        "metrics": {"max_drawdown": -0.2, "turnover": 0.74},
        "spec": {"normalization": "rank", "model_family": "lgbm"},
    }
    assert report_mod.find_leaks(prose, universe) == []

    # A genuine identifier in a data position must still be caught.
    assert report_mod.find_leaks({**prose, "top": ["NVDA", "GAP"]}, universe) == ["GAP", "NVDA"]


def test_scrub_removes_identity_keys():
    dirty = {"a": 1, "tickers": ["AAPL"], "nested": {"holdings": ["MSFT"], "keep": 2}}
    clean = report_mod.scrub(dirty)
    assert "tickers" not in clean
    assert "holdings" not in clean["nested"]
    assert clean["nested"]["keep"] == 2


# --------------------------------------------------------------------------- the graph
def test_graph_runs_offline_end_to_end_and_never_promotes():
    """Two full rounds, four agents each, no tokens spent and no artifact written."""
    from api.quant.research import graph as graph_mod

    os.environ["MZQA_RESEARCH_DISABLE_LLM"] = "1"
    try:
        raw = make_panel(months=90, names=90)
        final = graph_mod.run_research(
            "US", "forward_1m", max_iterations=2,
            config={"offline": True, "_raw_panel": raw})
    finally:
        os.environ.pop("MZQA_RESEARCH_DISABLE_LLM", None)

    reports = final["iterations"]
    assert len(reports) == 2
    for rep in reports:
        assert rep["headline"]["rank_ic"] is not None
        assert rep["sections"]["robustness"]["perturbation_rating"]["available"]
        assert rep["sections"]["domain_adaptability"]["available"]
        assert rep["spec_hash"]

    roles = {n["role"] for n in final["agent_notes"]}
    assert roles == {"validation", "portfolio_manager", "external_advisor", "researcher"}

    # Round 2 must differ from round 1 — a loop that proposes nothing is not a loop.
    assert reports[1]["spec_hash"] != reports[0]["spec_hash"]
    assert reports[1]["spec_changes"]

    assert final["champion"]["available"]
    # An injected panel is a test by definition; nothing may reach the production artifact.
    assert final["promoted"] is False
    assert "suppressed" in final["promotion_reason"]


def test_validation_vetoes_block_promotion():
    """A blocking verdict must remove that round from champion selection."""
    from api.quant.research import nodes

    state = {
        "candidates": [
            {"iteration": 1, "headline": {"rank_ic": 0.9}, "model": object(),
             "spec": default_spec(), "predictions": pd.DataFrame()},
            {"iteration": 2, "headline": {"rank_ic": 0.1}, "model": object(),
             "spec": default_spec(), "predictions": pd.DataFrame()},
        ],
        "iterations": [{"iteration": 1}, {"iteration": 2}],
        "agent_notes": [
            {"role": "validation", "iteration": 1, "verdict": {"blocking": True}},
            {"role": "validation", "iteration": 2, "verdict": {"blocking": False}},
        ],
        "config": {"enable_ensemble": False},
    }
    champion = nodes.select_champion_node(state)["champion"]
    assert champion["iteration"] == 2, "the vetoed round must not win despite a better score"
    assert champion["vetoed_iterations"] == [1]
