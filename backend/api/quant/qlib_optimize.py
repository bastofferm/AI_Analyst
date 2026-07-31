"""Portfolio-optimizer dispatcher: native (api.quant.optimize) + qlib backends.

Both optimizer families are available at runtime and selected by the ``optimizer``
argument — nothing is a default-only path:

    "native"                  -> api.quant.optimize.optimize  (SLSQP / cvxpy-MIP;
                                 full constraint set: sector caps, vol cap, cardinality)
    "qlib_mvo"|"qlib_gmv"|
    "qlib_rp"|"qlib_inv"      -> qlib PortfolioOptimizer (full-investment, long-only)
    "qlib_enhanced_indexing"  -> qlib EnhancedIndexingOptimizer (benchmark-relative,
                                 tracking-error; consumes the StructuredCovEstimator
                                 decomposition from api.quant.qlib_risk)

Every backend is fed the same expected returns ``mu`` (annualized) and covariance
``sigma`` (annualized) and returns a uniform :class:`PortfolioSolution`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

NATIVE = "native"
ENHANCED = "qlib_enhanced_indexing"
QLIB_SIMPLE: dict[str, str] = {
    "qlib_mvo": "mvo",
    "qlib_gmv": "gmv",
    "qlib_rp": "rp",
    "qlib_inv": "inv",
}
BACKENDS: tuple[str, ...] = (NATIVE, "qlib_mvo", "qlib_gmv", "qlib_rp", "qlib_inv", ENHANCED)


def available_backends() -> list[str]:
    return list(BACKENDS)


@dataclass
class PortfolioSolution:
    backend: str
    tickers: list[str]
    weights: np.ndarray
    expected_return_annual: float
    vol_annual: float
    sharpe: float | None
    warnings: list[str] = field(default_factory=list)
    factor_exposures: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "weights": [
                {"ticker": t, "weight": float(w)}
                for t, w in zip(self.tickers, self.weights)
            ],
            "expected_return_annual": self.expected_return_annual,
            "vol_annual": self.vol_annual,
            "sharpe": self.sharpe,
            "factor_exposures": self.factor_exposures,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
        }


def _summary(w: np.ndarray, mu: np.ndarray, sigma: np.ndarray, rf: float) -> tuple[float, float, float | None]:
    er = float(w @ mu)
    var = float(w @ sigma @ w)
    vol = float(np.sqrt(max(var, 0.0)))
    sharpe = float((er - rf) / vol) if vol > 1e-12 else None
    return er, vol, sharpe


def solve(
    optimizer: str,
    tickers: Sequence[str],
    mu: np.ndarray,
    sigma: np.ndarray,
    *,
    risk_free_annual: float = 0.045,
    lamb: float | None = None,
    delta: float = 0.0,
    alpha: float = 0.0,
    w0: np.ndarray | None = None,
    # enhanced indexing only:
    decomposition: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
    wb: np.ndarray | None = None,
    b_dev: float = 0.05,
    # native only (optional full constraint model):
    native_inputs: Any | None = None,
) -> PortfolioSolution:
    """Solve the portfolio for ``optimizer`` and return a uniform solution.

    ``mu``/``sigma`` must be annualized and aligned to ``tickers``. Raises
    ``ValueError`` for an unknown backend or missing enhanced-indexing inputs.
    """
    tickers = [str(t) for t in tickers]
    mu = np.asarray(mu, dtype=float).reshape(-1)
    sigma = np.asarray(sigma, dtype=float)
    N = len(tickers)

    if optimizer == NATIVE:
        return _solve_native(tickers, mu, sigma, risk_free_annual, native_inputs)
    if optimizer in QLIB_SIMPLE:
        return _solve_qlib_simple(optimizer, tickers, mu, sigma, risk_free_annual, lamb, delta, alpha, w0)
    if optimizer == ENHANCED:
        return _solve_enhanced(tickers, mu, sigma, risk_free_annual, decomposition, wb, w0, lamb, delta, b_dev)
    raise ValueError(f"unknown optimizer backend {optimizer!r}; choose one of {BACKENDS}")


def _solve_native(tickers, mu, sigma, rf, native_inputs) -> PortfolioSolution:
    from .optimize import Constraints, Objective, OptimizeRequestInputs, optimize

    if native_inputs is None:
        native_inputs = OptimizeRequestInputs(
            tickers=list(tickers),
            mu=mu,
            sigma=sigma,
            B=None,
            factor_names=[],
            sector_codes=[None] * len(tickers),
            objective=Objective(),
            factor_targets=[],
            constraints=Constraints(),
            risk_free_annual=rf,
        )
    res = optimize(native_inputs)
    diag = dict(res.diagnostics or {})
    diag["efficient_frontier"] = res.efficient_frontier
    diag["marginal_risk_contribution"] = np.asarray(res.marginal_risk_contribution).tolist()
    return PortfolioSolution(
        backend=NATIVE,
        tickers=list(native_inputs.tickers),
        weights=np.asarray(res.weights, dtype=float),
        expected_return_annual=float(res.expected_return_annual),
        vol_annual=float(res.vol_annual),
        sharpe=res.sharpe,
        warnings=list(res.warnings or []),
        factor_exposures=dict(res.factor_exposures or {}),
        diagnostics=diag,
    )


def _solve_qlib_simple(optimizer, tickers, mu, sigma, rf, lamb, delta, alpha, w0) -> PortfolioSolution:
    from qlib.contrib.strategy.optimizer import PortfolioOptimizer

    method = QLIB_SIMPLE[optimizer]
    if lamb is None:
        lamb = 1.0 if method == "mvo" else 0.0
    opt = PortfolioOptimizer(method=method, lamb=lamb, delta=delta, alpha=alpha)
    r = mu if method == "mvo" else None
    w = np.asarray(opt(sigma, r=r, w0=w0), dtype=float).reshape(-1)
    er, vol, sharpe = _summary(w, mu, sigma, rf)
    return PortfolioSolution(
        backend=optimizer,
        tickers=list(tickers),
        weights=w,
        expected_return_annual=er,
        vol_annual=vol,
        sharpe=sharpe,
        diagnostics={"method": method, "lamb": lamb, "delta": delta, "alpha": alpha},
    )


def _solve_enhanced(tickers, mu, sigma, rf, decomposition, wb, w0, lamb, delta, b_dev) -> PortfolioSolution:
    from qlib.contrib.strategy.optimizer import EnhancedIndexingOptimizer

    if decomposition is None:
        raise ValueError(
            "qlib_enhanced_indexing requires a (F, cov_b, var_u) decomposition "
            "(use api.quant.qlib_risk.structured_cov)."
        )
    F, cov_b, var_u = (np.asarray(x, dtype=float) for x in decomposition)
    var_u = var_u.reshape(-1)
    N = len(tickers)
    if wb is None:
        wb = np.ones(N) / N
    if w0 is None:
        w0 = wb.copy()
    eio = EnhancedIndexingOptimizer(
        lamb=1.0 if lamb is None else lamb,
        delta=0.2 if not delta else delta,
        b_dev=b_dev,
    )
    warnings: list[str] = []
    try:
        w = np.asarray(eio(mu, F, cov_b, var_u, np.asarray(w0), np.asarray(wb)), dtype=float).reshape(-1)
    except Exception as exc:  # noqa: BLE001 - degrade to benchmark on solver failure
        warnings.append(f"enhanced-indexing solve failed ({exc.__class__.__name__}); returned benchmark weights.")
        w = np.asarray(wb, dtype=float).reshape(-1)
    er, vol, sharpe = _summary(w, mu, sigma, rf)
    exposures = {f"qf{i}": float((w - wb) @ F[:, i]) for i in range(F.shape[1])}
    return PortfolioSolution(
        backend=ENHANCED,
        tickers=list(tickers),
        weights=w,
        expected_return_annual=er,
        vol_annual=vol,
        sharpe=sharpe,
        warnings=warnings,
        factor_exposures=exposures,
        diagnostics={"lamb": lamb, "delta": delta, "b_dev": b_dev, "active_share": float(0.5 * np.abs(w - wb).sum())},
    )
