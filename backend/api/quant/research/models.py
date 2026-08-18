"""Model families the Researcher may choose between, behind one prediction contract.

``qlib_alpha.predict`` reaches through the artifact for a booster and calls it on a plain
array::

    booster = artifact.model.model
    scores = booster.predict(X.values)

Everything here satisfies exactly that shape — an object with a ``.model`` attribute whose
``.predict(ndarray)`` returns 1-D scores — so a random forest, an elastic net or an ensemble
persists, loads and serves through the *existing* path with no changes to
``qlib_alpha``, ``alpha_signal`` or the optimizer.

Why more than LightGBM: Kang, Ryu & Webb (2025) compare eight families on a short, noisy
emerging-market panel and find tree ensembles beat neural nets, with elastic net / PLS / PCR
as informative cheap comparators — and, importantly, that the *right* family is market-
dependent. Making the family a searchable knob is the honest response; every family here
ships with the installed scikit-learn, so this costs no new dependency.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .spec import TrainingSpec, family_defaults

logger = logging.getLogger("mzqa.quant.research.models")

# qlib's LGBModel takes these as fit() arguments, not as booster params.
_LGB_FIT_ARGS = {"num_boost_round", "early_stopping_rounds"}
# qlib spells the objective "loss"; raw LightGBM wants "objective".
_LGB_LOSS_TO_OBJECTIVE = {"mse": "regression", "mae": "regression_l1", "huber": "huber"}


class _Adapter:
    """Wraps any estimator so ``predict(ndarray)`` returns flat float scores."""

    __slots__ = ("estimator", "kind")

    def __init__(self, estimator: Any, kind: str) -> None:
        self.estimator = estimator
        self.kind = kind

    def predict(self, X: Any) -> np.ndarray:
        arr = np.asarray(X, dtype=float)
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        out = self.estimator.predict(arr)
        return np.asarray(out, dtype=float).reshape(-1)   # PLS returns (n, 1)


@dataclass
class FittedModel:
    """A trained model plus the column identity it was trained against.

    ``model`` is the attribute ``qlib_alpha.predict`` reaches for; the extra fields are what
    the report and the ensemble builder need.
    """

    model: _Adapter
    family: str
    feature_cols: list[str]
    fit_seconds: float = 0.0
    n_train: int = 0
    best_iteration: int | None = None
    pred_mean: float = 0.0
    pred_std: float = 1.0
    resolved_params: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> pd.Series:
        """Score a feature frame, aligning it to this model's own columns first."""
        if X.empty:
            return pd.Series(dtype=float)
        aligned = X.reindex(columns=self.feature_cols).fillna(0.0)
        return pd.Series(self.model.predict(aligned.to_numpy(dtype=float)),
                         index=X.index, name="alpha")

    def importance(self) -> pd.Series:
        """Native importance where the family has one, else an empty Series."""
        est = self.model.estimator
        try:
            if self.family == "lgbm":
                return pd.Series(est.feature_importance("gain"),
                                 index=self.feature_cols).sort_values(ascending=False)
            if hasattr(est, "feature_importances_"):
                return pd.Series(np.asarray(est.feature_importances_, dtype=float),
                                 index=self.feature_cols).sort_values(ascending=False)
            if hasattr(est, "coef_"):
                coef = np.asarray(est.coef_, dtype=float).reshape(-1)
                if coef.size == len(self.feature_cols):
                    return pd.Series(np.abs(coef),
                                     index=self.feature_cols).sort_values(ascending=False)
        except Exception:  # noqa: BLE001 - importance is advisory
            logger.debug("importance unavailable for %s", self.family, exc_info=True)
        return pd.Series(dtype=float)

    def shap_values(self, X: pd.DataFrame, max_rows: int = 2000) -> pd.Series:
        """Mean |TreeSHAP| per feature — LightGBM only, via its native ``pred_contrib``.

        Uses the booster's built-in TreeSHAP rather than the ``shap`` package, so the
        contribution decomposition costs no new dependency.
        """
        if self.family != "lgbm" or X.empty:
            return pd.Series(dtype=float)
        try:
            sample = X.reindex(columns=self.feature_cols).fillna(0.0)
            if len(sample) > max_rows:
                sample = sample.sample(max_rows, random_state=0)
            contrib = self.model.estimator.predict(sample.to_numpy(dtype=float),
                                                   pred_contrib=True)
            # last column is the expected-value / bias term
            vals = np.abs(np.asarray(contrib, dtype=float)[:, :-1]).mean(axis=0)
            return pd.Series(vals, index=self.feature_cols).sort_values(ascending=False)
        except Exception:  # noqa: BLE001
            logger.debug("TreeSHAP unavailable", exc_info=True)
            return pd.Series(dtype=float)


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #
def _split_lgb_params(params: dict[str, Any], seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    booster: dict[str, Any] = {}
    fit_args: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key in _LGB_FIT_ARGS:
            fit_args[key] = value
        elif key == "loss":
            booster["objective"] = _LGB_LOSS_TO_OBJECTIVE.get(str(value), "regression")
        else:
            booster[key] = value
    booster.setdefault("objective", "regression")
    booster.setdefault("verbose", -1)
    booster.setdefault("num_threads", 0)
    booster["seed"] = seed
    booster["deterministic"] = True
    fit_args.setdefault("num_boost_round", 500)
    return booster, fit_args


def _build_estimator(family: str, params: dict[str, Any], seed: int,
                     *, n_features: int = 0, n_samples: int = 0) -> Any:
    """Instantiate a scikit-learn estimator for a non-LightGBM family.

    ``n_components`` is clamped to the data's actual rank. PLS and PCA raise outright when
    asked for more components than there are features or samples, and that is reachable from
    an ordinary agent proposal — switching to ``pcr`` (16 components by default) while the
    spec also restricts ``families`` to a single block that yields 5 metrics would otherwise
    crash the round rather than degrade it.
    """
    p = {**family_defaults(family), **(params or {})}
    if family in ("pls", "pcr"):
        ceiling = max(1, min(int(n_features or 1), max(1, int(n_samples or 1) - 1)))
        p["n_components"] = max(1, min(int(p.get("n_components", 8)), ceiling))
    if family == "rf":
        from sklearn.ensemble import RandomForestRegressor

        return RandomForestRegressor(
            n_estimators=int(p.get("n_estimators", 300)),
            max_depth=int(p["max_depth"]) if p.get("max_depth") else None,
            min_samples_leaf=int(p.get("min_samples_leaf", 50)),
            max_features=float(p.get("max_features", 0.5)),
            n_jobs=int(p.get("n_jobs", -1)), random_state=seed,
        )
    if family == "enet":
        from sklearn.linear_model import ElasticNet

        return ElasticNet(alpha=float(p.get("alpha", 1e-3)),
                          l1_ratio=float(p.get("l1_ratio", 0.5)),
                          max_iter=int(p.get("max_iter", 5000)), random_state=seed)
    if family == "ridge":
        from sklearn.linear_model import Ridge

        return Ridge(alpha=float(p.get("alpha", 1.0)), random_state=seed)
    if family == "pls":
        from sklearn.cross_decomposition import PLSRegression

        return PLSRegression(n_components=int(p.get("n_components", 8)),
                             max_iter=int(p.get("max_iter", 1000)))
    if family == "pcr":
        # Principal Component Regression = PCA then OLS, as a single pipeline.
        from sklearn.decomposition import PCA
        from sklearn.linear_model import LinearRegression
        from sklearn.pipeline import make_pipeline

        return make_pipeline(PCA(n_components=int(p.get("n_components", 16)),
                                 random_state=seed),
                             LinearRegression())
    raise ValueError(f"unknown model family {family!r}")


def _effective_components(estimator: Any) -> dict[str, Any]:
    """Report the component count actually used, so a clamp is visible in the report."""
    for attr in ("n_components",):
        if hasattr(estimator, attr):
            return {attr: int(getattr(estimator, attr))}
    steps = getattr(estimator, "named_steps", None)   # PCR pipeline
    if steps and "pca" in steps:
        return {"n_components": int(steps["pca"].n_components)}
    return {}


def fit(
    spec: TrainingSpec,
    X: pd.DataFrame,
    y: pd.Series,
    *,
    X_valid: pd.DataFrame | None = None,
    y_valid: pd.Series | None = None,
) -> FittedModel:
    """Train ``spec.model_family`` on ``(X, y)``.

    A validation block, when supplied, is used only by LightGBM for early stopping — the
    other families have no iterative budget to stop. Every family is seeded from
    ``spec.seed`` so a refit is reproducible, which is what the report's consistency check
    asserts.
    """
    cols = [str(c) for c in X.columns]
    Xa = np.nan_to_num(X.to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    ya = np.nan_to_num(y.to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    t0 = time.perf_counter()
    best_iter: int | None = None

    if spec.model_family == "lgbm":
        import lightgbm as lgb

        booster_params, fit_args = _split_lgb_params(spec.params, spec.seed)
        dtrain = lgb.Dataset(Xa, ya)
        valid_sets, callbacks = [], []
        stop = int(fit_args.get("early_stopping_rounds") or 0)
        if X_valid is not None and y_valid is not None and len(X_valid) and stop > 0:
            dvalid = lgb.Dataset(
                np.nan_to_num(X_valid.reindex(columns=cols).to_numpy(dtype=float), nan=0.0),
                np.nan_to_num(y_valid.to_numpy(dtype=float), nan=0.0), reference=dtrain)
            valid_sets = [dvalid]
            callbacks = [lgb.early_stopping(stop, verbose=False)]
        estimator = lgb.train(
            booster_params, dtrain,
            num_boost_round=int(fit_args.get("num_boost_round", 500)),
            valid_sets=valid_sets or None, callbacks=callbacks or None,
        )
        best_iter = getattr(estimator, "best_iteration", None) or None
        resolved = {**booster_params, **fit_args}
    else:
        estimator = _build_estimator(spec.model_family, spec.params, spec.seed,
                                     n_features=Xa.shape[1], n_samples=Xa.shape[0])
        estimator.fit(Xa, ya)
        resolved = {**family_defaults(spec.model_family), **(spec.params or {})}
        resolved.update(_effective_components(estimator))

    adapter = _Adapter(estimator, spec.model_family)
    in_sample = adapter.predict(Xa)
    return FittedModel(
        model=adapter, family=spec.model_family, feature_cols=cols,
        fit_seconds=round(time.perf_counter() - t0, 3), n_train=int(len(ya)),
        best_iteration=best_iter,
        pred_mean=float(np.nanmean(in_sample)) if in_sample.size else 0.0,
        pred_std=float(np.nanstd(in_sample)) or 1.0,
        resolved_params=resolved,
    )


# --------------------------------------------------------------------------- #
# Ensembling
# --------------------------------------------------------------------------- #
class _EnsembleBooster:
    """``.predict(ndarray)`` over the union feature space, combining standardized members.

    Each member is scored on its own columns (selected by position out of the union matrix),
    standardized by the mean/std of its own training predictions, then weighted and summed.
    Standardizing makes the combination scale-invariant across families — an elastic net and
    a GBDT do not produce comparable magnitudes — while staying a *pointwise* function, so
    it works through ``qlib_alpha.predict`` which has no date grouping available.
    """

    def __init__(self, members: Sequence[tuple[np.ndarray, _Adapter, float, float, float]]) -> None:
        self._members = list(members)

    def predict(self, X: Any) -> np.ndarray:
        arr = np.asarray(X, dtype=float)
        total = np.zeros(arr.shape[0], dtype=float)
        wsum = 0.0
        for idx, adapter, weight, mean, std in self._members:
            raw = adapter.predict(arr[:, idx])
            total += weight * ((raw - mean) / (std or 1.0))
            wsum += weight
        return total / wsum if wsum else total


@dataclass
class EnsembleAlpha:
    """Champion wrapper for a combination of candidates. Same contract as ``FittedModel``."""

    model: _EnsembleBooster
    family: str
    feature_cols: list[str]
    members: list[dict[str, Any]] = field(default_factory=list)
    fit_seconds: float = 0.0
    n_train: int = 0
    best_iteration: int | None = None
    resolved_params: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> pd.Series:
        if X.empty:
            return pd.Series(dtype=float)
        aligned = X.reindex(columns=self.feature_cols).fillna(0.0)
        return pd.Series(self.model.predict(aligned.to_numpy(dtype=float)),
                         index=X.index, name="alpha")

    def importance(self) -> pd.Series:
        return pd.Series(dtype=float)

    def shap_values(self, X: pd.DataFrame, max_rows: int = 2000) -> pd.Series:
        return pd.Series(dtype=float)


def build_ensemble(
    members: Sequence[FittedModel],
    weights: Sequence[float] | None = None,
) -> EnsembleAlpha:
    """Combine fitted candidates over the union of their feature spaces.

    Huang, Capretz & Ho (arXiv:2202.05702) found a bagged combination beat every constituent
    model *and* the benchmark index; a research loop produces its constituents for free, so
    the combination is worth competing against the best single round rather than assuming
    the single best is the champion.
    """
    members = [m for m in members if m is not None]
    if not members:
        raise ValueError("cannot build an ensemble with no members")
    w = list(weights) if weights is not None else [1.0] * len(members)
    if len(w) != len(members):
        raise ValueError("weights must align with members")

    union: list[str] = []
    seen: set[str] = set()
    for m in members:
        for c in m.feature_cols:
            if c not in seen:
                seen.add(c)
                union.append(c)
    pos = {c: i for i, c in enumerate(union)}

    packed = [
        (np.array([pos[c] for c in m.feature_cols], dtype=int), m.model,
         float(wi), float(m.pred_mean), float(m.pred_std))
        for m, wi in zip(members, w)
    ]
    return EnsembleAlpha(
        model=_EnsembleBooster(packed), family="ensemble", feature_cols=union,
        members=[{"family": m.family, "n_features": len(m.feature_cols), "weight": float(wi)}
                 for m, wi in zip(members, w)],
        fit_seconds=float(sum(m.fit_seconds for m in members)),
        n_train=int(max(m.n_train for m in members)),
        resolved_params={"members": len(members)},
    )
