"""Phase-label calibration for jurisdiction-local cycle models.

Latent factors from PCA, VAEs, and HMMs have arbitrary sign and, for VAEs,
arbitrary dimension order. This module maps those latent coordinates into
human-readable phase names by anchoring them to local recession/stress proxies
when those proxies are available in the macro store.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.connection import connect


PHASES = ("contraction", "late_cycle", "mid_expansion", "early_expansion")
DEFAULT_PHASE_THRESHOLDS = {
    "contraction": 0.80,
    "late_cycle": 0.55,
    "mid_expansion": 0.30,
}
NBER_US_RECESSION_WINDOWS = (
    ("1960-04", "1961-02"),
    ("1969-12", "1970-11"),
    ("1973-11", "1975-03"),
    ("1980-01", "1980-07"),
    ("1981-07", "1982-11"),
    ("1990-07", "1991-03"),
    ("2001-03", "2001-11"),
    ("2007-12", "2009-06"),
    ("2020-02", "2020-04"),
)


@dataclass(frozen=True)
class PhaseCalibration:
    selected_factor: str
    selected_factor_index: int
    raw_percentile: pd.Series
    stress_percentile: pd.Series
    high_score_is_stress: bool
    benchmark_correlation: float | None
    benchmark_points: int
    benchmark_series: tuple[str, ...]
    method: str

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("raw_percentile", None)
        out.pop("stress_percentile", None)
        return out


@dataclass(frozen=True)
class PhaseLabelDecision:
    date: Any
    proposed_label: str | None
    display_label: str | None
    label_source: str
    switch_probability: float | None = None
    override_reason: str | None = None


def apply_display_label_policy(
    rows: list[dict[str, Any]],
    *,
    probability_threshold: float = 0.55,
    overrides: list[dict[str, Any]] | None = None,
) -> list[PhaseLabelDecision]:
    """Apply investor-facing carry-forward and manual override rules.

    Expected row keys are ``date``, ``label`` and optionally ``probabilities``.
    Overrides are dictionaries with effective_start/effective_end dates,
    override_label, and optional reason/source fields. Raw model labels are not
    changed; this returns a separate display decision stream.
    """

    if not rows:
        return []
    active_overrides = overrides or []
    ordered = sorted(rows, key=lambda item: str(item.get("date") or ""))
    previous_display: str | None = None
    decisions: list[PhaseLabelDecision] = []
    threshold = float(probability_threshold)

    for row in ordered:
        row_date = row.get("date")
        proposed = row.get("label")
        probabilities = row.get("probabilities") or {}
        proposed_probability = _probability_for_label(probabilities, proposed)
        override = _matching_label_override(active_overrides, row_date)

        if override:
            display = str(override.get("override_label") or proposed or previous_display or "")
            display = display or None
            source = str(override.get("source") or "manual_override")
            reason = override.get("reason")
        elif (
            previous_display is not None
            and proposed is not None
            and proposed != previous_display
            and proposed_probability is not None
            and proposed_probability < threshold
        ):
            display = previous_display
            source = "carried_forward_low_probability"
            reason = None
        else:
            display = proposed
            source = "model"
            reason = None

        previous_display = display or previous_display
        decisions.append(
            PhaseLabelDecision(
                date=row_date,
                proposed_label=proposed,
                display_label=display,
                label_source=source,
                switch_probability=proposed_probability,
                override_reason=str(reason) if reason else None,
            )
        )

    return decisions


def _probability_for_label(probabilities: Any, label: str | None) -> float | None:
    if not label or not isinstance(probabilities, dict):
        return None
    try:
        value = float(probabilities.get(label))
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return max(0.0, min(1.0, value))


def _matching_label_override(overrides: list[dict[str, Any]], row_date: Any) -> dict[str, Any] | None:
    if row_date is None:
        return None
    row_ts = pd.Timestamp(row_date).date()
    for override in overrides:
        label = override.get("override_label")
        if not label:
            continue
        start = override.get("effective_start") or override.get("start_date")
        end = override.get("effective_end") or override.get("end_date")
        start_date = pd.Timestamp(start).date() if start else None
        end_date = pd.Timestamp(end).date() if end else None
        if start_date and row_ts < start_date:
            continue
        if end_date and row_ts > end_date:
            continue
        return override
    return None


def phase_from_stress_percentile(value: float | None, thresholds: dict[str, float] | None = None) -> str | None:
    if value is None or not np.isfinite(value):
        return None
    active = thresholds or DEFAULT_PHASE_THRESHOLDS
    if value >= float(active.get("contraction", DEFAULT_PHASE_THRESHOLDS["contraction"])):
        return "contraction"
    if value >= float(active.get("late_cycle", DEFAULT_PHASE_THRESHOLDS["late_cycle"])):
        return "late_cycle"
    if value >= float(active.get("mid_expansion", DEFAULT_PHASE_THRESHOLDS["mid_expansion"])):
        return "mid_expansion"
    return "early_expansion"


def phase_series_from_stress(stress: pd.Series, thresholds: dict[str, float] | None = None) -> pd.Series:
    return stress.map(lambda value: phase_from_stress_percentile(value, thresholds))


def smooth_stress_percentile(stress: pd.Series, *, span: int = 6) -> pd.Series:
    if stress.empty:
        return pd.Series(dtype=float, index=stress.index)
    clean = pd.to_numeric(stress, errors="coerce").astype(float)
    return clean.ewm(span=max(1, int(span)), min_periods=1, adjust=False).mean().clip(lower=0.0, upper=1.0)


def nber_recession_series(jurisdiction: str, index: pd.Index) -> pd.Series:
    """Monthly US recession target from NBER peak/trough dates.

    The model never receives this as a feature. It is a target/benchmark used
    after the latent stress score exists, following FRED USREC's convention:
    the recession period starts in the month after the NBER peak and runs
    through the trough month.
    """
    out = pd.Series(False, index=index, dtype=bool)
    if jurisdiction.upper() != "US" or len(index) == 0:
        return out
    months = pd.to_datetime(index).to_period("M")
    for peak, trough in NBER_US_RECESSION_WINDOWS:
        start = pd.Period(peak, freq="M") + 1
        end = pd.Period(trough, freq="M")
        out.loc[(months >= start) & (months <= end)] = True
    return out


def calibrate_phase_thresholds(
    jurisdiction: str,
    stress_percentile: pd.Series,
    *,
    default_thresholds: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    base = dict(default_thresholds or DEFAULT_PHASE_THRESHOLDS)
    target = nber_recession_series(jurisdiction, stress_percentile.index)
    joined = pd.concat(
        [
            pd.to_numeric(stress_percentile, errors="coerce").rename("stress"),
            target.astype(int).rename("recession"),
        ],
        axis=1,
    ).dropna()
    if jurisdiction.upper() != "US" or joined.empty or joined["recession"].sum() < 2:
        return base, {
            "method": "default_thresholds",
            "target": "none",
            "thresholds": base,
            "target_points": int(len(joined)),
            "target_positive": int(joined["recession"].sum()) if not joined.empty else 0,
        }

    best_threshold = float(base["contraction"])
    best_score = -1.0
    for threshold in np.arange(0.45, 0.91, 0.025):
        pred = joined["stress"] >= threshold
        actual = joined["recession"].astype(bool)
        positives = actual.sum()
        negatives = (~actual).sum()
        if positives == 0 or negatives == 0:
            continue
        tpr = float((pred & actual).sum() / positives)
        tnr = float(((~pred) & (~actual)).sum() / negatives)
        score = 0.5 * (tpr + tnr)
        if score > best_score or (score == best_score and abs(threshold - base["contraction"]) < abs(best_threshold - base["contraction"])):
            best_score = score
            best_threshold = float(threshold)

    thresholds = dict(base)
    thresholds["contraction"] = best_threshold
    thresholds["late_cycle"] = min(float(base["late_cycle"]), max(0.35, best_threshold - 0.10))
    thresholds["mid_expansion"] = min(float(base["mid_expansion"]), max(0.20, thresholds["late_cycle"] - 0.15))
    return thresholds, {
        "method": "nber_balanced_accuracy",
        "target": "NBER_USREC_trough_method",
        "thresholds": thresholds,
        "target_points": int(len(joined)),
        "target_positive": int(joined["recession"].sum()),
        "balanced_accuracy": best_score,
    }


def enforce_min_phase_duration(labels: pd.Series, *, min_duration: int = 3) -> pd.Series:
    if labels.empty or min_duration <= 1:
        return labels.copy()
    values = list(labels)
    for _ in range(10):
        changed = False
        runs: list[tuple[int, int, Any]] = []
        start = 0
        for i in range(1, len(values) + 1):
            if i == len(values) or values[i] != values[start]:
                runs.append((start, i, values[start]))
                start = i
        for idx, (start, end, label) in enumerate(runs):
            if label is None or end - start >= min_duration:
                continue
            prev_label = runs[idx - 1][2] if idx > 0 else None
            next_label = runs[idx + 1][2] if idx + 1 < len(runs) else None
            replacement = prev_label if prev_label is not None else next_label
            if replacement is None or replacement == label:
                continue
            for j in range(start, end):
                values[j] = replacement
            changed = True
        if not changed:
            break
    return pd.Series(values, index=labels.index)


def phase_confidence(value: float | None) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return float(max(0.45, min(0.90, abs(float(value) - 0.5) * 1.5 + 0.45)))


def phase_probs(label: str | None, confidence: float | None = None) -> dict[str, float]:
    if not label or label not in PHASES:
        return {}
    conf = float(confidence if confidence is not None else 0.70)
    rest = (1.0 - conf) / (len(PHASES) - 1)
    return {phase: (conf if phase == label else rest) for phase in PHASES}


def rolling_percentile(series: pd.Series, *, lookback: int = 120, min_periods: int = 24) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    window = min(lookback, len(series))
    pct = series.rolling(window, min_periods=min(min_periods, window)).apply(
        lambda w: float((w <= w.iloc[-1]).mean()),
        raw=False,
    )
    return pct.fillna(series.rank(pct=True))


def calibrate_phase_series(jurisdiction: str, factors: pd.DataFrame) -> PhaseCalibration:
    if factors.empty:
        empty = pd.Series(dtype=float, index=factors.index)
        return PhaseCalibration(
            selected_factor="",
            selected_factor_index=0,
            raw_percentile=empty,
            stress_percentile=empty,
            high_score_is_stress=True,
            benchmark_correlation=None,
            benchmark_points=0,
            benchmark_series=(),
            method="empty_factor_matrix",
        )

    matrix = factors.replace([np.inf, -np.inf], np.nan).astype(float)
    benchmark, benchmark_series = _load_benchmark_stress(jurisdiction, matrix.index)
    best_col = str(matrix.columns[0])
    best_idx = 0
    best_corr: float | None = None
    best_points = 0

    if not benchmark.empty:
        for idx, col in enumerate(matrix.columns):
            joined = pd.concat([matrix[col].rename("score"), benchmark.rename("benchmark")], axis=1).dropna()
            if len(joined) < 12:
                continue
            corr = joined["score"].corr(joined["benchmark"])
            if corr is None or not np.isfinite(corr):
                continue
            if best_corr is None or abs(float(corr)) > abs(best_corr):
                best_col = str(col)
                best_idx = idx
                best_corr = float(corr)
                best_points = int(len(joined))

    score = matrix.iloc[:, best_idx]
    raw_pct = rolling_percentile(score)
    if best_corr is not None and abs(best_corr) >= 0.05:
        high_score_is_stress = best_corr > 0
        method = "benchmark_correlation"
    else:
        high_score_is_stress = True
        method = "fallback_high_score_is_stress"

    stress_pct = raw_pct if high_score_is_stress else 1.0 - raw_pct
    stress_pct = stress_pct.clip(lower=0.0, upper=1.0)
    return PhaseCalibration(
        selected_factor=best_col,
        selected_factor_index=best_idx,
        raw_percentile=raw_pct,
        stress_percentile=stress_pct,
        high_score_is_stress=high_score_is_stress,
        benchmark_correlation=best_corr,
        benchmark_points=best_points,
        benchmark_series=tuple(benchmark_series),
        method=method,
    )


def state_phase_mapping(labels: np.ndarray, factors: pd.DataFrame, n_states: int, calibration: PhaseCalibration) -> dict[int, str]:
    if factors.empty:
        return {state: PHASES[min(state, len(PHASES) - 1)] for state in range(n_states)}
    score = factors.iloc[:, min(calibration.selected_factor_index, factors.shape[1] - 1)].to_numpy(dtype=float)
    means = []
    for state in range(n_states):
        if np.any(labels == state):
            means.append((state, float(np.nanmean(score[labels == state]))))
        else:
            means.append((state, float("-inf")))
    ordered = [state for state, _ in sorted(means, key=lambda item: item[1], reverse=calibration.high_score_is_stress)]
    return {state: PHASES[min(i, len(PHASES) - 1)] for i, state in enumerate(ordered)}


def _load_benchmark_stress(jurisdiction: str, index: pd.Index) -> tuple[pd.Series, list[str]]:
    if len(index) == 0:
        return pd.Series(dtype=float), []
    start = pd.Timestamp(index.min()).date()
    end = pd.Timestamp(index.max()).date()
    sql = """
        SELECT f.date, f.series_id, s.units, f.value::float AS value
        FROM   fact_macro f
        JOIN   ref_macro_series s ON s.series_id = f.series_id
        WHERE  s.jurisdiction = %s
          AND  s.is_active = TRUE
          AND  s.category IN ('state_probability', 'state_proxy')
          AND  f.date BETWEEN %s AND %s
          AND  f.value IS NOT NULL
        ORDER  BY f.date, f.series_id
    """
    try:
        with connect() as conn:
            df = pd.read_sql(sql, conn, params=(jurisdiction, start, end))
    except Exception:
        return pd.Series(dtype=float), []
    if df.empty:
        return pd.Series(dtype=float), []

    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp("M")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    units = df["units"].fillna("").str.lower()
    looks_percent = units.str.contains("percent|percentage")
    df.loc[looks_percent, "value"] = df.loc[looks_percent, "value"] / 100.0
    too_large = df["value"].abs() > 1.5
    df.loc[too_large, "value"] = df.loc[too_large, "value"] / 100.0
    df["value"] = df["value"].clip(lower=0.0, upper=1.0)
    out = df.groupby("month")["value"].mean().sort_index()
    target_index = pd.to_datetime(index).to_period("M").to_timestamp("M")
    out = out.reindex(target_index)
    out.index = index
    return out, sorted(str(v) for v in df["series_id"].dropna().unique())
