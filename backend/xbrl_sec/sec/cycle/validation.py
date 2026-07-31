"""Validation and scoring helpers for jurisdiction-local cycle models."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from xbrl_sec.sec.cycle.baselines import latest_run_id
from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.connection import connect


def score_cycle_state(
    jurisdiction: str,
    *,
    run_id: Optional[str] = None,
    model_family: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the latest persisted monthly state for a jurisdiction-local model."""

    config = get_config(jurisdiction)
    resolved_run_id = run_id or latest_run_id(
        config.code,
        (model_family,) if model_family else ("hmm", "dfm", "pca", "vae"),
    )
    if not resolved_run_id:
        return {
            "jurisdiction": config.code,
            "run_id": None,
            "status": "missing_model_run",
        }

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.jurisdiction,
                    s.run_id,
                    r.model_family,
                    r.model_version,
                    s.date,
                    s.phase_label,
                    s.confidence,
                    s.uncertainty,
                    s.phase_probabilities,
                    s.latent_cycle,
                    s.modality_contrib_json,
                    s.diagnostics_json
                FROM fact_cycle_state_monthly s
                JOIN fact_cycle_model_run r ON r.run_id = s.run_id
                WHERE s.jurisdiction = %s
                  AND s.run_id = %s
                ORDER BY s.date DESC
                LIMIT 1
                """,
                (config.code, resolved_run_id),
            )
            row = cur.fetchone()

    if not row:
        return {
            "jurisdiction": config.code,
            "run_id": resolved_run_id,
            "status": "missing_state",
        }

    return {
        "jurisdiction": row[0],
        "run_id": row[1],
        "model_family": row[2],
        "model_version": row[3],
        "date": row[4].isoformat() if row[4] else None,
        "phase_label": row[5],
        "confidence": float(row[6]) if row[6] is not None else None,
        "uncertainty": float(row[7]) if row[7] is not None else None,
        "phase_probabilities": _json_value(row[8], {}),
        "latent_vector": [float(v) for v in (row[9] or [])],
        "contribution": _json_value(row[10], {}),
        "diagnostics": _json_value(row[11], {}),
        "status": "ok",
    }


def validate_cycle_model(
    jurisdiction: str,
    *,
    run_id: Optional[str] = None,
    model_family: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute compact validation diagnostics and attach them to the model run."""

    config = get_config(jurisdiction)
    resolved_run_id = run_id or latest_run_id(
        config.code,
        (model_family,) if model_family else ("hmm", "dfm", "pca", "vae"),
    )
    if not resolved_run_id:
        return {
            "jurisdiction": config.code,
            "status": "missing_model_run",
            "validation": {},
        }

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ordered AS (
                    SELECT
                        date,
                        phase_label,
                        confidence,
                        lag(phase_label) OVER (ORDER BY date) AS previous_phase
                    FROM fact_cycle_state_monthly
                    WHERE jurisdiction = %s
                      AND run_id = %s
                )
                SELECT
                    COUNT(*) AS n_months,
                    MIN(date) AS start_month,
                    MAX(date) AS end_month,
                    AVG(confidence) AS avg_confidence,
                    SUM(CASE WHEN previous_phase IS NOT NULL AND phase_label <> previous_phase THEN 1 ELSE 0 END)::FLOAT
                        / NULLIF(COUNT(*) - 1, 0) AS label_switch_rate
                FROM ordered
                """,
                (config.code, resolved_run_id),
            )
            summary = cur.fetchone()

            cur.execute(
                """
                SELECT phase_label, COUNT(*) AS n
                FROM fact_cycle_state_monthly
                WHERE jurisdiction = %s
                  AND run_id = %s
                GROUP BY phase_label
                ORDER BY n DESC, phase_label
                """,
                (config.code, resolved_run_id),
            )
            phase_counts = {row[0] or "unlabeled": int(row[1]) for row in cur.fetchall()}

            anchor_coverage = {
                "macro_regime_quadrant": _anchor_overlap_count(
                    cur,
                    table_name="fact_macro_regime",
                    date_column="quarter_end",
                    jurisdiction=config.code,
                    run_id=resolved_run_id,
                    extra_where="",
                    params=(),
                ),
                "macro_cycle_assessment": _anchor_overlap_count(
                    cur,
                    table_name="fact_macro_cycle_assessment",
                    date_column="period_end",
                    jurisdiction=config.code,
                    run_id=resolved_run_id,
                    extra_where="",
                    params=(),
                ),
                "macro_pca_factor": _anchor_overlap_count(
                    cur,
                    table_name="fact_macro_factor",
                    date_column="date",
                    jurisdiction=config.code,
                    run_id=resolved_run_id,
                    extra_where="AND factor_id = %s",
                    params=(config.cycle_factor_id,),
                    join_jurisdiction=False,
                ),
            }

            validation = {
                "n_months": int(summary[0] or 0),
                "start_month": summary[1].isoformat() if summary and summary[1] else None,
                "end_month": summary[2].isoformat() if summary and summary[2] else None,
                "avg_confidence": float(summary[3]) if summary and summary[3] is not None else None,
                "label_switch_rate": float(summary[4]) if summary and summary[4] is not None else None,
                "phase_counts": phase_counts,
                "anchor_overlap_months": anchor_coverage,
                "jurisdiction_scope": "standalone",
            }

            cur.execute(
                """
                UPDATE fact_cycle_model_run
                SET metrics_json = COALESCE(metrics_json, '{}'::jsonb)
                    || jsonb_build_object('validation', %s::jsonb)
                WHERE run_id = %s
                  AND jurisdiction = %s
                """,
                (json.dumps(validation, sort_keys=True), resolved_run_id, config.code),
            )
        conn.commit()

    return {
        "jurisdiction": config.code,
        "run_id": resolved_run_id,
        "status": "ok",
        "validation": validation,
    }


def _anchor_overlap_count(
    cur,
    *,
    table_name: str,
    date_column: str,
    jurisdiction: str,
    run_id: str,
    extra_where: str,
    params: tuple[Any, ...],
    join_jurisdiction: bool = True,
) -> int:
    jurisdiction_join = "AND a.jurisdiction = s.jurisdiction" if join_jurisdiction else ""
    try:
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT s.date)
            FROM fact_cycle_state_monthly s
            JOIN {table_name} a
              ON date_trunc('month', a.{date_column}) = date_trunc('month', s.date)
             {jurisdiction_join}
            WHERE s.jurisdiction = %s
              AND s.run_id = %s
              {extra_where}
            """,
            (jurisdiction, run_id, *params),
        )
        return int(cur.fetchone()[0] or 0)
    except Exception:
        cur.connection.rollback()
        return 0


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value
