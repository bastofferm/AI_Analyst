"""Standalone jurisdiction-local temporal multimodal VAE prototype.

The VAE path is intentionally optional at runtime. If PyTorch is not available,
the trainer records a PCA-surrogate run with the same persisted output shape so
API/UI integration and downstream validation can proceed.
"""
from __future__ import annotations

import json
import logging
import os
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

from xbrl_sec.sec.cycle.baselines import _pca
from xbrl_sec.sec.cycle.data import load_cycle_dataset, modality_from_feature_key
from xbrl_sec.sec.cycle.phase import (
    PHASES,
    calibrate_phase_series,
    calibrate_phase_thresholds,
    enforce_min_phase_duration,
    nber_recession_series,
    phase_confidence,
    phase_from_stress_percentile,
    phase_probs,
    phase_series_from_stress,
    smooth_stress_percentile,
)
from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.cycle.vae")


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _artifact_dir() -> Path:
    path = Path("artifacts") / "cycle"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _modality_errors(columns: list[str], x: np.ndarray, recon: np.ndarray) -> dict[str, float]:
    err = (x - recon) ** 2
    out: dict[str, float] = {}
    for modality in ("macro", "market", "fundamental"):
        idx = [i for i, col in enumerate(columns) if modality_from_feature_key(col) == modality]
        if idx:
            out[modality] = float(np.mean(err[:, idx]))
    return out


def _prepare_torch_runtime() -> None:
    # Windows Anaconda + pip PyTorch can load duplicate Intel OpenMP runtimes.
    # Scope the workaround to the optional VAE torch path.
    if os.name == "nt":
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def _split_latents(z: np.ndarray) -> dict[str, list[float]]:
    vals = [float(v) for v in z]
    return {
        "latent_cycle": vals,
        "latent_growth": vals[0:1],
        "latent_inflation": vals[1:2],
        "latent_rates_liquidity": vals[2:3],
        "latent_credit_stress": vals[3:4],
        "latent_market": vals[4:5],
        "latent_fundamentals": vals[5:6],
    }


def _train_torch_vae(x: np.ndarray, latent_dim: int, epochs: int, seed: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any], Any]:
    _prepare_torch_runtime()
    import torch
    from torch import nn

    torch.manual_seed(seed)
    tensor = torch.tensor(x, dtype=torch.float32)
    hidden = max(16, min(96, x.shape[1] * 2))

    class TinyVAE(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = nn.Sequential(nn.Linear(x.shape[1], hidden), nn.ReLU())
            self.mu = nn.Linear(hidden, latent_dim)
            self.logvar = nn.Linear(hidden, latent_dim)
            self.decoder = nn.Sequential(nn.Linear(latent_dim, hidden), nn.ReLU(), nn.Linear(hidden, x.shape[1]))

        def forward(self, batch):
            h = self.encoder(batch)
            mu = self.mu(h)
            logvar = self.logvar(h).clamp(-6.0, 4.0)
            eps = torch.randn_like(mu)
            z = mu + eps * torch.exp(0.5 * logvar)
            return self.decoder(z), mu, logvar

    model = TinyVAE()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    losses: list[float] = []
    for _ in range(max(1, epochs)):
        opt.zero_grad()
        recon, mu, logvar = model(tensor)
        recon_loss = torch.mean((recon - tensor) ** 2)
        kl = -0.5 * torch.mean(1 + logvar - mu.pow(2) - logvar.exp())
        smooth = torch.mean((mu[1:] - mu[:-1]) ** 2) if len(mu) > 1 else torch.tensor(0.0)
        loss = recon_loss + 0.05 * kl + 0.02 * smooth
        loss.backward()
        opt.step()
        losses.append(float(loss.detach()))
    with torch.no_grad():
        recon, mu, logvar = model(tensor)
    metrics = {
        "engine": "torch",
        "epochs": epochs,
        "loss_initial": losses[0] if losses else None,
        "loss_final": losses[-1] if losses else None,
        "latent_dim": latent_dim,
    }
    return mu.numpy(), recon.numpy(), metrics, model


def _fallback_vae(matrix: pd.DataFrame, latent_dim: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any], None]:
    factors, loadings, explained = _pca(matrix, n_components=latent_dim)
    z = factors.to_numpy(dtype=float)
    # Low-rank reconstruction through least squares keeps diagnostics useful.
    beta, *_ = np.linalg.lstsq(z, matrix.to_numpy(dtype=float), rcond=None)
    recon = z @ beta
    metrics = {
        "engine": "pca_surrogate",
        "latent_dim": latent_dim,
        "explained_variance": [float(v) for v in explained],
        "top_loadings": loadings.get("cycle_pc1", [])[:10],
        "note": "PyTorch unavailable; persisted PCA-surrogate VAE-shaped output.",
    }
    return z, recon, metrics, None


def train_vae(
    jurisdiction: str,
    *,
    model_version: str = "v1",
    start: str | None = None,
    end: str | None = None,
    latent_dim: int = 6,
    epochs: int = 80,
    random_state: int = 42,
    modalities: tuple[str, ...] | None = None,
    stress_smooth_span: int = 6,
    min_phase_duration: int = 3,
    calibrate_to_nber: bool = True,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    dataset = load_cycle_dataset(cfg.code, start=start, end=end, train_end=end, modalities=modalities)
    matrix = dataset.matrix
    if matrix.shape[0] < 24 or matrix.shape[1] < 3:
        raise RuntimeError(f"Insufficient cycle feature matrix for VAE: shape={matrix.shape}")
    x = matrix.to_numpy(dtype=float)
    try:
        z, recon, metrics, model = _train_torch_vae(x, latent_dim=latent_dim, epochs=epochs, seed=random_state)
    except Exception as exc:  # pragma: no cover - environment-dependent fallback
        logger.warning("VAE torch path unavailable; using PCA surrogate: %s", exc)
        z, recon, metrics, model = _fallback_vae(matrix, latent_dim=latent_dim)

    run_id = f"{cfg.code.lower()}_vae_{uuid.uuid4().hex[:12]}"
    artifact = _artifact_dir() / f"{run_id}.json"
    payload = {
        "jurisdiction": cfg.code,
        "model_family": "vae",
        "model_version": model_version,
        "feature_columns": list(matrix.columns),
        "feature_manifest": dataset.feature_manifest,
        "scaler": dataset.scaler.to_dict(),
        "modality_feature_counts": dataset.modality_feature_counts(),
        "coverage_by_modality": dataset.coverage_by_modality(),
        "jurisdiction_local": True,
        "pooled_global_model": False,
        "metrics": metrics,
    }
    artifact.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if model is not None:
        try:
            _prepare_torch_runtime()
            import torch

            torch.save(model.state_dict(), artifact.with_suffix(".pt"))
        except Exception:
            logger.exception("failed to persist VAE torch artifact")

    factor_df = pd.DataFrame(z, index=matrix.index, columns=[f"vae_z{i + 1}" for i in range(z.shape[1])])
    calibration = calibrate_phase_series(cfg.code, factor_df)
    phase_calibration = calibration.to_dict()
    raw_stress = calibration.stress_percentile
    smoothed_stress = smooth_stress_percentile(raw_stress, span=stress_smooth_span)
    phase_thresholds, label_policy = (
        calibrate_phase_thresholds(cfg.code, smoothed_stress)
        if calibrate_to_nber
        else (
            {"contraction": 0.80, "late_cycle": 0.55, "mid_expansion": 0.30},
            {
                "method": "default_thresholds",
                "target": "disabled",
                "thresholds": {"contraction": 0.80, "late_cycle": 0.55, "mid_expansion": 0.30},
            },
        )
    )
    raw_labels = phase_series_from_stress(raw_stress, phase_thresholds)
    smoothed_labels = phase_series_from_stress(smoothed_stress, phase_thresholds)
    final_labels = enforce_min_phase_duration(smoothed_labels, min_duration=min_phase_duration)
    nber_target = nber_recession_series(cfg.code, matrix.index)
    modality_errors = _modality_errors(list(matrix.columns), x, recon)
    metrics = {
        **metrics,
        "n_months": int(matrix.shape[0]),
        "n_features": int(matrix.shape[1]),
        "modality_feature_counts": dataset.modality_feature_counts(),
        "coverage_by_modality": dataset.coverage_by_modality(),
        "modality_reconstruction_mse": modality_errors,
        "phase_counts": final_labels.value_counts(dropna=True).to_dict(),
        "phase_calibration": phase_calibration,
        "label_policy": {
            **label_policy,
            "stress_smooth_span": stress_smooth_span,
            "min_phase_duration": min_phase_duration,
            "calibrate_to_nber": calibrate_to_nber,
            "raw_signal": "calibrated VAE stress percentile",
            "final_label": "smoothed stress percentile plus min-duration phase filter",
        },
    }
    payload["phase_calibration"] = phase_calibration
    payload["label_policy"] = metrics["label_policy"]
    payload["metrics"] = metrics
    artifact.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_cycle_model_run
                (run_id, jurisdiction, model_family, model_version, train_start, train_end,
                 feature_set_version, hyperparams_json, metrics_json, artifact_path, status)
            VALUES (%s, %s, 'vae', %s, %s, %s, 'cycle_feature_v1',
                    %s::jsonb, %s::jsonb, %s, 'complete')
            """,
            (
                run_id,
                cfg.code,
                model_version,
                matrix.index.min().date(),
                matrix.index.max().date(),
                json.dumps({
                    "latent_dim": latent_dim,
                    "epochs": epochs,
                    "random_state": random_state,
                    "modalities": list(modalities) if modalities else None,
                    "stress_smooth_span": stress_smooth_span,
                    "min_phase_duration": min_phase_duration,
                    "calibrate_to_nber": calibrate_to_nber,
                }),
                json.dumps(metrics, default=str),
                str(artifact),
            ),
        )

    rows = []
    for i, dt in enumerate(matrix.index):
        stress_pct = raw_stress.loc[dt] if dt in raw_stress.index else np.nan
        smooth_pct = smoothed_stress.loc[dt] if dt in smoothed_stress.index else np.nan
        raw_pct = calibration.raw_percentile.loc[dt] if dt in calibration.raw_percentile.index else np.nan
        stress_pct_value = float(stress_pct) if pd.notna(stress_pct) else None
        smooth_pct_value = float(smooth_pct) if pd.notna(smooth_pct) else None
        raw_pct_value = float(raw_pct) if pd.notna(raw_pct) else None
        raw_phase = raw_labels.loc[dt] if dt in raw_labels.index else phase_from_stress_percentile(stress_pct_value, phase_thresholds)
        smooth_phase = smoothed_labels.loc[dt] if dt in smoothed_labels.index else phase_from_stress_percentile(smooth_pct_value, phase_thresholds)
        phase = final_labels.loc[dt] if dt in final_labels.index else smooth_phase
        conf = phase_confidence(smooth_pct_value)
        latents = _split_latents(z[i])
        diagnostics = {
            "engine": metrics.get("engine"),
            "selected_factor": phase_calibration.get("selected_factor"),
            "selected_factor_percentile": raw_pct_value,
            "raw_stress_percentile": stress_pct_value,
            "smoothed_stress_percentile": smooth_pct_value,
            "stress_percentile": smooth_pct_value,
            "raw_phase_label": raw_phase,
            "smoothed_phase_label": smooth_phase,
            "final_phase_label": phase,
            "nber_recession_target": bool(nber_target.loc[dt]) if dt in nber_target.index else None,
            "label_policy": metrics["label_policy"],
            "phase_calibration": phase_calibration,
            "reconstruction_mse": float(np.mean((x[i] - recon[i]) ** 2)),
            "jurisdiction_local": True,
        }
        rows.append(
            (
                run_id,
                dt.date(),
                cfg.code,
                latents["latent_cycle"],
                latents["latent_growth"],
                latents["latent_inflation"],
                latents["latent_rates_liquidity"],
                latents["latent_credit_stress"],
                latents["latent_market"],
                latents["latent_fundamentals"],
                phase,
                _json(phase_probs(phase, conf)),
                float(conf) if conf is not None else None,
                float(1.0 - conf) if conf is not None else None,
                _json(diagnostics),
                _json({"reconstruction_mse": modality_errors}),
            )
        )
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(
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
    result = {"run_id": run_id, "states": written, **metrics}
    logger.info("trained cycle VAE: %s", result)
    return result
