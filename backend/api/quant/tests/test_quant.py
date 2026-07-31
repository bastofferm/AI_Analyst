"""Tests for the qlib quant integration (api/quant/*).

Pure-numeric tests (optimizer dispatcher, risk model, scoring) run with no DB.
The panel/alpha integration tests are skipped automatically when the warehouse
is unreachable.
"""
from __future__ import annotations

import socket

import numpy as np
import pytest


# --------------------------------------------------------------------------- #
# Risk model + optimizer dispatcher (no DB)
# --------------------------------------------------------------------------- #
def _synthetic_returns(n_assets: int = 12, n_obs: int = 300, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    factor = rng.standard_normal((n_obs, 2)) * 0.01
    loadings = rng.standard_normal((n_assets, 2))
    idio = rng.standard_normal((n_obs, n_assets)) * 0.008
    return factor @ loadings.T + idio


def test_structured_cov_shape_and_psd():
    from api.quant import qlib_risk

    tickers = [f"T{i}" for i in range(12)]
    R = _synthetic_returns(12, 300)
    sr = qlib_risk.structured_cov(tickers, R, num_factors=4)
    assert sr.sigma.shape == (12, 12)
    assert sr.factor_exposure.shape[0] == 12
    assert sr.factor_cov.shape[0] == sr.factor_cov.shape[1]
    assert sr.specific_var.shape == (12,)
    # symmetric + positive semidefinite
    assert np.allclose(sr.sigma, sr.sigma.T, atol=1e-8)
    assert np.linalg.eigvalsh(sr.sigma).min() > -1e-8
    # annualized vols are in a sane equity range
    assert (sr.vol > 0).all() and (sr.vol < 3).all()


@pytest.mark.parametrize(
    "backend",
    ["native", "qlib_mvo", "qlib_gmv", "qlib_rp", "qlib_inv", "qlib_enhanced_indexing"],
)
def test_optimizer_backends_return_valid_portfolio(backend):
    from api.quant import qlib_optimize, qlib_risk

    tickers = [f"T{i}" for i in range(12)]
    R = _synthetic_returns(12, 300, seed=1)
    sr = qlib_risk.structured_cov(tickers, R, num_factors=4)
    rng = np.random.default_rng(2)
    mu = rng.standard_normal(12) * 0.05  # annualized expected returns

    kwargs = {}
    if backend == qlib_optimize.ENHANCED:
        kwargs = dict(decomposition=sr.decomposition, wb=np.ones(12) / 12)
    sol = qlib_optimize.solve(backend, tickers, mu, sr.sigma, **kwargs)

    assert sol.backend == backend
    assert sol.weights.shape == (12,)
    assert abs(float(sol.weights.sum()) - 1.0) < 1e-3        # fully invested
    assert float(sol.weights.min()) >= -1e-6                 # long-only
    assert sol.vol_annual > 0
    d = sol.to_dict()
    assert len(d["weights"]) == 12 and d["backend"] == backend


def test_unknown_backend_raises():
    from api.quant import qlib_optimize

    with pytest.raises(ValueError):
        qlib_optimize.solve("nope", ["A", "B"], np.zeros(2), np.eye(2))


# --------------------------------------------------------------------------- #
# Scoring: exact legacy fallback + alpha blend + IC re-split (no DB)
# --------------------------------------------------------------------------- #
_ROWS = [
    {"ticker": "AAA", "name": "A", "sector": "Tech", "metrics": {"fcf_yield": 0.08, "pe": 12.0, "rev_yoy": 0.10}},
    {"ticker": "BBB", "name": "B", "sector": "Tech", "metrics": {"fcf_yield": 0.05, "pe": 18.0, "rev_yoy": 0.20}},
    {"ticker": "CCC", "name": "C", "sector": "Ind", "metrics": {"fcf_yield": 0.12, "pe": 9.0, "rev_yoy": 0.05}},
    {"ticker": "DDD", "name": "D", "sector": "Ind", "metrics": {"fcf_yield": 0.06, "pe": 15.0, "rev_yoy": 0.30}},
]
_TONE = {"AAA": {"tone": 0.5}, "BBB": {"tone": -0.2}, "CCC": {"tone": None}, "DDD": {"tone": 0.1}}
_NEWS = {"AAA": 0.3, "BBB": None, "CCC": 0.1, "DDD": None}


def _legacy_reference():
    def norm(key, invert=False):
        vals = {r["ticker"]: r["metrics"][key] for r in _ROWS if r["metrics"].get(key) is not None}
        lo, hi = min(vals.values()), max(vals.values())
        hi = hi if hi > lo else lo + 1.0
        return {t: (1.0 - (x - lo) / (hi - lo)) if invert else (x - lo) / (hi - lo) for t, x in vals.items()}

    fcf, pe, g = norm("fcf_yield"), norm("pe", invert=True), norm("rev_yoy")
    out = {}
    for r in _ROWS:
        t = r["ticker"]
        v_fcf, v_pe, v_g = fcf.get(t, 0.5), pe.get(t, 0.5), g.get(t, 0.5)
        tone = _TONE[t]["tone"]
        v_tone = (tone + 1) / 2 if tone is not None else 0.5
        news = _NEWS[t]
        v_news = (news + 1) / 2 if isinstance(news, (int, float)) else None
        if v_news is not None:
            s = 0.28 * v_fcf + 0.17 * v_pe + 0.17 * v_g + 0.22 * v_tone + 0.16 * v_news
        else:
            s = 0.32 * v_fcf + 0.19 * v_pe + 0.19 * v_g + 0.30 * v_tone
        out[t] = round(100 * s, 1)
    return out


def test_rank_legacy_fallback_is_exact():
    from api.routers.screener_agent import _rank

    scored = _rank([dict(r) for r in _ROWS], _TONE, _NEWS, has_mda=True)
    ref = _legacy_reference()
    for s in scored:
        assert abs(s.interest_score - ref[s.ticker]) < 1e-9
        assert s.alpha is None and s.score_components == {} or "base" in s.score_components


def test_rank_alpha_blend_reorders_and_populates():
    from api.routers.screener_agent import _rank

    amap = {"AAA": -0.02, "BBB": 0.05, "CCC": -0.01, "DDD": 0.04}
    scored = _rank([dict(r) for r in _ROWS], _TONE, _NEWS, has_mda=True, alpha_map=amap, alpha_weight=0.5)
    top = scored[0]
    assert top.ticker in {"BBB", "DDD"}          # high-alpha names rise
    assert top.alpha is not None
    assert "alpha" in top.score_components and "base" in top.score_components
    assert 0.0 <= (top.alpha_percentile or 0) <= 100.0


def test_rank_ic_weights_shift_value_vs_growth():
    from api.routers.screener_agent import _rank

    value_heavy = {s.ticker: s.interest_score
                   for s in _rank([dict(r) for r in _ROWS], _TONE, _NEWS, has_mda=True,
                                  ic_weights={"value": 0.9, "growth": 0.1})}
    growth_heavy = {s.ticker: s.interest_score
                    for s in _rank([dict(r) for r in _ROWS], _TONE, _NEWS, has_mda=True,
                                   ic_weights={"value": 0.1, "growth": 0.9})}
    # DDD is the highest-growth name -> should score higher when growth is emphasized.
    assert growth_heavy["DDD"] >= value_heavy["DDD"]


# --------------------------------------------------------------------------- #
# DB-gated integration: panel build + alpha train
# --------------------------------------------------------------------------- #
def _db_up(host: str = "127.0.0.1", port: int = 5432) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


requires_db = pytest.mark.skipif(not _db_up(), reason="warehouse Postgres not reachable")


@requires_db
def test_build_panel_and_train_smoke():
    from datetime import date

    from api.quant import qlib_alpha, qlib_data

    panel = qlib_data.build_panel("US", start=date(2023, 1, 1), end=date(2024, 12, 31),
                                  label="forward_1m", min_names_per_date=50)
    if panel.empty:
        pytest.skip("no US panel data in range")
    assert ("label", "y") in panel.columns
    assert (panel.columns.get_level_values(0) == "feature").sum() > 0

    art = qlib_alpha.train("US", start=date(2023, 1, 1), end=date(2024, 12, 31))
    assert art.feature_cols and art.metric_ids
    # test-segment IC should have been computed
    assert "rank_ic_mean" in art.metrics
    pred = qlib_alpha.predict(art, panel)
    assert len(pred) == len(panel)
