"""Jurisdiction-aware rolling Fama-French factor model for MZQA."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict
import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

MODEL_FF3 = "FF3"
MODEL_FF4 = "FF4"
MODEL_FF5 = "FF5"
MODEL_FF6 = "FF6"
ALL_MODELS = (MODEL_FF3, MODEL_FF4, MODEL_FF5, MODEL_FF6)
MODEL_FACTORS = {
    MODEL_FF3: ("MKT", "SMB", "HML"),
    MODEL_FF4: ("MKT", "SMB", "HML", "MOM"),
    MODEL_FF5: ("MKT", "SMB", "HML", "RMW", "CMA"),
    MODEL_FF6: ("MKT", "SMB", "HML", "RMW", "CMA", "MOM"),
}
WINDOW_DAYS = 252
SHIFT_DAYS = 21
MIN_OBS = 126
NW_LAGS = 5
HUBER_DELTA = 1.345
HUBER_ITER = 5
WINSOR_Q_LO = 0.01
WINSOR_Q_HI = 0.99

FF_PRIORITY = {
    "US": {
        "mkt": ["F-F_Research_Data_Factors_daily", "F-F_Research_Data_Factors"],
        "smb": ["F-F_Research_Data_Factors_daily", "F-F_Research_Data_Factors"],
        "hml": ["F-F_Research_Data_Factors_daily", "F-F_Research_Data_Factors"],
        "rmw": ["F-F_Research_Data_5_Factors_2x3_daily", "F-F_Research_Data_5_Factors_2x3"],
        "cma": ["F-F_Research_Data_5_Factors_2x3_daily", "F-F_Research_Data_5_Factors_2x3"],
        "mom": ["F-F_Momentum_Factor_daily", "F-F_Momentum_Factor"],
    },
    "JP": {
        "mkt": ["Japan_3_Factors_Daily", "Japan_3_Factors"],
        "smb": ["Japan_3_Factors_Daily", "Japan_3_Factors"],
        "hml": ["Japan_3_Factors_Daily", "Japan_3_Factors"],
        "rmw": ["Japan_5_Factors_Daily", "Japan_5_Factors"],
        "cma": ["Japan_5_Factors_Daily", "Japan_5_Factors"],
        "mom": ["Japan_Mom_Factor_Daily", "Japan_Mom_Factor"],
    },
    "INTL": {
        "mkt": ["Developed_3_Factors_Daily", "Developed_3_Factors"],
        "smb": ["Developed_3_Factors_Daily", "Developed_3_Factors"],
        "hml": ["Developed_3_Factors_Daily", "Developed_3_Factors"],
        "rmw": ["Developed_5_Factors_Daily", "Developed_5_Factors"],
        "cma": ["Developed_5_Factors_Daily", "Developed_5_Factors"],
        "mom": ["Developed_Mom_Factor_Daily", "Developed_Mom_Factor"],
    },
    "EM": {
        "mkt": ["Emerging_Markets_3_Factors_Daily", "Emerging_Markets_3_Factors", "Developed_3_Factors_Daily"],
        "smb": ["Emerging_Markets_3_Factors_Daily", "Emerging_Markets_3_Factors", "Developed_3_Factors_Daily"],
        "hml": ["Emerging_Markets_3_Factors_Daily", "Emerging_Markets_3_Factors", "Developed_3_Factors_Daily"],
        "rmw": ["Developed_5_Factors_Daily", "Developed_5_Factors"],
        "cma": ["Developed_5_Factors_Daily", "Developed_5_Factors"],
        "mom": ["Emerging_MOM_Factor", "Developed_Mom_Factor_Daily", "Developed_Mom_Factor"],
    },
}

COUNTRY_TO_REGION = {
    "US": "US",
    "UNITED STATES": "US",
    "JP": "JP",
    "JAPAN": "JP",
    "CN": "EM",
    "CHINA": "EM",
    "IN": "EM",
    "INDIA": "EM",
    "KR": "EM",
    "KOREA": "EM",
    "BR": "EM",
    "BRAZIL": "EM",
    "TW": "EM",
    "TAIWAN": "EM",
}

FACTOR_ALIASES = {
    "MKT": ["Mkt-RF", "Rm-Rf", "MKT", "Mkt_RF", "Market", "mktrf", "Rm_Rf"],
    "SMB": ["SMB", "smb"],
    "HML": ["HML", "hml"],
    "MOM": ["Mom", "MOM", "WML", "mom", "wml", "UMD", "PR1YR"],
    "RMW": ["RMW", "rmw"],
    "CMA": ["CMA", "cma"],
    "RF": ["RF", "Rf", "rf"],
}


def _sf(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def _country_to_region(country: str | None, jurisdiction: str) -> str:
    if jurisdiction == "US":
        return "US"
    if jurisdiction == "JP":
        return "JP"
    return COUNTRY_TO_REGION.get((country or "").upper(), "INTL")


def _resolve(available: dict[str, str], choices: list[str]) -> str | None:
    for choice in choices:
        found = available.get(choice.lower())
        if found:
            return found
    return None


def _dataset_mapping(conn) -> dict[str, dict[str, str | None]]:
    with conn.cursor() as cur:
        cur.execute("SELECT dataset FROM dim_ff_dataset")
        available = {row[0].lower(): row[0] for row in cur.fetchall()}
    mapping: dict[str, dict[str, str | None]] = {}
    for region, priorities in FF_PRIORITY.items():
        mapping[region] = {factor: _resolve(available, choices) for factor, choices in priorities.items()}
    return mapping


def _companies(
    conn,
    tickers: list[str] | None = None,
    max_tickers: int | None = None,
    jurisdiction: str | None = None,
) -> list[dict[str, Any]]:
    jurisdiction = jurisdiction.upper() if jurisdiction else None
    if jurisdiction == "US":
        source_sql = """
            SELECT 'US' AS jurisdiction, primary_ticker AS ticker, name, country_code,
                   gics_sector_name, mapping_sector
            FROM dim_company_us
        """
    elif jurisdiction == "JP":
        source_sql = """
            SELECT 'JP' AS jurisdiction, primary_ticker AS ticker,
                   COALESCE(name_en, name, primary_ticker) AS name, country_code,
                   gics_sector_name, mapping_sector
            FROM dim_company_jp
        """
    else:
        source_sql = """
            SELECT jurisdiction, ticker, name, country_code, gics_sector_name, mapping_sector
            FROM v_dim_company
        """

    where = ["ticker IS NOT NULL", "ticker <> ''"]
    params: list[Any] = []
    if jurisdiction and "v_dim_company" in source_sql:
        where.append("jurisdiction = %s")
        params.append(jurisdiction)
    if tickers:
        where.append("UPPER(ticker) = ANY(%s)")
        params.append([t.upper() for t in tickers])
    sql = f"""
        SELECT jurisdiction, ticker, name, country_code, gics_sector_name, mapping_sector
        FROM ({source_sql}) companies
        WHERE {' AND '.join(where)}
        ORDER BY jurisdiction, ticker
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = [
        {
            "jurisdiction": row[0],
            "ticker": row[1],
            "name": row[2],
            "country_code": row[3],
            "gics_sector_name": row[4],
            "mapping_sector": row[5],
        }
        for row in rows
    ]
    return out[:max_tickers] if max_tickers else out


def _find_factor_col(columns: list[str], canonical: str) -> str | None:
    lower = {str(col).lower(): str(col) for col in columns}
    for alias in FACTOR_ALIASES.get(canonical, [canonical]):
        if alias in columns:
            return alias
        if alias.lower() in lower:
            return lower[alias.lower()]
    return None


def _load_ff_datasets(conn, dataset_ids: set[str]) -> dict[str, pd.DataFrame]:
    if not dataset_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset, date, factor, return_pct
            FROM fact_fama_french
            WHERE dataset = ANY(%s)
              AND return_pct IS NOT NULL
            ORDER BY dataset, date
            """,
            (list(dataset_ids),),
        )
        rows = cur.fetchall()
    if not rows:
        return {}
    raw = pd.DataFrame(rows, columns=["dataset", "date", "factor", "return_pct"])
    result: dict[str, pd.DataFrame] = {}
    for dataset, group in raw.groupby("dataset"):
        wide = (
            group.assign(date=lambda x: pd.to_datetime(x["date"]))
            .pivot_table(index="date", columns="factor", values="return_pct", aggfunc="first")
            .sort_index()
        )
        normalized = {}
        for canonical in ("MKT", "SMB", "HML", "MOM", "RMW", "CMA", "RF"):
            col = _find_factor_col([str(c) for c in wide.columns], canonical)
            if col:
                normalized[canonical] = wide[col] / 100.0
        result[str(dataset)] = pd.DataFrame(normalized).sort_index()
    return result


def _normalize_models(models: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not models:
        return ALL_MODELS
    normalized: list[str] = []
    for model in models:
        value = str(model).upper().strip()
        if value not in MODEL_FACTORS:
            raise ValueError(f"unsupported factor model {model!r}; expected one of {', '.join(ALL_MODELS)}")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _required_dataset_keys(model: str) -> tuple[str, ...]:
    keys = ["mkt"]
    factors = set(MODEL_FACTORS[model])
    if "RMW" in factors:
        keys.append("rmw")
    if "CMA" in factors:
        keys.append("cma")
    if "MOM" in factors:
        keys.append("mom")
    return tuple(keys)


def _build_factor_frame(ff_cache: dict[str, pd.DataFrame], datasets: dict[str, str | None], model: str) -> pd.DataFrame | None:
    missing_dataset = [key for key in _required_dataset_keys(model) if not datasets.get(key) or datasets[key] not in ff_cache]
    if missing_dataset:
        return None

    base_dataset = datasets["mkt"]
    assert base_dataset is not None
    base = ff_cache[base_dataset]
    required = MODEL_FACTORS[model]
    if not all(col in base.columns for col in ("MKT", "SMB", "HML")):
        return None

    factor_df = base[[c for c in ("MKT", "SMB", "HML", "RF") if c in base.columns]].copy()
    joins = {
        "RMW": datasets.get("rmw"),
        "CMA": datasets.get("cma"),
        "MOM": datasets.get("mom"),
    }
    for factor, dataset in joins.items():
        if factor not in required:
            continue
        if not dataset or dataset not in ff_cache or factor not in ff_cache[dataset].columns:
            return None
        if factor not in factor_df.columns:
            factor_df = factor_df.join(ff_cache[dataset][[factor]], how="left")
    if not all(factor in factor_df.columns for factor in required):
        return None
    return factor_df


def _ticker_aliases(jurisdiction: str, ticker: str) -> set[str]:
    aliases = {ticker}
    if jurisdiction == "JP":
        if ticker.endswith(".T"):
            aliases.add(ticker[:-2])
        else:
            aliases.add(f"{ticker}.T")
    return aliases


def _load_price_returns(conn, companies: list[dict[str, Any]]) -> dict[tuple[str, str], dict[pd.Timestamp, float]]:
    by_juris: dict[str, list[str]] = defaultdict(list)
    alias_to_ticker: dict[tuple[str, str], set[str]] = defaultdict(set)
    for company in companies:
        jurisdiction = company["jurisdiction"]
        ticker = company["ticker"]
        for alias in _ticker_aliases(jurisdiction, ticker):
            by_juris[jurisdiction].append(alias)
            alias_to_ticker[(jurisdiction, alias)].add(ticker)
    result: dict[tuple[str, str], dict[pd.Timestamp, float]] = defaultdict(dict)
    for jurisdiction, table in (("US", "fact_prices_us"), ("JP", "fact_prices_jp")):
        tickers = by_juris.get(jurisdiction) or []
        if not tickers:
            continue
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ticker, date, log_return
                FROM {table}
                WHERE ticker = ANY(%s)
                  AND log_return IS NOT NULL
                ORDER BY ticker, date
                """,
                (tickers,),
            )
            for ticker, date_value, log_return in cur.fetchall():
                for canonical in alias_to_ticker.get((jurisdiction, ticker), {ticker}):
                    result[(jurisdiction, canonical)][pd.Timestamp(date_value)] = float(log_return)
    return result


def _winsorize(values: np.ndarray) -> np.ndarray:
    lo = np.nanquantile(values, WINSOR_Q_LO)
    hi = np.nanquantile(values, WINSOR_Q_HI)
    return np.clip(values, lo, hi)


def _huber_weights(resid: np.ndarray) -> np.ndarray:
    mad = np.median(np.abs(resid)) / 0.6745
    if mad < 1e-12:
        return np.ones(len(resid))
    z = np.abs(resid) / mad
    return np.where(z <= HUBER_DELTA, 1.0, HUBER_DELTA / z)


def _newey_west_cov(x: np.ndarray, resid: np.ndarray) -> np.ndarray:
    xtx_inv = np.linalg.pinv(x.T @ x)
    scores = resid[:, None] * x
    s_mat = scores.T @ scores
    for lag in range(1, NW_LAGS + 1):
        weight = 1.0 - lag / (NW_LAGS + 1.0)
        gamma = scores[lag:].T @ scores[:-lag]
        s_mat += weight * (gamma + gamma.T)
    return xtx_inv @ s_mat @ xtx_inv


def _durbin_watson(resid: np.ndarray) -> float:
    denom = np.sum(resid**2)
    return float(np.sum(np.diff(resid) ** 2) / denom) if denom > 0 else 2.0


def _run_ols(y: np.ndarray, x: np.ndarray, factor_names: list[str]) -> dict[str, Any] | None:
    n_obs, k_cols = x.shape
    if n_obs < k_cols + 2:
        return None
    weights = np.ones(n_obs)
    beta = None
    for _ in range(HUBER_ITER):
        w = np.diag(weights)
        xtw = x.T @ w
        try:
            beta = np.linalg.lstsq(xtw @ x, xtw @ y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return None
        resid = y - x @ beta
        weights = _huber_weights(resid)
    if beta is None:
        return None
    resid = y - x @ beta
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 0.0
    adj_r2 = 1.0 - (1.0 - r2) * (n_obs - 1) / (n_obs - k_cols)
    rmse = float(np.sqrt(ss_res / (n_obs - k_cols)))
    residual_vol = float(np.std(resid, ddof=k_cols) * np.sqrt(252.0))
    dw = _durbin_watson(resid)
    cond_num = float(np.linalg.cond(x[:, 1:])) if k_cols > 1 else 1.0
    try:
        cov = _newey_west_cov(x, resid)
    except Exception:
        cov = np.linalg.pinv(x.T @ x) * (ss_res / (n_obs - k_cols))
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    se_fallback = np.sqrt(np.maximum(np.diag(np.linalg.pinv(x.T @ x)) * (ss_res / (n_obs - k_cols)), 0.0))
    se = np.where(se > 1e-12, se, se_fallback)
    with np.errstate(invalid="ignore", divide="ignore"):
        t_stat = np.where(se > 1e-12, beta / se, np.nan)
    p_val = 2.0 * (1.0 - scipy_stats.t.cdf(np.abs(t_stat), df=n_obs - k_cols))
    k_slopes = k_cols - 1
    if k_slopes > 0 and r2 < 1.0 - 1e-12:
        f_stat = (r2 / k_slopes) / ((1.0 - r2) / (n_obs - k_cols))
        f_pvalue = float(1.0 - scipy_stats.f.cdf(f_stat, dfn=k_slopes, dfd=n_obs - k_cols))
    else:
        f_stat, f_pvalue = np.nan, np.nan
    names = ["alpha"] + factor_names
    return {
        "beta": dict(zip(names, beta)),
        "t": dict(zip(names, t_stat)),
        "p": dict(zip(names, p_val)),
        "se": dict(zip(names, se)),
        "r2": r2,
        "adj_r2": adj_r2,
        "f_stat": f_stat,
        "f_pvalue": f_pvalue,
        "rmse": rmse,
        "residual_vol": residual_vol,
        "dw": dw,
        "cond_num": cond_num,
        "n_obs": n_obs,
    }


def _rolling_betas(
    jurisdiction: str,
    ticker: str,
    ret_series: pd.Series,
    factor_df: pd.DataFrame,
    ff_region: str,
    model: str,
    full: bool,
    existing: set[tuple[pd.Timestamp, str]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    factors = list(MODEL_FACTORS[model])
    if any(factor not in factor_df.columns for factor in factors):
        return [], []
    rf = factor_df["RF"].reindex(ret_series.index).fillna(0.0) if "RF" in factor_df.columns else pd.Series(0.0, index=ret_series.index)
    excess = (ret_series - rf).dropna()
    common = excess.index.intersection(factor_df.index)
    if len(common) < MIN_OBS:
        return [], []
    y_all = excess.reindex(common).values.astype(float)
    x_all = factor_df[factors].reindex(common).values.astype(float)
    dates = common.to_numpy()
    loadings: list[tuple[Any, ...]] = []
    meta: list[tuple[Any, ...]] = []
    for end_idx in range(WINDOW_DAYS - 1, len(dates), SHIFT_DAYS):
        start_idx = end_idx - WINDOW_DAYS + 1
        window_end = pd.Timestamp(dates[end_idx]).date()
        window_start = pd.Timestamp(dates[start_idx]).date()
        if not full and (pd.Timestamp(window_end), model) in existing:
            continue
        y_w = y_all[start_idx : end_idx + 1]
        x_w = x_all[start_idx : end_idx + 1]
        valid = ~(np.isnan(y_w) | np.any(np.isnan(x_w), axis=1))
        y_w = y_w[valid]
        x_w = x_w[valid]
        if len(y_w) < MIN_OBS:
            continue
        y_w = _winsorize(y_w)
        design = np.column_stack([np.ones(len(y_w)), x_w])
        result = _run_ols(y_w, design, factors)
        if result is None:
            continue
        beta = result["beta"]
        tstat = result["t"]
        pval = result["p"]
        se = result["se"]
        alpha_ann = _sf(beta.get("alpha", 0.0) * 252.0)
        loadings.append(
            (
                jurisdiction,
                ticker,
                window_end,
                model,
                window_start,
                int(result["n_obs"]),
                ff_region,
                alpha_ann,
                _sf(beta.get("MKT")),
                _sf(beta.get("SMB")),
                _sf(beta.get("HML")),
                _sf(beta.get("MOM")),
                _sf(beta.get("RMW")),
                _sf(beta.get("CMA")),
                _sf(tstat.get("alpha")),
                _sf(tstat.get("MKT")),
                _sf(tstat.get("SMB")),
                _sf(tstat.get("HML")),
                _sf(tstat.get("MOM")),
                _sf(tstat.get("RMW")),
                _sf(tstat.get("CMA")),
            )
        )
        quality = int(
            (result["adj_r2"] > 0.20)
            + (result["f_pvalue"] < 0.05)
            + (result["n_obs"] >= 200)
            + (result["cond_num"] < 30)
            + (1.5 <= result["dw"] <= 2.5)
        )
        meta.append(
            (
                jurisdiction,
                ticker,
                window_end,
                model,
                int(result["n_obs"]),
                _sf(result["r2"]),
                _sf(result["adj_r2"]),
                _sf(result["f_stat"]),
                _sf(result["f_pvalue"]),
                _sf(result["rmse"]),
                _sf(result["residual_vol"]),
                _sf(result["dw"]),
                _sf(result["cond_num"]),
                _sf(pval.get("alpha")),
                _sf(pval.get("MKT")),
                _sf(pval.get("SMB")),
                _sf(pval.get("HML")),
                _sf(pval.get("MOM")),
                _sf(pval.get("RMW")),
                _sf(pval.get("CMA")),
                _sf(se.get("alpha")),
                _sf(se.get("MKT")),
                _sf(se.get("SMB")),
                _sf(se.get("HML")),
                _sf(se.get("MOM")),
                _sf(se.get("RMW")),
                _sf(se.get("CMA")),
                quality,
            )
        )
    return loadings, meta


def _factor_implied_returns(
    jurisdiction: str,
    ticker: str,
    model: str,
    factor_df: pd.DataFrame,
    loadings: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    if not loadings:
        return []
    rows: list[tuple[Any, ...]] = []
    ordered = sorted(loadings, key=lambda row: row[2])
    factor_cols = list(MODEL_FACTORS[model])
    rf = factor_df["RF"] if "RF" in factor_df.columns else pd.Series(0.0, index=factor_df.index)
    for idx, loading in enumerate(ordered):
        window_end = pd.Timestamp(loading[2])
        next_window_end = pd.Timestamp(ordered[idx + 1][2]) if idx + 1 < len(ordered) else None
        beta_map = {
            "MKT": loading[8],
            "SMB": loading[9],
            "HML": loading[10],
            "MOM": loading[11],
            "RMW": loading[12],
            "CMA": loading[13],
        }
        alpha_daily = (loading[7] or 0.0) / 252.0
        date_mask = factor_df.index > window_end
        if next_window_end is not None:
            date_mask &= factor_df.index <= next_window_end
        use = factor_df.loc[date_mask]
        if use.empty:
            continue
        for dt, factor_row in use.iterrows():
            implied = alpha_daily + float(rf.reindex([dt]).iloc[0] if dt in rf.index else 0.0)
            valid = True
            for factor in factor_cols:
                if factor not in use.columns:
                    valid = False
                    break
                value = factor_row.get(factor)
                beta = beta_map.get(factor)
                if pd.isna(value) or beta is None:
                    valid = False
                    break
                implied += float(beta) * float(value)
            if valid:
                rows.append((jurisdiction, ticker, pd.Timestamp(dt).date(), model, _sf(implied), loading[2]))
    return rows


INSERT_LOADINGS = """
    INSERT INTO fact_factor_loadings
        (jurisdiction, ticker, window_end, model, window_start, n_obs, ff_region,
         alpha, beta_mkt, beta_smb, beta_hml, beta_mom, beta_rmw, beta_cma,
         t_alpha, t_mkt, t_smb, t_hml, t_mom, t_rmw, t_cma)
    VALUES %s
    ON CONFLICT (jurisdiction, ticker, window_end, model) DO UPDATE SET
        window_start=EXCLUDED.window_start, n_obs=EXCLUDED.n_obs, ff_region=EXCLUDED.ff_region,
        alpha=EXCLUDED.alpha, beta_mkt=EXCLUDED.beta_mkt, beta_smb=EXCLUDED.beta_smb,
        beta_hml=EXCLUDED.beta_hml, beta_mom=EXCLUDED.beta_mom, beta_rmw=EXCLUDED.beta_rmw,
        beta_cma=EXCLUDED.beta_cma, t_alpha=EXCLUDED.t_alpha, t_mkt=EXCLUDED.t_mkt,
        t_smb=EXCLUDED.t_smb, t_hml=EXCLUDED.t_hml, t_mom=EXCLUDED.t_mom,
        t_rmw=EXCLUDED.t_rmw, t_cma=EXCLUDED.t_cma, updated_at=now()
"""

INSERT_META = """
    INSERT INTO fact_factor_reg_meta
        (jurisdiction, ticker, window_end, model, n_obs, r2, adj_r2, f_stat, f_pvalue,
         rmse, residual_vol, durbin_watson, condition_number,
         p_alpha, p_mkt, p_smb, p_hml, p_mom, p_rmw, p_cma,
         se_alpha, se_mkt, se_smb, se_hml, se_mom, se_rmw, se_cma, quality_score)
    VALUES %s
    ON CONFLICT (jurisdiction, ticker, window_end, model) DO UPDATE SET
        n_obs=EXCLUDED.n_obs, r2=EXCLUDED.r2, adj_r2=EXCLUDED.adj_r2,
        f_stat=EXCLUDED.f_stat, f_pvalue=EXCLUDED.f_pvalue, rmse=EXCLUDED.rmse,
        residual_vol=EXCLUDED.residual_vol, durbin_watson=EXCLUDED.durbin_watson,
        condition_number=EXCLUDED.condition_number, p_alpha=EXCLUDED.p_alpha,
        p_mkt=EXCLUDED.p_mkt, p_smb=EXCLUDED.p_smb, p_hml=EXCLUDED.p_hml,
        p_mom=EXCLUDED.p_mom, p_rmw=EXCLUDED.p_rmw, p_cma=EXCLUDED.p_cma,
        se_alpha=EXCLUDED.se_alpha, se_mkt=EXCLUDED.se_mkt, se_smb=EXCLUDED.se_smb,
        se_hml=EXCLUDED.se_hml, se_mom=EXCLUDED.se_mom, se_rmw=EXCLUDED.se_rmw,
        se_cma=EXCLUDED.se_cma, quality_score=EXCLUDED.quality_score, updated_at=now()
"""

INSERT_IMPLIED = """
    INSERT INTO fact_factor_implied_returns
        (jurisdiction, ticker, date, model, implied_return, window_end)
    VALUES %s
    ON CONFLICT (jurisdiction, ticker, date, model) DO UPDATE SET
        implied_return=EXCLUDED.implied_return, window_end=EXCLUDED.window_end, updated_at=now()
"""


def compute_factor_model(
    full: bool = False,
    max_tickers: int | None = None,
    tickers: list[str] | None = None,
    jurisdiction: str | None = None,
    models: list[str] | tuple[str, ...] | None = None,
    implied_retention_years: int | None = 3,
) -> dict[str, int]:
    with connect() as conn:
        selected_models = _normalize_models(models)
        mapping = _dataset_mapping(conn)
        companies = _companies(
            conn,
            tickers=tickers,
            max_tickers=max_tickers,
            jurisdiction=jurisdiction,
        )
        for company in companies:
            region = _country_to_region(company.get("country_code"), company["jurisdiction"])
            company["ff_region"] = region
            company["ff_datasets"] = mapping.get(region, mapping["INTL"])
        needed = {
            dataset
            for company in companies
            for key, dataset in company["ff_datasets"].items()
            if dataset and any(key in _required_dataset_keys(model) for model in selected_models)
        }
        ff_cache = _load_ff_datasets(conn, needed)
        price_map = _load_price_returns(conn, companies)
        if full:
            clear_tickers = tickers or ([company["ticker"] for company in companies] if max_tickers else None)
            _clear_existing(conn, selected_models, jurisdiction=jurisdiction, tickers=clear_tickers)
            conn.commit()
        existing: dict[tuple[str, str], set[tuple[pd.Timestamp, str]]] = defaultdict(set)
        if not full:
            with conn.cursor() as cur:
                cur.execute("SELECT jurisdiction, ticker, window_end, model FROM fact_factor_loadings")
                for jurisdiction, ticker, window_end, model in cur.fetchall():
                    existing[(jurisdiction, ticker)].add((pd.Timestamp(window_end), model))
        load_total = 0
        meta_total = 0
        implied_total = 0
        skipped = 0
        skipped_models = 0
        with conn.cursor() as cur:
            for company in companies:
                jurisdiction = company["jurisdiction"]
                ticker = company["ticker"]
                returns = price_map.get((jurisdiction, ticker))
                datasets = company["ff_datasets"]
                if not returns or not datasets.get("mkt") or datasets["mkt"] not in ff_cache:
                    skipped += 1
                    continue
                ret_series = pd.Series(returns, dtype=float).sort_index()
                for model in selected_models:
                    factor_df = _build_factor_frame(ff_cache, datasets, model)
                    if factor_df is None:
                        skipped_models += 1
                        continue
                    loadings, meta = _rolling_betas(
                        jurisdiction,
                        ticker,
                        ret_series,
                        factor_df,
                        company["ff_region"],
                        model,
                        full,
                        existing.get((jurisdiction, ticker), set()),
                    )
                    implied = _factor_implied_returns(jurisdiction, ticker, model, factor_df, loadings)
                    load_total += execute_values(cur, INSERT_LOADINGS, loadings, page_size=1000)
                    meta_total += execute_values(cur, INSERT_META, meta, page_size=1000)
                    implied_total += execute_values(cur, INSERT_IMPLIED, implied, page_size=1000)
                conn.commit()
        if implied_retention_years is not None:
            _apply_implied_retention(
                conn,
                selected_models,
                jurisdiction=jurisdiction,
                tickers=tickers,
                years=implied_retention_years,
            )
            conn.commit()
        return {
            "companies": len(companies),
            "loadings": load_total,
            "meta": meta_total,
            "implied_returns": implied_total,
            "skipped": skipped,
            "skipped_models": skipped_models,
        }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _factor_model_worker(args: tuple[bool, list[str], str | None, tuple[str, ...], int | None]) -> dict[str, int]:
    full, tickers, jurisdiction, models, implied_retention_years = args
    return compute_factor_model(
        full=full,
        tickers=tickers,
        jurisdiction=jurisdiction,
        models=models,
        implied_retention_years=implied_retention_years,
    )


def compute_factor_model_parallel(
    full: bool = False,
    max_tickers: int | None = None,
    tickers: list[str] | None = None,
    jurisdiction: str | None = None,
    models: list[str] | tuple[str, ...] | None = None,
    workers: int = 1,
    chunk_size: int = 25,
    implied_retention_years: int | None = 3,
) -> dict[str, int]:
    selected_models = _normalize_models(models)
    if workers <= 1:
        return compute_factor_model(
            full=full,
            max_tickers=max_tickers,
            tickers=tickers,
            jurisdiction=jurisdiction,
            models=selected_models,
            implied_retention_years=implied_retention_years,
        )

    with connect() as conn:
        companies = _companies(conn, tickers=tickers, max_tickers=max_tickers, jurisdiction=jurisdiction)
        selected_tickers = [company["ticker"] for company in companies]
        if full:
            _clear_existing(conn, selected_models, jurisdiction=jurisdiction, tickers=selected_tickers)
            conn.commit()

    batches = [batch for batch in _chunks(selected_tickers, max(1, chunk_size)) if batch]
    totals = {
        "companies": 0,
        "loadings": 0,
        "meta": 0,
        "implied_returns": 0,
        "skipped": 0,
        "skipped_models": 0,
        "batches": len(batches),
    }
    worker_args = [(False, batch, jurisdiction, selected_models, implied_retention_years) for batch in batches]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_factor_model_worker, arg) for arg in worker_args]
        for future in as_completed(futures):
            result = future.result()
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + int(value)
    return totals


def _clear_existing(
    conn,
    models: tuple[str, ...],
    jurisdiction: str | None = None,
    tickers: list[str] | None = None,
) -> None:
    clauses = ["model = ANY(%s)"]
    params: list[Any] = [list(models)]
    if jurisdiction:
        clauses.append("jurisdiction = %s")
        params.append(jurisdiction.upper())
    if tickers:
        clauses.append("UPPER(ticker) = ANY(%s)")
        params.append([ticker.upper() for ticker in tickers])
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        for table in ("fact_factor_implied_returns", "fact_factor_loadings", "fact_factor_reg_meta"):
            cur.execute(f"DELETE FROM {table} WHERE {where}", params)


def _apply_implied_retention(
    conn,
    models: tuple[str, ...],
    jurisdiction: str | None = None,
    tickers: list[str] | None = None,
    years: int = 3,
) -> None:
    clauses = ["model = ANY(%s)", "date < CURRENT_DATE - (%s * INTERVAL '1 year')"]
    params: list[Any] = [list(models), years]
    if jurisdiction:
        clauses.append("jurisdiction = %s")
        params.append(jurisdiction.upper())
    if tickers:
        clauses.append("UPPER(ticker) = ANY(%s)")
        params.append([ticker.upper() for ticker in tickers])
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM fact_factor_implied_returns WHERE {where}", params)
