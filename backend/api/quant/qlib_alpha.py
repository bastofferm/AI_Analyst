"""Cross-sectional LightGBM alpha model (return prediction) built on qlib.

Wraps qlib's ``LGBModel`` (the genuine gap in the app: every ``mu`` elsewhere is a
naive historical mean) to forecast forward stock returns from the monthly
fundamental panel in :mod:`api.quant.qlib_data`.

Design notes
------------
* ``AlphaLGB`` subclasses ``qlib.contrib.model.gbdt.LGBModel`` and overrides ``fit``
  to drop the ``qlib.workflow.R`` metric logging, so training needs **no**
  ``qlib.init`` and no mlflow tracking backend (the installed mlflow 3.x rejects the
  file store). Training otherwise uses the real qlib ``DatasetH`` pipeline.
* Prediction runs the stored LightGBM booster directly on a reindexed feature matrix,
  so it works on any panel from :func:`qlib_data.build_panel` (including a single
  live month) without rebuilding a ``DatasetH``.
* Artifacts are dilled to ``QLIB_ALPHA_MODEL_DIR`` (default ``<project>/output/
  quant_models``) and lazily cached per jurisdiction for the router / committee node.

CLI::

    python -m api.quant.qlib_alpha train --jurisdiction US --start 2022-01-01
    python -m api.quant.qlib_alpha predict --jurisdiction US
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from . import qlib_data

logger = logging.getLogger("mzqa.quant.qlib_alpha")

# Reasonable regularized defaults for a monthly cross-sectional GBDT.
DEFAULT_LGB_PARAMS: dict[str, Any] = {
    "loss": "mse",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "max_depth": 6,
    "colsample_bytree": 0.8,
    "subsample": 0.8,
    "subsample_freq": 1,
    "lambda_l1": 1.0,
    "lambda_l2": 1.0,
    "min_child_samples": 50,
    "num_boost_round": 500,
    "early_stopping_rounds": 40,
}


def _model_dir() -> Path:
    env = os.environ.get("QLIB_ALPHA_MODEL_DIR")
    if env:
        return Path(env)
    # backend/api/quant/qlib_alpha.py -> parents[3] == project root (AI_Analyst)
    return Path(__file__).resolve().parents[3] / "output" / "quant_models"


def _artifact_path(jurisdiction: str) -> Path:
    # model_key may be "INTL:DE" — colons are illegal in Windows filenames, so use "_".
    key = jurisdiction.lower().replace(":", "_")
    return _model_dir() / f"alpha_{key}.dill"


def _lgb():  # lazy import so module import stays cheap / side-effect free
    import lightgbm as lgb
    from qlib.contrib.model.gbdt import LGBModel

    class AlphaLGB(LGBModel):
        """LGBModel without qlib-recorder logging (no qlib.init / mlflow needed)."""

        def fit(self, dataset, num_boost_round=None, early_stopping_rounds=None,
                verbose_eval=0, evals_result=None, reweighter=None, **kwargs):
            if evals_result is None:
                evals_result = {}
            ds_l = self._prepare_data(dataset, reweighter)
            ds, names = list(zip(*ds_l))
            callbacks = [lgb.log_evaluation(period=verbose_eval), lgb.record_evaluation(evals_result)]
            stop = self.early_stopping_rounds if early_stopping_rounds is None else early_stopping_rounds
            if stop and len(ds) > 1:
                callbacks.insert(0, lgb.early_stopping(stop))
            self.model = lgb.train(
                self.params, ds[0],
                num_boost_round=self.num_boost_round if num_boost_round is None else num_boost_round,
                valid_sets=ds, valid_names=names, callbacks=callbacks, **kwargs)
            self.evals_result_ = evals_result
            return self

    return AlphaLGB


@dataclass
class AlphaArtifact:
    """A trained alpha model plus everything needed to score a fresh panel."""

    model: Any                       # fitted AlphaLGB (holds the LightGBM booster)
    jurisdiction: str
    label: str                       # forward_1m | forward_3m
    families: tuple[str, ...]
    metric_ids: list[str]            # exact feature metric_ids (column identity/order)
    feature_cols: list[str]
    trained_at: str
    train_range: tuple[str, str]
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def horizon_months(self) -> int:
        return 3 if self.label == "forward_3m" else 1

    @property
    def annualization(self) -> float:
        return 12.0 / self.horizon_months


def evaluate(pred: pd.Series, label: pd.Series) -> dict[str, float]:
    """IC / RankIC / ICIR of a prediction Series against realized labels."""
    from qlib.contrib.eva.alpha import calc_ic

    ic, ric = calc_ic(pred, label)
    out = {
        "ic_mean": float(ic.mean()),
        "ic_std": float(ic.std()),
        "rank_ic_mean": float(ric.mean()),
        "rank_ic_std": float(ric.std()),
        "n_dates": int(ic.shape[0]),
    }
    out["icir"] = float(out["ic_mean"] / out["ic_std"]) if out["ic_std"] else 0.0
    out["rank_icir"] = float(out["rank_ic_mean"] / out["rank_ic_std"]) if out["rank_ic_std"] else 0.0
    return out


def train(
    jurisdiction: str = "US",
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    label: str = "forward_1m",
    families: Sequence[str] = qlib_data.DEFAULT_FAMILIES,
    params: dict[str, Any] | None = None,
    valid_frac: float = 0.15,
    test_frac: float = 0.15,
    min_names_per_date: int = 30,
) -> AlphaArtifact:
    """Build the panel, train ``AlphaLGB``, and return a scored artifact.

    Trains on ``train``, early-stops on ``valid``, and reports IC on the held-out
    ``test`` segment in ``artifact.metrics``. Raises ``ValueError`` if the warehouse
    yields no usable panel.
    """
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP

    families = tuple(families)
    metric_ids = qlib_data.feature_metric_ids(jurisdiction, start=start, end=end, families=families)
    panel = qlib_data.build_panel(
        jurisdiction, start=start, end=end, label=label, metric_ids=metric_ids,
        min_names_per_date=min_names_per_date,
    )
    if panel.empty:
        raise ValueError(f"empty alpha panel for {jurisdiction} [{start}..{end}]")

    segments = qlib_data.time_segments(panel, valid_frac=valid_frac, test_frac=test_frac)
    handler = DataHandlerLP.from_df(panel)
    dataset = DatasetH(handler, segments=segments)

    cfg = {**DEFAULT_LGB_PARAMS, **(params or {})}
    AlphaLGB = _lgb()
    model = AlphaLGB(**cfg)
    model.fit(dataset, verbose_eval=0)

    feature_cols = [str(c) for c in panel["feature"].columns]
    metrics: dict[str, float] = {}
    try:
        pred = model.predict(dataset, segment="test")
        label_df = dataset.prepare("test", col_set="label")
        metrics = evaluate(pred, label_df.iloc[:, 0])
    except Exception:  # noqa: BLE001 - evaluation is advisory, never fail training on it
        logger.warning("alpha test-segment evaluation failed", exc_info=True)

    tr = segments["train"]
    artifact = AlphaArtifact(
        model=model,
        jurisdiction=jurisdiction.upper(),
        label=label,
        families=families,
        metric_ids=metric_ids,
        feature_cols=feature_cols,
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        train_range=(str(pd.Timestamp(tr[0]).date()), str(pd.Timestamp(segments["test"][1]).date())),
        metrics=metrics,
    )
    logger.info("trained alpha %s: %s", jurisdiction, metrics)
    return artifact


def predict(artifact: AlphaArtifact, panel: pd.DataFrame) -> pd.Series:
    """Score a feature/label panel; returns expected returns indexed by (datetime, instrument)."""
    if panel.empty:
        return pd.Series(dtype=float)
    X = panel["feature"].reindex(columns=artifact.feature_cols).fillna(0.0)
    booster = artifact.model.model
    scores = booster.predict(X.values)
    return pd.Series(scores, index=X.index, name="alpha")


def predict_cross_section(
    artifact: AlphaArtifact,
    *,
    end: date | str | None = None,
    lookback_months: int = 6,
    as_of: pd.Timestamp | None = None,
) -> pd.Series:
    """Expected returns for each instrument's LATEST available month, one row per instrument.

    Builds a **features-only** panel (``require_label=False``) over a short window with the
    artifact's exact ``metric_ids``, scores it, and keeps each instrument's most recent row.

    Point-in-time fundamentals are 90d-lagged and month-aligned, so any single month holds only
    the names that "refreshed" that month — the *latest* month is the thinnest (e.g. a live US
    panel had ~3,500 names one month but only ~318 the next). Snapshotting that single month
    dropped ~90% of the universe (and, notably, most mega-caps). Taking the latest row PER
    INSTRUMENT — each already cross-sectionally z-scored within its own month, so the score is
    comparable — recovers the full recent cross-section. ``as_of`` caps the window (no lookahead).
    """
    end = end or date.today()
    start = (pd.Timestamp(end) - pd.DateOffset(months=lookback_months)).date()
    panel = qlib_data.build_panel(
        artifact.jurisdiction, start=start, end=end, label=artifact.label,
        metric_ids=artifact.metric_ids, min_names_per_date=1, require_label=False,
    )
    if panel.empty:
        return pd.Series(dtype=float)
    scores = predict(artifact, panel).sort_index()
    if as_of is not None:
        scores = scores[scores.index.get_level_values("datetime") <= pd.Timestamp(as_of)]
        if scores.empty:
            return pd.Series(dtype=float)
    # Latest available month PER instrument (sorted by datetime, so tail(1) is the newest row).
    cross = scores.groupby(level="instrument").tail(1).droplevel("datetime")
    return cross.sort_values(ascending=False)


# --------------------------------------------------------------------------- #
# Persistence + lazy cache
# --------------------------------------------------------------------------- #
def save(artifact: AlphaArtifact, path: Path | None = None) -> Path:
    import dill

    path = path or _artifact_path(artifact.jurisdiction)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        dill.dump(artifact, fh)
    logger.info("saved alpha artifact -> %s", path)
    return path


def load(path: Path) -> AlphaArtifact:
    import dill

    with open(path, "rb") as fh:
        return dill.load(fh)


_CACHE: dict[str, tuple[float, AlphaArtifact]] = {}


def get_model(jurisdiction: str = "US") -> AlphaArtifact | None:
    """Load the persisted artifact for ``jurisdiction`` (cached; hot-reloaded on mtime).

    Returns ``None`` if no artifact exists yet — callers must degrade gracefully so the
    app runs before any model is trained.
    """
    path = _artifact_path(jurisdiction)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _CACHE.get(jurisdiction.upper())
    if cached and cached[0] == mtime:
        return cached[1]
    artifact = load(path)
    _CACHE[jurisdiction.upper()] = (mtime, artifact)
    return artifact


def train_and_save(jurisdiction: str = "US", **kwargs: Any) -> AlphaArtifact:
    artifact = train(jurisdiction, **kwargs)
    save(artifact)
    return artifact


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="qlib cross-sectional alpha model")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("train", help="train + persist an alpha model")
    pt.add_argument("--jurisdiction", default="US")
    pt.add_argument("--start", default=None)
    pt.add_argument("--end", default=None)
    pt.add_argument("--label", default="forward_1m", choices=list(qlib_data.LABEL_HORIZONS))

    pp = sub.add_parser("predict", help="print latest cross-section from the saved model")
    pp.add_argument("--jurisdiction", default="US")
    pp.add_argument("--top", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "train":
        art = train_and_save(args.jurisdiction, start=args.start, end=args.end, label=args.label)
        print(f"trained {art.jurisdiction} label={art.label} trained_at={art.trained_at}")
        print(f"metrics: {art.metrics}")
        print(f"saved -> {_artifact_path(art.jurisdiction)}")
    elif args.cmd == "predict":
        art = get_model(args.jurisdiction)
        if art is None:
            print(f"no trained model for {args.jurisdiction}; run 'train' first")
            return 1
        cross = predict_cross_section(art)
        print(f"as-of latest month, top {args.top} expected forward-{art.horizon_months}m returns:")
        print(cross.head(args.top).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
