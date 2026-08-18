"""``TrainingSpec`` — the parametrization the research agents search over.

One frozen dataclass holds every knob: sample selection, data-quality treatment, feature
selection, the purged split, the model family and its hyperparameters. Defaults reproduce
today's ``qlib_alpha.train`` behaviour exactly, so round 0 of a research run is a faithful
baseline of the incumbent pipeline and every later round is a measurable delta against it.

The agents never mutate a spec directly. The Researcher emits a *patch* — a plain dict — and
:func:`apply_patch` is the only way it becomes a spec. Every key is whitelisted, every value
is range-clamped or enum-checked, and anything unrecognized is dropped and reported. That is
the boundary that keeps a hallucinated ``{"learning_rate": 500}`` or ``{"os.system": "..."}``
away from the fit (the FlowMind principle: ground the model in a fixed API surface rather
than trusting free-form output).

Provenance of the non-default options:

* ``normalization="rank"`` and ``fill_missing="cross_sectional_median"`` — Kang, Ryu & Webb
  (Financial Innovation 11(1), 2025) use rank normalization to preserve ordinal information
  and median imputation specifically to blunt outliers. Today's code z-scores and fills with
  0.0, i.e. the mean, which is the value their argument is against.
* ``model_family`` beyond LightGBM — the same paper compares eight families and finds tree
  ensembles beat neural nets on short, noisy panels; the linear//dimension-reduction families
  are cheap comparators worth keeping in the search.
* ``feature_selector="importance"`` — Huang, Capretz & Ho (arXiv:2202.05702) improve weaker
  learners with RF-based feature selection on quarterly fundamentals.
* ``embargo_months`` — closes the leak in ``qlib_data.time_segments``, which splits
  contiguously so a ``forward_12m`` label straddles the train/valid boundary.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

from ..qlib_alpha import DEFAULT_LGB_PARAMS
from ..qlib_data import DEFAULT_FAMILIES, HORIZON_MONTHS

# Metric families selectable as feature blocks (keys of ic._METRIC_FAMILY_PATTERNS).
VALID_FAMILIES: frozenset[str] = frozenset(
    {"value", "quality", "growth", "market_factor", "accounting", "all"}
)
VALID_NORMALIZATION: frozenset[str] = frozenset({"zscore", "robust_zscore", "rank"})
VALID_FILL: frozenset[str] = frozenset({"zero", "cross_sectional_median", "drop_row"})
VALID_SELECTOR: frozenset[str] = frozenset({"none", "univariate_ic", "importance"})
VALID_NEUTRALIZE: frozenset[str] = frozenset({"sector", "size"})
VALID_FAMILY_MODELS: frozenset[str] = frozenset({"lgbm", "rf", "enet", "ridge", "pls", "pcr"})


@dataclass(frozen=True)
class TrainingSpec:
    """A complete, self-contained description of one training attempt."""

    # --- sample selection -------------------------------------------------- #
    start: str | None = None
    end: str | None = None
    min_names_per_date: int = 30
    min_market_cap_usd: float | None = None
    min_feature_coverage: float = 0.0      # drop a feature whose non-null share is below this
    min_obs_per_name: int = 1

    # --- data quality / outliers ------------------------------------------- #
    winsorize_features: float | None = None   # per-date quantile clip on RAW feature values
    winsorize_label: float | None = None      # per-date quantile clip on the LABEL
    normalization: str = "zscore"
    clip_sigma: float = 3.0
    fill_missing: str = "zero"
    neutralize: tuple[str, ...] = ()

    # --- features ----------------------------------------------------------- #
    families: tuple[str, ...] = DEFAULT_FAMILIES
    include_macro: bool = False
    feature_selector: str = "none"
    max_features: int | None = None
    feature_drop: tuple[str, ...] = ()

    # --- split -------------------------------------------------------------- #
    valid_frac: float = 0.15
    test_frac: float = 0.15
    embargo_months: int | None = None      # None -> the label horizon (see resolved_embargo)

    # --- model -------------------------------------------------------------- #
    model_family: str = "lgbm"
    params: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LGB_PARAMS))
    seed: int = 7

    # --- evaluation ---------------------------------------------------------- #
    wf_min_train_months: int = 24
    wf_refit_every: int = 12               # annual refit (Kang et al. expanding window)
    eval_topk: int = 30

    # --- context (not searched; carried so the spec is self-describing) ------ #
    label: str = "forward_1m"

    @property
    def horizon_months(self) -> int:
        return HORIZON_MONTHS.get(self.label, 1)

    @property
    def resolved_embargo(self) -> int:
        """Embargo actually applied: the explicit value, else the label horizon.

        Defaulting to the horizon is the minimum that removes label overlap — a
        ``forward_12m`` label observed at month *t* is not realized until *t+12*, so any
        training row within 12 months of a validation row shares realized future returns.
        """
        return self.horizon_months if self.embargo_months is None else int(self.embargo_months)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def hash(self) -> str:
        """Stable content hash — the reproducibility key stored with every iteration."""
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def default_spec(label: str = "forward_1m", **overrides: Any) -> TrainingSpec:
    """The incumbent pipeline expressed as a spec, plus any caller overrides.

    Overrides run through the same validation as an agent patch, so a caller-supplied
    ``spec_overrides`` from the REST layer cannot smuggle in an unclamped value either.
    """
    base = TrainingSpec(label=label)
    if not overrides:
        return base
    spec, _changes, _rejected = apply_patch(base, overrides)
    return spec


# --------------------------------------------------------------------------- #
# Patch validation
# --------------------------------------------------------------------------- #
# (low, high, caster). A None bound means unbounded on that side. `None` stays legal for
# fields typed Optional — see _OPTIONAL.
_NUMERIC: dict[str, tuple[Any, Any, Any]] = {
    "min_names_per_date": (1, 500, int),
    "min_market_cap_usd": (0.0, 1e13, float),
    "min_feature_coverage": (0.0, 0.95, float),
    "min_obs_per_name": (1, 240, int),
    "winsorize_features": (0.0, 0.20, float),
    "winsorize_label": (0.0, 0.20, float),
    "clip_sigma": (0.5, 10.0, float),
    "max_features": (2, 2000, int),
    "valid_frac": (0.05, 0.40, float),
    "test_frac": (0.05, 0.40, float),
    "embargo_months": (0, 36, int),
    "seed": (0, 2**31 - 1, int),
    "wf_min_train_months": (6, 240, int),
    "wf_refit_every": (1, 60, int),
    "eval_topk": (5, 500, int),
}
_OPTIONAL: frozenset[str] = frozenset(
    {"start", "end", "min_market_cap_usd", "winsorize_features", "winsorize_label",
     "max_features", "embargo_months"}
)
_ENUM: dict[str, frozenset[str]] = {
    "normalization": VALID_NORMALIZATION,
    "fill_missing": VALID_FILL,
    "feature_selector": VALID_SELECTOR,
    "model_family": VALID_FAMILY_MODELS,
}
# name -> allowed members (None = free-form strings, e.g. metric ids to drop)
_TUPLE: dict[str, frozenset[str] | None] = {
    "families": VALID_FAMILIES,
    "neutralize": VALID_NEUTRALIZE,
    "feature_drop": None,
}
_BOOL: frozenset[str] = frozenset({"include_macro"})
_DATE: frozenset[str] = frozenset({"start", "end"})

_MAX_DROPPED_FEATURES = 400

# Per-family hyperparameter bounds. Anything not listed here is dropped, so a family can
# never be handed a keyword its estimator does not accept.
_PARAM_BOUNDS: dict[str, dict[str, tuple[Any, Any, Any]]] = {
    "lgbm": {
        "learning_rate": (1e-4, 0.5, float),
        "num_leaves": (2, 512, int),
        "max_depth": (1, 32, int),
        "colsample_bytree": (0.1, 1.0, float),
        "subsample": (0.1, 1.0, float),
        "subsample_freq": (0, 20, int),
        "lambda_l1": (0.0, 1000.0, float),
        "lambda_l2": (0.0, 1000.0, float),
        "min_child_samples": (1, 5000, int),
        "num_boost_round": (10, 5000, int),
        "early_stopping_rounds": (0, 500, int),
        "num_threads": (1, 256, int),
    },
    "rf": {
        "n_estimators": (10, 2000, int),
        "max_depth": (1, 64, int),
        "min_samples_leaf": (1, 5000, int),
        "max_features": (0.01, 1.0, float),
        "n_jobs": (-1, 256, int),
    },
    "enet": {"alpha": (1e-6, 100.0, float), "l1_ratio": (0.0, 1.0, float),
             "max_iter": (100, 100000, int)},
    "ridge": {"alpha": (1e-6, 1e6, float)},
    "pls": {"n_components": (1, 200, int), "max_iter": (100, 100000, int)},
    "pcr": {"n_components": (1, 200, int)},
}

# Sensible starting hyperparameters per family, used when the Researcher switches family
# without supplying params (the common case — it proposes a family, not a full config).
_FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "lgbm": dict(DEFAULT_LGB_PARAMS),
    "rf": {"n_estimators": 300, "max_depth": 8, "min_samples_leaf": 50, "max_features": 0.5,
           "n_jobs": -1},
    "enet": {"alpha": 0.001, "l1_ratio": 0.5, "max_iter": 5000},
    "ridge": {"alpha": 1.0},
    "pls": {"n_components": 8, "max_iter": 1000},
    "pcr": {"n_components": 16},
}

MUTABLE_FIELDS: tuple[str, ...] = tuple(
    sorted({*_NUMERIC, *_ENUM, *_TUPLE, *_BOOL, *_DATE, "params"})
)


def family_defaults(model_family: str) -> dict[str, Any]:
    return dict(_FAMILY_DEFAULTS.get(model_family, {}))


def _clamp(name: str, value: Any) -> tuple[Any, str | None]:
    """Cast + range-clamp one numeric field. Returns ``(value, note)``."""
    lo, hi, cast = _NUMERIC[name]
    try:
        out = cast(value)
    except (TypeError, ValueError):
        return None, f"{name}: {value!r} is not a number — ignored"
    clamped = min(max(out, lo), hi)
    if clamped != out:
        return clamped, f"{name}: {out} clamped to {clamped} (allowed {lo}..{hi})"
    return clamped, None


def _coerce_tuple(name: str, value: Any) -> tuple[tuple[str, ...] | None, str | None]:
    allowed = _TUPLE[name]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return None, f"{name}: expected a list, got {type(value).__name__} — ignored"
    items = [str(v).strip() for v in value if str(v).strip()]
    if allowed is not None:
        bad = [v for v in items if v not in allowed]
        items = [v for v in items if v in allowed]
        if bad:
            return tuple(dict.fromkeys(items)), f"{name}: dropped unknown {bad}"
    else:
        items = items[:_MAX_DROPPED_FEATURES]
    return tuple(dict.fromkeys(items)), None


def _coerce_params(family: str, value: Any) -> tuple[dict[str, Any], list[str]]:
    """Whitelist + clamp a hyperparameter dict against ``family``'s bound table."""
    notes: list[str] = []
    if not isinstance(value, dict):
        return {}, [f"params: expected an object, got {type(value).__name__} — ignored"]
    bounds = _PARAM_BOUNDS.get(family, {})
    out: dict[str, Any] = {}
    for key, raw in value.items():
        b = bounds.get(str(key))
        if b is None:
            notes.append(f"params.{key}: not a {family} hyperparameter — ignored")
            continue
        lo, hi, cast = b
        try:
            cast_val = cast(raw)
        except (TypeError, ValueError):
            notes.append(f"params.{key}: {raw!r} is not a number — ignored")
            continue
        clamped = min(max(cast_val, lo), hi)
        if clamped != cast_val:
            notes.append(f"params.{key}: {cast_val} clamped to {clamped} (allowed {lo}..{hi})")
        out[str(key)] = clamped
    return out, notes


def apply_patch(
    spec: TrainingSpec, patch: dict[str, Any] | None
) -> tuple[TrainingSpec, list[str], list[str]]:
    """Apply an agent-proposed patch to ``spec``.

    Returns ``(new_spec, changes, rejected)`` where ``changes`` are human-readable
    "field: old -> new" lines for the report, and ``rejected`` records every key or value
    that failed validation. Both lists are surfaced in the UI, so an agent that keeps
    proposing nonsense is visible rather than silently ignored.

    Unknown keys never raise — a research run must survive a malformed proposal and simply
    continue with the previous spec.
    """
    changes: list[str] = []
    rejected: list[str] = []
    if not patch:
        return spec, changes, rejected

    updates: dict[str, Any] = {}
    # Resolve the family first: it decides which hyperparameters are legal below.
    family = spec.model_family
    if "model_family" in patch:
        cand = str(patch.get("model_family") or "").strip().lower()
        if cand in VALID_FAMILY_MODELS:
            family = cand
        else:
            rejected.append(f"model_family: {patch['model_family']!r} is not one of "
                            f"{sorted(VALID_FAMILY_MODELS)} — ignored")

    for key, raw in patch.items():
        if key == "model_family":
            continue  # already resolved
        if key == "params":
            continue  # handled after the loop, needs the resolved family
        if key not in MUTABLE_FIELDS:
            rejected.append(f"{key}: not a spec field — ignored")
            continue

        if raw is None:
            if key in _OPTIONAL:
                updates[key] = None
            else:
                rejected.append(f"{key}: null is not allowed for this field — ignored")
            continue

        if key in _DATE:
            updates[key] = str(raw)[:10]
        elif key in _BOOL:
            updates[key] = bool(raw)
        elif key in _ENUM:
            cand = str(raw).strip().lower()
            if cand in _ENUM[key]:
                updates[key] = cand
            else:
                rejected.append(f"{key}: {raw!r} is not one of {sorted(_ENUM[key])} — ignored")
        elif key in _TUPLE:
            val, note = _coerce_tuple(key, raw)
            if note:
                rejected.append(note)
            if val is not None:
                updates[key] = val
        elif key in _NUMERIC:
            val, note = _clamp(key, raw)
            if note:
                rejected.append(note)
            if val is not None:
                updates[key] = val

    if family != spec.model_family:
        updates["model_family"] = family
        # Switching family invalidates the old hyperparameters entirely.
        updates.setdefault("params", family_defaults(family))

    if "params" in patch:
        merged = dict(updates.get("params", {}) or (
            family_defaults(family) if family != spec.model_family else spec.params))
        clean, notes = _coerce_params(family, patch["params"])
        rejected.extend(notes)
        merged.update(clean)
        updates["params"] = merged

    # A spec with no features cannot train; refuse the patch rather than fail the fit.
    if "families" in updates and not updates["families"]:
        rejected.append("families: refusing an empty feature-family set — kept the previous value")
        updates.pop("families")

    if not updates:
        return spec, changes, rejected

    before = spec.to_dict()
    new_spec = replace(spec, **updates)
    after = new_spec.to_dict()
    for key in sorted(updates):
        if before.get(key) != after.get(key):
            changes.append(f"{key}: {_fmt(before.get(key))} -> {_fmt(after.get(key))}")
    return new_spec, changes, rejected


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in sorted(value.items())) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def describe_search_space() -> dict[str, Any]:
    """Machine-readable description of every knob, its type and its bounds.

    This is the "lecture" the Researcher's prompt is built from (FlowMind): the agent is
    taught the exact API it may address rather than being left to invent field names.
    """
    numeric = {k: {"min": lo, "max": hi, "type": cast.__name__,
                   "nullable": k in _OPTIONAL}
               for k, (lo, hi, cast) in sorted(_NUMERIC.items())}
    return {
        "numeric": numeric,
        "enums": {k: sorted(v) for k, v in sorted(_ENUM.items())},
        "lists": {k: (sorted(v) if v else "free-form metric ids")
                  for k, v in sorted(_TUPLE.items())},
        "booleans": sorted(_BOOL),
        "dates": sorted(_DATE),
        "params_by_family": {
            fam: {k: {"min": lo, "max": hi, "type": cast.__name__}
                  for k, (lo, hi, cast) in sorted(bounds.items())}
            for fam, bounds in sorted(_PARAM_BOUNDS.items())
        },
    }


def diff(a: TrainingSpec, b: TrainingSpec) -> list[str]:
    """Human-readable field-level diff, used for the iteration timeline's patch summary."""
    da, db = a.to_dict(), b.to_dict()
    return [f"{k}: {_fmt(da.get(k))} -> {_fmt(db.get(k))}"
            for k in sorted(set(da) | set(db)) if da.get(k) != db.get(k)]


def summarize(spec: TrainingSpec, keys: Iterable[str] | None = None) -> dict[str, Any]:
    """Compact spec view for an agent packet — the searched knobs, not the whole object."""
    data = spec.to_dict()
    wanted = list(keys) if keys is not None else [*MUTABLE_FIELDS, "label"]
    return {k: data[k] for k in wanted if k in data}
