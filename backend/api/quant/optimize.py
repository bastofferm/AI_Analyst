"""Convex(-ish) portfolio optimizer.

Solves

  max_w   wᵀ μ
         − ½ λ_risk wᵀ Σ w
         − γ_factor ‖S · (Bᵀw − t)‖²
         − γ_to ‖w − w_curr‖_1
         − γ_conc ‖w‖²

subject to long_only, weight bounds, gross-exposure cap, sector caps,
factor hard caps / ranges, and an annualized vol cap.

We use scipy SLSQP — it handles the nonlinear vol/factor constraints natively
and converges fast at portfolio sizes (≤ 100 names). CVXPY would give a
cleaner formulation but adds ~50 MB of solver dependencies; the architecture
here is identical so a switch is one function away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from scipy.optimize import minimize


FactorMode = Literal["free", "target", "cap", "range"]


@dataclass
class FactorTarget:
    factor: str
    mode: FactorMode = "free"
    target: float | None = None
    cap: float | None = None
    lo: float | None = None
    hi: float | None = None
    scale: float = 1.0


@dataclass
class Constraints:
    long_only: bool = True
    weight_min: float | None = 0.0
    weight_max: float | None = 1.0
    gross_max: float = 1.0           # Σ |wᵢ|
    sector_max: dict[str, float] | None = None  # GICS code → max weight
    vol_max_annual: float | None = None
    current_weights: list[float] | None = None
    # New constraints:
    # - weight_min_per_name: per-name floor (overrides weight_min when set).
    # - max_names: integer cap on number of nonzero positions (requires MIP).
    # - short_max_gross: ratio in [0, 1] capping Σ max(-wᵢ, 0) ≤ ratio · Σ |wᵢ|.
    weight_min_per_name: float | None = None
    max_names: int | None = None
    short_max_gross: float | None = None


@dataclass
class Objective:
    lambda_risk: float = 5.0
    gamma_factor: float = 1.0
    gamma_turnover: float = 0.0
    gamma_concentration: float = 0.0


@dataclass
class OptimizeRequestInputs:
    tickers: list[str]
    mu: np.ndarray                              # (N,)
    sigma: np.ndarray                           # (N, N)
    B: Optional[np.ndarray]                     # (N, K) or None
    factor_names: list[str]                     # length K
    sector_codes: list[str | None]              # per-ticker; None if unknown
    objective: Objective
    factor_targets: list[FactorTarget]
    constraints: Constraints
    risk_free_annual: float = 0.045


@dataclass
class OptimizeResult:
    weights: np.ndarray
    expected_return_annual: float
    vol_annual: float
    sharpe: float | None
    factor_exposures: dict[str, float]
    marginal_risk_contribution: np.ndarray
    diagnostics: dict
    efficient_frontier: list[dict]
    warnings: list[str]


def _objective_value(
    w: np.ndarray,
    mu: np.ndarray,
    sigma: np.ndarray,
    obj: Objective,
    B: np.ndarray | None,
    factor_target_vec: np.ndarray | None,
    factor_scale_vec: np.ndarray | None,
    factor_active: np.ndarray | None,
    w_curr: np.ndarray | None,
) -> float:
    # Maximization → return negative for minimize.
    val = float(w @ mu)
    val -= 0.5 * obj.lambda_risk * float(w @ sigma @ w)
    if B is not None and factor_target_vec is not None and factor_active is not None:
        diff = B.T @ w - factor_target_vec
        masked = diff * factor_active * (factor_scale_vec if factor_scale_vec is not None else 1.0)
        val -= obj.gamma_factor * float(masked @ masked)
    if obj.gamma_turnover > 0 and w_curr is not None:
        val -= obj.gamma_turnover * float(np.sum(np.abs(w - w_curr)))
    if obj.gamma_concentration > 0:
        val -= obj.gamma_concentration * float(w @ w)
    return -val


def _objective_grad(
    w: np.ndarray,
    mu: np.ndarray,
    sigma: np.ndarray,
    obj: Objective,
    B: np.ndarray | None,
    factor_target_vec: np.ndarray | None,
    factor_scale_vec: np.ndarray | None,
    factor_active: np.ndarray | None,
    w_curr: np.ndarray | None,
) -> np.ndarray:
    g = -mu.copy()
    g += obj.lambda_risk * (sigma @ w)
    if B is not None and factor_target_vec is not None and factor_active is not None:
        # d/dw of γ ‖S * a * (Bᵀw − t)‖² = 2 γ B (S²·a²·(Bᵀw − t))
        diff = B.T @ w - factor_target_vec
        s2 = (factor_scale_vec ** 2) if factor_scale_vec is not None else 1.0
        g += 2.0 * obj.gamma_factor * (B @ (s2 * factor_active * diff))
    if obj.gamma_turnover > 0 and w_curr is not None:
        # subgradient of L1
        g += obj.gamma_turnover * np.sign(w - w_curr)
    if obj.gamma_concentration > 0:
        g += 2.0 * obj.gamma_concentration * w
    return g


def _build_constraint_specs(
    w0_n: int,
    inputs: OptimizeRequestInputs,
):
    """Return (bounds, constraints_list) for scipy.minimize SLSQP."""
    c = inputs.constraints
    # Per-name floor: if `weight_min_per_name` is set we use that for *every*
    # name regardless of long_only. When long_only is off and no per-name
    # floor is set we allow shorts down to -weight_max.
    if c.weight_min_per_name is not None:
        per_name_lo = c.weight_min_per_name
    else:
        per_name_lo = (
            c.weight_min if c.weight_min is not None
            else (0.0 if c.long_only else -(c.weight_max if c.weight_max is not None else 1.0))
        )
    bounds: list[tuple[float | None, float | None]] = []
    for _ in range(w0_n):
        hi = c.weight_max if c.weight_max is not None else 1.0
        bounds.append((per_name_lo, hi))

    cons: list[dict] = []
    # Σwᵢ = 1 (fully invested).
    cons.append({"type": "eq", "fun": lambda w: np.sum(w) - 1.0, "jac": lambda w: np.ones_like(w)})

    # Gross exposure cap: Σ|wᵢ| ≤ gross_max. With long_only this is implied;
    # if shorts allowed, we approximate with smooth quadratic relaxation.
    if not c.long_only and c.gross_max is not None:
        cons.append({"type": "ineq", "fun": lambda w, g=c.gross_max: g - np.sum(np.abs(w))})

    # Short-leg cap as fraction of gross:
    #   Σ max(-wᵢ, 0)  ≤  ratio · Σ |wᵢ|
    # Rewrite as:  ratio · Σ|wᵢ| − Σ max(-wᵢ, 0) ≥ 0.
    if (
        not c.long_only
        and c.short_max_gross is not None
        and 0.0 <= c.short_max_gross <= 1.0
    ):
        ratio = c.short_max_gross
        cons.append({
            "type": "ineq",
            "fun": lambda w, r=ratio: r * float(np.sum(np.abs(w))) - float(np.sum(np.maximum(-w, 0.0))),
        })

    # Sector caps.
    if c.sector_max:
        for sec, cap in c.sector_max.items():
            mask = np.array([1.0 if (inputs.sector_codes[i] == sec) else 0.0 for i in range(w0_n)])
            cons.append({
                "type": "ineq",
                "fun": lambda w, m=mask, k=cap: k - float(np.sum(w * m)),
                "jac": lambda w, m=mask: -m,
            })

    # Annualized vol cap: √(wᵀΣw) ≤ σ_max  ⇔  σ_max² − wᵀΣw ≥ 0.
    if c.vol_max_annual is not None and c.vol_max_annual > 0:
        sigma = inputs.sigma
        smax2 = c.vol_max_annual ** 2
        cons.append({
            "type": "ineq",
            "fun": lambda w, S=sigma, k=smax2: k - float(w @ S @ w),
            "jac": lambda w, S=sigma: -2.0 * (S @ w),
        })

    # Factor caps / ranges.
    if inputs.B is not None and inputs.factor_targets:
        B = inputs.B
        name_to_idx = {n: i for i, n in enumerate(inputs.factor_names)}
        for ft in inputs.factor_targets:
            j = name_to_idx.get(ft.factor)
            if j is None or ft.mode in ("free", "target"):
                continue
            b_col = B[:, j]
            if ft.mode == "cap" and ft.cap is not None:
                cap = abs(ft.cap)
                # |Bᵀw|_j ≤ cap → split into two inequalities.
                cons.append({"type": "ineq", "fun": lambda w, c=cap, bc=b_col: c - float(bc @ w), "jac": lambda w, bc=b_col: -bc})
                cons.append({"type": "ineq", "fun": lambda w, c=cap, bc=b_col: c + float(bc @ w), "jac": lambda w, bc=b_col: bc})
            elif ft.mode == "range":
                if ft.hi is not None:
                    cons.append({"type": "ineq", "fun": lambda w, hi=ft.hi, bc=b_col: hi - float(bc @ w), "jac": lambda w, bc=b_col: -bc})
                if ft.lo is not None:
                    cons.append({"type": "ineq", "fun": lambda w, lo=ft.lo, bc=b_col: float(bc @ w) - lo, "jac": lambda w, bc=b_col: bc})

    return bounds, cons


def _factor_target_vectors(
    factor_names: list[str],
    factor_targets: list[FactorTarget],
):
    if not factor_names:
        return None, None, None
    target = np.zeros(len(factor_names))
    scale = np.ones(len(factor_names))
    active = np.zeros(len(factor_names))  # only "target" factors enter the penalty
    name_to_idx = {n: i for i, n in enumerate(factor_names)}
    for ft in factor_targets:
        j = name_to_idx.get(ft.factor)
        if j is None:
            continue
        scale[j] = ft.scale if ft.scale is not None else 1.0
        if ft.mode == "target" and ft.target is not None:
            target[j] = ft.target
            active[j] = 1.0
    return target, scale, active


class MipSolverUnavailable(RuntimeError):
    """Raised when `max_names` is requested but no MIP backend is reachable."""


def _solve_mip_cardinality(inputs: OptimizeRequestInputs) -> tuple[np.ndarray, dict]:
    """Solve the cardinality-constrained problem with CVXPY + a MIP solver.

    Variables:
      w : continuous weights summing to 1
      y : binary inclusion indicators (length N)
    Inclusion logic:
      0 ≤ w ≤ wmax · y                (long-only)
      |w| ≤ wmax · y                  (long/short)
      w ≥ wmin_per_name · y           (floor on included names)
      Σy ≤ max_names
    Plus the convex constraints already used by the SLSQP path (vol cap,
    sector caps, factor caps, short-leg cap).

    Raises `MipSolverUnavailable` if cvxpy or a MIP backend isn't reachable.
    """
    try:
        import cvxpy as cp  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise MipSolverUnavailable(
            "cvxpy is required for the max_names cardinality constraint."
        ) from exc

    c = inputs.constraints
    N = len(inputs.tickers)
    w_max = c.weight_max if c.weight_max is not None else 1.0
    floor = c.weight_min_per_name if c.weight_min_per_name is not None else 0.0

    w = cp.Variable(N)
    y = cp.Variable(N, boolean=True)

    constraints = [cp.sum(w) == 1, cp.sum(y) <= c.max_names]
    if c.long_only:
        constraints += [w >= 0, w <= w_max * y]
        if floor > 0:
            constraints.append(w >= floor * y)
    else:
        constraints += [w <= w_max * y, w >= -w_max * y]
        if c.short_max_gross is not None:
            # Short leg in absolute terms ≤ short_max_gross · gross.
            # gross_max bounds Σ|w|; combined with gross_max yields short cap.
            short_part = cp.sum(cp.neg(w))  # |min(w, 0)|
            gross = cp.sum(cp.abs(w))
            constraints.append(short_part <= c.short_max_gross * gross)
        if c.gross_max is not None:
            constraints.append(cp.sum(cp.abs(w)) <= c.gross_max)
    # Vol cap.
    if c.vol_max_annual is not None and c.vol_max_annual > 0:
        constraints.append(cp.quad_form(w, inputs.sigma) <= c.vol_max_annual ** 2)
    # Sector caps.
    if c.sector_max:
        for sec, cap in c.sector_max.items():
            mask = np.array(
                [1.0 if (inputs.sector_codes[i] == sec) else 0.0 for i in range(N)]
            )
            constraints.append(mask @ w <= cap)
    # Factor caps and ranges.
    if inputs.B is not None and inputs.factor_targets:
        name_to_idx = {n: i for i, n in enumerate(inputs.factor_names)}
        for ft in inputs.factor_targets:
            j = name_to_idx.get(ft.factor)
            if j is None:
                continue
            b_col = inputs.B[:, j]
            if ft.mode == "cap" and ft.cap is not None:
                constraints.append(b_col @ w <= abs(ft.cap))
                constraints.append(b_col @ w >= -abs(ft.cap))
            elif ft.mode == "range":
                if ft.hi is not None:
                    constraints.append(b_col @ w <= ft.hi)
                if ft.lo is not None:
                    constraints.append(b_col @ w >= ft.lo)

    target_vec, scale_vec, active_vec = _factor_target_vectors(
        inputs.factor_names, inputs.factor_targets,
    )
    obj_expr = inputs.mu @ w - 0.5 * inputs.objective.lambda_risk * cp.quad_form(w, inputs.sigma)
    if (
        inputs.B is not None
        and target_vec is not None
        and active_vec is not None
        and active_vec.sum() > 0
    ):
        scaled = cp.multiply(
            np.asarray(active_vec) * np.asarray(scale_vec or np.ones_like(target_vec)),
            inputs.B.T @ w - np.asarray(target_vec),
        )
        obj_expr -= inputs.objective.gamma_factor * cp.sum_squares(scaled)
    if inputs.objective.gamma_concentration > 0:
        obj_expr -= inputs.objective.gamma_concentration * cp.sum_squares(w)

    problem = cp.Problem(cp.Maximize(obj_expr), constraints)
    tried = []
    last_err: Exception | None = None
    for solver in ("HIGHS", "SCIP", "CBC", "GLPK_MI", "MOSEK"):
        if solver not in cp.installed_solvers():
            continue
        tried.append(solver)
        try:
            problem.solve(solver=solver)
            if problem.status in ("optimal", "optimal_inaccurate"):
                weights = np.asarray(w.value, dtype=float)
                weights[np.abs(weights) < 1e-6] = 0.0
                if weights.sum() > 0:
                    weights = weights / weights.sum()
                return weights, {
                    "solver_status": problem.status,
                    "solver_backend": solver,
                    "tried_solvers": tried,
                }
        except Exception as exc:  # pragma: no cover - solver-specific
            last_err = exc
            continue
    raise MipSolverUnavailable(
        f"No MIP backend solved the cardinality problem. Tried: {tried or '<none>'}. "
        f"Install one of HiGHS, SCIP, CBC, GLPK_MI, or MOSEK. Last error: {last_err}"
    )


def optimize(inputs: OptimizeRequestInputs) -> OptimizeResult:
    N = len(inputs.tickers)
    warnings: list[str] = []

    # Cardinality-constrained path: solve the MIP via cvxpy. Bubbles up the
    # MipSolverUnavailable exception so the route can return a clear 503.
    if (
        inputs.constraints.max_names is not None
        and inputs.constraints.max_names > 0
        and inputs.constraints.max_names < N
    ):
        w, mip_diag = _solve_mip_cardinality(inputs)
        return _finalize_result(inputs, w, warnings, extra_diag=mip_diag)

    w0 = np.ones(N) / N
    if inputs.constraints.current_weights and len(inputs.constraints.current_weights) == N:
        w0 = np.array(inputs.constraints.current_weights, dtype=float)
        w0 = w0 / max(1e-9, w0.sum())

    target_vec, scale_vec, active_vec = _factor_target_vectors(
        inputs.factor_names, inputs.factor_targets,
    )
    w_curr = (
        np.array(inputs.constraints.current_weights, dtype=float)
        if inputs.constraints.current_weights and len(inputs.constraints.current_weights) == N
        else None
    )
    bounds, cons = _build_constraint_specs(N, inputs)

    def fun(w):
        return _objective_value(w, inputs.mu, inputs.sigma, inputs.objective,
                                inputs.B, target_vec, scale_vec, active_vec, w_curr)

    def jac(w):
        return _objective_grad(w, inputs.mu, inputs.sigma, inputs.objective,
                               inputs.B, target_vec, scale_vec, active_vec, w_curr)

    res = minimize(
        fun, w0, jac=jac, method="SLSQP", bounds=bounds, constraints=cons,
        options={"maxiter": 300, "ftol": 1e-8, "disp": False},
    )

    lo = np.array([b[0] if b[0] is not None else -np.inf for b in bounds], dtype=float)
    hi = np.array([b[1] if b[1] is not None else np.inf for b in bounds], dtype=float)
    w = np.clip(np.asarray(res.x, dtype=float), lo, hi)
    if not res.success:
        warnings.append(f"Solver did not converge: {res.message}")
    if abs(float(w.sum()) - 1.0) > 1e-6:
        # Successful SLSQP solves should already satisfy the equality
        # constraint. If numerical clipping moved the sum by a tiny amount,
        # renormalize; otherwise preserve the bounded vector and surface the
        # failed solver status rather than silently violating active caps.
        if res.success:
            w = w / max(1e-12, float(w.sum()))
        elif w.sum() <= 0:
            w = np.ones(N) / N

    exp_ret = float(w @ inputs.mu)
    var = float(w @ inputs.sigma @ w)
    vol = float(np.sqrt(max(0.0, var)))
    sharpe = (exp_ret - inputs.risk_free_annual) / vol if vol > 0 else None

    # Factor exposures Bᵀw.
    factor_exposures = {}
    if inputs.B is not None:
        for j, n in enumerate(inputs.factor_names):
            factor_exposures[n] = float(inputs.B[:, j] @ w)

    # Marginal contribution to risk: σᵢ × wᵢ × (Σw)_i / total vol.
    if vol > 0:
        mc = (inputs.sigma @ w) * w / vol
    else:
        mc = np.zeros(N)

    diagnostics = {
        "solver_status": res.message,
        "solver_success": bool(res.success),
        "objective_value": float(-res.fun) if res.fun is not None else None,
        "n_iter": int(res.nit) if hasattr(res, "nit") else None,
        "effective_n": float(1.0 / np.sum(w**2)) if np.sum(w**2) > 0 else None,
        "condition_number": float(np.linalg.cond(inputs.sigma)) if N > 1 else 1.0,
    }

    # Mini efficient frontier: sweep λ over a fixed grid, re-solve quickly.
    frontier: list[dict] = []
    for lam in [0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0]:
        sub_obj = Objective(
            lambda_risk=lam,
            gamma_factor=inputs.objective.gamma_factor,
            gamma_turnover=0.0,
            gamma_concentration=inputs.objective.gamma_concentration,
        )
        sub_inputs = OptimizeRequestInputs(
            tickers=inputs.tickers,
            mu=inputs.mu,
            sigma=inputs.sigma,
            B=inputs.B,
            factor_names=inputs.factor_names,
            sector_codes=inputs.sector_codes,
            objective=sub_obj,
            factor_targets=[],  # no penalties for frontier sweep, only λ
            constraints=inputs.constraints,
            risk_free_annual=inputs.risk_free_annual,
        )
        # Inline solve to skip recursion / log noise.
        bounds_f, cons_f = _build_constraint_specs(N, sub_inputs)
        target_vec_f, scale_vec_f, active_vec_f = _factor_target_vectors(
            sub_inputs.factor_names, [],
        )
        r2 = minimize(
            lambda w, _i=sub_inputs, _t=target_vec_f, _s=scale_vec_f, _a=active_vec_f: _objective_value(
                w, _i.mu, _i.sigma, _i.objective, _i.B, _t, _s, _a, None,
            ),
            np.ones(N) / N,
            method="SLSQP", bounds=bounds_f, constraints=cons_f,
            options={"maxiter": 150, "ftol": 1e-7, "disp": False},
        )
        wf = np.clip(r2.x, [b[0] for b in bounds_f], [b[1] for b in bounds_f])
        if wf.sum() > 0:
            wf = wf / wf.sum()
        var_f = float(wf @ inputs.sigma @ wf)
        frontier.append({
            "lambda": lam,
            "ret": float(wf @ inputs.mu),
            "vol": float(np.sqrt(max(0.0, var_f))),
        })

    return OptimizeResult(
        weights=w,
        expected_return_annual=exp_ret,
        vol_annual=vol,
        sharpe=sharpe if sharpe is not None and np.isfinite(sharpe) else None,
        factor_exposures=factor_exposures,
        marginal_risk_contribution=mc,
        diagnostics=diagnostics,
        efficient_frontier=frontier,
        warnings=warnings,
    )


def _finalize_result(
    inputs: OptimizeRequestInputs,
    w: np.ndarray,
    warnings: list[str],
    *,
    extra_diag: dict | None = None,
) -> OptimizeResult:
    """Build OptimizeResult from a solved weight vector.
    Used by the MIP path (which skips the frontier sweep).
    """
    N = len(inputs.tickers)
    exp_ret = float(w @ inputs.mu)
    var = float(w @ inputs.sigma @ w)
    vol = float(np.sqrt(max(0.0, var)))
    sharpe = (exp_ret - inputs.risk_free_annual) / vol if vol > 0 else None

    factor_exposures: dict[str, float] = {}
    if inputs.B is not None:
        for j, n in enumerate(inputs.factor_names):
            factor_exposures[n] = float(inputs.B[:, j] @ w)

    if vol > 0:
        mc = (inputs.sigma @ w) * w / vol
    else:
        mc = np.zeros(N)

    diagnostics = {
        "solver_status": "MIP",
        "solver_success": True,
        "effective_n": float(1.0 / np.sum(w**2)) if np.sum(w**2) > 0 else None,
        "condition_number": float(np.linalg.cond(inputs.sigma)) if N > 1 else 1.0,
        "active_names": int(np.count_nonzero(np.abs(w) > 1e-8)),
    }
    if extra_diag:
        diagnostics.update(extra_diag)

    return OptimizeResult(
        weights=w,
        expected_return_annual=exp_ret,
        vol_annual=vol,
        sharpe=sharpe if sharpe is not None and np.isfinite(sharpe) else None,
        factor_exposures=factor_exposures,
        marginal_risk_contribution=mc,
        diagnostics=diagnostics,
        efficient_frontier=[],
        warnings=warnings,
    )
