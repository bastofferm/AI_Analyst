"""Expanded PCA and DFM-style baseline models for cycle states."""
from __future__ import annotations

import json
import logging
import site
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
from psycopg2.extras import Json

from xbrl_sec.sec.cycle.data import load_cycle_dataset, modality_from_feature_key
from xbrl_sec.sec.cycle.phase import PHASES, calibrate_phase_series, phase_confidence, phase_from_stress_percentile, phase_probs
from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.cycle.baselines")


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _artifact_dir() -> Path:
    path = Path("artifacts") / "cycle"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_feature_matrix(
    jurisdiction: str,
    *,
    start: str | None = None,
    end: str | None = None,
    min_obs: int = 24,
    modalities: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    return load_cycle_dataset(
        jurisdiction,
        start=start,
        end=end,
        train_end=end,
        min_obs=min_obs,
        modalities=modalities,
    ).matrix


def _pca(matrix: pd.DataFrame, n_components: int) -> tuple[pd.DataFrame, dict[str, list[dict[str, float]]], np.ndarray]:
    x = matrix.to_numpy(dtype=float)
    x = x - x.mean(axis=0, keepdims=True)
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    k = max(1, min(n_components, vt.shape[0]))
    factors = u[:, :k] * s[:k]
    factor_df = pd.DataFrame(factors, index=matrix.index, columns=[f"cycle_pc{i + 1}" for i in range(k)])
    loadings: dict[str, list[dict[str, float]]] = {}
    for i in range(k):
        pairs = sorted(
            ((matrix.columns[j], float(vt[i, j])) for j in range(len(matrix.columns))),
            key=lambda item: -abs(item[1]),
        )[:20]
        loadings[f"cycle_pc{i + 1}"] = [{"feature": f, "loading": round(v, 6)} for f, v in pairs]
    explained = (s[:k] ** 2) / np.maximum((s**2).sum(), 1e-12)
    return factor_df, loadings, explained


def _insert_run(
    *,
    run_id: str,
    jurisdiction: str,
    model_family: str,
    model_version: str,
    train_start: Any,
    train_end: Any,
    feature_set_version: str,
    hyperparams: dict[str, Any],
    metrics: dict[str, Any],
    artifact_path: str | None,
    status: str = "complete",
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_cycle_model_run
                (run_id, jurisdiction, model_family, model_version, train_start, train_end,
                 feature_set_version, hyperparams_json, metrics_json, artifact_path, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                metrics_json = EXCLUDED.metrics_json,
                artifact_path = EXCLUDED.artifact_path,
                status = EXCLUDED.status
            """,
            (
                run_id,
                jurisdiction,
                model_family,
                model_version,
                train_start,
                train_end,
                feature_set_version,
                json.dumps(hyperparams, default=str),
                json.dumps(metrics, default=str),
                artifact_path,
                status,
            ),
        )


def _upsert_states(
    *,
    run_id: str,
    jurisdiction: str,
    factors: pd.DataFrame,
    stress_percentile: pd.Series,
    raw_percentile: pd.Series,
    phase_calibration: dict[str, Any],
    loadings: dict[str, list[dict[str, float]]],
    model_family: str,
) -> int:
    rows = []
    selected_factor = str(phase_calibration.get("selected_factor") or "cycle_pc1")
    selected_loadings = loadings.get(selected_factor, loadings.get("cycle_pc1", []))
    for dt, values in factors.iterrows():
        stress_pct = stress_percentile.loc[dt] if dt in stress_percentile.index else np.nan
        raw_pct = raw_percentile.loc[dt] if dt in raw_percentile.index else np.nan
        pct_value = float(stress_pct) if pd.notna(stress_pct) else None
        raw_pct_value = float(raw_pct) if pd.notna(raw_pct) else None
        label = phase_from_stress_percentile(pct_value)
        confidence = phase_confidence(pct_value)
        latent = [float(v) for v in values.to_numpy()]
        diagnostics = {
            "model_family": model_family,
            "selected_factor": selected_factor,
            "selected_factor_percentile": raw_pct_value,
            "stress_percentile": pct_value,
            "phase_calibration": phase_calibration,
            "top_loadings": selected_loadings[:8],
        }
        modality_contrib: dict[str, float] = {}
        for item in selected_loadings[:30]:
            feature = item["feature"]
            modality = modality_from_feature_key(feature)
            modality_contrib[modality] = modality_contrib.get(modality, 0.0) + abs(float(item["loading"]))
        total = sum(modality_contrib.values()) or 1.0
        modality_contrib = {k: v / total for k, v in sorted(modality_contrib.items())}
        rows.append(
            (
                run_id,
                dt.date(),
                jurisdiction,
                latent,
                [float(values.iloc[0])] if len(values) else [],
                [],
                [],
                [],
                [],
                [],
                label,
                _json(phase_probs(label, confidence)),
                float(confidence) if confidence is not None else None,
                float(1.0 - confidence) if confidence is not None else None,
                _json(diagnostics),
                _json(modality_contrib),
            )
        )
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO fact_cycle_state_monthly
                (run_id, date, jurisdiction, latent_cycle, latent_growth, latent_inflation,
                 latent_rates_liquidity, latent_credit_stress, latent_market, latent_fundamentals,
                 phase_label, phase_probabilities, confidence, uncertainty, diagnostics_json,
                 modality_contrib_json)
            VALUES %s
            ON CONFLICT (run_id, date, jurisdiction) DO UPDATE SET
                latent_cycle = EXCLUDED.latent_cycle,
                latent_growth = EXCLUDED.latent_growth,
                latent_inflation = EXCLUDED.latent_inflation,
                latent_rates_liquidity = EXCLUDED.latent_rates_liquidity,
                latent_credit_stress = EXCLUDED.latent_credit_stress,
                latent_market = EXCLUDED.latent_market,
                latent_fundamentals = EXCLUDED.latent_fundamentals,
                phase_label = EXCLUDED.phase_label,
                phase_probabilities = EXCLUDED.phase_probabilities,
                confidence = EXCLUDED.confidence,
                uncertainty = EXCLUDED.uncertainty,
                diagnostics_json = EXCLUDED.diagnostics_json,
                modality_contrib_json = EXCLUDED.modality_contrib_json,
                updated_at = now()
            """,
            rows,
        )


def train_baseline(
    jurisdiction: str,
    *,
    model_family: str = "pca",
    model_version: str = "v1",
    start: str | None = None,
    end: str | None = None,
    n_components: int = 6,
    feature_set_version: str = "cycle_feature_v1",
    modalities: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    family = model_family.lower()
    if family not in {"pca", "dfm"}:
        raise ValueError("train_baseline supports model_family='pca' or 'dfm'")
    dataset = load_cycle_dataset(cfg.code, start=start, end=end, train_end=end, modalities=modalities)
    matrix = dataset.matrix
    if matrix.shape[0] < 24 or matrix.shape[1] < 3:
        raise RuntimeError(f"Insufficient cycle feature matrix for {cfg.code}: shape={matrix.shape}")
    factors, loadings, explained = _pca(matrix, n_components=n_components)
    if family == "dfm":
        factors = factors.ewm(span=3, min_periods=1, adjust=False).mean()
    calibration = calibrate_phase_series(cfg.code, factors)
    phase_calibration = calibration.to_dict()
    labels = calibration.stress_percentile.map(phase_from_stress_percentile)

    run_id = f"{cfg.code.lower()}_{family}_{uuid.uuid4().hex[:12]}"
    artifact = _artifact_dir() / f"{run_id}.json"
    payload = {
        "jurisdiction": cfg.code,
        "model_family": family,
        "model_version": model_version,
        "features": list(matrix.columns),
        "feature_manifest": dataset.feature_manifest,
        "scaler": dataset.scaler.to_dict(),
        "modality_feature_counts": dataset.modality_feature_counts(),
        "coverage_by_modality": dataset.coverage_by_modality(),
        "modalities": list(modalities) if modalities else None,
        "loadings": loadings,
        "phase_calibration": phase_calibration,
        "explained_variance": [float(v) for v in explained],
        "jurisdiction_local": True,
        "pooled_global_model": False,
    }
    artifact.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    metrics = {
        "n_months": int(matrix.shape[0]),
        "n_features": int(matrix.shape[1]),
        "modality_feature_counts": dataset.modality_feature_counts(),
        "coverage_by_modality": dataset.coverage_by_modality(),
        "explained_variance": [float(v) for v in explained],
        "phase_counts": labels.value_counts(dropna=True).to_dict(),
        "phase_calibration": phase_calibration,
    }
    _insert_run(
        run_id=run_id,
        jurisdiction=cfg.code,
        model_family=family,
        model_version=model_version,
        train_start=matrix.index.min().date(),
        train_end=matrix.index.max().date(),
        feature_set_version=feature_set_version,
        hyperparams={
            "n_components": n_components,
            "smoothing": "ewm_span_3" if family == "dfm" else None,
            "modalities": list(modalities) if modalities else None,
        },
        metrics=metrics,
        artifact_path=str(artifact),
    )
    written = _upsert_states(
        run_id=run_id,
        jurisdiction=cfg.code,
        factors=factors,
        stress_percentile=calibration.stress_percentile,
        raw_percentile=calibration.raw_percentile,
        phase_calibration=phase_calibration,
        loadings=loadings,
        model_family=family,
    )
    result = {"run_id": run_id, "states": written, **metrics}
    logger.info("trained cycle baseline: %s", result)
    return result


def latest_run_id(jurisdiction: str, model_families: tuple[str, ...]) -> str | None:
    cfg = get_config(jurisdiction)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id
            FROM   fact_cycle_model_run
            WHERE  jurisdiction = %s
              AND  model_family = ANY(%s)
              AND  status = 'complete'
            ORDER  BY trained_at DESC
            LIMIT  1
            """,
            (cfg.code, list(model_families)),
        )
        row = cur.fetchone()
        return row[0] if row else None
