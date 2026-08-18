"""Sample selection, outlier treatment, normalization and purged splitting.

This is the deterministic half of the loop: given a raw panel loaded once per run and a
:class:`~api.quant.research.spec.TrainingSpec`, produce the exact feature/label matrix that
spec describes. Nothing here talks to an LLM, and re-running it with the same spec on the
same raw panel is bit-identical — that is what makes ``spec_hash`` a meaningful
reproducibility key.

What the incumbent pipeline does today, and what each knob changes:

* ``qlib_data._cross_sectional_zscore`` z-scores with mean/std then clips at ±3σ. A single
  extreme value moves the mean *and* inflates the std for that whole month before the clip
  can act, so the clip protects the model from the outlier but not the other names from its
  effect on their z-scores. ``normalization="robust_zscore"`` (median/MAD) and
  ``"rank"`` remove that channel entirely.
* Missing features become ``0.0`` *after* z-scoring, i.e. imputed at the cross-sectional
  mean, with no floor on how sparse a feature may be. ``min_feature_coverage`` and
  ``fill_missing="cross_sectional_median"`` are the two knobs that fix that.
* The label is never winsorized at training time, although the walk-forward backtest clips
  it at 1%/99% per month (``qlib_backtest``). ``winsorize_label`` closes that asymmetry.
* The split is contiguous with no gap, so multi-month labels straddle it.
  ``embargo_months`` purges the boundary.

Order of operations is fixed and deliberate: coverage filtering and winsorization happen on
RAW values (a quantile clip after z-scoring would clip a distribution the outlier already
distorted), normalization next, then neutralization on the normalized block, then imputation
last so that "missing" is still distinguishable from "zero" for as long as possible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .. import qlib_data
from . import refdata
from .spec import TrainingSpec

logger = logging.getLogger("mzqa.quant.research.preprocess")

FEATURE = qlib_data.FEATURE
LABEL = qlib_data.LABEL
LABEL_COL = qlib_data.LABEL_COL

_MAD_TO_SIGMA = 1.4826  # makes the MAD a consistent estimator of sigma under normality


@dataclass
class RawPanel:
    """The once-per-run warehouse pull every iteration re-derives its matrix from."""

    features: pd.DataFrame          # (datetime, instrument) x metric_id, NaNs INTACT
    label: pd.Series                # (datetime, instrument) -> forward return
    metric_ids: list[str]
    jurisdiction: str
    label_name: str
    sectors: pd.DataFrame = field(default_factory=pd.DataFrame)   # instrument -> sector/industry
    mcap: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    ff_betas: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Realized 1-month forward return per (datetime, instrument) — the backtest's P&L unit,
    # decoupled from the (possibly multi-month) horizon the model ranks on.
    ret_1m: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    # Monthly Fama-French 5 + momentum factors, indexed by month-end.
    ff_factors: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def empty(self) -> bool:
        return self.features.empty or self.label.empty

    def describe(self) -> dict[str, Any]:
        dts = self.features.index.get_level_values("datetime")
        return {
            "rows": int(self.features.shape[0]),
            "features": int(self.features.shape[1]),
            "instruments": int(self.features.index.get_level_values("instrument").nunique()),
            "months": int(dts.nunique()),
            "first_month": str(pd.Timestamp(dts.min()).date()) if len(dts) else None,
            "last_month": str(pd.Timestamp(dts.max()).date()) if len(dts) else None,
            "sector_coverage": float(
                self.features.index.get_level_values("instrument")
                .isin(self.sectors.index).mean()) if not self.sectors.empty else 0.0,
            "ff_beta_coverage": float(
                self.features.index.isin(self.ff_betas.index).mean()
            ) if not self.ff_betas.empty else 0.0,
        }


def load_raw(jurisdiction: str, spec: TrainingSpec, *, with_refdata: bool = True) -> RawPanel:
    """Pull the panel and the reference maps once. Everything downstream is pandas.

    Uses ``fillna=None`` / ``normalize=False`` so missingness and raw scale survive to the
    per-iteration processing; ``min_names_per_date=1`` because the spec's own thin-month
    gate is applied later, after its row filters have run.
    """
    metric_ids = qlib_data.feature_metric_ids(
        jurisdiction, start=spec.start, end=spec.end, families=spec.families)
    if not metric_ids:
        return RawPanel(pd.DataFrame(), pd.Series(dtype=float), [], jurisdiction, spec.label)

    panel = qlib_data.build_panel(
        jurisdiction, start=spec.start, end=spec.end, label=spec.label,
        metric_ids=metric_ids, min_names_per_date=1,
        normalize=False, require_label=True, fillna=None,
    )
    if panel.empty:
        return RawPanel(pd.DataFrame(), pd.Series(dtype=float), [], jurisdiction, spec.label)

    features = panel[FEATURE].copy()
    label = panel[(LABEL, LABEL_COL)].astype(float)
    features.index = features.index.set_names(["datetime", "instrument"])
    label.index = label.index.set_names(["datetime", "instrument"])
    # Uppercase instruments once so every reference-data join lines up.
    features.index = features.index.set_levels(
        features.index.levels[1].astype(str).str.upper(), level="instrument")
    label.index = features.index

    raw = RawPanel(features=features, label=label, metric_ids=list(metric_ids),
                   jurisdiction=jurisdiction, label_name=spec.label)
    if with_refdata:
        dts = features.index.get_level_values("datetime")
        lo, hi = pd.Timestamp(dts.min()).date(), pd.Timestamp(dts.max()).date()
        raw.sectors = refdata.load_gics(jurisdiction)
        raw.mcap = refdata.load_market_cap(jurisdiction, start=lo, end=hi)
        raw.ff_betas = refdata.load_ff_betas(jurisdiction, start=lo, end=hi)
        # Both of these were previously re-queried inside every evaluation. They do not
        # depend on the spec, so a research run loads them exactly once here and the whole
        # metric battery downstream stays free of warehouse access (and unit-testable).
        try:
            raw.ret_1m = qlib_data.realized_forward_returns(
                jurisdiction, start=lo, end=hi, horizon_months=1)
        except Exception:  # noqa: BLE001
            logger.warning("1m realized-return load failed", exc_info=True)
        try:
            from .. import qlib_backtest

            months = pd.Index(sorted(features.index.get_level_values("datetime").unique()))
            ff = qlib_backtest._ff_monthly(jurisdiction, months)
            if ff is not None:
                raw.ff_factors = ff
        except Exception:  # noqa: BLE001
            logger.warning("Fama-French factor load failed", exc_info=True)
    return raw


# --------------------------------------------------------------------------- #
# Outlier treatment
# --------------------------------------------------------------------------- #
def _winsorize_block(block: pd.Series, q: float) -> pd.Series:
    """Replace the k most extreme observations at each tail with the next order statistic.

    Deliberately NOT ``quantile(q)`` / ``quantile(1-q)``. An interpolated quantile is
    computed FROM the sample that contains the outlier, so on a small cross-section the
    outlier drags its own clip bound up to meet it: with 60 names and q=0.01, a planted
    value of 500 pulled the 99th percentile to ~206 and the clip did essentially nothing.

    The classical definition — winsorize the k most extreme values, where k = floor(q*n) but
    at least 1 — has no such feedback, because the bound is an observation that is not itself
    extreme. It also does something sensible when q*n < 1, which is the common case for thin
    INTL cross-sections: the user asked for winsorization, so clip at least one value rather
    than silently clipping none.
    """
    clean = block.dropna()
    n = len(clean)
    k = max(1, int(np.floor(q * n)))
    if n < 2 * k + 3:
        return block                    # too few observations to winsorize meaningfully
    lo = float(clean.nsmallest(k + 1).iloc[-1])
    hi = float(clean.nlargest(k + 1).iloc[-1])
    return block.clip(lower=lo, upper=hi)


def winsorize_by_date(df: pd.DataFrame | pd.Series, q: float) -> pd.DataFrame | pd.Series:
    """Winsorize each column **within each date**, at ``q`` in each tail.

    Cross-sectional rather than pooled: a 2020-03 return of -60% is an ordinary member of
    that month's distribution and should not be clipped against a full-sample bound that
    calm months dominate.
    """
    if q is None or q <= 0 or df.empty:
        return df
    q = min(float(q), 0.49)
    grp = df.groupby(level="datetime", group_keys=False)
    if isinstance(df, pd.Series):
        return grp.apply(lambda s: _winsorize_block(s, q))
    return grp.apply(lambda block: block.apply(lambda s: _winsorize_block(s, q)))


def normalize_by_date(df: pd.DataFrame, method: str, clip_sigma: float = 3.0) -> pd.DataFrame:
    """Cross-sectionally standardize each feature within each date.

    ``zscore``         mean/std then clip — reproduces ``qlib_data._cross_sectional_zscore``.
    ``robust_zscore``  median/MAD then clip — the outlier moves neither centre nor scale.
    ``rank``           within-date percentile mapped to [-1, 1]; discards magnitude and keeps
                       only ordering, which is all a ranking model consumes anyway and is the
                       transform Kang et al. (2025) recommend for noisy panels.
    """
    if df.empty:
        return df
    grp = df.groupby(level="datetime")

    if method == "rank":
        pct = grp.rank(pct=True)
        return (2.0 * (pct - 0.5)).astype(float)

    if method == "robust_zscore":
        med = grp.transform("median")
        mad = (df - med).abs().groupby(level="datetime").transform("median")
        scale = (_MAD_TO_SIGMA * mad).replace(0.0, np.nan)
        z = (df - med) / scale
        return z.clip(-clip_sigma, clip_sigma)

    # default: zscore
    mean = grp.transform("mean")
    std = grp.transform("std").replace(0.0, np.nan)
    return ((df - mean) / std).clip(-clip_sigma, clip_sigma)


def impute(df: pd.DataFrame, method: str) -> pd.DataFrame:
    """Fill remaining NaNs. ``drop_row`` is handled by the caller (it changes the index)."""
    if df.empty or method == "drop_row":
        return df
    if method == "cross_sectional_median":
        med = df.groupby(level="datetime").transform("median")
        return df.fillna(med).fillna(0.0)   # a month where a feature is wholly absent -> 0
    return df.fillna(0.0)


def neutralize_by_date(
    X: pd.DataFrame,
    *,
    sectors: pd.Series | None = None,
    size: pd.Series | None = None,
) -> pd.DataFrame:
    """Residualize every feature against sector dummies and/or log size, within each date.

    Answers "does this feature say anything beyond which industry (or how large) the company
    is?" — the same question the report's GICS breakdown asks of the finished model, applied
    upstream at the feature level. Implemented as one least-squares solve per date for the
    whole feature block at once rather than per feature.
    """
    if X.empty or (sectors is None and size is None):
        return X

    out = X.copy()
    for dt, block in X.groupby(level="datetime"):
        idx = block.index
        cols: list[np.ndarray] = [np.ones(len(idx))]
        if sectors is not None:
            s = sectors.reindex(idx.get_level_values("instrument")).fillna("__unknown__")
            dummies = pd.get_dummies(pd.Series(s.values, index=idx), drop_first=True)
            if dummies.shape[1]:
                cols.append(dummies.to_numpy(dtype=float))
        if size is not None:
            v = size.reindex(idx).astype(float)
            lv = np.log(v.where(v > 0)).to_numpy()
            if np.isfinite(lv).sum() >= 3:
                lv = np.nan_to_num(lv, nan=float(np.nanmean(lv)))
                lv = (lv - lv.mean()) / (lv.std() or 1.0)
                cols.append(lv.reshape(-1, 1))

        Z = np.column_stack(cols)
        if Z.shape[0] <= Z.shape[1] + 1:
            continue  # too few names this month to residualize against; leave as-is

        Y = block.to_numpy(dtype=float)
        mask = np.isfinite(Y)
        filled = np.where(mask, Y, 0.0)
        try:
            coef, *_ = np.linalg.lstsq(Z, filled, rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = filled - Z @ coef
        out.loc[idx, :] = np.where(mask, resid, np.nan)   # keep genuine gaps missing
    return out


# --------------------------------------------------------------------------- #
# Sample selection
# --------------------------------------------------------------------------- #
def _coverage(features: pd.DataFrame) -> pd.Series:
    return features.notna().mean(axis=0)


def apply_spec(
    raw: RawPanel,
    spec: TrainingSpec,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build the qlib-shaped feature/label panel this spec describes.

    Returns ``(panel, provenance)`` where ``panel`` has the usual MultiIndex columns
    ``("feature", metric_id...) + ("label", "y")`` so it drops straight into the existing
    scoring code, and ``provenance`` records exactly what each filter removed — the numbers
    the Model Validation agent reads to detect sample-selection bias between rounds.
    """
    prov: dict[str, Any] = {"stages": []}
    if raw.empty:
        return pd.DataFrame(), {"stages": [], "reason": "empty raw panel"}

    X = raw.features
    y = raw.label
    start_rows, start_cols = X.shape

    def _note(stage: str, **extra: Any) -> None:
        prov["stages"].append({"stage": stage, "rows": int(X.shape[0]),
                               "features": int(X.shape[1]), **extra})

    # --- feature-level filters (before any row filtering, so coverage is measured on the
    #     full sample rather than on whatever the row filters happened to leave behind) ---
    if spec.feature_drop:
        drop = [c for c in X.columns if str(c) in set(spec.feature_drop)]
        if drop:
            X = X.drop(columns=drop)
            _note("feature_drop", dropped=len(drop))

    if spec.min_feature_coverage > 0:
        cov = _coverage(X)
        keep = cov[cov >= spec.min_feature_coverage].index
        dropped = int(X.shape[1] - len(keep))
        if len(keep) >= 2 and dropped:
            X = X[keep]
            _note("min_feature_coverage", dropped=dropped,
                  threshold=spec.min_feature_coverage)
        elif len(keep) < 2:
            prov["warnings"] = prov.get("warnings", []) + [
                f"min_feature_coverage={spec.min_feature_coverage} would leave "
                f"{len(keep)} features; skipped"]

    # --- row-level filters ---
    if spec.min_market_cap_usd and not raw.mcap.empty:
        caps = raw.mcap.reindex(X.index)
        keep_mask = caps >= float(spec.min_market_cap_usd)
        # Names with no cap observation are kept: absence of a size datum is not evidence
        # of a small company, and dropping them would bias the sample toward covered names.
        keep_mask = keep_mask | caps.isna()
        removed = int((~keep_mask).sum())
        if removed and keep_mask.sum() > 0:
            X, y = X[keep_mask], y[keep_mask]
            _note("min_market_cap_usd", removed=removed, floor=spec.min_market_cap_usd)

    if spec.min_obs_per_name > 1:
        counts = y.groupby(level="instrument").size()
        keep_names = counts[counts >= spec.min_obs_per_name].index
        mask = X.index.get_level_values("instrument").isin(keep_names)
        removed = int((~mask).sum())
        if removed and mask.sum() > 0:
            X, y = X[mask], y[mask]
            _note("min_obs_per_name", removed=removed, threshold=spec.min_obs_per_name)

    # --- outlier treatment on RAW values ---
    if spec.winsorize_features:
        X = winsorize_by_date(X, spec.winsorize_features)
        _note("winsorize_features", q=spec.winsorize_features)
    if spec.winsorize_label:
        y = winsorize_by_date(y, spec.winsorize_label)
        _note("winsorize_label", q=spec.winsorize_label)

    # --- normalize, neutralize, impute ---
    X = normalize_by_date(X, spec.normalization, spec.clip_sigma)
    _note("normalize", method=spec.normalization)

    if spec.neutralize:
        sectors = (raw.sectors["sector"] if "sector" in raw.sectors.columns
                   and "sector" in spec.neutralize else None)
        size = raw.mcap if "size" in spec.neutralize else None
        if sectors is not None or size is not None:
            X = neutralize_by_date(X, sectors=sectors, size=size)
            _note("neutralize", against=list(spec.neutralize))

    if spec.fill_missing == "drop_row":
        mask = X.notna().all(axis=1)
        if mask.sum() >= max(50, spec.min_names_per_date):
            removed = int((~mask).sum())
            X, y = X[mask], y[mask]
            _note("fill_missing.drop_row", removed=removed)
        else:
            X = impute(X, "cross_sectional_median")
            prov["warnings"] = prov.get("warnings", []) + [
                "fill_missing='drop_row' would empty the panel; used median imputation"]
    else:
        X = impute(X, spec.fill_missing)
        _note("impute", method=spec.fill_missing)

    # --- thin cross-sections last, once every other filter has taken its rows ---
    if spec.min_names_per_date > 1:
        sizes = X.groupby(level="datetime").size()
        keep_dates = sizes[sizes >= spec.min_names_per_date].index
        mask = X.index.get_level_values("datetime").isin(keep_dates)
        removed = int((~mask).sum())
        if mask.sum() > 0 and removed:
            X, y = X[mask], y[mask]
            _note("min_names_per_date", removed=removed, threshold=spec.min_names_per_date)

    if X.empty or X.shape[1] < 2:
        return pd.DataFrame(), {**prov, "reason": "all rows or features filtered out"}

    y = y.reindex(X.index)
    keep = y.notna()
    X, y = X[keep], y[keep]
    if X.empty:
        return pd.DataFrame(), {**prov, "reason": "no rows retained a label"}

    panel = pd.concat({FEATURE: X, LABEL: y.to_frame(LABEL_COL)}, axis=1).sort_index()
    panel.index = panel.index.set_names(["datetime", "instrument"])

    dts = panel.index.get_level_values("datetime")
    prov.update({
        "rows_in": int(start_rows), "rows_out": int(panel.shape[0]),
        "features_in": int(start_cols), "features_out": int(X.shape[1]),
        "months": int(dts.nunique()),
        "names": int(panel.index.get_level_values("instrument").nunique()),
        "first_month": str(pd.Timestamp(dts.min()).date()),
        "last_month": str(pd.Timestamp(dts.max()).date()),
        "row_retention": round(float(panel.shape[0] / max(1, start_rows)), 4),
    })
    return panel, prov


# --------------------------------------------------------------------------- #
# Feature selection (train-window only — selecting on the full panel is lookahead)
# --------------------------------------------------------------------------- #
def select_features(
    panel: pd.DataFrame,
    spec: TrainingSpec,
    *,
    train_end: pd.Timestamp | None = None,
) -> list[str]:
    """Choose the feature subset, fitting the selector on training rows ONLY.

    ``univariate_ic`` ranks by mean within-date Spearman correlation with the label;
    ``importance`` ranks by a quick gradient-boosted fit's gain (Huang et al. use an RF for
    exactly this and find it lifts the weaker learners). Selecting on the whole panel would
    leak the evaluation window's answers into the feature set, so ``train_end`` bounds it.
    """
    cols = [str(c) for c in panel[FEATURE].columns]
    if spec.feature_selector == "none" or not spec.max_features or spec.max_features >= len(cols):
        return cols

    fit = panel
    if train_end is not None:
        fit = panel[panel.index.get_level_values("datetime") <= pd.Timestamp(train_end)]
    if fit.empty or fit.shape[0] < 50:
        return cols

    X = fit[FEATURE]
    y = fit[(LABEL, LABEL_COL)].astype(float)

    if spec.feature_selector == "importance":
        try:
            import lightgbm as lgb

            booster = lgb.train(
                {"objective": "regression", "learning_rate": 0.1, "num_leaves": 31,
                 "verbose": -1, "seed": spec.seed, "num_threads": 0},
                lgb.Dataset(X.to_numpy(dtype=float), y.to_numpy(dtype=float)),
                num_boost_round=100,
            )
            gain = pd.Series(booster.feature_importance("gain"), index=X.columns)
            return [str(c) for c in gain.sort_values(ascending=False).head(spec.max_features).index]
        except Exception:  # noqa: BLE001 - fall through to the univariate path
            logger.warning("importance-based selection failed; using univariate IC", exc_info=True)

    # univariate_ic (and the importance fallback)
    ranks_x = X.groupby(level="datetime").rank()
    ranks_y = y.groupby(level="datetime").rank()
    ic = ranks_x.corrwith(ranks_y)
    order = ic.abs().sort_values(ascending=False)
    return [str(c) for c in order.head(spec.max_features).index]


# --------------------------------------------------------------------------- #
# Purged splitting
# --------------------------------------------------------------------------- #
def purged_segments(panel: pd.DataFrame, spec: TrainingSpec) -> dict[str, tuple[Any, Any]]:
    """Train/valid/test boundaries with the label horizon purged from each seam."""
    return qlib_data.time_segments(
        panel, valid_frac=spec.valid_frac, test_frac=spec.test_frac,
        embargo_months=spec.resolved_embargo,
    )


def walk_forward_dates(
    panel: pd.DataFrame, spec: TrainingSpec
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """``(prediction_month, train_cutoff)`` pairs for the expanding-window evaluation.

    The cutoff is ``prediction_month`` minus the embargo, so the training window never
    contains a row whose label overlaps the month being predicted. With ``embargo=12`` and a
    ``forward_12m`` label, a model predicting 2024-06 is fit only on labels realized by
    2023-06 — which is what an honest out-of-sample number requires and what today's
    contiguous split does not do.
    """
    dates = pd.Index(sorted(panel.index.get_level_values("datetime").unique()))
    gap = spec.resolved_embargo
    out: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for i, t in enumerate(dates):
        if i < spec.wf_min_train_months + gap:
            continue
        cutoff = pd.Timestamp(t) - pd.DateOffset(months=gap)
        out.append((pd.Timestamp(t), cutoff))
    return out
