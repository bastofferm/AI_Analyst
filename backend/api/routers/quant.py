"""Quant router — qlib-powered return/risk prediction, portfolio optimization, backtest.

Activates the previously-dormant quant surface and exposes the qlib integration:

    GET  /api/quant/backends   — optimizer backends, risk models, alpha-model metadata
    POST /api/quant/alpha      — expected forward returns (qlib cross-sectional model)
    POST /api/quant/risk       — factor-structured forward covariance / vol / exposures
    POST /api/quant/optimize   — portfolio optimization; ``optimizer`` selects native OR
                                 any qlib backend (both always available), fed qlib mu/Sigma
    POST /api/quant/backtest   — cross-sectional signal backtest (qlib risk_analysis)

All qlib work is synchronous (LightGBM / scikit-learn / cvxpy) and warehouse access uses
the sync psycopg2 path, so every handler offloads to a worker thread via
``asyncio.to_thread`` to keep the event loop free.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..quant import alpha_signal, qlib_alpha, qlib_backtest, qlib_data, qlib_optimize, qlib_risk, simulate
from ..quant import risk as risk_mod

router = APIRouter()
logger = logging.getLogger("mzqa.quant")

RISK_MODELS = ("qlib_structured", "ledoit_wolf", "sample")


async def _in_thread(fn, *args, **kwargs):
    """Run sync qlib work off the event loop, turning any failure into a clean
    HTTPException. Without this an unhandled exception becomes a bare 500 that
    escapes the CORS middleware, so the browser only sees an opaque 'NetworkError'
    instead of the real cause."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("quant %s failed", getattr(fn, "__name__", fn))
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@router.get("/backends")
async def backends() -> dict[str, Any]:
    """List optimizer backends, risk models, and per-jurisdiction alpha-model metadata."""
    metas = {j: await asyncio.to_thread(alpha_signal.model_meta, j) for j in ("US", "JP")}
    ledger = await asyncio.to_thread(alpha_signal.list_trained_models)
    return {
        "optimizers": qlib_optimize.available_backends(),
        "risk_models": list(RISK_MODELS),
        "alpha_sources": ["model", "historical"],
        "alpha_models": {j: m for j, m in metas.items() if m is not None},
        # Full training ledger: US, JP, and per-country INTL models (rank_ic, coverage, next_due).
        "trained_models": ledger,
    }


# --------------------------------------------------------------------------- #
# Alpha (expected returns)
# --------------------------------------------------------------------------- #
class AlphaRequest(BaseModel):
    jurisdiction: str = "US"
    tickers: list[str] | None = None       # None -> the model's latest full cross-section
    top: int = Field(default=25, ge=1, le=500)
    label: str = "forward_1m"              # forward-return horizon: forward_1m|3m|6m|12m


@router.post("/alpha")
async def alpha(req: AlphaRequest) -> dict[str, Any]:
    return await _in_thread(_run_alpha, req)


def _run_alpha(req: AlphaRequest) -> dict[str, Any]:
    meta = alpha_signal.model_meta(req.jurisdiction, req.label)
    if meta is None:
        return {"available": False, "model": None, "rows": [],
                "note": f"no trained {req.label} alpha model for {req.jurisdiction}"}
    # The prediction is a compounded horizon return; annualize it geometrically
    # (qlib_data.annualize_return), consistent with the optimizer mu and backtest.
    h = int(meta["horizon_months"])
    _ann = lambda v: (qlib_data.annualize_return(float(v), h) if v is not None else None)
    if req.tickers:
        er = alpha_signal.expected_returns(req.jurisdiction, req.tickers, label=req.label)
        rows = [
            {"ticker": t, "expected_return_monthly": v, "expected_return_annual": _ann(v)}
            for t, v in er.items()
        ]
        n_covered = sum(1 for v in er.values() if v is not None)
    else:
        cross = alpha_signal.latest_cross_section(req.jurisdiction, label=req.label)
        n_covered = int(cross.shape[0])          # names the model actually scored this month
        rows = [
            {"ticker": t, "expected_return_monthly": float(v), "expected_return_annual": _ann(v)}
            for t, v in cross.head(req.top).items()
        ]
    return {"available": True, "model": meta, "rows": rows, "n_covered": n_covered}


# --------------------------------------------------------------------------- #
# Risk (forward covariance)
# --------------------------------------------------------------------------- #
class RiskRequest(BaseModel):
    jurisdiction: str = "US"
    tickers: list[str] = Field(min_length=2)
    lookback_months: int = Field(default=24, ge=6, le=120)
    num_factors: int = Field(default=10, ge=1, le=30)


@router.post("/risk")
async def risk(req: RiskRequest) -> dict[str, Any]:
    return await _in_thread(_run_risk, req)


def _run_risk(req: RiskRequest) -> dict[str, Any]:
    start = date.today() - timedelta(days=int(req.lookback_months * 30.5))
    present, R, _dates = qlib_risk.load_price_returns(req.tickers, req.jurisdiction, start, date.today())
    if len(present) < 2 or R.shape[0] < 3:
        return {"available": False, "note": "insufficient overlapping price history", "rows": []}
    sr = qlib_risk.structured_cov(present, R, num_factors=req.num_factors)
    rows = [
        {
            "ticker": t,
            "forward_vol_annual": round(float(sr.vol[i]), 4),
            "factor_exposures": {sr.factor_names[j]: round(float(sr.factor_exposure[i, j]), 4)
                                 for j in range(sr.factor_exposure.shape[1])},
        }
        for i, t in enumerate(present)
    ]
    return {
        "available": True,
        "n_obs": sr.n_obs,
        "factor_names": sr.factor_names,
        "tickers_dropped": [t for t in req.tickers if t not in present],
        "rows": rows,
        "warnings": sr.warnings,
    }


# --------------------------------------------------------------------------- #
# Optimize (native + qlib backends)
# --------------------------------------------------------------------------- #
class OptimizeRequest(BaseModel):
    jurisdiction: str = "US"
    tickers: list[str] = Field(min_length=2)
    optimizer: str = "qlib_mvo"            # any of qlib_optimize.BACKENDS
    risk_model: str = "qlib_structured"    # any of RISK_MODELS
    alpha_source: str = "model"            # "model" | "historical"
    label: str = "forward_1m"              # alpha horizon: forward_1m|3m|6m|12m (model source)
    lookback_months: int = Field(default=24, ge=6, le=120)
    num_factors: int = Field(default=10, ge=1, le=30)
    lamb: float | None = None
    delta: float = 0.0
    b_dev: float = 0.05
    risk_free_annual: float = 0.045
    # Per-name weight cap (concentration guard). Long-only MVO dumps into the single
    # highest-expected-return name; a cap keeps the book diversified. null disables it.
    max_weight: float | None = Field(default=0.35, ge=0.0, le=1.0)


@router.post("/optimize")
async def optimize(req: OptimizeRequest) -> dict[str, Any]:
    if req.optimizer not in qlib_optimize.BACKENDS:
        raise HTTPException(422, f"unknown optimizer {req.optimizer!r}; choose one of {list(qlib_optimize.BACKENDS)}")
    if req.risk_model not in RISK_MODELS:
        raise HTTPException(422, f"unknown risk_model {req.risk_model!r}; choose one of {list(RISK_MODELS)}")
    result = await _in_thread(_run_optimize, req)
    if not result.get("ok", True):
        raise HTTPException(422, result.get("note", "optimization failed"))
    return result


def _apply_weight_cap(w: np.ndarray, cap: float, iters: int = 200) -> np.ndarray:
    """Project long-only weights onto ``{0 <= wᵢ <= cap, Σw = 1}`` by water-filling: clip
    over-cap names to ``cap`` and redistribute the excess to the rest in proportion to their
    weight. Preserves the optimizer's relative ordering while removing single-name dominance.
    """
    w = np.clip(np.asarray(w, dtype=float), 0.0, None)
    total = w.sum()
    w = w / total if total > 0 else np.full(len(w), 1.0 / len(w))
    for _ in range(iters):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        under = ~over
        pool = float(w[under].sum())
        if pool <= 1e-12:  # everything at the cap → spread the remainder evenly
            w[under] += excess / max(1, int(under.sum()))
        else:
            w[under] += excess * (w[under] / pool)
    return w


def _book_analytics(
    tickers: list[str], weights: np.ndarray, jurisdiction: str, horizon_months: int,
    *, history_years: float = 7.0, roll_window: int = 24,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return-distribution *and* fixed-weight portfolio backtest for the optimized book.

    Loads a long common daily history for ``tickers`` **once**, aligns ``weights`` by ticker
    (renormalizing over the names that actually have history), then hands the same panel to
    the historical-simulation distribution (:func:`api.quant.simulate`) and the fixed-weight
    portfolio backtest (:func:`api.quant.qlib_backtest.weighted_portfolio_backtest`). Advisory
    — any failure degrades both to ``available=False`` rather than sinking the optimize.
    """
    def _fail(reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"available": False, "reason": reason}, {"available": False, "reason": reason}

    try:
        w_by_ticker = {t: float(w) for t, w in zip(tickers, weights)}
        start = date.today() - timedelta(days=int(365.25 * history_years))
        present, R, dates = qlib_risk.load_price_returns(list(tickers), jurisdiction, start, date.today())
        if len(present) < 1 or R.shape[0] < 3:
            return _fail("insufficient price history for this book")
        w = np.array([w_by_ticker.get(t, 0.0) for t in present], dtype=float)
        covered = float(w.sum())
        if covered <= 1e-9:
            return _fail("no weighted names have price history")
        w = w / covered  # renormalize over the names that have history

        dist = simulate.portfolio_return_distribution(R, w, horizon_months)
        if dist.get("available"):
            dist["weight_covered"] = covered
            dist["names_used"] = len(present)
            dist["history_from"] = str(dates[0])[:10] if dates else None
            dist["history_to"] = str(dates[-1])[:10] if dates else None

        bt = qlib_backtest.weighted_portfolio_backtest(
            present, R, dates, w, jurisdiction, roll_window=roll_window)
        if bt.get("available"):
            bt["weight_covered"] = covered

        return dist, bt
    except Exception as exc:  # noqa: BLE001 - advisory; never sink the optimize
        logger.warning("book analytics failed", exc_info=True)
        return _fail(f"{type(exc).__name__}: {exc}")


def _run_optimize(req: OptimizeRequest) -> dict[str, Any]:
    start = date.today() - timedelta(days=int(req.lookback_months * 30.5))
    present, R, _dates = qlib_risk.load_price_returns(req.tickers, req.jurisdiction, start, date.today())
    if len(present) < 2 or R.shape[0] < 3:
        return {"ok": False, "note": "insufficient overlapping price history for these tickers"}

    # Covariance + decomposition. Always compute the qlib decomposition so the
    # enhanced-indexing backend and factor exposures are available regardless of risk_model.
    sr = qlib_risk.structured_cov(present, R, num_factors=req.num_factors)
    if req.risk_model == "qlib_structured":
        sigma = sr.sigma
    else:  # sample | ledoit_wolf
        sigma = risk_mod.covariance_matrix(R, req.risk_model)

    # Expected returns (annualized), aligned to the tickers with price history. Per ticker:
    # the trained model where it has a prediction, else an on-the-spot historical mean — so a
    # partly-covered universe is never silently zeroed (which collapses MVO onto one name).
    # Low-confidence historical proxy for names the model doesn't cover yet: the annualized
    # mean, shrunk 50% toward zero and clipped to ±20% so a noisy 2-year momentum mean can't
    # dominate the book (we're least sure about exactly the names that lack a model forecast).
    hist_annual = {t: float(np.clip(0.5 * R[:, i].mean() * 252.0, -0.20, 0.20)) for i, t in enumerate(present)}
    if req.alpha_source == "model":
        mu_list, mu_sources = alpha_signal.expected_returns_with_fallback(
            req.jurisdiction, present, hist_annual, label=req.label)
        mu = np.array(mu_list, dtype=float)
        n_fb = mu_sources.count("historical")
        alpha_note = (
            f"{n_fb}/{len(present)} names had no trained alpha; used an on-the-spot historical "
            f"mean return for those (train/retrain the alpha model to cover them)."
        ) if n_fb else None
    else:
        mu = np.array([hist_annual[t] for t in present], dtype=float)
        mu_sources = ["historical"] * len(present)
        alpha_note = None

    wb = np.ones(len(present)) / len(present)
    sol = qlib_optimize.solve(
        req.optimizer, present, mu, sigma,
        risk_free_annual=req.risk_free_annual, lamb=req.lamb, delta=req.delta, b_dev=req.b_dev,
        decomposition=sr.decomposition, wb=wb,
    )

    # Concentration guard: cap per-name weight (backend-agnostic) so a single dominant
    # expected return can't take the whole book. Skipped when the universe is too small for
    # the cap to be feasible (N * cap < 1).
    cap = req.max_weight
    if cap and 0 < cap < 1 and len(present) * cap >= 1.0 - 1e-9:
        capped = _apply_weight_cap(np.asarray(sol.weights, dtype=float), cap)
        if not np.allclose(capped, np.asarray(sol.weights, dtype=float), atol=1e-6):
            sol.weights = capped
            sol.expected_return_annual, sol.vol_annual, sol.sharpe = qlib_optimize._summary(
                capped, mu, sigma, req.risk_free_annual)
            sol.warnings.append(f"Capped per-name weight at {cap:.0%} to prevent single-name concentration.")

    # Per-name predicted return & risk over the forecast horizon (h months). mu / vol
    # are annualized; de-annualize geometrically / by √time for the horizon figures.
    h = qlib_data.HORIZON_MONTHS.get(req.label, 1)
    hz = h / 12.0
    w_final = np.asarray(sol.weights, dtype=float)
    vol = sr.vol
    per_name = [
        {
            "ticker": t,
            "weight": float(w_final[i]),
            "expected_return_annual": float(mu[i]),
            "expected_return_horizon": float((1.0 + mu[i]) ** hz - 1.0) if mu[i] > -1.0 else -1.0,
            "forward_vol_annual": float(vol[i]),
            "forward_vol_horizon": float(vol[i] * np.sqrt(hz)),
            "alpha_source": mu_sources[i],
        }
        for i, t in enumerate(present)
    ]

    # Historical-simulation distribution + fixed-weight portfolio backtest of the book (see
    # api.quant.simulate / qlib_backtest). Both use the optimizer weights over a long common
    # history, loaded once.
    distribution, portfolio_backtest = _book_analytics(present, w_final, req.jurisdiction, h)

    out = sol.to_dict()
    out.update({
        "ok": True,
        "risk_model": req.risk_model,
        "alpha_source": req.alpha_source,
        "alpha_sources": {t: s for t, s in zip(present, mu_sources)},
        "tickers_dropped": [t for t in req.tickers if t not in present],
        "n_obs": sr.n_obs,
        "horizon_months": h,
        "per_name": per_name,
        "distribution": distribution,
        "portfolio_backtest": portfolio_backtest,
    })
    if alpha_note:
        out.setdefault("warnings", []).append(alpha_note)
    return out


# --------------------------------------------------------------------------- #
# Backtest (signal quality gate)
# --------------------------------------------------------------------------- #
class BacktestRequest(BaseModel):
    jurisdiction: str = "US"
    start: str | None = "2022-01-01"   # matches the startup pre-warm so the default hits cache
    end: str | None = None
    topk: int = Field(default=30, ge=5, le=200)
    long_short: bool = False
    label: str = "forward_1m"          # alpha horizon to backtest: forward_1m|3m|6m|12m


@router.post("/backtest")
async def backtest(req: BacktestRequest) -> dict[str, Any]:
    art = await _in_thread(qlib_alpha.get_model, req.jurisdiction, req.label)
    if art is None:
        raise HTTPException(404, f"no trained {req.label} alpha model for {req.jurisdiction}; train one first")
    return await _in_thread(
        qlib_backtest.backtest_alpha, art,
        start=req.start, end=req.end, topk=req.topk, long_short=req.long_short,
    )


# --------------------------------------------------------------------------- #
# Retrain (extend alpha coverage to names the model doesn't yet forecast)
# --------------------------------------------------------------------------- #
class RetrainRequest(BaseModel):
    jurisdiction: str = "US"
    label: str = "forward_1m"          # horizon model to (re)train: forward_1m|3m|6m|12m


@router.post("/retrain")
async def retrain(req: RetrainRequest) -> dict[str, Any]:
    """(Re)train + persist the (jurisdiction, horizon) alpha model, refresh the coverage
    ledger, and drop the cached cross-section. Synchronous and slow (a full panel build +
    LightGBM fit, ~1–3 min) — the UI runs it behind a spinner."""
    if req.label not in qlib_data.LABEL_HORIZONS:
        raise HTTPException(422, f"unknown horizon {req.label!r}; choose one of {list(qlib_data.LABEL_HORIZONS)}")
    if req.jurisdiction.upper() not in ("US", "JP"):
        raise HTTPException(422, "retrain is available for the US and JP models")
    return await _in_thread(_run_retrain, req)


def _run_retrain(req: RetrainRequest) -> dict[str, Any]:
    import os

    from ..quant import qlib_train_all

    start = (date.today() - timedelta(days=365 * 8)).isoformat()
    threads = max(1, (os.cpu_count() or 4) // 2)
    rec = qlib_train_all._train_one(req.jurisdiction.upper(), req.label, start, threads)
    # A freshly-saved artifact hot-reloads by mtime, but the latest-cross-section cache is
    # TTL'd — drop it so the next optimize re-scores against the new model.
    alpha_signal.clear_cache()
    return {
        "ok": bool(rec.get("ok")),
        "jurisdiction": req.jurisdiction.upper(),
        "label": req.label,
        "rank_ic": rec.get("rank_ic"),
        "coverage": rec.get("coverage"),
        "error": rec.get("error"),
        "model": alpha_signal.model_meta(req.jurisdiction, req.label),
    }


# --------------------------------------------------------------------------- #
# Agentic research (the iterative replacement for one-shot retraining)
# --------------------------------------------------------------------------- #
# A research run is several fits plus a dozen LLM calls — far past any request timeout — so
# unlike /retrain it is a background job with a polled ledger. /retrain stays for the batch
# CLI and as a fast deterministic fallback.
class ResearchStartRequest(BaseModel):
    jurisdiction: str = "US"
    label: str = "forward_1m"
    max_iterations: int = Field(default=4, ge=1, le=12)
    # LLM credentials for the researcher / validation / PM agents. Supplied by the browser
    # vault via withSessionLlm, else resolved from the server environment.
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    # The external advisor deliberately runs on a DIFFERENT provider when one is available,
    # so the outside view carries genuinely different priors.
    advisor_provider: str | None = None
    advisor_model: str | None = None
    advisor_api_key: str | None = None
    # Optional starting-spec overrides; validated and clamped by spec.apply_patch.
    spec_overrides: dict[str, Any] | None = None
    offline: bool = False       # run the deterministic ladder, spending no tokens


@router.post("/research/start")
async def research_start(req: ResearchStartRequest) -> dict[str, Any]:
    """Register a research run and return its id immediately."""
    from ..quant.research import runner

    if req.label not in qlib_data.LABEL_HORIZONS:
        raise HTTPException(422, f"unknown horizon {req.label!r}; "
                                 f"choose one of {list(qlib_data.LABEL_HORIZONS)}")
    if req.jurisdiction.upper() not in ("US", "JP"):
        raise HTTPException(422, "research runs are available for the US and JP models")

    config: dict[str, Any] = {"offline": bool(req.offline)}
    if req.spec_overrides:
        config["spec_overrides"] = req.spec_overrides

    result = await asyncio.to_thread(
        runner.start_run,
        jurisdiction=req.jurisdiction.upper(), label=req.label,
        max_iterations=req.max_iterations, provider=req.provider, api_key=req.api_key,
        model=req.model, advisor_provider=req.advisor_provider,
        advisor_api_key=req.advisor_api_key, advisor_model=req.advisor_model,
        config=config,
    )
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "could not start the research run"))
    return result


@router.get("/research/latest")
async def research_latest(jurisdiction: str = "US", label: str = "forward_1m") -> dict[str, Any]:
    """Most recent run for a market/horizon, so the panel opens populated."""
    from ..quant.research import runner

    run = await asyncio.to_thread(runner.latest_run, jurisdiction, label)
    return run or {"available": False,
                   "note": f"no research run yet for {jurisdiction} {label}"}


@router.get("/research/runs")
async def research_runs(jurisdiction: str | None = None, label: str | None = None,
                        limit: int = 20) -> dict[str, Any]:
    """Run history, newest first."""
    from ..quant.research import runner

    rows = await asyncio.to_thread(runner.list_runs, jurisdiction, label, min(limit, 100))
    return {"runs": rows}


@router.get("/research/{run_id}")
async def research_get(run_id: str) -> dict[str, Any]:
    """Run header plus every persisted round — the endpoint the UI polls."""
    from ..quant.research import runner

    run = await asyncio.to_thread(runner.get_run, run_id)
    if run is None:
        raise HTTPException(404, f"no research run {run_id!r}")
    return run


@router.post("/research/{run_id}/cancel")
async def research_cancel(run_id: str) -> dict[str, Any]:
    """Cooperative cancel; the run stops after the round in flight completes."""
    from ..quant.research import runner

    result = await asyncio.to_thread(runner.cancel_run, run_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "no active run with that id"))
    return result


@router.get("/research/{run_id}/report.pdf")
async def research_report_pdf(run_id: str):
    """Render the run's validation dossier and return it as a PDF."""
    from fastapi.responses import FileResponse

    from ..quant.research import report_pdf, runner

    run = await asyncio.to_thread(runner.get_run, run_id)
    if run is None:
        raise HTTPException(404, f"no research run {run_id!r}")
    result = await _in_thread(report_pdf.write_report, run)
    path = result.get("pdf") or result.get("html")
    if not path:
        raise HTTPException(500, "could not render the report")
    media = "application/pdf" if result.get("pdf_ok") else "text/html"
    suffix = "pdf" if result.get("pdf_ok") else "html"
    return FileResponse(path, media_type=media,
                        filename=f"alpha_research_{run_id[:8]}.{suffix}")
