"""Monthly point-in-time feature construction for US/JP cycle models."""
from __future__ import annotations

import json
import logging
import site
from dataclasses import asdict
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
from psycopg2.extras import Json

from xbrl_sec.sec.cycle.registry import get_config
from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger("mzqa.cycle.features")


def _json(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    return cur.fetchone()[0] is not None


def _month_end(value: pd.Series) -> pd.Series:
    return pd.to_datetime(value).dt.to_period("M").dt.to_timestamp("M").dt.date


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return out


def _date_bound(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _month_delta(later: Any, earlier: Any) -> int | None:
    if later is None or earlier is None:
        return None
    try:
        lhs = pd.Timestamp(later)
        rhs = pd.Timestamp(earlier)
    except Exception:
        return None
    return max(0, int((lhs.year - rhs.year) * 12 + (lhs.month - rhs.month)))


def _feature_rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["jurisdiction", "scope", "modality", "feature_id", "date"])
    z_parts: list[pd.DataFrame] = []
    for _, g in df.groupby(["jurisdiction", "scope", "modality", "feature_id"], sort=False):
        vals = pd.to_numeric(g["feature_value"], errors="coerce")
        mu = vals.expanding(min_periods=12).mean()
        sigma = vals.expanding(min_periods=12).std(ddof=0).replace(0.0, np.nan)
        part = g.copy()
        part["feature_z"] = ((vals - mu) / sigma).replace([np.inf, -np.inf], np.nan)
        z_parts.append(part)
    out = pd.concat(z_parts, ignore_index=True)
    out["date"] = out["date"].dt.date
    return out


def _base_row(
    *,
    date_value: date,
    jurisdiction: str,
    modality: str,
    feature_id: str,
    value: float | None,
    transform: str,
    source_table: str,
    source_detail: dict[str, Any],
    as_of_policy: str,
    raw_observation_date: date | None = None,
    available_as_of: date | None = None,
    stale_months: int | None = None,
    scope: str = "regional",
    coverage: float | None = 1.0,
    missing_ratio: float | None = 0.0,
) -> dict[str, Any]:
    available_date = available_as_of or raw_observation_date or date_value
    return {
        "date": date_value,
        "jurisdiction": jurisdiction,
        "scope": scope,
        "modality": modality,
        "feature_id": feature_id,
        "feature_value": _safe_float(value),
        "feature_z": None,
        "feature_transform": transform,
        "source_table": source_table,
        "source_detail": source_detail,
        "as_of_policy": as_of_policy,
        "raw_observation_date": raw_observation_date,
        "available_as_of": available_date,
        "stale_months": stale_months if stale_months is not None else _month_delta(date_value, available_date),
        "coverage": coverage,
        "missing_ratio": missing_ratio,
    }


def _macro_rows(jurisdiction: str, start: str | date | None, end: str | date | None) -> list[dict[str, Any]]:
    cfg = get_config(jurisdiction)
    params: list[Any] = [jurisdiction, list(cfg.macro_categories)]
    bounds = ""
    if start:
        bounds += " AND f.date >= %s"
        params.append(start)
    if end:
        bounds += " AND f.date <= %s"
        params.append(end)

    sql = f"""
        SELECT f.date AS obs_date, s.series_id, s.category, s.frequency, s.units, f.value::float AS value
        FROM   fact_macro f
        JOIN   ref_macro_series s ON s.series_id = f.series_id
        WHERE  s.jurisdiction = %s
          AND  s.category = ANY(%s)
          AND  s.is_active = TRUE
          {bounds}
        ORDER  BY s.series_id, f.date
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn, params=tuple(params))
    if df.empty:
        return []

    df["month"] = _month_end(df["obs_date"])
    df = df.sort_values(["series_id", "month", "obs_date"]).groupby(["series_id", "month"], as_index=False).tail(1)
    rows: list[dict[str, Any]] = []
    for series_id, g in df.groupby("series_id"):
        g = g.sort_values("month").copy()
        meta = g.iloc[-1][["category", "frequency", "units"]].to_dict()
        g["diff_1m"] = g["value"].diff()
        g["yoy_12m"] = g["value"].pct_change(12) * 100.0
        for _, row in g.iterrows():
            source_detail = {"series_id": series_id, **meta}
            raw_date = row["obs_date"].date() if hasattr(row["obs_date"], "date") else row["obs_date"]
            rows.append(
                _base_row(
                    date_value=row["month"],
                    jurisdiction=jurisdiction,
                    modality="macro",
                    feature_id=f"macro:{series_id}:level",
                    value=row["value"],
                    transform="month_end_last",
                    source_table="fact_macro",
                    source_detail=source_detail,
                    as_of_policy="observation_date_month_end",
                    raw_observation_date=raw_date,
                    available_as_of=raw_date,
                )
            )
            if pd.notna(row["diff_1m"]):
                rows.append(
                    _base_row(
                        date_value=row["month"],
                        jurisdiction=jurisdiction,
                        modality="macro",
                        feature_id=f"macro:{series_id}:diff_1m",
                        value=row["diff_1m"],
                        transform="month_end_last_diff_1m",
                        source_table="fact_macro",
                        source_detail=source_detail,
                        as_of_policy="observation_date_month_end",
                        raw_observation_date=raw_date,
                        available_as_of=raw_date,
                    )
                )
            if pd.notna(row["yoy_12m"]):
                rows.append(
                    _base_row(
                        date_value=row["month"],
                        jurisdiction=jurisdiction,
                        modality="macro",
                        feature_id=f"macro:{series_id}:yoy_12m",
                        value=row["yoy_12m"],
                        transform="month_end_last_yoy_12m_pct",
                        source_table="fact_macro",
                        source_detail=source_detail,
                        as_of_policy="observation_date_month_end",
                        raw_observation_date=raw_date,
                        available_as_of=raw_date,
                    )
                )
    return rows


def _market_rows(jurisdiction: str, start: str | date | None, end: str | date | None) -> list[dict[str, Any]]:
    cfg = get_config(jurisdiction)
    rows: list[dict[str, Any]] = []
    with connect() as conn, conn.cursor() as cur:
        if not _table_exists(cur, cfg.price_table):
            return rows
        params: list[Any] = []
        bounds = ""
        if start:
            bounds += " AND date >= %s"
            params.append(start)
        if end:
            bounds += " AND date <= %s"
            params.append(end)
        price_sql = f"""
            WITH monthly AS (
                SELECT date_trunc('month', date)::date AS month,
                       ticker,
                       EXP(SUM(LN(GREATEST(1.0 + COALESCE(return, 0.0), 0.000001)))) - 1.0 AS ret
                FROM   {cfg.price_table}
                WHERE  return IS NOT NULL {bounds}
                GROUP  BY date_trunc('month', date), ticker
            )
            SELECT month AS date,
                   AVG(ret)::float AS equal_return,
                   STDDEV_SAMP(ret)::float AS dispersion,
                   AVG((ret > 0)::int)::float AS breadth_positive,
                   COUNT(*)::int AS n_tickers
            FROM   monthly
            GROUP  BY month
            ORDER  BY month
        """
        cur.execute(price_sql, tuple(params))
        for month, equal_ret, dispersion, breadth, n_tickers in cur.fetchall():
            detail = {"n_tickers": n_tickers}
            for feature, value, transform in (
                ("market:return_equal_weighted", equal_ret, "monthly_compound_equal_weighted"),
                ("market:return_dispersion", dispersion, "monthly_cross_section_stddev"),
                ("market:return_breadth_positive", breadth, "monthly_positive_return_share"),
            ):
                rows.append(
                    _base_row(
                        date_value=month,
                        jurisdiction=jurisdiction,
                        modality="market",
                        feature_id=feature,
                        value=value,
                        transform=transform,
                        source_table=cfg.price_table,
                        source_detail=detail,
                        as_of_policy="month_end_market_data",
                        raw_observation_date=month,
                        available_as_of=month,
                        coverage=float(n_tickers or 0),
                    )
                )

        if _table_exists(cur, "fact_sector_returns"):
            cur.execute(
                """
                WITH monthly AS (
                    SELECT date_trunc('month', date)::date AS month,
                           gics_code,
                           gics_name,
                           EXP(SUM(LN(GREATEST(1.0 + COALESCE(cap_weighted_return, 0.0), 0.000001)))) - 1.0 AS ret
                    FROM   fact_sector_returns
                    WHERE  jurisdiction = %s
                      AND  grouping_level = 'sector'
                      AND  cap_weighted_return IS NOT NULL
                    GROUP  BY date_trunc('month', date), gics_code, gics_name
                )
                SELECT month AS date,
                       AVG(ret)::float AS sector_return_mean,
                       STDDEV_SAMP(ret)::float AS sector_return_dispersion,
                       AVG((ret > 0)::int)::float AS sector_breadth_positive,
                       COUNT(*)::int AS n_sectors
                FROM monthly
                GROUP BY month
                ORDER BY month
                """,
                (jurisdiction,),
            )
            for month, mean_ret, dispersion, breadth, n_sectors in cur.fetchall():
                detail = {"n_sectors": n_sectors}
                for feature, value, transform in (
                    ("market:sector_return_mean", mean_ret, "monthly_sector_return_mean"),
                    ("market:sector_return_dispersion", dispersion, "monthly_sector_return_stddev"),
                    ("market:sector_breadth_positive", breadth, "monthly_positive_sector_share"),
                ):
                    rows.append(
                        _base_row(
                            date_value=month,
                            jurisdiction=jurisdiction,
                            modality="market",
                            feature_id=feature,
                            value=value,
                            transform=transform,
                            source_table="fact_sector_returns",
                            source_detail=detail,
                            as_of_policy="month_end_market_data",
                            raw_observation_date=month,
                            available_as_of=month,
                            coverage=float(n_sectors or 0),
                        )
                    )

        if _table_exists(cur, "fact_fama_french"):
            cur.execute(
                """
                SELECT date_trunc('month', date)::date AS month,
                       factor,
                       SUM(value)::float AS factor_return
                FROM   fact_fama_french
                WHERE  factor <> 'RF'
                GROUP  BY date_trunc('month', date), factor
                ORDER  BY month, factor
                """
            )
            for month, factor, value in cur.fetchall():
                rows.append(
                    _base_row(
                        date_value=month,
                        jurisdiction=jurisdiction,
                        modality="market",
                        feature_id=f"market:fama_french:{factor}",
                        value=value,
                        transform="monthly_factor_return_sum",
                        source_table="fact_fama_french",
                        source_detail={"factor": factor},
                        as_of_policy="month_end_market_data",
                        raw_observation_date=month,
                        available_as_of=month,
                    )
                )
    return rows


def _fundamental_rows(jurisdiction: str, start: str | date | None, end: str | date | None) -> list[dict[str, Any]]:
    cfg = get_config(jurisdiction)
    start_date = _date_bound(start)
    end_date = _date_bound(end)
    rows: list[dict[str, Any]] = []
    with connect() as conn, conn.cursor() as cur:
        if _table_exists(cur, cfg.metrics_table):
            cur.execute(
                f"""
                SELECT (date_trunc('month', period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date AS month,
                       metric_id,
                       COALESCE(category, 'unknown') AS category,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value)::float AS median_value,
                       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY value)::float
                         - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY value)::float AS iqr_value,
                       AVG((value > 0)::int)::float AS positive_share,
                       MAX(period_end)::date AS raw_period_end,
                       COUNT(*)::int AS n_obs
                FROM   {cfg.metrics_table}
                WHERE  period_end IS NOT NULL
                  AND  value IS NOT NULL
                  AND  COALESCE(importance, 9) <= 2
                GROUP  BY date_trunc('month', period_end + INTERVAL '90 days'), metric_id, COALESCE(category, 'unknown')
                HAVING COUNT(*) >= 10
                ORDER  BY month, metric_id
                """
            )
            for month, metric_id, category, median_value, iqr_value, positive_share, raw_period_end, n_obs in cur.fetchall():
                if start_date and month < start_date:
                    continue
                if end_date and month > end_date:
                    continue
                detail = {"metric_id": metric_id, "category": category, "n_obs": n_obs}
                for suffix, value, transform in (
                    ("median", median_value, "period_end_plus_90d_cross_section_median"),
                    ("iqr", iqr_value, "period_end_plus_90d_cross_section_iqr"),
                    ("positive_share", positive_share, "period_end_plus_90d_positive_share"),
                ):
                    rows.append(
                        _base_row(
                            date_value=month,
                            jurisdiction=jurisdiction,
                            modality="fundamental",
                            feature_id=f"fundamental:metric:{metric_id}:{suffix}",
                            value=value,
                            transform=transform,
                            source_table=cfg.metrics_table,
                            source_detail=detail,
                            as_of_policy="period_end_plus_90d_proxy",
                            raw_observation_date=raw_period_end,
                            available_as_of=month,
                            scope="fundamental_breadth",
                            coverage=float(n_obs or 0),
                        )
                    )

        if _table_exists(cur, cfg.fundamentals_table):
            cur.execute(
                f"""
                SELECT (date_trunc('month', s.filed_date) + INTERVAL '1 month - 1 day')::date AS month,
                       s.line_item_id,
                       COALESCE(r.category, 'unknown') AS category,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.value)::float AS median_value,
                       PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY s.value)::float
                         - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY s.value)::float AS iqr_value,
                       MAX(s.filed_date)::date AS max_filed_date,
                       COUNT(*)::int AS n_obs
                FROM   {cfg.fundamentals_table} s
                LEFT   JOIN ref_standardized_line_items r ON r.line_item_id = s.line_item_id
                WHERE  s.filed_date IS NOT NULL
                  AND  s.value IS NOT NULL
                  AND  COALESCE(r.importance, 9) <= 2
                GROUP  BY date_trunc('month', s.filed_date), s.line_item_id, COALESCE(r.category, 'unknown')
                HAVING COUNT(*) >= 10
                ORDER  BY month, s.line_item_id
                """
            )
            for month, line_item_id, category, median_value, iqr_value, max_filed_date, n_obs in cur.fetchall():
                if start_date and month < start_date:
                    continue
                if end_date and month > end_date:
                    continue
                detail = {"line_item_id": line_item_id, "category": category, "n_obs": n_obs}
                for suffix, value, transform in (
                    ("median", median_value, "filed_date_cross_section_median"),
                    ("iqr", iqr_value, "filed_date_cross_section_iqr"),
                ):
                    rows.append(
                        _base_row(
                            date_value=month,
                            jurisdiction=jurisdiction,
                            modality="fundamental",
                            feature_id=f"fundamental:line_item:{line_item_id}:{suffix}",
                            value=value,
                            transform=transform,
                            source_table=cfg.fundamentals_table,
                            source_detail=detail,
                            as_of_policy="filed_date",
                            raw_observation_date=max_filed_date,
                            available_as_of=max_filed_date,
                            scope="fundamental_breadth",
                            coverage=float(n_obs or 0),
                        )
                    )
    return rows


def _upsert_features(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = []
    for row in df.to_dict("records"):
        rows.append(
            (
                row["date"],
                row["jurisdiction"],
                row["scope"],
                row["modality"],
                row["feature_id"],
                _safe_float(row["feature_value"]),
                _safe_float(row["feature_z"]),
                row["feature_transform"],
                row["source_table"],
                _json(row["source_detail"]),
                row["as_of_policy"],
                row["raw_observation_date"],
                row["available_as_of"],
                int(row["stale_months"]) if pd.notna(row["stale_months"]) else None,
                _safe_float(row["coverage"]),
                _safe_float(row["missing_ratio"]),
            )
        )
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO fact_cycle_feature_monthly
                (date, jurisdiction, scope, modality, feature_id, feature_value, feature_z,
                 feature_transform, source_table, source_detail, as_of_policy,
                 raw_observation_date, available_as_of, stale_months, coverage, missing_ratio)
            VALUES %s
            ON CONFLICT (date, jurisdiction, scope, modality, feature_id) DO UPDATE SET
                scope = EXCLUDED.scope,
                feature_value = EXCLUDED.feature_value,
                feature_z = EXCLUDED.feature_z,
                feature_transform = EXCLUDED.feature_transform,
                source_table = EXCLUDED.source_table,
                source_detail = EXCLUDED.source_detail,
                as_of_policy = EXCLUDED.as_of_policy,
                raw_observation_date = EXCLUDED.raw_observation_date,
                available_as_of = EXCLUDED.available_as_of,
                stale_months = EXCLUDED.stale_months,
                coverage = EXCLUDED.coverage,
                missing_ratio = EXCLUDED.missing_ratio,
                updated_at = now()
            """,
            rows,
        )


def build_cycle_features(
    jurisdiction: str,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = get_config(jurisdiction)
    raw_rows = []
    raw_rows.extend(_macro_rows(cfg.code, start, end))
    raw_rows.extend(_market_rows(cfg.code, start, end))
    raw_rows.extend(_fundamental_rows(cfg.code, start, end))
    df = _feature_rows_to_frame(raw_rows)
    counts = {
        "jurisdiction": cfg.code,
        "rows": int(len(df)),
        "features": int(df["feature_id"].nunique()) if not df.empty else 0,
        "months": int(df["date"].nunique()) if not df.empty else 0,
        "by_modality": df.groupby("modality").size().to_dict() if not df.empty else {},
        "config": asdict(cfg),
        "dry_run": dry_run,
    }
    if dry_run:
        return counts
    written = _upsert_features(df)
    counts["written"] = written
    logger.info("cycle features built: %s", counts)
    return counts
