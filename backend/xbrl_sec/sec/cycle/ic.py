"""Regime-conditioned equity factor IC calculations."""
from __future__ import annotations

import json
import logging
import site
import time
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
from psycopg2.extras import Json

from xbrl_sec.sec.cycle.baselines import latest_run_id
from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.cycle.ic")


_MARKET_FACTOR_PATTERNS = (
    "_ff3",
    "_ff4",
    "_ff5",
    "_ff6",
    "market_beta",
    "beta",
    "volatility",
    "momentum",
    "residual",
    "abnormal_return",
)

_METRIC_FAMILY_PATTERNS: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "market_factor": _MARKET_FACTOR_PATTERNS,
    "quality": ("return_on", "roe", "roa", "margin", "profit", "accrual", "quality", "cash_conversion", "asset_turnover"),
    "value": ("book_to", "earnings_yield", "free_cash_flow_yield", "dividend", "ev_", "enterprise_value", "valuation", "value"),
    "growth": ("growth", "investment", "capex", "research_and_development", "r_and_d", "sales_change", "revenue_change"),
    "accounting": ("__exclude_market_factor__",),
}


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _month_end(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series).dt.to_period("M").dt.to_timestamp("M").dt.date


def _spearman(x: pd.Series, y: pd.Series) -> tuple[float | None, float | None]:
    mask = x.notna() & y.notna()
    if mask.sum() < 3:
        return None, None
    try:
        from scipy.stats import spearmanr

        corr, p_value = spearmanr(x[mask], y[mask], nan_policy="omit")
        if not np.isfinite(corr):
            return None, None
        return float(corr), float(p_value) if np.isfinite(p_value) else None
    except Exception:
        corr = x[mask].rank().corr(y[mask].rank())
        return (float(corr) if pd.notna(corr) else None), None


def _weighted_spearman(x: pd.Series, y: pd.Series, weights: pd.Series) -> tuple[float | None, float | None]:
    clean = pd.DataFrame({"x": x, "y": y, "w": weights}).replace([np.inf, -np.inf], np.nan).dropna()
    clean = clean[clean["w"] > 0]
    if len(clean) < 3 or clean["w"].sum() <= 0:
        return None, None
    xr = clean["x"].rank(method="average")
    yr = clean["y"].rank(method="average")
    w = clean["w"].astype(float)
    w_sum = float(w.sum())
    x_mean = float((w * xr).sum() / w_sum)
    y_mean = float((w * yr).sum() / w_sum)
    cov = float((w * (xr - x_mean) * (yr - y_mean)).sum() / w_sum)
    x_var = float((w * (xr - x_mean) ** 2).sum() / w_sum)
    y_var = float((w * (yr - y_mean) ** 2).sum() / w_sum)
    denom = np.sqrt(x_var * y_var)
    if not np.isfinite(denom) or denom <= 0:
        return None, None
    corr = cov / denom
    return (float(corr) if np.isfinite(corr) else None), None


def _json_payload(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _probability_map(value: Any) -> dict[str, float]:
    payload = _json_payload(value, {})
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in payload.items():
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if np.isfinite(val):
            out[str(key)] = max(0.0, min(1.0, val))
    return out


def _horizon_months(horizons: tuple[str, ...]) -> int:
    out = 1
    for horizon in horizons:
        if horizon.endswith("m"):
            try:
                out = max(out, int(horizon[:-1]))
            except ValueError:
                continue
    return out


def _normalize_metric_family(metric_family: str | None) -> str:
    family = (metric_family or "all").lower().strip()
    return family if family in _METRIC_FAMILY_PATTERNS else "accounting"


def _metric_matches_family(metric_id: str, metric_family: str | None) -> bool:
    family = _normalize_metric_family(metric_family)
    patterns = _METRIC_FAMILY_PATTERNS[family]
    if patterns is None:
        return True
    metric = str(metric_id).lower()
    is_market = any(pattern in metric for pattern in _MARKET_FACTOR_PATTERNS)
    if patterns == ("__exclude_market_factor__",):
        return not is_market
    return any(pattern in metric for pattern in patterns)


def _chunks(values: list[str], chunk_size: int) -> list[list[str]]:
    size = max(1, int(chunk_size))
    return [values[i : i + size] for i in range(0, len(values), size)]


def _job_key(
    jurisdiction: str,
    run_id: str,
    *,
    metric_family: str,
    horizons: tuple[str, ...],
    start: Any | None,
    end: Any | None,
) -> str:
    return "|".join(
        [
            jurisdiction.upper(),
            run_id,
            _normalize_metric_family(metric_family),
            ",".join(horizons),
            str(start or ""),
            str(end or ""),
        ]
    )


def _ensure_ic_status_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_cycle_ic_job_status (
            job_key                  TEXT PRIMARY KEY,
            jurisdiction             CHAR(2) NOT NULL,
            run_id                   TEXT NOT NULL,
            status                   TEXT NOT NULL DEFAULT 'pending',
            metric_family            TEXT NOT NULL DEFAULT 'all',
            horizons_json            JSONB NOT NULL DEFAULT '[]'::jsonb,
            chunk_size               INTEGER NOT NULL DEFAULT 25,
            total_metrics            INTEGER NOT NULL DEFAULT 0,
            completed_metrics        INTEGER NOT NULL DEFAULT 0,
            failed_metrics           INTEGER NOT NULL DEFAULT 0,
            rows_written             INTEGER NOT NULL DEFAULT 0,
            hard_rows_written        INTEGER NOT NULL DEFAULT 0,
            probability_rows_written INTEGER NOT NULL DEFAULT 0,
            state_start              DATE,
            state_end                DATE,
            started_at               TIMESTAMPTZ,
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at             TIMESTAMPTZ,
            elapsed_seconds          DOUBLE PRECISION,
            diagnostics_json         JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_cycle_ic_job_metric_status (
            job_key       TEXT NOT NULL REFERENCES fact_cycle_ic_job_status (job_key) ON DELETE CASCADE,
            metric_id     TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            rows_written  INTEGER NOT NULL DEFAULT 0,
            error_text    TEXT,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (job_key, metric_id)
        )
        """
    )


def get_regime_factor_ic_job_status(
    jurisdiction: str,
    *,
    run_id: str,
    metric_family: str = "all",
    horizons: tuple[str, ...] = ("1m", "3m"),
    start: Any | None = None,
    end: Any | None = None,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    family = _normalize_metric_family(metric_family)
    key = _job_key(cfg.code, run_id, metric_family=family, horizons=horizons, start=start, end=end)
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        cur.execute(
            """
            SELECT job_key, jurisdiction, run_id, status, metric_family, horizons_json,
                   chunk_size, total_metrics, completed_metrics, failed_metrics,
                   rows_written, hard_rows_written, probability_rows_written,
                   state_start, state_end, started_at, updated_at, completed_at,
                   elapsed_seconds, diagnostics_json
            FROM   fact_cycle_ic_job_status
            WHERE  job_key = %s
            """,
            (key,),
        )
        row = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM   fact_equity_factor_ic_regime
            WHERE  jurisdiction = %s
              AND  run_id = %s
              AND  forward_return_window = ANY(%s)
            """,
            (cfg.code, run_id, list(horizons)),
        )
        ic_rows = int(cur.fetchone()[0] or 0)
    if not row:
        return {
            "job_key": key,
            "jurisdiction": cfg.code,
            "run_id": run_id,
            "status": "not_started",
            "metric_family": family,
            "horizons": list(horizons),
            "chunk_size": None,
            "total_metrics": 0,
            "completed_metrics": 0,
            "failed_metrics": 0,
            "rows_written": 0,
            "hard_rows_written": 0,
            "probability_rows_written": 0,
            "ic_table_rows": ic_rows,
            "state_start": None,
            "state_end": None,
            "started_at": None,
            "updated_at": None,
            "completed_at": None,
            "elapsed_seconds": None,
            "diagnostics": {},
        }
    diagnostics = _json_payload(row[19], {})
    if row[3] == "complete" and isinstance(diagnostics, dict):
        diagnostics = {key: value for key, value in diagnostics.items() if key != "error"}
    horizons_payload = _json_payload(row[5], list(horizons))
    return {
        "job_key": row[0],
        "jurisdiction": row[1],
        "run_id": row[2],
        "status": row[3],
        "metric_family": row[4],
        "horizons": horizons_payload if isinstance(horizons_payload, list) else list(horizons),
        "chunk_size": int(row[6]) if row[6] is not None else None,
        "total_metrics": int(row[7] or 0),
        "completed_metrics": int(row[8] or 0),
        "failed_metrics": int(row[9] or 0),
        "rows_written": int(row[10] or 0),
        "hard_rows_written": int(row[11] or 0),
        "probability_rows_written": int(row[12] or 0),
        "ic_table_rows": ic_rows,
        "state_start": row[13].isoformat() if row[13] else None,
        "state_end": row[14].isoformat() if row[14] else None,
        "started_at": row[15].isoformat() if row[15] else None,
        "updated_at": row[16].isoformat() if row[16] else None,
        "completed_at": row[17].isoformat() if row[17] else None,
        "elapsed_seconds": float(row[18]) if row[18] is not None else None,
        "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
    }


def _intl_country_tickers(country_code: str) -> list[str]:
    """Primary tickers for one INTL country. fact_metrics_intl has no country_code column, so
    per-country scoping of the alpha panel resolves the ticker set from dim_company_intl."""
    with connect() as conn:
        df = pd.read_sql(
            "SELECT DISTINCT primary_ticker FROM dim_company_intl "
            "WHERE country_code = %s AND primary_ticker IS NOT NULL AND primary_ticker <> ''",
            conn, params=(country_code.upper(),),
        )
    return [str(t) for t in df["primary_ticker"].dropna().tolist()]


def _load_monthly_returns(jurisdiction: str, *, start: Any | None = None, end: Any | None = None, forward_months: int = 3) -> pd.DataFrame:
    cfg = get_config(jurisdiction)
    params: list[Any] = []
    filters = ["return IS NOT NULL"]
    if cfg.country_code:  # INTL per-country: fact_prices_intl has a native country_code column
        filters.append("country_code = %s")
        params.append(cfg.country_code)
    if start is not None:
        filters.append("date >= %s")
        params.append(start)
    if end is not None:
        end_buffer = (pd.Timestamp(end) + pd.DateOffset(months=max(1, forward_months) + 1)).date()
        filters.append("date <= %s")
        params.append(end_buffer)
    with connect() as conn:
        df = pd.read_sql(
            f"""
            SELECT date, ticker, return
            FROM   {cfg.price_table}
            WHERE  {" AND ".join(filters)}
            ORDER  BY ticker, date
            """,
            conn,
            params=tuple(params),
        )
    if df.empty:
        return pd.DataFrame()
    df["month"] = _month_end(df["date"])
    monthly = (
        df.groupby(["ticker", "month"])["return"]
        .apply(lambda s: float(np.exp(np.log1p(s.clip(lower=-0.999999)).sum()) - 1.0))
        .reset_index(name="ret_1m")
        .sort_values(["ticker", "month"])
    )
    monthly["forward_1m"] = monthly.groupby("ticker")["ret_1m"].shift(-1)
    monthly["forward_3m"] = (
        monthly.groupby("ticker")["ret_1m"]
        .transform(lambda s: (1.0 + s.shift(-1)) * (1.0 + s.shift(-2)) * (1.0 + s.shift(-3)) - 1.0)
    )
    return monthly.rename(columns={"month": "date"})


def _load_metric_ids(
    jurisdiction: str,
    *,
    start: Any | None = None,
    end: Any | None = None,
    metric_family: str | None = None,
) -> list[str]:
    cfg = get_config(jurisdiction)
    params: list[Any] = []
    filters = ["period_end IS NOT NULL", "value IS NOT NULL", "COALESCE(importance, 9) <= 2"]
    if cfg.country_code:  # INTL per-country: fact_metrics_intl has no country_code → scope by ticker
        filters.append("ticker = ANY(%s)")
        params.append(_intl_country_tickers(cfg.country_code))
    if start is not None:
        filters.append("(date_trunc('month', period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date >= %s")
        params.append(start)
    if end is not None:
        filters.append("(date_trunc('month', period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date <= %s")
        params.append(end)
    with connect() as conn:
        df = pd.read_sql(
            f"""
            SELECT DISTINCT metric_id
            FROM   {cfg.metrics_table}
            WHERE  {" AND ".join(filters)}
            ORDER  BY metric_id
            """,
            conn,
            params=tuple(params),
        )
    if df.empty:
        return []
    metric_ids = [str(value) for value in df["metric_id"].dropna().unique()]
    return [metric for metric in metric_ids if _metric_matches_family(metric, metric_family)]


def _load_metrics(
    jurisdiction: str,
    *,
    start: Any | None = None,
    end: Any | None = None,
    metric_ids: list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    cfg = get_config(jurisdiction)
    params: list[Any] = []
    # CTE filters run inside the metric_rows CTE (which appears first in the SQL), so their
    # params must precede the outer filters' params.
    cte_filters = ["period_end IS NOT NULL", "value IS NOT NULL", "COALESCE(importance, 9) <= 2"]
    if cfg.country_code:  # INTL per-country: scope the metric cross-section by ticker
        cte_filters.append("ticker = ANY(%s)")
        params.append(_intl_country_tickers(cfg.country_code))
    outer_filters = []
    if start is not None:
        outer_filters.append("date >= %s")
        params.append(start)
    if end is not None:
        outer_filters.append("date <= %s")
        params.append(end)
    if metric_ids:
        outer_filters.append("metric_id = ANY(%s)")
        params.append(list(metric_ids))
    where_sql = f"WHERE {' AND '.join(outer_filters)}" if outer_filters else ""
    with connect() as conn:
        df = pd.read_sql(
            f"""
            WITH metric_rows AS (
                SELECT (date_trunc('month', period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date AS date,
                       ticker,
                       metric_id,
                       value::float AS value
                FROM   {cfg.metrics_table}
                WHERE  {' AND '.join(cte_filters)}
            )
            SELECT date, ticker, metric_id, value
            FROM   metric_rows
            {where_sql}
            """,
            conn,
            params=tuple(params),
        )
    if df.empty:
        return pd.DataFrame()
    # errors="coerce" guards against corrupt warehouse period_ends (e.g. a year-6016 date whose
    # +90d month-end overflows pandas' ns range) — NaT, then dropped. Yahoo-backed INTL data
    # makes such rows likelier; bounded callers used to avoid it by luck of the date filter.
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if df.empty:
        return pd.DataFrame()
    df["date"] = df["date"].dt.date
    return df


def _load_states(jurisdiction: str, run_id: str) -> pd.DataFrame:
    with connect() as conn:
        df = pd.read_sql(
            """
            SELECT date, phase_label, phase_probabilities
            FROM   fact_cycle_state_monthly
            WHERE  jurisdiction = %s
              AND  run_id = %s
              AND  phase_label IS NOT NULL
            """,
            conn,
            params=(jurisdiction, run_id),
        )
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["phase_probabilities"] = df["phase_probabilities"].map(_probability_map)
    labels = sorted({label for item in df["phase_probabilities"] for label in item})
    for label in labels:
        df[f"prob::{label}"] = df["phase_probabilities"].map(lambda item, key=label: float(item.get(key, 0.0)))
    return df


def _probability_weighted_ic_rows(
    panel: pd.DataFrame,
    *,
    min_obs: int,
    horizons: tuple[str, ...],
    lookback_months: int,
) -> list[dict[str, Any]]:
    prob_cols = [col for col in panel.columns if col.startswith("prob::")]
    if not prob_cols:
        return []
    working = panel.copy()
    working["date_ts"] = pd.to_datetime(working["date"])
    rows: list[dict[str, Any]] = []
    for as_of in sorted(working["date_ts"].dropna().unique()):
        as_of_ts = pd.Timestamp(as_of)
        start_ts = as_of_ts - pd.DateOffset(months=max(1, lookback_months) - 1)
        window = working[(working["date_ts"] >= start_ts) & (working["date_ts"] <= as_of_ts)]
        if window.empty:
            continue
        for prob_col in prob_cols:
            regime = prob_col.removeprefix("prob::")
            for metric_id, group in window.groupby("metric_id"):
                for horizon in horizons:
                    target = f"forward_{horizon}"
                    if target not in group:
                        continue
                    clean = group[["value", target, prob_col]].dropna()
                    clean = clean[clean[prob_col] > 0]
                    if len(clean) < min_obs:
                        continue
                    corr, p_value = _weighted_spearman(clean["value"], clean[target], clean[prob_col])
                    rows.append(
                        {
                            "date": as_of_ts.date(),
                            "regime_label": regime,
                            "metric_id": str(metric_id),
                            "forward_return_window": horizon,
                            "spearman_ic": corr,
                            "p_value": p_value,
                            "n_obs": int(len(clean)),
                            "diagnostics": {
                                "min_obs": min_obs,
                                "lookback_months": lookback_months,
                                "probability_weight_sum": float(clean[prob_col].sum()),
                            },
                        }
                    )
    return rows


def _ic_rows_for_panel(
    panel: pd.DataFrame,
    *,
    run_id: str,
    jurisdiction: str,
    min_obs: int,
    horizons: tuple[str, ...],
    probability_lookback_months: int,
) -> tuple[list[tuple], int, int]:
    rows = []
    hard_rows = 0
    for (month, regime, metric_id), group in panel.groupby(["date", "phase_label", "metric_id"]):
        for horizon in horizons:
            target = f"forward_{horizon}"
            if target not in group:
                continue
            clean = group[["value", target]].dropna()
            if len(clean) < min_obs:
                continue
            corr, p_value = _spearman(clean["value"], clean[target])
            rows.append(
                (
                    run_id,
                    month,
                    jurisdiction,
                    "cycle_model",
                    str(regime),
                    str(metric_id),
                    horizon,
                    corr,
                    p_value,
                    int(len(clean)),
                    _json({"min_obs": min_obs}),
                )
            )
            hard_rows += 1
    probability_rows = _probability_weighted_ic_rows(
        panel,
        min_obs=min_obs,
        horizons=horizons,
        lookback_months=probability_lookback_months,
    )
    for row in probability_rows:
        rows.append(
            (
                run_id,
                row["date"],
                jurisdiction,
                "cycle_model_probability",
                row["regime_label"],
                row["metric_id"],
                row["forward_return_window"],
                row["spearman_ic"],
                row["p_value"],
                row["n_obs"],
                _json(row["diagnostics"]),
            )
        )
    return rows, hard_rows, len(probability_rows)


def _write_ic_rows(cur, rows: list[tuple]) -> int:
    if not rows:
        return 0
    return execute_values(
        cur,
        """
        INSERT INTO fact_equity_factor_ic_regime
            (run_id, date, jurisdiction, regime_source, regime_label, metric_id,
             forward_return_window, spearman_ic, p_value, n_obs, diagnostics_json)
        VALUES %s
        ON CONFLICT (run_id, date, jurisdiction, regime_source, regime_label, metric_id, forward_return_window)
        DO UPDATE SET
            spearman_ic = EXCLUDED.spearman_ic,
            p_value = EXCLUDED.p_value,
            n_obs = EXCLUDED.n_obs,
            diagnostics_json = EXCLUDED.diagnostics_json
        """,
        rows,
    )


def _initialize_ic_job(
    *,
    job_key: str,
    jurisdiction: str,
    run_id: str,
    metric_family: str,
    horizons: tuple[str, ...],
    chunk_size: int,
    total_metrics: int,
    state_start: Any,
    state_end: Any,
    diagnostics: dict[str, Any],
) -> None:
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        cur.execute(
            """
            INSERT INTO fact_cycle_ic_job_status
                (job_key, jurisdiction, run_id, status, metric_family, horizons_json,
                 chunk_size, total_metrics, state_start, state_end, started_at,
                 updated_at, diagnostics_json)
            VALUES (%s, %s, %s, 'running', %s, %s::jsonb,
                    %s, %s, %s, %s, now(), now(), %s::jsonb)
            ON CONFLICT (job_key) DO UPDATE SET
                status = 'running',
                metric_family = EXCLUDED.metric_family,
                horizons_json = EXCLUDED.horizons_json,
                chunk_size = EXCLUDED.chunk_size,
                total_metrics = EXCLUDED.total_metrics,
                state_start = EXCLUDED.state_start,
                state_end = EXCLUDED.state_end,
                started_at = COALESCE(fact_cycle_ic_job_status.started_at, now()),
                updated_at = now(),
                diagnostics_json = fact_cycle_ic_job_status.diagnostics_json || EXCLUDED.diagnostics_json
            """,
            (
                job_key,
                jurisdiction,
                run_id,
                metric_family,
                json.dumps(list(horizons)),
                int(chunk_size),
                int(total_metrics),
                state_start,
                state_end,
                json.dumps(diagnostics, default=str),
            ),
        )


def _reset_ic_job(job_key: str, jurisdiction: str, run_id: str, horizons: tuple[str, ...]) -> None:
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        cur.execute("DELETE FROM fact_cycle_ic_job_metric_status WHERE job_key = %s", (job_key,))
        cur.execute(
            """
            DELETE FROM fact_equity_factor_ic_regime
            WHERE  jurisdiction = %s
              AND  run_id = %s
              AND  regime_source IN ('cycle_model', 'cycle_model_probability')
              AND  forward_return_window = ANY(%s)
            """,
            (jurisdiction, run_id, list(horizons)),
        )
        cur.execute(
            """
            UPDATE fact_cycle_ic_job_status
            SET status = 'pending',
                completed_metrics = 0,
                failed_metrics = 0,
                rows_written = 0,
                hard_rows_written = 0,
                probability_rows_written = 0,
                completed_at = NULL,
                elapsed_seconds = NULL,
                updated_at = now(),
                diagnostics_json = '{}'::jsonb
            WHERE job_key = %s
            """,
            (job_key,),
        )


def _completed_metric_ids(job_key: str) -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        cur.execute(
            """
            SELECT metric_id
            FROM   fact_cycle_ic_job_metric_status
            WHERE  job_key = %s
              AND  status = 'complete'
            """,
            (job_key,),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _mark_metric_chunk(
    *,
    job_key: str,
    metric_ids: list[str],
    status: str,
    rows_written: int,
    error_text: str | None = None,
    hard_rows: int = 0,
    probability_rows: int = 0,
) -> None:
    per_metric = int(rows_written / max(1, len(metric_ids))) if status == "complete" else 0
    metric_rows = [(job_key, metric_id, status, per_metric, error_text) for metric_id in metric_ids]
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        execute_values(
            cur,
            """
            INSERT INTO fact_cycle_ic_job_metric_status
                (job_key, metric_id, status, rows_written, error_text)
            VALUES %s
            ON CONFLICT (job_key, metric_id) DO UPDATE SET
                status = EXCLUDED.status,
                rows_written = EXCLUDED.rows_written,
                error_text = EXCLUDED.error_text,
                updated_at = now()
            """,
            metric_rows,
        )
        cur.execute(
            """
            WITH counts AS (
                SELECT
                    COUNT(*) FILTER (WHERE status = 'complete') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COALESCE(SUM(rows_written) FILTER (WHERE status = 'complete'), 0) AS written
                FROM fact_cycle_ic_job_metric_status
                WHERE job_key = %s
            )
            UPDATE fact_cycle_ic_job_status j
            SET completed_metrics = counts.completed,
                failed_metrics = counts.failed,
                rows_written = j.rows_written + %s,
                hard_rows_written = j.hard_rows_written + %s,
                probability_rows_written = j.probability_rows_written + %s,
                updated_at = now()
            FROM counts
            WHERE j.job_key = %s
            """,
            (job_key, int(rows_written), int(hard_rows), int(probability_rows), job_key),
        )


def _finish_ic_job(job_key: str, started_at: float, *, failed: bool = False, error: str | None = None) -> dict[str, Any]:
    elapsed = time.time() - started_at
    with connect() as conn, conn.cursor() as cur:
        _ensure_ic_status_tables(cur)
        cur.execute(
            """
            UPDATE fact_cycle_ic_job_status
            SET status = CASE WHEN %s THEN 'failed' ELSE 'complete' END,
                completed_at = CASE WHEN %s THEN completed_at ELSE now() END,
                elapsed_seconds = %s,
                updated_at = now(),
                diagnostics_json = CASE
                    WHEN %s THEN diagnostics_json || %s::jsonb
                    ELSE (diagnostics_json - 'error') || %s::jsonb
                END
            WHERE job_key = %s
            """,
            (
                failed,
                failed,
                elapsed,
                failed,
                json.dumps({"error": error} if error else {}, default=str),
                json.dumps({}, default=str),
                job_key,
            ),
        )
        cur.execute(
            """
            SELECT jurisdiction, run_id, metric_family, horizons_json, state_start, state_end
            FROM fact_cycle_ic_job_status
            WHERE job_key = %s
            """,
            (job_key,),
        )
        row = cur.fetchone()
    if not row:
        return {"job_key": job_key, "status": "missing"}
    horizons_payload = _json_payload(row[3], ["1m", "3m"])
    horizons = tuple(str(v) for v in horizons_payload) if isinstance(horizons_payload, list) else ("1m", "3m")
    return get_regime_factor_ic_job_status(
        str(row[0]),
        run_id=str(row[1]),
        metric_family=str(row[2]),
        horizons=horizons,
        start=row[4],
        end=row[5],
    )


def compute_regime_factor_ic(
    jurisdiction: str,
    *,
    run_id: str | None = None,
    min_obs: int = 25,
    horizons: tuple[str, ...] = ("1m", "3m"),
    probability_lookback_months: int = 60,
    metric_family: str = "all",
    chunk_size: int = 25,
    resume: bool = False,
    date_start: Any | None = None,
    date_end: Any | None = None,
    full: bool = False,
    status_only: bool = False,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    if run_id is None:
        run_id = latest_run_id(cfg.code, ("hmm", "dfm", "pca", "vae"))
    if run_id is None:
        raise RuntimeError(f"No completed cycle model run found for {cfg.code}. Train a baseline or HMM first.")
    family = _normalize_metric_family(metric_family)
    horizons = tuple(str(h).lower() for h in horizons if str(h).lower() in {"1m", "3m"}) or ("1m", "3m")

    states = _load_states(cfg.code, run_id)
    state_start = states["date"].min() if not states.empty else None
    state_end = states["date"].max() if not states.empty else None
    if date_start is not None:
        state_start = max(state_start, pd.Timestamp(date_start).date()) if state_start else pd.Timestamp(date_start).date()
    if date_end is not None:
        state_end = min(state_end, pd.Timestamp(date_end).date()) if state_end else pd.Timestamp(date_end).date()
    if not states.empty and state_start and state_end:
        states = states[(states["date"] >= state_start) & (states["date"] <= state_end)]

    job_key = _job_key(cfg.code, run_id, metric_family=family, horizons=horizons, start=state_start, end=state_end)
    if status_only:
        return get_regime_factor_ic_job_status(cfg.code, run_id=run_id, metric_family=family, horizons=horizons, start=state_start, end=state_end)

    metric_ids = _load_metric_ids(cfg.code, start=state_start, end=state_end, metric_family=family)
    if states.empty or not metric_ids:
        raise RuntimeError(
            f"Insufficient IC inputs for {cfg.code}: states={len(states)} metrics={len(metric_ids)}"
        )

    if resume:
        status_snapshot = get_regime_factor_ic_job_status(
            cfg.code,
            run_id=run_id,
            metric_family=family,
            horizons=horizons,
            start=state_start,
            end=state_end,
        )
        if status_snapshot.get("status") == "complete" and int(status_snapshot.get("completed_metrics") or 0) >= len(metric_ids):
            return {
                "jurisdiction": cfg.code,
                "run_id": run_id,
                "job_key": job_key,
                "panel_rows": 0,
                "ic_rows": 0,
                "hard_ic_rows": 0,
                "probability_ic_rows": 0,
                "probability_lookback_months": probability_lookback_months,
                "state_start": state_start,
                "state_end": state_end,
                "horizons": list(horizons),
                "metric_family": family,
                "total_metrics": len(metric_ids),
                "completed_before_resume": int(status_snapshot.get("completed_metrics") or 0),
                "remaining_metrics_started": 0,
                "status": status_snapshot,
            }

    started_at = time.time()
    if not resume:
        _reset_ic_job(job_key, cfg.code, run_id, horizons)
    _initialize_ic_job(
        job_key=job_key,
        jurisdiction=cfg.code,
        run_id=run_id,
        metric_family=family,
        horizons=horizons,
        chunk_size=chunk_size,
        total_metrics=len(metric_ids),
        state_start=state_start,
        state_end=state_end,
        diagnostics={
            "min_obs": min_obs,
            "probability_lookback_months": probability_lookback_months,
            "full": bool(full),
        },
    )

    completed = _completed_metric_ids(job_key) if resume else set()
    remaining_ids = [metric_id for metric_id in metric_ids if metric_id not in completed]
    panel_rows = 0
    total_written = 0
    total_hard_rows = 0
    total_probability_rows = 0
    had_failures = False

    forward_months = _horizon_months(horizons)
    returns = _load_monthly_returns(cfg.code, start=state_start, end=state_end, forward_months=forward_months)
    if returns.empty:
        raise RuntimeError(f"Insufficient IC inputs for {cfg.code}: returns={len(returns)}")

    try:
        for chunk in _chunks(remaining_ids, chunk_size):
            try:
                metrics = _load_metrics(cfg.code, start=state_start, end=state_end, metric_ids=chunk)
                if metrics.empty:
                    _mark_metric_chunk(job_key=job_key, metric_ids=chunk, status="complete", rows_written=0)
                    continue
                panel = metrics.merge(returns, on=["date", "ticker"], how="inner").merge(states, on="date", how="inner")
                panel_rows += int(len(panel))
                if panel.empty:
                    _mark_metric_chunk(job_key=job_key, metric_ids=chunk, status="complete", rows_written=0)
                    continue
                rows, hard_rows, probability_rows = _ic_rows_for_panel(
                    panel,
                    run_id=run_id,
                    jurisdiction=cfg.code,
                    min_obs=min_obs,
                    horizons=horizons,
                    probability_lookback_months=probability_lookback_months,
                )
                with connect() as conn, conn.cursor() as cur:
                    written = _write_ic_rows(cur, rows)
                total_written += written
                total_hard_rows += hard_rows
                total_probability_rows += probability_rows
                _mark_metric_chunk(
                    job_key=job_key,
                    metric_ids=chunk,
                    status="complete",
                    rows_written=written,
                    hard_rows=hard_rows,
                    probability_rows=probability_rows,
                )
                logger.info(
                    "cycle IC chunk complete job=%s metrics=%s rows=%s",
                    job_key,
                    len(chunk),
                    written,
                )
            except Exception as exc:
                had_failures = True
                logger.exception("cycle IC chunk failed job=%s metrics=%s", job_key, chunk)
                _mark_metric_chunk(
                    job_key=job_key,
                    metric_ids=chunk,
                    status="failed",
                    rows_written=0,
                    error_text=str(exc)[:2000],
                )
                if not resume:
                    raise
        status = _finish_ic_job(job_key, started_at, failed=had_failures, error="one_or_more_metric_chunks_failed" if had_failures else None)
    except Exception as exc:
        status = _finish_ic_job(job_key, started_at, failed=True, error=str(exc))
        raise

    result = {
        "jurisdiction": cfg.code,
        "run_id": run_id,
        "job_key": job_key,
        "panel_rows": int(panel_rows),
        "ic_rows": total_written,
        "hard_ic_rows": total_hard_rows,
        "probability_ic_rows": total_probability_rows,
        "probability_lookback_months": probability_lookback_months,
        "state_start": state_start,
        "state_end": state_end,
        "horizons": list(horizons),
        "metric_family": family,
        "total_metrics": len(metric_ids),
        "completed_before_resume": len(completed),
        "remaining_metrics_started": len(remaining_ids),
        "status": status,
    }
    logger.info("computed cycle regime ICs: %s", result)
    return result
