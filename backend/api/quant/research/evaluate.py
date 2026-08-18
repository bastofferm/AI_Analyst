"""Purged walk-forward scoring and the quality-attribute metric battery.

The incumbent pipeline reports one number the UI calls "Model skill": the rank-IC of a
single held-out block that shares a boundary with training. This module replaces it with an
expanding-window, embargoed, out-of-sample evaluation and a battery organized by *quality
attribute* rather than by whatever was easy to compute.

The attribute framing follows Lewis et al. (arXiv:2602.05043), whose survey finds ~19% of ML
testing looks past predictive accuracy — and that multi-attribute testing surfaces a larger
and more diverse set of defects. For a cross-sectional return model the attributes that
carry meaning are: functional correctness, ranking quality, economic value, robustness,
explainability, factor hygiene, consistency and monitorability. Each is a section here and a
section in the report.

Two metric choices worth naming:

* ``r2_oos`` uses the zero-benchmarked convention ``1 - SSE / sum(y^2)`` standard in the
  return-prediction literature (Gu-Kelly-Xiu; the form Kang et al. report). The
  mean-benchmarked variant is reported alongside because they diverge sharply when the mean
  return is non-trivial, and quoting one without saying which is how implausible R² numbers
  get published.
* ``rank_icir_annualized`` scales by sqrt(12). ``qlib_alpha.evaluate`` returns a bare
  mean/std ratio labelled ICIR, which is not comparable to the annualized information ratio
  the name implies. Both are reported; the raw key keeps its meaning for existing callers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from .. import qlib_backtest, qlib_data
from . import models as models_mod
from . import preprocess as pp
from .preprocess import FEATURE, LABEL, LABEL_COL
from .spec import TrainingSpec

logger = logging.getLogger("mzqa.quant.research.evaluate")

_MIN_NAMES_FOR_IC = 10
_THIN_BUCKET_NAMES = 30      # below this a bucket's numbers are flagged, not trusted
_BOOTSTRAP_DRAWS = 400


# --------------------------------------------------------------------------- #
# Walk-forward out-of-sample prediction
# --------------------------------------------------------------------------- #
@dataclass
class WalkForward:
    """OOS predictions plus the per-refit diagnostics stability is measured from."""

    predictions: pd.DataFrame                     # (datetime, instrument) -> alpha, ret
    importances: list[pd.Series] = field(default_factory=list)
    refit_months: list[str] = field(default_factory=list)
    best_iterations: list[int] = field(default_factory=list)
    final_model: Any = None
    train_predictions: pd.DataFrame | None = None
    fit_seconds: float = 0.0
    n_refits: int = 0

    @property
    def empty(self) -> bool:
        return self.predictions.empty


def walk_forward(
    panel: pd.DataFrame,
    spec: TrainingSpec,
    *,
    ret_1m: pd.Series | None = None,
    feature_cols: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> WalkForward:
    """Expanding-window OOS predictions with the label horizon embargoed.

    At each prediction month *t* the model may only be fit on rows whose label was already
    realized by ``t - embargo``. With a ``forward_12m`` label that means a 2024-06 prediction
    is fit on data through 2023-06 — the constraint a live deployment actually operates
    under, and the one today's contiguous split violates.

    Refits every ``wf_refit_every`` months (annual by default, per Kang et al.'s expanding
    window) rather than monthly, which is both realistic and ~12x cheaper.
    """
    if panel.empty:
        return WalkForward(pd.DataFrame())

    cols = [str(c) for c in (feature_cols or panel[FEATURE].columns)]
    X = panel[FEATURE].reindex(columns=cols).astype(float)
    y = panel[(LABEL, LABEL_COL)].astype(float)
    dt = panel.index.get_level_values("datetime")

    schedule = pp.walk_forward_dates(panel, spec)
    if not schedule:
        return WalkForward(pd.DataFrame())

    parts: list[pd.DataFrame] = []
    train_parts: list[pd.DataFrame] = []
    importances: list[pd.Series] = []
    refit_months: list[str] = []
    best_iters: list[int] = []
    fitted: models_mod.FittedModel | None = None
    total_seconds = 0.0
    since_refit = 0

    for i, (month, cutoff) in enumerate(schedule):
        month_mask = dt == month
        if int(month_mask.sum()) < _MIN_NAMES_FOR_IC:
            continue
        need_refit = fitted is None or since_refit >= spec.wf_refit_every
        if need_refit:
            train_mask = dt <= cutoff
            if int(train_mask.sum()) < 100:
                continue
            # Carve the tail of the (already embargoed) training window as an early-stopping
            # block. Without it LightGBM always runs the full num_boost_round, best_iteration
            # is never set, and the hyperparameter-stability diagnostic — dispersion of the
            # chosen complexity across windows, which Kang et al. read as a signal-to-noise
            # measure — has nothing to measure. The split is chronological and stays inside
            # the training window, so it introduces no lookahead.
            fit_X, fit_y = X[train_mask], y[train_mask]
            val_X = val_y = None
            tr_dates = pd.Index(sorted(fit_X.index.get_level_values("datetime").unique()))
            n_val = int(round(len(tr_dates) * spec.valid_frac))
            if n_val >= 2 and len(tr_dates) - n_val >= 12:
                split_at = tr_dates[len(tr_dates) - n_val]
                inner = fit_X.index.get_level_values("datetime")
                val_X, val_y = fit_X[inner >= split_at], fit_y[inner >= split_at]
                fit_X, fit_y = fit_X[inner < split_at], fit_y[inner < split_at]
            try:
                fitted = models_mod.fit(spec, fit_X, fit_y, X_valid=val_X, y_valid=val_y)
            except Exception:  # noqa: BLE001 - a bad spec must degrade, not crash the run
                logger.warning("walk-forward fit failed at %s", month, exc_info=True)
                continue
            total_seconds += fitted.fit_seconds
            imp = fitted.importance()
            if not imp.empty:
                importances.append(imp)
            refit_months.append(str(pd.Timestamp(month).date()))
            if fitted.best_iteration:
                best_iters.append(int(fitted.best_iteration))
            since_refit = 0
            if progress:
                progress(f"walk-forward refit {len(refit_months)} @ {refit_months[-1]}")

            # Score the fit window itself, once per refit. The train-minus-OOS IC gap is the
            # single clearest overfitting tell, and the Model Validation agent keys on it —
            # without this it is permanently None and that check silently never fires.
            # Sampled, because scoring an expanding window every refit is otherwise the most
            # expensive thing in the loop for a diagnostic that needs only a stable estimate.
            probe = (fit_X if len(fit_X) <= 20000
                     else fit_X.sample(20000, random_state=spec.seed))
            train_parts.append(pd.DataFrame(
                {"alpha": fitted.model.predict(probe.to_numpy(dtype=float)),
                 "ret": fit_y.reindex(probe.index).to_numpy(dtype=float)},
                index=probe.index))

        since_refit += 1
        block = X[month_mask]
        parts.append(pd.DataFrame(
            {"alpha": fitted.model.predict(block.to_numpy(dtype=float)),
             "ret": y[month_mask].to_numpy(dtype=float)},
            index=block.index))

    preds = pd.concat(parts) if parts else pd.DataFrame()
    train_preds = (pd.concat(train_parts).groupby(level=["datetime", "instrument"]).last()
                   if train_parts else None)
    if not preds.empty:
        # Realized ONE-month return per name for P&L, decoupled from the ranking horizon —
        # the same correction qlib_backtest applies so a 12m signal is not compounded on
        # overlapping windows. Supplied by the caller from the once-per-run RawPanel.
        preds["ret_1m"] = (ret_1m.reindex(preds.index)
                           if ret_1m is not None and not ret_1m.empty else np.nan)

    return WalkForward(
        predictions=preds, importances=importances, refit_months=refit_months,
        best_iterations=best_iters, final_model=fitted, fit_seconds=round(total_seconds, 2),
        n_refits=len(refit_months), train_predictions=train_preds,
    )


# --------------------------------------------------------------------------- #
# Core statistics
# --------------------------------------------------------------------------- #
def _per_date_ic(preds: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Pearson and Spearman IC per date, dropping months too thin to be meaningful."""
    if preds.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    ic, ric = {}, {}
    for dt, g in preds.groupby(level="datetime"):
        g = g[["alpha", "ret"]].dropna()
        if len(g) < _MIN_NAMES_FOR_IC or g["alpha"].nunique() < 2:
            continue
        ic[dt] = float(g["alpha"].corr(g["ret"]))
        ric[dt] = float(g["alpha"].corr(g["ret"], method="spearman"))
    return (pd.Series(ic, dtype=float).sort_index(),
            pd.Series(ric, dtype=float).sort_index())


def _bootstrap_ci(series: pd.Series, draws: int = _BOOTSTRAP_DRAWS,
                  seed: int = 0) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI for the mean of a per-month series.

    Gupta et al. (arXiv:2412.15386) make the point plainly: a headline number without an
    interval invites over-reading. A rank-IC of 0.03 over 20 months and one over 200 months
    are not the same claim.
    """
    vals = series.dropna().to_numpy(dtype=float)
    n = len(vals)
    if n < 8:
        return None, None
    rng = np.random.default_rng(seed)
    means = vals[rng.integers(0, n, size=(draws, n))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _f(x: Any) -> float | None:
    """Finite float or None — NaN/inf are invalid JSON and invalid float8."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def r2_oos(y_true: pd.Series | np.ndarray, y_pred: pd.Series | np.ndarray) -> dict[str, float | None]:
    """Out-of-sample R², both conventions.

    ``zero_benchmarked`` compares against a zero-return forecast (Gu-Kelly-Xiu; the form
    Kang et al. report) and is the one to quote for return prediction — the historical mean
    is itself a fitted quantity and using it flatters the model. ``mean_benchmarked`` is the
    textbook R² and is reported so the two are never confused.
    """
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(yt) & np.isfinite(yp)
    yt, yp = yt[mask], yp[mask]
    if yt.size < 3:
        return {"zero_benchmarked": None, "mean_benchmarked": None}
    sse = float(((yt - yp) ** 2).sum())
    ss0 = float((yt ** 2).sum())
    ssm = float(((yt - yt.mean()) ** 2).sum())
    return {
        "zero_benchmarked": _f(1.0 - sse / ss0) if ss0 > 0 else None,
        "mean_benchmarked": _f(1.0 - sse / ssm) if ssm > 0 else None,
    }


def _quantile_profile(preds: pd.DataFrame, n_q: int = 10) -> dict[str, Any]:
    """Decile means, the top-minus-bottom spread with a t-stat, and monotonicity.

    Monotonicity — the Spearman correlation between decile index and mean realized return —
    is the check a headline spread cannot give you: a signal can post a large top-minus-
    bottom gap while being noise in between, which is a much weaker claim than a monotone
    ordering across the whole cross-section.
    """
    if preds.empty:
        return {"available": False}
    pnl_col = "ret_1m" if ("ret_1m" in preds.columns and preds["ret_1m"].notna().any()) else "ret"
    rows: list[pd.Series] = []
    for _dt, g in preds.groupby(level="datetime"):
        g = g[["alpha", pnl_col]].dropna()
        if len(g) < max(n_q * 2, _MIN_NAMES_FOR_IC):
            continue
        try:
            q = pd.qcut(g["alpha"].rank(method="first"), n_q, labels=False, duplicates="drop")
        except ValueError:
            continue
        rows.append(g[pnl_col].groupby(q).mean())
    if not rows:
        return {"available": False}

    by_q = pd.concat(rows, axis=1).T                       # months x deciles
    means = by_q.mean(axis=0)
    spread_series = (by_q[by_q.columns.max()] - by_q[by_q.columns.min()]).dropna()
    n = len(spread_series)
    t_stat = (float(spread_series.mean() / (spread_series.std(ddof=1) / math.sqrt(n)))
              if n > 2 and spread_series.std(ddof=1) > 0 else None)
    mono = (float(pd.Series(means.index, index=means.index).corr(means, method="spearman"))
            if len(means) > 2 else None)
    top = by_q[by_q.columns.max()].dropna()
    return {
        "available": True,
        "n_quantiles": int(len(means)),
        "decile_mean_returns": [_f(v) for v in means.tolist()],
        "top_minus_bottom": _f(spread_series.mean()),
        "top_minus_bottom_tstat": _f(t_stat),
        "monotonicity": _f(mono),
        "top_decile_hit_rate": _f((top > 0).mean()) if len(top) else None,
        "n_months": int(n),
    }


def _turnover(preds: pd.DataFrame, topk: int) -> float | None:
    """Fraction of the top-k book replaced month to month.

    The economic gate the PM cares about: a spread that only exists at 90% monthly turnover
    is a costs story, not an alpha story.
    """
    months = sorted(preds.index.get_level_values("datetime").unique())
    if len(months) < 2:
        return None
    prev: set[str] | None = None
    churn: list[float] = []
    for m in months:
        g = preds.xs(m, level="datetime")["alpha"].dropna()
        if len(g) < topk:
            continue
        cur = set(g.nlargest(topk).index)
        if prev is not None and prev:
            churn.append(1.0 - len(cur & prev) / float(topk))
        prev = cur
    return _f(np.mean(churn)) if churn else None


def _by_year(ric: pd.Series) -> dict[str, Any]:
    if ric.empty:
        return {}
    year = pd.Series(ric.index.year, index=ric.index)
    grouped = ric.groupby(year).mean()
    return {
        "rank_ic_by_year": {str(int(k)): _f(v) for k, v in grouped.items()},
        "worst_year": str(int(grouped.idxmin())) if len(grouped) else None,
        "worst_year_rank_ic": _f(grouped.min()) if len(grouped) else None,
        "positive_years": int((grouped > 0).sum()),
        "total_years": int(len(grouped)),
    }


def _regime_split(ric: pd.Series, ff: pd.DataFrame | None) -> dict[str, Any]:
    """Rank-IC in up- vs down-market months, split on the sign of the FF market factor."""
    if ric.empty:
        return {"available": False}
    if ff is None or ff.empty or "mkt_rf" not in ff.columns:
        return {"available": False, "reason": "no Fama-French factors for this market"}
    common = ric.index.intersection(ff.index)
    if len(common) < 8:
        return {"available": False, "reason": "too few overlapping months"}
    mkt = ff.loc[common, "mkt_rf"]
    up, down = ric.loc[common][mkt > 0], ric.loc[common][mkt <= 0]
    return {
        "available": True,
        "up_market_rank_ic": _f(up.mean()), "up_months": int(len(up)),
        "down_market_rank_ic": _f(down.mean()), "down_months": int(len(down)),
        "gap": _f(up.mean() - down.mean()) if len(up) and len(down) else None,
    }


def core_metrics(
    wf: WalkForward,
    spec: TrainingSpec,
    *,
    ff: pd.DataFrame | None = None,
    train_preds: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """The functional-correctness / ranking / economic / robustness sections."""
    preds = wf.predictions
    if preds.empty:
        return {"available": False, "reason": "no out-of-sample predictions"}

    ic, ric = _per_date_ic(preds)
    n_dates = int(len(ric))
    ic_std = float(ic.std(ddof=1)) if len(ic) > 1 else 0.0
    ric_std = float(ric.std(ddof=1)) if len(ric) > 1 else 0.0
    icir = (ic.mean() / ic_std) if ic_std else 0.0
    ricir = (ric.mean() / ric_std) if ric_std else 0.0
    lo, hi = _bootstrap_ci(ric, seed=spec.seed)

    t_stat = (float(ric.mean() / (ric_std / math.sqrt(n_dates)))
              if n_dates > 2 and ric_std > 0 else None)
    p_value = None
    if t_stat is not None:
        try:
            from scipy import stats

            p_value = float(2.0 * stats.t.sf(abs(t_stat), df=max(1, n_dates - 1)))
        except Exception:  # noqa: BLE001
            p_value = None

    if train_preds is None:
        train_preds = wf.train_predictions   # collected during the walk-forward refits

    out: dict[str, Any] = {
        "available": True,
        "functional_correctness": {
            "ic_mean": _f(ic.mean()), "ic_std": _f(ic_std),
            "rank_ic_mean": _f(ric.mean()), "rank_ic_std": _f(ric_std),
            "icir": _f(icir), "rank_icir": _f(ricir),
            "icir_annualized": _f(icir * math.sqrt(12.0)),
            "rank_icir_annualized": _f(ricir * math.sqrt(12.0)),
            "rank_ic_t_stat": _f(t_stat), "rank_ic_p_value": _f(p_value),
            "rank_ic_ci95": [_f(lo), _f(hi)],
            "ic_hit_rate": _f((ric > 0).mean()) if n_dates else None,
            "signal_autocorr": _f(_signal_autocorr(preds)),
            "n_dates": n_dates,
            "r2_oos": r2_oos(preds["ret"], preds["alpha"]),
        },
        "ranking_quality": _quantile_profile(preds),
        "robustness": {
            **_by_year(ric),
            "regime_split": _regime_split(ric, ff),
        },
    }

    # Train-vs-OOS gap: the overfitting tell the Validation agent keys on.
    if train_preds is not None and not train_preds.empty:
        _tic, tric = _per_date_ic(train_preds)
        if len(tric):
            gap = float(tric.mean()) - float(ric.mean())
            out["robustness"]["train_rank_ic"] = _f(tric.mean())
            out["robustness"]["train_oos_gap"] = _f(gap)

    out["economic_value"] = _economics(preds, spec, ff)
    return out


def _signal_autocorr(preds: pd.DataFrame) -> float | None:
    """Month-to-month cross-sectional autocorrelation of the signal (a turnover proxy)."""
    months = sorted(preds.index.get_level_values("datetime").unique())
    if len(months) < 3:
        return None
    vals: list[float] = []
    prev: pd.Series | None = None
    for m in months:
        cur = preds.xs(m, level="datetime")["alpha"].dropna()
        if prev is not None:
            common = cur.index.intersection(prev.index)
            if len(common) >= _MIN_NAMES_FOR_IC:
                c = cur.loc[common].corr(prev.loc[common], method="spearman")
                if pd.notna(c):
                    vals.append(float(c))
        prev = cur
    return _f(np.mean(vals)) if vals else None


def _economics(preds: pd.DataFrame, spec: TrainingSpec,
               ff: pd.DataFrame | None) -> dict[str, Any]:
    """Long-short P&L, its risk profile, and the Fama-French decomposition of it."""
    try:
        pnl = qlib_backtest._portfolio_pnl(preds, topk=spec.eval_topk, long_short=True)
    except Exception:  # noqa: BLE001
        logger.warning("long-short pnl failed", exc_info=True)
        return {"available": False}
    if pnl.empty or len(pnl) < 6:
        return {"available": False, "reason": "too few investable months"}

    eq = (1.0 + pnl).cumprod()
    ann = 12.0 / len(pnl)
    vol = float(pnl.std(ddof=1)) * math.sqrt(12.0) if len(pnl) > 1 else 0.0
    total = float((1.0 + pnl).prod())
    ann_ret = total ** ann - 1.0
    out: dict[str, Any] = {
        "available": True,
        "long_short_annualized_return": _f(ann_ret),
        "long_short_annualized_vol": _f(vol),
        "long_short_sharpe": _f(ann_ret / vol) if vol > 1e-9 else None,
        "max_drawdown": _f((eq / eq.cummax() - 1.0).min()),
        "hit_rate": _f((pnl > 0).mean()),
        "turnover": _turnover(preds, spec.eval_topk),
        "topk": int(spec.eval_topk),
        "n_months": int(len(pnl)),
    }
    out["factor_regression"] = _factor_hygiene(pnl, ff)
    return out


def _factor_hygiene(pnl: pd.Series, ff: pd.DataFrame | None) -> dict[str, Any]:
    """Is the spread alpha, or is it Fama-French beta wearing a hat?

    Reuses ``qlib_backtest._factor_regression`` (OLS with a t-stat on the intercept) against
    FF5 + momentum. An ``alpha_tstat`` near zero with a large ``r2`` means the strategy is a
    factor tilt the desk could buy far more cheaply than by running a model.
    """
    if ff is None or ff.empty:
        return {"available": False, "reason": "no Fama-French factors for this market"}
    try:
        common = pnl.index.intersection(ff.index)
        if len(common) < 8:
            return {"available": False, "reason": "too few overlapping months"}
        F = ff.reindex(common)
        return qlib_backtest._factor_regression(
            pnl.reindex(common) - F["rf"], F[qlib_backtest._FACTOR_KEYS])
    except Exception:  # noqa: BLE001
        logger.warning("factor regression failed", exc_info=True)
        return {"available": False}


def factor_neutral_ic(preds: pd.DataFrame, ff_betas: pd.DataFrame) -> dict[str, Any]:
    """Rank-IC after residualizing the signal on the names' FF betas, cross-sectionally.

    If the signal's skill survives having its factor exposure regressed out, it is saying
    something about the company. If it collapses, the model has rediscovered size or value.
    """
    if preds.empty or ff_betas.empty:
        return {"available": False, "reason": "no factor loadings available"}
    n_factors = int(ff_betas.shape[1])
    resid_rows: list[pd.Series] = []
    for _dt, g in preds.groupby(level="datetime"):
        B = ff_betas.reindex(g.index).dropna()
        if len(B) < max(20, n_factors + 5):
            continue
        a = g.loc[B.index, "alpha"].astype(float)
        Z = np.column_stack([np.ones(len(B)), B.to_numpy(dtype=float)])
        try:
            coef, *_ = np.linalg.lstsq(Z, a.to_numpy(dtype=float), rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid_rows.append(pd.Series(a.to_numpy(dtype=float) - Z @ coef, index=B.index))
    if not resid_rows:
        return {"available": False, "reason": "insufficient factor-loading coverage"}

    neutral = pd.concat(resid_rows)
    joined = preds.loc[neutral.index].copy()
    joined["alpha"] = neutral
    _ic, ric = _per_date_ic(joined)
    return {
        "available": True,
        "factor_neutral_rank_ic": _f(ric.mean()),
        "n_dates": int(len(ric)),
        "coverage": _f(len(neutral) / max(1, len(preds))),
    }


# --------------------------------------------------------------------------- #
# Explainability
# --------------------------------------------------------------------------- #
def permutation_importance_ic(
    model: Any, X: pd.DataFrame, y: pd.Series, *, seed: int = 0, top: int = 20,
) -> dict[str, float | None]:
    """Drop in mean within-date rank-IC when each feature is shuffled.

    Scored on rank-IC rather than sklearn's default R² because rank-IC is what this model is
    for; a feature can barely move squared error while carrying most of the ordering.
    """
    if X.empty or len(X) < 100:
        return {}
    rng = np.random.default_rng(seed)
    base_pred = pd.Series(model.model.predict(X.to_numpy(dtype=float)), index=X.index)
    base = _mean_rank_ic(base_pred, y)
    if base is None:
        return {}
    scores: dict[str, float | None] = {}
    arr = X.to_numpy(dtype=float)
    for j, col in enumerate(X.columns):
        shuffled = arr.copy()
        shuffled[:, j] = shuffled[rng.permutation(len(shuffled)), j]
        p = pd.Series(model.model.predict(shuffled), index=X.index)
        got = _mean_rank_ic(p, y)
        scores[str(col)] = _f(base - got) if got is not None else None
    ranked = sorted(scores.items(), key=lambda kv: (kv[1] is None, -(kv[1] or 0.0)))
    return dict(ranked[:top])


def _mean_rank_ic(pred: pd.Series, y: pd.Series) -> float | None:
    df = pd.DataFrame({"alpha": pred, "ret": y.reindex(pred.index)})
    _ic, ric = _per_date_ic(df)
    return float(ric.mean()) if len(ric) else None


def partial_dependence_curves(
    model: Any, X: pd.DataFrame, features: Sequence[str], *, grid: int = 9, max_rows: int = 3000,
) -> dict[str, Any]:
    """Partial-dependence curve per feature, computed directly rather than via sklearn.

    Implemented by hand because the predictor may be a raw LightGBM booster, which does not
    satisfy sklearn's estimator interface. The definition is the same: fix feature *j* at
    each grid value across the whole sample and average the prediction.

    This is the diagnostic that showed Kang et al. their momentum threshold effect — a
    monotone-looking factor whose predictive power reverses past a cutoff.
    """
    if X.empty or not len(features):
        return {"available": False}
    sample = X if len(X) <= max_rows else X.sample(max_rows, random_state=0)
    arr = sample.to_numpy(dtype=float)
    cols = list(sample.columns)
    out: dict[str, Any] = {"available": True, "curves": {}}
    for feat in features:
        if feat not in cols:
            continue
        j = cols.index(feat)
        qs = np.linspace(0.05, 0.95, grid)
        xs = np.nanquantile(arr[:, j], qs)
        ys: list[float | None] = []
        for v in xs:
            probe = arr.copy()
            probe[:, j] = v
            ys.append(_f(np.nanmean(model.model.predict(probe))))
        out["curves"][str(feat)] = {"x": [_f(v) for v in xs], "y": ys}
    return out


def importance_stability(importances: Sequence[pd.Series], top_k: int = 10) -> dict[str, Any]:
    """How much the feature ranking moves between walk-forward refits.

    Mishra, Dutta, Long & Magazzeni (arXiv:2111.00358) survey exactly this problem: an
    attribution is a claim that itself needs a robustness argument before anyone acts on it.
    Here the check is concrete — mean Jaccard overlap of the top-k sets and mean Spearman
    correlation of the full rankings across consecutive refits. A low score means the report
    should not be read as "the model uses these features", and the Researcher is told not to
    act on the ranking.
    """
    usable = [s for s in importances if s is not None and not s.empty]
    if len(usable) < 2:
        return {"available": False, "reason": "fewer than two refits to compare"}
    jac: list[float] = []
    spear: list[float] = []
    for a, b in zip(usable, usable[1:]):
        sa, sb = set(a.head(top_k).index), set(b.head(top_k).index)
        if sa or sb:
            jac.append(len(sa & sb) / len(sa | sb))
        common = a.index.intersection(b.index)
        if len(common) >= 3:
            c = a.loc[common].rank().corr(b.loc[common].rank(), method="spearman")
            if pd.notna(c):
                spear.append(float(c))
    mean_jac = _f(np.mean(jac)) if jac else None
    return {
        "available": True,
        "top_k": int(top_k),
        "mean_top_k_jaccard": mean_jac,
        "mean_rank_correlation": _f(np.mean(spear)) if spear else None,
        "n_comparisons": len(jac),
        # Below ~0.5 overlap the ranking is not reproducible enough to reason from.
        "stable": bool(mean_jac is not None and mean_jac >= 0.5),
    }


def explainability(
    wf: WalkForward, panel: pd.DataFrame, feature_cols: Sequence[str], spec: TrainingSpec,
) -> dict[str, Any]:
    """Gain, permutation importance, TreeSHAP, partial dependence and their stability."""
    model = wf.final_model
    if model is None or panel.empty:
        return {"available": False}
    X = panel[FEATURE].reindex(columns=list(feature_cols)).astype(float).fillna(0.0)
    y = panel[(LABEL, LABEL_COL)].astype(float)

    gain = model.importance()
    gain_top = {str(k): _f(v) for k, v in gain.head(20).items()} if not gain.empty else {}
    total = float(gain.sum()) if not gain.empty else 0.0
    concentration = _f(float(gain.head(5).sum()) / total) if total > 0 else None

    shap = model.shap_values(X)
    top_feats = list(gain.head(5).index) if not gain.empty else list(X.columns[:5])

    return {
        "available": True,
        "gain_importance": gain_top,
        "gain_concentration_top5": concentration,
        "permutation_importance_rank_ic_drop": permutation_importance_ic(
            model, X, y, seed=spec.seed),
        "mean_abs_shap": {str(k): _f(v) for k, v in shap.head(20).items()} if not shap.empty else {},
        "partial_dependence": partial_dependence_curves(model, X, top_feats),
        "stability": importance_stability(wf.importances),
    }


def hyperparameter_stability(wf: WalkForward) -> dict[str, Any]:
    """Dispersion of the chosen boosting length across refits.

    Kang et al. read hyperparameters that swing between windows as evidence of a low
    signal-to-noise ratio rather than of a well-tuned model — if the optimal complexity is
    unstable, the "optimum" is fitting noise. Reported as a first-class diagnostic instead
    of being discarded with the rest of the fit metadata.
    """
    iters = [i for i in wf.best_iterations if i]
    if len(iters) < 2:
        return {"available": False, "reason": "no early-stopping signal across refits"}
    arr = np.asarray(iters, dtype=float)
    cv = float(arr.std(ddof=1) / arr.mean()) if arr.mean() else None
    return {
        "available": True,
        "best_iterations": [int(i) for i in iters],
        "mean": _f(arr.mean()), "std": _f(arr.std(ddof=1)),
        "coefficient_of_variation": _f(cv),
        # A CV above ~0.5 means the "optimal" complexity roughly doubles between windows.
        "stable": bool(cv is not None and cv < 0.5),
    }


# --------------------------------------------------------------------------- #
# Sub-population breakdown
# --------------------------------------------------------------------------- #
def breakdown(
    preds: pd.DataFrame,
    groups: pd.Series,
    *,
    name: str,
    min_names: int = _THIN_BUCKET_NAMES,
) -> list[dict[str, Any]]:
    """Per-bucket skill, with identical columns for every cut.

    ``groups`` maps either ``instrument`` (GICS: a company's sector does not change month to
    month for this purpose) or ``(datetime, instrument)`` (FF exposure: a company's size or
    value loading very much does) to a bucket label.

    Buckets thinner than ``min_names`` are computed but flagged ``thin``. They are not
    dropped, because "this model has no coverage in Utilities" is itself a finding the PM
    needs — but a headline that rests on one is a Validation finding.
    """
    if preds.empty or groups is None or groups.empty:
        return []

    if isinstance(groups.index, pd.MultiIndex):
        labels = groups.reindex(preds.index)
    else:
        labels = pd.Series(
            groups.reindex(preds.index.get_level_values("instrument")).to_numpy(),
            index=preds.index)
    labels = labels.dropna()
    if labels.empty:
        return []

    rows: list[dict[str, Any]] = []
    for bucket, idx in labels.groupby(labels).groups.items():
        sub = preds.loc[list(idx)]
        n_names = int(sub.index.get_level_values("instrument").nunique())
        _ic, ric = _per_date_ic(sub)
        std = float(ric.std(ddof=1)) if len(ric) > 1 else 0.0
        t = (float(ric.mean() / (std / math.sqrt(len(ric)))) if len(ric) > 2 and std > 0 else None)
        prof = _quantile_profile(sub, n_q=5)
        rows.append({
            "cut": name,
            "bucket": str(bucket),
            "n_names": n_names,
            "n_months": int(len(ric)),
            "n_obs": int(len(sub)),
            "rank_ic": _f(ric.mean()),
            "rank_icir": _f(ric.mean() / std) if std else None,
            "rank_ic_t_stat": _f(t),
            "r2_oos": r2_oos(sub["ret"], sub["alpha"])["zero_benchmarked"],
            "top_decile_spread": prof.get("top_minus_bottom") if prof.get("available") else None,
            "coverage": _f(len(sub) / max(1, len(preds))),
            "thin": bool(n_names < min_names or len(ric) < 6),
        })
    rows.sort(key=lambda r: (r["rank_ic"] is None, -(r["rank_ic"] or -9)))
    return rows


def all_breakdowns(preds: pd.DataFrame, raw: pp.RawPanel) -> dict[str, Any]:
    """Both cuts the plan calls for: GICS classification and Fama-French exposure.

    They answer different questions and neither substitutes for the other. GICS asks "where
    does this work?" in terms a PM allocates by. FF exposure asks "what kind of company does
    this work on?" in terms a risk model prices — a signal that only works in the smallest
    SMB quintile is a size bet regardless of which sectors those names sit in.
    """
    out: dict[str, Any] = {"available": False, "cuts": {}}
    if preds.empty:
        return out

    if not raw.sectors.empty:
        for col, label in (("sector", "GICS sector"),
                           ("industry_group", "GICS industry group")):
            if col in raw.sectors.columns:
                rows = breakdown(preds, raw.sectors[col].dropna(), name=label)
                if rows:
                    out["cuts"][label] = rows
                    out["available"] = True

    if not raw.ff_betas.empty:
        from .refdata import FF_BETA_LABELS

        for col, label in FF_BETA_LABELS.items():
            if col not in raw.ff_betas.columns:
                continue
            series = raw.ff_betas[col].reindex(preds.index).dropna()
            if len(series) < 200:
                continue
            buckets = _quintiles_within_date(series)
            rows = breakdown(preds, buckets, name=label)
            if rows:
                out["cuts"][label] = rows
                out["available"] = True

    return out


def _quintiles_within_date(values: pd.Series, n: int = 5) -> pd.Series:
    """Within-date quintile labels; a fixed global cut would drift with the factor."""
    from .refdata import quantile_buckets

    return quantile_buckets(values, n=n, labels="Q").dropna()
