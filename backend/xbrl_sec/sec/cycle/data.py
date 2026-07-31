"""Jurisdiction-local cycle feature datasets and train-window scaling."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.connection import connect


MODALITIES = ("macro", "market", "fundamental", "text_optional", "label_anchor")


def _date_bound(value: str | date | None) -> pd.Timestamp | None:
    if value is None:
        return None
    return pd.Timestamp(value)


def _json_value(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def feature_key(scope: str | None, modality: str, raw_feature_id: str) -> str:
    """Build a stable feature key without duplicating modality prefixes."""

    clean_scope = (scope or "regional").strip() or "regional"
    clean_modality = modality.strip()
    raw = raw_feature_id.strip()
    local = raw if raw.startswith(f"{clean_modality}:") else f"{clean_modality}:{raw}"
    if clean_scope == "regional":
        return local
    return f"{clean_scope}:{local}"


def modality_from_feature_key(key: str) -> str:
    for part in key.split(":"):
        if part in MODALITIES:
            return part
    return key.split(":", 1)[0]


def _month_delta(as_of: pd.Series, available: pd.Series) -> pd.Series:
    lhs = pd.to_datetime(as_of, errors="coerce")
    rhs = pd.to_datetime(available, errors="coerce")
    delta = (lhs.dt.year - rhs.dt.year) * 12 + (lhs.dt.month - rhs.dt.month)
    return delta.clip(lower=0)


@dataclass(frozen=True)
class CycleFeatureScaler:
    train_start: str | None
    train_end: str | None
    fill_values: dict[str, float]
    means: dict[str, float]
    stds: dict[str, float]

    @classmethod
    def fit(cls, matrix: pd.DataFrame, train_mask: pd.Series) -> "CycleFeatureScaler":
        if matrix.empty:
            return cls(train_start=None, train_end=None, fill_values={}, means={}, stds={})
        train = matrix.loc[train_mask].copy()
        fill = train.median(axis=0, skipna=True).fillna(0.0)
        filled = train.fillna(fill).fillna(0.0)
        means = filled.mean(axis=0)
        stds = filled.std(axis=0, ddof=0).replace(0.0, 1.0).fillna(1.0)
        return cls(
            train_start=train.index.min().date().isoformat() if not train.empty else None,
            train_end=train.index.max().date().isoformat() if not train.empty else None,
            fill_values={str(k): float(v) for k, v in fill.items()},
            means={str(k): float(v) for k, v in means.items()},
            stds={str(k): float(v) for k, v in stds.items()},
        )

    def transform(self, matrix: pd.DataFrame) -> pd.DataFrame:
        fill = pd.Series(self.fill_values, dtype=float).reindex(matrix.columns).fillna(0.0)
        means = pd.Series(self.means, dtype=float).reindex(matrix.columns).fillna(0.0)
        stds = pd.Series(self.stds, dtype=float).reindex(matrix.columns).replace(0.0, 1.0).fillna(1.0)
        return ((matrix.fillna(fill).fillna(0.0) - means) / stds).fillna(0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "fill_values": self.fill_values,
            "means": self.means,
            "stds": self.stds,
        }


@dataclass(frozen=True)
class CycleDataset:
    jurisdiction: str
    matrix: pd.DataFrame
    raw_matrix: pd.DataFrame
    mask_matrix: pd.DataFrame
    stale_months: pd.DataFrame
    feature_manifest: list[dict[str, Any]]
    scaler: CycleFeatureScaler

    def modality_matrix(self, modality: str) -> pd.DataFrame:
        cols = [item["feature_key"] for item in self.feature_manifest if item["modality"] == modality]
        return self.matrix[[col for col in cols if col in self.matrix.columns]]

    def modality_feature_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.feature_manifest:
            modality = str(item.get("modality") or "unknown")
            counts[modality] = counts.get(modality, 0) + 1
        return dict(sorted(counts.items()))

    def coverage_by_modality(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for modality in MODALITIES:
            mat = self.modality_matrix(modality)
            if not mat.empty:
                out[modality] = float(self.mask_matrix[mat.columns].mean().mean())
        return out


def load_cycle_dataset(
    jurisdiction: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    train_end: str | date | None = None,
    min_obs: int = 24,
    modalities: tuple[str, ...] | None = None,
) -> CycleDataset:
    cfg = get_config(jurisdiction)
    params: list[Any] = [cfg.code]
    bounds = ""
    if start:
        bounds += " AND date >= %s"
        params.append(start)
    if end:
        bounds += " AND date <= %s"
        params.append(end)
    sql = f"""
        SELECT date, jurisdiction, COALESCE(scope, 'regional') AS scope,
               modality, feature_id, feature_value, feature_z, feature_transform,
               source_table, source_detail, as_of_policy,
               coverage, missing_ratio,
               raw_observation_date, available_as_of, stale_months
        FROM   fact_cycle_feature_monthly
        WHERE  jurisdiction = %s
          AND  feature_value IS NOT NULL
          {bounds}
        ORDER  BY date, scope, modality, feature_id
    """
    with connect() as conn:
        frame = pd.read_sql(sql, conn, params=tuple(params))
    return build_cycle_dataset_from_frame(
        frame,
        jurisdiction=cfg.code,
        start=start,
        end=end,
        train_end=train_end or end,
        min_obs=min_obs,
        modalities=modalities,
    )


def build_cycle_dataset_from_frame(
    frame: pd.DataFrame,
    *,
    jurisdiction: str,
    start: str | date | None = None,
    end: str | date | None = None,
    train_end: str | date | None = None,
    min_obs: int = 24,
    modalities: tuple[str, ...] | None = None,
) -> CycleDataset:
    cfg = get_config(jurisdiction)
    if frame.empty:
        empty = pd.DataFrame()
        scaler = CycleFeatureScaler.fit(empty, pd.Series(dtype=bool))
        return CycleDataset(cfg.code, empty, empty, empty, empty, [], scaler)

    df = frame.copy()
    df["jurisdiction"] = df["jurisdiction"].astype(str).str.strip().str.upper()
    if set(df["jurisdiction"].dropna()) - {cfg.code}:
        raise ValueError("Cycle datasets must be jurisdiction-local; pooled frames are not allowed.")
    df["date"] = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp("M")
    start_ts = _date_bound(start)
    end_ts = _date_bound(end)
    if start_ts is not None:
        df = df[df["date"] >= start_ts]
    if end_ts is not None:
        df = df[df["date"] <= end_ts]
    if modalities:
        allowed = set(modalities)
        df = df[df["modality"].isin(allowed)]
    if df.empty:
        empty = pd.DataFrame()
        scaler = CycleFeatureScaler.fit(empty, pd.Series(dtype=bool))
        return CycleDataset(cfg.code, empty, empty, empty, empty, [], scaler)

    if "scope" not in df:
        df["scope"] = "regional"
    else:
        df["scope"] = df["scope"].fillna("regional")
    df["feature_key"] = [
        feature_key(scope, modality, feature_id)
        for scope, modality, feature_id in zip(df["scope"], df["modality"], df["feature_id"])
    ]
    df["value"] = pd.to_numeric(df["feature_value"], errors="coerce")
    available = df["available_as_of"] if "available_as_of" in df else df["date"]
    df["available_as_of"] = pd.to_datetime(available.fillna(df["date"]), errors="coerce").fillna(df["date"])
    if "stale_months" not in df or df["stale_months"].isna().all():
        df["stale_months"] = _month_delta(df["date"], df["available_as_of"])
    df["stale_months"] = pd.to_numeric(df["stale_months"], errors="coerce").fillna(0.0)

    raw = df.pivot_table(index="date", columns="feature_key", values="value", aggfunc="last").sort_index()
    mask = raw.notna().astype(float)
    stale = df.pivot_table(index="date", columns="feature_key", values="stale_months", aggfunc="last").sort_index()
    stale = stale.reindex(index=raw.index, columns=raw.columns).fillna(0.0)

    train_end_ts = _date_bound(train_end) or raw.index.max()
    train_mask = pd.Series(raw.index <= train_end_ts, index=raw.index)
    train_obs = raw.loc[train_mask].notna().sum(axis=0)
    good_cols = [col for col in raw.columns if int(train_obs.get(col, 0)) >= min_obs]
    raw = raw[good_cols]
    mask = mask[good_cols]
    stale = stale[good_cols]

    scaler = CycleFeatureScaler.fit(raw, train_mask)
    matrix = scaler.transform(raw)
    manifest = _manifest(df[df["feature_key"].isin(good_cols)])
    return CycleDataset(cfg.code, matrix, raw, mask, stale, manifest, scaler)


def _manifest(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for key, group in df.sort_values("date").groupby("feature_key", sort=True):
        row = group.iloc[-1]
        rows.append(
            {
                "feature_key": key,
                "scope": str(row.get("scope") or "regional"),
                "modality": str(row.get("modality") or modality_from_feature_key(str(key))),
                "feature_id": str(row.get("feature_id") or key),
                "feature_transform": row.get("feature_transform"),
                "source_table": row.get("source_table"),
                "source_detail": _json_value(row.get("source_detail")),
                "as_of_policy": row.get("as_of_policy"),
                "coverage": _finite_or_none(row.get("coverage")),
                "missing_ratio": _finite_or_none(row.get("missing_ratio")),
            }
        )
    return rows


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None
