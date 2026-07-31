"""qlib factor-structured risk model (forward covariance) for the optimizer.

Wraps ``qlib.model.riskmodel.StructuredCovEstimator`` to estimate a statistical
factor covariance ``Σ = F · cov_b · Fᵀ + diag(var_u)`` from a daily return panel, and
exposes the decomposition ``(F, cov_b, var_u)`` that
``qlib.contrib.strategy.optimizer.EnhancedIndexingOptimizer`` consumes directly.

This complements the existing :mod:`api.quant.risk` estimators (sample / Ledoit-Wolf /
Fama-French structured) as an additional, selectable covariance model — see the
``qlib_structured`` branch in ``risk.build_risk_bundle`` and the optimizer dispatcher
in :mod:`api.quant.qlib_optimize`.

All qlib imports are lazy so importing this module is side-effect free. Everything is
pure numpy/scikit-learn — no ``qlib.init`` or data provider needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

import numpy as np

TRADING_DAYS = 252.0


@dataclass
class StructuredRisk:
    """Annualized factor-structured covariance and its decomposition."""

    tickers: list[str]
    sigma: np.ndarray            # (N, N) annualized covariance
    factor_exposure: np.ndarray  # F     (N, K)
    factor_cov: np.ndarray       # cov_b (K, K) annualized
    specific_var: np.ndarray     # var_u (N,)  annualized
    factor_names: list[str]
    n_obs: int
    warnings: list[str] = field(default_factory=list)

    @property
    def vol(self) -> np.ndarray:
        """Annualized forward volatility per name = √diag(Σ)."""
        return np.sqrt(np.clip(np.diag(self.sigma), 0.0, None))

    @property
    def decomposition(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return self.factor_exposure, self.factor_cov, self.specific_var


def structured_cov(
    tickers: Sequence[str],
    R: np.ndarray,
    *,
    num_factors: int = 10,
    factor_model: str = "pca",
    annualization: float = TRADING_DAYS,
) -> StructuredRisk:
    """Estimate an annualized factor-structured covariance from ``R`` (T×N returns).

    ``num_factors`` is clamped to a safe range given N and the number of observations.
    The full Σ is rebuilt from the annualized decomposition so both stay consistent
    (the enhanced-indexing optimizer uses the decomposition; mvo/gmv use Σ).
    """
    from qlib.model.riskmodel import StructuredCovEstimator  # lazy

    tickers = list(tickers)
    R = np.asarray(R, dtype=float)
    N = R.shape[1]
    T = R.shape[0]
    warnings: list[str] = []

    k = int(min(num_factors, max(1, N - 1), max(1, T - 2)))
    if k < num_factors:
        warnings.append(f"num_factors reduced {num_factors}->{k} (N={N}, T={T}).")

    # scale_return=False -> covariance stays in raw return units (the qlib default
    # rescales returns to percentage, inflating variance by 100^2).
    est = StructuredCovEstimator(factor_model=factor_model, num_factors=k, scale_return=False)
    F, cov_b, var_u = est.predict(R, is_price=False, return_decomposed_components=True)
    F = np.asarray(F, dtype=float)
    cov_b = np.asarray(cov_b, dtype=float) * annualization
    var_u = np.asarray(var_u, dtype=float).reshape(-1) * annualization

    sigma = F @ cov_b @ F.T + np.diag(var_u)
    # Symmetrize + tiny ridge for numerical PSD safety.
    sigma = 0.5 * (sigma + sigma.T)
    sigma += np.eye(N) * 1e-10

    return StructuredRisk(
        tickers=tickers,
        sigma=sigma,
        factor_exposure=F,
        factor_cov=cov_b,
        specific_var=var_u,
        factor_names=[f"qf{i}" for i in range(k)],
        n_obs=T,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# Sync price/returns loader (for the CLI, backtests, and router worker threads)
# --------------------------------------------------------------------------- #
def load_price_returns(
    tickers: Sequence[str],
    jurisdiction: str,
    start: date | str,
    end: date | str,
) -> tuple[list[str], np.ndarray, list]:
    """Load daily simple returns aligned on common dates.

    Returns ``(tickers_present, R, dates)`` where ``R`` is (T, N). Uses the same sync
    warehouse connection as the cycle/IC code. Tickers with no overlapping history are
    dropped from ``tickers_present``.
    """
    import pandas as pd
    from xbrl_sec.sec.cycle.registry import get_config
    from xbrl_sec.sec.db.connection import connect

    cfg = get_config(jurisdiction)
    ts = [t[:-2] if str(t).upper().endswith(".T") else t for t in tickers]
    with connect() as conn:
        df = pd.read_sql(
            f"""
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM   {cfg.price_table}
            WHERE  ticker = ANY(%s)
              AND  date BETWEEN %s AND %s
              AND  COALESCE(adj_close, close) IS NOT NULL
            ORDER  BY date
            """,
            conn,
            params=(list(ts), start, end),
        )
    if df.empty:
        return [], np.zeros((0, 0)), []
    wide = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").dropna()
    if wide.shape[0] < 3 or wide.shape[1] < 1:
        return [], np.zeros((0, 0)), []
    P = wide.values
    R = P[1:] / P[:-1] - 1.0
    present = [str(c) for c in wide.columns]
    return present, R, list(wide.index[1:])
