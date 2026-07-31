"""Covariance matrix and factor-exposure assembly for the portfolio optimizer.

Three risk models, chosen by ``RiskModel.type``:

- ``sample``        — plain sample covariance of daily returns × 252.
- ``ledoit_wolf``   — Ledoit-Wolf shrinkage toward a constant-correlation target.
- ``ff5_plus_mom``  / ``ff3`` / ``ff5`` — structured covariance
                       Σ = B F Bᵀ + D
                      where B is loaded from ``fact_factor_loadings`` (per-ticker
                      latest window) and F is the factor return covariance from
                      ``fact_fama_french`` over the same lookback.  D is a
                      diagonal of squared idiosyncratic volatilities from
                      ``fact_factor_reg_meta.residual_vol`` (annualized).

When the factor loadings are missing for some tickers we fall back to the
sample diagonal for those rows and surface a warning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Literal

import numpy as np

# Map our public risk-model id to the `model` column value in
# fact_factor_loadings / fact_factor_reg_meta.
# Maps our public risk-model id to the `model` column value stored in
# fact_factor_loadings / fact_factor_reg_meta. The DB convention is FF3/FF5/FF6
# where FF6 = FF5 + Momentum (Carhart-flavored).
_FF_MODEL_BY_TYPE: dict[str, str] = {
    "ff3":          "FF3",
    "ff5":          "FF5",
    "ff5_plus_mom": "FF6",
}

# Map our public factor names to the FF factor labels in fact_fama_french.
_FF_FACTORS_BY_MODEL: dict[str, list[str]] = {
    "ff3":          ["Mkt-RF", "SMB", "HML"],
    "ff5":          ["Mkt-RF", "SMB", "HML", "RMW", "CMA"],
    "ff5_plus_mom": ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"],
}

# Map our public factor names to fact_factor_loadings column names.
_BETA_COL: dict[str, str] = {
    "Mkt-RF": "beta_mkt",
    "SMB":    "beta_smb",
    "HML":    "beta_hml",
    "RMW":    "beta_rmw",
    "CMA":    "beta_cma",
    "Mom":    "beta_mom",
}


def _ff_dataset(jurisdiction: str, model: str, factor: str) -> str:
    """Pick the FF dataset name to use for a given (jurisdiction, model, factor).

    fact_fama_french has duplicates per date (one per dataset). We pick one
    canonical series so the factor return panel has exactly one value per date.
    """
    # Momentum has its own dataset (only US Ken French series).
    if factor == "Mom":
        return "F-F_Momentum_Factor_daily"
    if jurisdiction == "JP":
        return "Japan_5_Factors_Daily" if model in ("FF5", "FF6") else "Japan_3_Factors_Daily"
    # US default — 5-factor research file covers FF3 factors too.
    return "F-F_Research_Data_5_Factors_2x3_daily" if model in ("FF5", "FF6") else "F-F_Research_Data_Factors_daily"


@dataclass
class RiskBundle:
    """Everything the optimizer needs for one fit."""
    tickers: list[str]
    sigma: np.ndarray                       # (N, N), annualized
    mu: np.ndarray                          # (N,), historical mean × 252
    B: np.ndarray | None                    # (N, K) factor loadings, None for non-factor models
    factor_names: list[str]                 # length K (empty for non-factor models)
    factor_cov_annual: np.ndarray | None    # (K, K), annualized
    warnings: list[str]


async def fetch_price_series(
    conn,
    tickers_us: list[str],
    tickers_jp: list[str],
    start: date,
    end: date,
) -> dict[str, dict[date, float]]:
    """Mirror of `routers.portfolio._fetch_series`. Kept separate so quant
    code doesn't depend on the routers package."""
    series: dict[str, dict[date, float]] = {}
    if tickers_us:
        rows = await conn.fetch(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM   fact_prices_us
            WHERE  ticker = ANY($1::text[])
              AND  date BETWEEN $2 AND $3
              AND  COALESCE(adj_close, close) IS NOT NULL
            """,
            tickers_us, start, end,
        )
        for r in rows:
            series.setdefault(r["ticker"], {})[r["date"]] = float(r["close"])
    if tickers_jp:
        bare = [t[:-2] if t.upper().endswith(".T") else t for t in tickers_jp]
        rows = await conn.fetch(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM   fact_prices_jp
            WHERE  ticker = ANY($1::text[])
              AND  date BETWEEN $2 AND $3
              AND  COALESCE(adj_close, close) IS NOT NULL
            """,
            bare, start, end,
        )
        for r in rows:
            series.setdefault(r["ticker"] + ".T", {})[r["date"]] = float(r["close"])
    return series


def align_series(series: dict[str, dict[date, float]]) -> tuple[list[date], dict[str, np.ndarray]]:
    if not series:
        return [], {}
    common: set[date] | None = None
    for s in series.values():
        common = set(s.keys()) if common is None else (common & set(s.keys()))
    if not common:
        return [], {}
    dates = sorted(common)
    return dates, {t: np.array([series[t][d] for d in dates], dtype=float) for t in series}


def daily_returns_from_levels(P: np.ndarray) -> np.ndarray:
    """Simple returns from a (T, N) price matrix."""
    return P[1:] / P[:-1] - 1.0


def _shrink_ledoit_wolf(R: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf shrinkage to a constant-correlation target.

    Minimal implementation; sklearn would do this in one call but we don't want
    to add a heavy dep just for one estimator. Returns daily-frequency Σ; caller
    annualizes.
    """
    n, p = R.shape
    if n < 3:
        return np.cov(R.T)
    mean = R.mean(axis=0, keepdims=True)
    Xc = R - mean
    S = (Xc.T @ Xc) / n  # MLE sample cov
    var = np.diag(S)
    sd = np.sqrt(np.clip(var, 1e-12, None))
    corr = S / np.outer(sd, sd)
    np.fill_diagonal(corr, 1.0)
    # Mean off-diagonal correlation.
    mask = ~np.eye(p, dtype=bool)
    r_bar = float(np.mean(corr[mask])) if mask.any() else 0.0
    F = r_bar * np.outer(sd, sd)
    np.fill_diagonal(F, var)
    # Pi: asymptotic variance of S.
    Xc2 = Xc**2
    Pi_mat = (Xc2.T @ Xc2) / n - S**2
    pi = float(np.sum(Pi_mat))
    # Gamma: ||S - F||_F^2
    gamma = float(np.sum((S - F) ** 2))
    if gamma <= 0:
        kappa = 0.0
    else:
        kappa = pi / gamma
    delta = max(0.0, min(1.0, kappa / n))
    return delta * F + (1 - delta) * S


def covariance_matrix(R: np.ndarray, model: Literal["sample", "ledoit_wolf"]) -> np.ndarray:
    """(T, N) returns → (N, N) covariance, annualized via × 252."""
    if R.shape[0] < 3:
        return np.eye(R.shape[1]) * 1e-4
    if model == "ledoit_wolf":
        S = _shrink_ledoit_wolf(R)
    else:
        S = np.cov(R.T, ddof=1)
    return S * 252.0


async def fetch_factor_loadings(
    conn,
    tickers: Iterable[str],
    jurisdiction: str,
    model: str,
    as_of: date,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """For each ticker, load the latest fact_factor_loadings row at or before
    `as_of` for the requested model.

    Returns (loadings, residual_vol) where:
      loadings[ticker] = {"beta_mkt": x, "beta_smb": y, ...}
      residual_vol[ticker] = daily-frequency residual vol from fact_factor_reg_meta
    """
    ff_model = _FF_MODEL_BY_TYPE.get(model)
    if ff_model is None:
        return {}, {}
    ts = list(tickers)
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (ticker)
               ticker, beta_mkt, beta_smb, beta_hml, beta_rmw, beta_cma, beta_mom
        FROM   fact_factor_loadings
        WHERE  jurisdiction = $1
          AND  model = $2
          AND  ticker = ANY($3::text[])
          AND  window_end <= $4
        ORDER  BY ticker, window_end DESC
        """,
        jurisdiction, ff_model, ts, as_of,
    )
    loadings: dict[str, dict[str, float]] = {}
    for r in rows:
        loadings[r["ticker"]] = {
            "beta_mkt": _f(r["beta_mkt"]),
            "beta_smb": _f(r["beta_smb"]),
            "beta_hml": _f(r["beta_hml"]),
            "beta_rmw": _f(r["beta_rmw"]),
            "beta_cma": _f(r["beta_cma"]),
            "beta_mom": _f(r["beta_mom"]),
        }
    meta_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (ticker) ticker, residual_vol
        FROM   fact_factor_reg_meta
        WHERE  jurisdiction = $1
          AND  model = $2
          AND  ticker = ANY($3::text[])
          AND  window_end <= $4
        ORDER  BY ticker, window_end DESC
        """,
        jurisdiction, ff_model, ts, as_of,
    )
    resid: dict[str, float] = {r["ticker"]: _f(r["residual_vol"]) or 0.0 for r in meta_rows}
    return loadings, resid


async def fetch_factor_return_panel(
    conn,
    factor_names: list[str],
    start: date,
    end: date,
    jurisdiction: str = "US",
    model: str = "FF5",
) -> tuple[list[date], np.ndarray, list[str]]:
    """Pull the FF factor return series. Returns (dates, panel, factor_order).
    panel shape: (T, K) decimal (not percent). Only dates with full coverage
    across the requested factors are returned.

    Each (factor, dataset) pair contributes one daily series; we select one
    canonical dataset per factor so the same date isn't double-counted.
    """
    if not factor_names:
        return [], np.zeros((0, 0)), []
    datasets = [_ff_dataset(jurisdiction, model, f) for f in factor_names]
    # SQL with explicit (factor, dataset) pairs via UNNEST.
    rows = await conn.fetch(
        """
        SELECT ff.date, ff.factor, ff.value
        FROM   fact_fama_french ff
        JOIN   UNNEST($1::text[], $2::text[]) AS pair(factor, dataset)
               ON ff.factor = pair.factor AND ff.dataset = pair.dataset
        WHERE  ff.date BETWEEN $3 AND $4
        """,
        factor_names, datasets, start, end,
    )
    by_date: dict[date, dict[str, float]] = {}
    for r in rows:
        by_date.setdefault(r["date"], {})[r["factor"]] = float(r["value"])
    aligned_dates = [d for d, m in by_date.items() if all(f in m for f in factor_names)]
    aligned_dates.sort()
    if not aligned_dates:
        return [], np.zeros((0, len(factor_names))), factor_names
    panel = np.array([[by_date[d][f] for f in factor_names] for d in aligned_dates], dtype=float)
    return aligned_dates, panel, factor_names


def build_risk_bundle(
    tickers: list[str],
    R: np.ndarray,
    mu_daily: np.ndarray,
    model: str,
    loadings: dict[str, dict[str, float]] | None,
    residual_vol_daily: dict[str, float] | None,
    factor_panel: np.ndarray | None,
    factor_names: list[str] | None,
) -> RiskBundle:
    warnings: list[str] = []
    N = len(tickers)
    mu_annual = mu_daily * 252.0

    if model in ("sample", "ledoit_wolf"):
        sigma = covariance_matrix(R, model)  # already annualized
        return RiskBundle(
            tickers=list(tickers),
            sigma=sigma,
            mu=mu_annual,
            B=None,
            factor_names=[],
            factor_cov_annual=None,
            warnings=warnings,
        )

    if model == "qlib_structured":
        # qlib StructuredCovEstimator: statistical (PCA) factor covariance.
        # The (F, cov_b, var_u) decomposition is exposed here as B / factor_cov;
        # the enhanced-indexing optimizer path recomputes it via qlib_risk directly.
        from .qlib_risk import structured_cov

        sr = structured_cov(tickers, R)
        return RiskBundle(
            tickers=list(tickers),
            sigma=sr.sigma,
            mu=mu_annual,
            B=sr.factor_exposure,
            factor_names=sr.factor_names,
            factor_cov_annual=sr.factor_cov,
            warnings=warnings + list(sr.warnings),
        )

    factor_keys = _FF_FACTORS_BY_MODEL.get(model)
    if not factor_keys or loadings is None or factor_panel is None or factor_names is None:
        warnings.append(f"Factor model '{model}' unavailable; falling back to Ledoit-Wolf.")
        return build_risk_bundle(tickers, R, mu_daily, "ledoit_wolf", None, None, None, None)

    # Build B: (N, K) using factor_keys order.
    K = len(factor_keys)
    B = np.zeros((N, K))
    missing: list[str] = []
    for i, t in enumerate(tickers):
        row = loadings.get(t, {})
        for k, fn in enumerate(factor_keys):
            col = _BETA_COL.get(fn)
            v = row.get(col, None) if col else None
            if v is None or not np.isfinite(v):
                B[i, k] = 0.0
                if fn not in ("Mom",):  # MOM is optional for FF3
                    missing.append(t)
            else:
                B[i, k] = v
    if missing:
        warnings.append(
            f"Missing factor loadings for {len({*missing})} ticker(s); rows zero-filled."
        )

    # Factor covariance (annualized).
    if factor_panel.shape[0] < 3:
        F = np.eye(K) * 1e-4
    else:
        F = np.cov(factor_panel.T, ddof=1) * 252.0

    # Idiosyncratic D — fact_factor_reg_meta.residual_vol is already annualized
    # (sanity check: AAPL ~ 0.20 ≈ 20% per year, consistent with stocks). Just
    # square it; do NOT multiply by 252 again.
    D_diag = np.zeros(N)
    for i, t in enumerate(tickers):
        rv = (residual_vol_daily or {}).get(t, 0.0)
        D_diag[i] = rv ** 2
    fallback = covariance_matrix(R, "sample")
    for i in range(N):
        if D_diag[i] <= 0:
            D_diag[i] = float(np.diag(fallback)[i])

    Sigma = B @ F @ B.T + np.diag(D_diag)

    return RiskBundle(
        tickers=list(tickers),
        sigma=Sigma,
        mu=mu_annual,
        B=B,
        factor_names=factor_keys,
        factor_cov_annual=F,
        warnings=warnings,
    )


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def lookback_window(months: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=months * 31 + 14)
    return start, end
