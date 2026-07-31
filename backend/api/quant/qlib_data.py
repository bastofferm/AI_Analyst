"""Postgres -> qlib panel adapter for the cross-sectional alpha model.

Builds a qlib-style feature/label panel (MultiIndex ``(datetime, instrument)`` rows,
MultiIndex ``("feature"|"label", name)`` columns) that ``qlib.data.dataset`` /
``DataHandlerLP.from_df`` consume directly — see ``api.quant.qlib_alpha``.

The heavy lifting (point-in-time alignment of fundamentals, forward-return labels,
metric-family selection) is **reused** from the regime-IC engine
``xbrl_sec.sec.cycle.ic`` so the alpha model and the IC research share one data path
and one lookahead-safe alignment (``period_end + 90d -> month-end``). All warehouse
access is lazy (imported inside functions) so importing this module is side-effect free.

The panel is **monthly and cross-sectional**: features are importance-ranked
fundamentals (value / quality / growth / market-factor families) z-scored within each
month; the label is the forward 1m/3m stock return. Expected returns are therefore
monthly forecasts — annualize by ``* 12`` when feeding a portfolio optimizer.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

# Fundamental families to pull as features (keys of ic._METRIC_FAMILY_PATTERNS).
DEFAULT_FAMILIES: tuple[str, ...] = ("value", "quality", "growth", "market_factor")

# Supported forward-return label horizons (columns produced by _load_monthly_returns).
LABEL_HORIZONS: tuple[str, ...] = ("forward_1m", "forward_3m")

FEATURE = "feature"
LABEL = "label"
LABEL_COL = "y"


def feature_metric_ids(
    jurisdiction: str,
    *,
    start: Any | None = None,
    end: Any | None = None,
    families: Sequence[str] = DEFAULT_FAMILIES,
) -> list[str]:
    """Importance-ranked metric_ids across ``families`` (deduped, order-stable)."""
    from xbrl_sec.sec.cycle.ic import _load_metric_ids  # lazy: DB + project deps

    seen: set[str] = set()
    out: list[str] = []
    for fam in families:
        for mid in _load_metric_ids(jurisdiction, start=start, end=end, metric_family=fam):
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


def _cross_sectional_zscore(wide: pd.DataFrame, *, clip: float = 3.0) -> pd.DataFrame:
    """Z-score each feature within each date (level 0), winsorized to +/-clip.

    Mirrors qlib's ``CSZScoreNorm`` so features are comparable across the panel and
    robust to per-metric scale. Columns with zero cross-sectional variance -> 0.
    """
    grp = wide.groupby(level="datetime")
    mean = grp.transform("mean")
    std = grp.transform("std").replace(0.0, np.nan)
    z = (wide - mean) / std
    return z.clip(-clip, clip)


def build_panel(
    jurisdiction: str = "US",
    *,
    start: date | str | None = None,
    end: date | str | None = None,
    label: str = "forward_1m",
    families: Sequence[str] = DEFAULT_FAMILIES,
    metric_ids: Iterable[str] | None = None,
    min_names_per_date: int = 30,
    normalize: bool = True,
    require_label: bool = True,
) -> pd.DataFrame:
    """Return a qlib feature/label panel for ``jurisdiction`` over ``[start, end]``.

    Rows: MultiIndex ``(datetime, instrument)`` (month-end timestamp, ticker), sorted.
    Cols: MultiIndex ``("feature", metric_id...)`` + ``("label", "y")``.
    Empty ``DataFrame`` if the warehouse has no usable rows in range.
    """
    if label not in LABEL_HORIZONS:
        raise ValueError(f"label must be one of {LABEL_HORIZONS}, got {label!r}")

    from xbrl_sec.sec.cycle.ic import _load_metrics, _load_monthly_returns  # lazy

    ids = list(metric_ids) if metric_ids is not None else feature_metric_ids(
        jurisdiction, start=start, end=end, families=families
    )
    if not ids:
        return pd.DataFrame()

    metrics = _load_metrics(jurisdiction, start=start, end=end, metric_ids=ids)
    if metrics.empty:
        return pd.DataFrame()

    # --- features: long -> wide, indexed by (datetime, instrument) ---
    metrics = metrics.copy()
    # JP fundamentals carry a ".T" suffix (e.g. "1301.T") while JP prices/returns use the
    # bare code ("1301"); strip it so the two sides join (US tickers are unaffected).
    metrics["ticker"] = metrics["ticker"].astype(str).str.replace(r"\.[Tt]$", "", regex=True)
    # errors="coerce" guards against corrupt warehouse dates (e.g. a period_end in year
    # 5926 that aligns to an out-of-bounds month) -> NaT, then dropped.
    metrics["datetime"] = pd.to_datetime(metrics["date"], errors="coerce")
    metrics = metrics.dropna(subset=["datetime"])
    if metrics.empty:
        return pd.DataFrame()
    feat = (
        metrics.pivot_table(
            index=["datetime", "ticker"], columns="metric_id", values="value", aggfunc="last"
        )
        .rename_axis(index={"ticker": "instrument"})
        .sort_index()
    )
    feat.columns = [str(c) for c in feat.columns]

    if require_label:
        # --- label: forward return aligned on the same month-end ---
        forward_months = 3 if label == "forward_3m" else 1
        returns = _load_monthly_returns(jurisdiction, start=start, end=end, forward_months=forward_months)
        if returns.empty:
            return pd.DataFrame()
        ret = returns[["ticker", "date", label]].dropna(subset=[label]).copy()
        ret["ticker"] = ret["ticker"].astype(str).str.replace(r"\.[Tt]$", "", regex=True)
        ret["datetime"] = pd.to_datetime(ret["date"], errors="coerce")
        ret = ret.dropna(subset=["datetime"])
        lab = (
            ret.rename(columns={"ticker": "instrument", label: LABEL_COL})
            .set_index(["datetime", "instrument"])[LABEL_COL]
        )
        df = feat.join(lab, how="inner")
        df = df[df[LABEL_COL].notna()]
    else:
        # Prediction path: features only, and much cheaper. Joining the label would
        # drop the *latest* month (its forward return is not realized yet) — exactly
        # the cross-section we want to score.
        df = feat
    if df.empty:
        return pd.DataFrame()

    # Drop thin cross-sections (unstable ranking / normalization) — label-independent.
    if min_names_per_date > 1:
        sizes = df.groupby(level="datetime").size()
        keep = sizes[sizes >= min_names_per_date].index
        df = df[df.index.get_level_values("datetime").isin(keep)]
        if df.empty:
            return pd.DataFrame()

    feat_cols = [c for c in df.columns if c != LABEL_COL]
    X = df[feat_cols]
    if normalize:
        X = _cross_sectional_zscore(X)
    X = X.fillna(0.0)  # missing / neutralized feature -> cross-sectional mean (0)

    groups: dict[str, pd.DataFrame] = {FEATURE: X}
    if require_label:
        groups[LABEL] = df[[LABEL_COL]]
    panel = pd.concat(groups, axis=1)
    panel.index = panel.index.set_names(["datetime", "instrument"])
    return panel.sort_index()


def time_segments(
    panel: pd.DataFrame,
    *,
    valid_frac: float = 0.15,
    test_frac: float = 0.15,
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous chronological train/valid/test split by unique dates.

    Returns a ``segments`` dict for ``qlib.data.dataset.DatasetH`` (inclusive ranges).
    """
    dates = panel.index.get_level_values("datetime").unique().sort_values()
    n = len(dates)
    if n < 3:
        raise ValueError("panel has too few distinct dates to split")
    n_test = max(1, int(round(n * test_frac)))
    n_valid = max(1, int(round(n * valid_frac)))
    n_train = n - n_valid - n_test
    if n_train < 1:
        raise ValueError("valid_frac + test_frac too large for this panel")
    tr, va, te = dates[:n_train], dates[n_train : n_train + n_valid], dates[n_train + n_valid :]
    return {
        "train": (tr[0], tr[-1]),
        "valid": (va[0], va[-1]),
        "test": (te[0], te[-1]),
    }


def latest_prediction_date(panel: pd.DataFrame) -> pd.Timestamp | None:
    """Most recent month in the panel (the as-of date for live scoring)."""
    if panel.empty:
        return None
    return panel.index.get_level_values("datetime").max()
