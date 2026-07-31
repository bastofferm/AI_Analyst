"""Jurisdiction-local HMM regime model over compressed cycle factors."""
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

from xbrl_sec.sec.cycle.baselines import latest_run_id, train_baseline
from xbrl_sec.sec.cycle.phase import PHASES, calibrate_phase_series, state_phase_mapping
from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.cycle.hmm")


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _artifact_dir() -> Path:
    path = Path("artifacts") / "cycle"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_factor_states(jurisdiction: str, run_id: str) -> pd.DataFrame:
    with connect() as conn:
        df = pd.read_sql(
            """
            SELECT date, latent_cycle
            FROM   fact_cycle_state_monthly
            WHERE  jurisdiction = %s
              AND  run_id = %s
              AND  array_length(latent_cycle, 1) IS NOT NULL
            ORDER  BY date
            """,
            conn,
            params=(jurisdiction, run_id),
        )
    if df.empty:
        return pd.DataFrame()
    max_len = max(len(v or []) for v in df["latent_cycle"])
    data = []
    for values in df["latent_cycle"]:
        row = list(values or [])
        row = row + [0.0] * (max_len - len(row))
        data.append(row)
    out = pd.DataFrame(data, index=pd.to_datetime(df["date"]), columns=[f"factor_{i + 1}" for i in range(max_len)])
    return out.replace([np.inf, -np.inf], np.nan).dropna(how="any")


def _fallback_hmm(x: np.ndarray, n_states: int, random_state: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(random_state)
    pc1 = x[:, 0]
    quantiles = np.quantile(pc1, np.linspace(0, 1, n_states + 2)[1:-1])
    labels = np.digitize(pc1, quantiles)
    if len(np.unique(labels)) < n_states:
        labels = rng.integers(0, n_states, size=len(pc1))
    for _ in range(25):
        centers = np.vstack([
            x[labels == k].mean(axis=0) if np.any(labels == k) else x[rng.integers(0, len(x))]
            for k in range(n_states)
        ])
        d = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        if np.array_equal(labels, new_labels):
            break
        labels = new_labels
    centers = np.vstack([
        x[labels == k].mean(axis=0) if np.any(labels == k) else x.mean(axis=0)
        for k in range(n_states)
    ])
    d = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
    inv = np.exp(-d / np.maximum(np.nanmedian(d), 1e-6))
    probs = inv / inv.sum(axis=1, keepdims=True)
    trans = np.ones((n_states, n_states)) * 1e-3
    for a, b in zip(labels[:-1], labels[1:]):
        trans[int(a), int(b)] += 1.0
    trans = trans / trans.sum(axis=1, keepdims=True)
    return labels, probs, trans, centers


def _fit_hmm(x: np.ndarray, n_states: int, random_state: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, str]:
    try:
        import warnings
        from hmmlearn.hmm import GaussianHMM
        from hmmlearn.base import ConvergenceMonitor

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = GaussianHMM(
                n_components=n_states,
                covariance_type="full",
                random_state=random_state,
                n_iter=200,
            )
            model.fit(x)
            labels = model.predict(x)
            probs = model.predict_proba(x)
        return labels, probs, model.transmat_, model.means_, "hmmlearn"
    except Exception as exc:  # pragma: no cover - environment-dependent fallback
        logger.warning("hmmlearn unavailable or failed; using deterministic fallback HMM: %s", exc)
        labels, probs, trans, centers = _fallback_hmm(x, n_states, random_state)
        return labels, probs, trans, centers, "numpy_fallback"


def train_hmm(
    jurisdiction: str,
    *,
    n_states: int = 4,
    model_version: str = "v1",
    base_run_id: str | None = None,
    random_state: int = 42,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    auto_base = None
    if base_run_id is None:
        base_run_id = latest_run_id(cfg.code, ("dfm", "pca"))
    if base_run_id is None:
        auto = train_baseline(cfg.code, model_family="pca", model_version="auto_for_hmm")
        auto_base = auto["run_id"]
        base_run_id = auto_base
    factors = _load_factor_states(cfg.code, base_run_id)
    if factors.shape[0] < 24:
        raise RuntimeError(f"Insufficient compressed factor history for HMM: shape={factors.shape}")
    x = factors.to_numpy(dtype=float)
    labels, probs, trans, centers, engine = _fit_hmm(x, n_states=n_states, random_state=random_state)
    calibration = calibrate_phase_series(cfg.code, factors)
    phase_calibration = calibration.to_dict()
    mapping = state_phase_mapping(labels, factors, n_states, calibration)

    run_id = f"{cfg.code.lower()}_hmm_{uuid.uuid4().hex[:12]}"
    artifact = _artifact_dir() / f"{run_id}.json"
    payload = {
        "jurisdiction": cfg.code,
        "model_family": "hmm",
        "model_version": model_version,
        "base_run_id": base_run_id,
        "auto_base_run_id": auto_base,
        "engine": engine,
        "transition_matrix": trans.tolist(),
        "state_centers": centers.tolist(),
        "phase_calibration": phase_calibration,
        "state_phase_mapping": {str(k): v for k, v in mapping.items()},
        "factor_columns": list(factors.columns),
    }
    artifact.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    metrics = {
        "engine": engine,
        "base_run_id": base_run_id,
        "auto_base_run_id": auto_base,
        "n_states": n_states,
        "n_months": int(len(factors)),
        "transition_matrix": trans.tolist(),
        "phase_calibration": phase_calibration,
        "state_phase_mapping": {str(k): v for k, v in mapping.items()},
        "state_counts": {str(k): int((labels == k).sum()) for k in range(n_states)},
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_cycle_model_run
                (run_id, jurisdiction, model_family, model_version, train_start, train_end,
                 feature_set_version, hyperparams_json, metrics_json, artifact_path, status)
            VALUES (%s, %s, 'hmm', %s, %s, %s, 'cycle_feature_v1',
                    %s::jsonb, %s::jsonb, %s, 'complete')
            """,
            (
                run_id,
                cfg.code,
                model_version,
                factors.index.min().date(),
                factors.index.max().date(),
                json.dumps({"n_states": n_states, "random_state": random_state, "base_run_id": base_run_id}),
                json.dumps(metrics, default=str),
                str(artifact),
            ),
        )

    rows = []
    for i, dt in enumerate(factors.index):
        state = int(labels[i])
        phase = mapping[state]
        prob_map = {mapping.get(k, f"state_{k}"): float(probs[i, k]) for k in range(n_states)}
        confidence = float(np.max(probs[i]))
        diagnostics = {"state": state, "engine": engine, "base_run_id": base_run_id}
        rows.append(
            (
                run_id,
                dt.date(),
                cfg.code,
                [float(v) for v in factors.iloc[i].to_numpy()],
                phase,
                _json(prob_map),
                confidence,
                float(1.0 - confidence),
                _json(diagnostics),
                _json({"cycle": 1.0}),
            )
        )
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(
            cur,
            """
            INSERT INTO fact_cycle_state_monthly
                (run_id, date, jurisdiction, latent_cycle, phase_label, phase_probabilities,
                 confidence, uncertainty, diagnostics_json, modality_contrib_json)
            VALUES %s
            ON CONFLICT (run_id, date, jurisdiction) DO UPDATE SET
                latent_cycle = EXCLUDED.latent_cycle,
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
    result = {"run_id": run_id, "states": written, **metrics}
    logger.info("trained cycle HMM: %s", result)
    return result
