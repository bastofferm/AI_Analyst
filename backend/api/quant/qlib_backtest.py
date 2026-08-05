"""Walk-forward, out-of-sample backtest of the alpha model with Fama-French benchmarking.

qlib's ``backtest_daily`` needs a ``.bin`` data provider the lean embed lacks, so we run
a cross-sectional signal backtest directly off the monthly panel:

- **Out-of-sample by construction** — each month the model is retrained on *all prior*
  months (refit every ``refit_every`` months) and used to rank that month's names, so no
  month is tested on data it trained on.
- **Investable** — restricted to a market-cap floor; forward returns winsorized to the
  1st/99th cross-sectional percentile (kills micro-cap data artifacts).
- **Benchmarked** — against the Fama-French market (Mkt-RF + RF), with a FF 5-factor +
  momentum regression separating genuine alpha from factor exposure.

The expensive walk-forward *predictions* are cached separately from portfolio
construction, so changing topk / long-short re-ranks instantly.
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from . import qlib_alpha, qlib_data

_TTL = float(os.environ.get("QLIB_BACKTEST_CACHE_TTL", "3600"))
_PRED_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}   # OOS (alpha, ret) per name/month
_BT_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}   # full result

_WF_PARAMS = {
    "objective": "mse", "verbosity": -1, "num_leaves": 31, "max_depth": 6,
    "learning_rate": 0.05, "feature_fraction": 0.8, "bagging_fraction": 0.8,
    "bagging_freq": 1, "lambda_l1": 1.0, "lambda_l2": 1.0, "min_child_samples": 50,
}
_FF_DATASETS = {
    "US": ("F-F_Research_Data_5_Factors_2x3_daily", "F-F_Momentum_Factor_daily"),
    "JP": ("Japan_5_Factors_Daily", "Japan_Mom_Factor_Daily"),
}
_FF_RENAME = {"Mkt-RF": "mkt_rf", "SMB": "smb", "HML": "hml", "RMW": "rmw",
              "CMA": "cma", "Mom": "mom", "RF": "rf"}
_FACTOR_KEYS = ["mkt_rf", "smb", "hml", "rmw", "cma", "mom"]


def _oos_predictions(
    artifact: "qlib_alpha.AlphaArtifact",
    start: date | str | None,
    end: date | str,
    min_market_cap_usd: float | None,
    min_train_months: int,
    refit_every: int,
) -> pd.DataFrame:
    """Walk-forward OOS predictions: rows (datetime, instrument) with alpha + realized ret.

    This is the expensive step (panel build + repeated LightGBM fits); cached so portfolio
    construction (topk / long-short) is cheap and instant to re-tune.
    """
    key = (artifact.jurisdiction, artifact.trained_at, str(start), str(end),
           min_market_cap_usd, min_train_months, refit_every)
    hit = _PRED_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]

    import lightgbm as lgb

    panel = qlib_data.build_panel(
        artifact.jurisdiction, start=start, end=end, label=artifact.label,
        metric_ids=artifact.metric_ids, min_names_per_date=1,
    )
    if panel.empty:
        _PRED_CACHE[key] = (time.time(), pd.DataFrame())
        return pd.DataFrame()
    if min_market_cap_usd:
        liquid = _liquid_instruments(artifact.jurisdiction, min_market_cap_usd)
        if liquid:
            panel = panel[panel.index.get_level_values("instrument").isin(liquid)]
    if panel.empty:
        _PRED_CACHE[key] = (time.time(), pd.DataFrame())
        return pd.DataFrame()

    feat = panel[qlib_data.FEATURE].reindex(columns=artifact.feature_cols).fillna(0.0)
    lab = panel[(qlib_data.LABEL, qlib_data.LABEL_COL)].astype(float)
    lab = lab.groupby(level="datetime").transform(lambda r: r.clip(r.quantile(0.01), r.quantile(0.99)))
    dt_index = panel.index.get_level_values("datetime")
    dates = pd.Index(sorted(dt_index.unique()))
    if len(dates) < min_train_months + 3:
        _PRED_CACHE[key] = (time.time(), pd.DataFrame())
        return pd.DataFrame()

    parts: list[pd.DataFrame] = []
    booster = None
    for i in range(min_train_months, len(dates)):
        t = dates[i]
        te = dt_index == t
        if int(te.sum()) < 10:
            continue
        if booster is None or (i - min_train_months) % refit_every == 0:
            tr = dt_index < t
            booster = lgb.train(_WF_PARAMS, lgb.Dataset(feat[tr].values, lab[tr].values), num_boost_round=80)
        p = booster.predict(feat[te].values)
        parts.append(pd.DataFrame({"alpha": p, "ret": lab[te].values}, index=feat[te].index))

    out = pd.concat(parts) if parts else pd.DataFrame()
    if not out.empty:
        # Realized *one-month* forward return per name, joined on the same month-end.
        # This is the backtest's P&L unit — decoupled from the (possibly multi-month)
        # horizon the model ranks on — so a forward_6m signal is no longer counted as a
        # month's return and compounded on overlapping windows (the ~170x-curve bug).
        r1m = qlib_data.realized_forward_returns(artifact.jurisdiction, start=start, end=end, horizon_months=1)
        out["ret_1m"] = r1m.reindex(out.index) if not r1m.empty else np.nan
    _PRED_CACHE[key] = (time.time(), out)
    return out


def backtest_alpha(
    artifact: "qlib_alpha.AlphaArtifact",
    *,
    start: date | str | None = "2022-01-01",
    end: date | str | None = None,
    topk: int = 30,
    long_short: bool = False,
    min_market_cap_usd: float | None = 2e9,
    min_train_months: int = 12,
    refit_every: int = 3,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Backtest the alpha model (walk-forward OOS) with FF benchmarking. See module docstring."""
    end = end or date.today()
    ckey = (artifact.jurisdiction, artifact.trained_at, str(start), str(end),
            topk, long_short, min_market_cap_usd, min_train_months, refit_every)
    if use_cache:
        hit = _BT_CACHE.get(ckey)
        if hit and (time.time() - hit[0]) < _TTL:
            return hit[1]

    preds = _oos_predictions(artifact, start, end, min_market_cap_usd, min_train_months, refit_every)
    if preds.empty:
        return {"available": False, "reason": "insufficient out-of-sample history"}

    ser = _portfolio_pnl(preds, topk=topk, long_short=long_short)
    if ser.empty:
        return {"available": False, "reason": "no out-of-sample month had enough investable names"}

    metrics = _risk_metrics(ser)
    # IC stays measured at the model's native horizon (what it actually predicts).
    ic_stats = qlib_alpha.evaluate(preds["alpha"], preds["ret"])

    ff = _ff_monthly(artifact.jurisdiction, ser.index)
    performance: dict[str, Any] = {}
    factor_reg: dict[str, Any] = {"available": False}
    bench_available = False
    curve: list[dict[str, Any]] = []
    if ff is not None and not ff.empty:
        common = ser.index.intersection(ff.index)
        s = ser.reindex(common).dropna()
        if len(s) >= 6:
            common = s.index
            F = ff.reindex(common)
            rf = F["rf"]
            mkt = F["mkt_rf"] + rf
            performance = _performance(s, mkt, rf)
            factor_reg = _factor_regression(s - rf, F[_FACTOR_KEYS])
            strat_eq = (1.0 + s).cumprod()
            bench_eq = (1.0 + mkt).cumprod()
            bench_available = True
            curve = [
                {"date": str(pd.Timestamp(d).date()), "ret": float(s.loc[d]),
                 "equity": float(strat_eq.loc[d]), "bench_equity": float(bench_eq.loc[d])}
                for d in common
            ]
    if not bench_available:
        equity = (1.0 + ser).cumprod()
        curve = [
            {"date": str(pd.Timestamp(d).date()), "ret": float(r), "equity": float(equity.loc[d])}
            for d, r in ser.items()
        ]

    result = {
        "available": True,
        "out_of_sample": True,
        "jurisdiction": artifact.jurisdiction,
        "label": artifact.label,
        "horizon_months": artifact.horizon_months,
        "rebalance": "monthly",
        "topk": topk,
        "long_short": long_short,
        "n_periods": int(ser.shape[0]),
        "metrics": metrics,
        "performance": performance,
        "factor_regression": factor_reg,
        "benchmark": {"available": bench_available,
                      "label": ("Japan" if artifact.jurisdiction.upper() == "JP" else "US")
                               + " Fama-French market (Mkt-RF + RF)"},
        "ic": {"rank_ic_mean": ic_stats.get("rank_ic_mean"), "ic_mean": ic_stats.get("ic_mean"),
               "rank_icir": ic_stats.get("rank_icir")},
        "curve": curve,
    }
    if use_cache:
        _BT_CACHE[ckey] = (time.time(), result)
    return result


def weighted_portfolio_backtest(
    tickers: list[str],
    R: np.ndarray,
    dates: list,
    weights: np.ndarray,
    jurisdiction: str,
    *,
    roll_window: int = 24,
) -> dict[str, Any]:
    """Backtest a **fixed-weight** book on past returns — the current optimizer weights
    held constant over history.

    This is what "run the backtest on past return data with the current optimal weights"
    means: form the portfolio the optimizer just produced and roll it backwards over the
    available daily history. It is *in-sample by construction* (the weights were fit using
    data through today), so it answers "what would this exact book have done", not "is the
    signal out-of-sample". Returns a monthly equity curve vs the Fama-French market, the
    usual performance/risk stats, a full-period FF 5-factor + momentum regression, and the
    book's **rolling factor exposures over time**.
    """
    w = np.asarray(weights, dtype=float).reshape(-1)
    R = np.asarray(R, dtype=float)
    if R.ndim != 2 or R.shape[1] != w.shape[0] or R.shape[0] < 60:
        return {"available": False, "reason": "insufficient overlapping history for a portfolio backtest"}

    # Daily book return -> monthly (compounded within each calendar month).
    idx = pd.to_datetime(pd.Index(dates))
    rp = pd.Series(R @ w, index=idx).sort_index()
    s = (1.0 + rp).groupby(rp.index.to_period("M").to_timestamp("M")).prod() - 1.0
    s = s.dropna().sort_index()
    if len(s) < roll_window + 3:
        return {"available": False, "reason": "need more monthly history for a portfolio backtest"}

    ff = _ff_monthly(jurisdiction, s.index)
    juris = (jurisdiction or "US").upper()
    bench_label = ("Japan" if juris == "JP" else "US") + " Fama-French market (Mkt-RF + RF)"
    if ff is None or ff.empty:
        eq = (1.0 + s).cumprod()
        curve = [{"date": str(pd.Timestamp(d).date()), "ret": float(s.loc[d]), "equity": float(eq.loc[d])}
                 for d in s.index]
        return {"available": True, "benchmarked": False, "n_months": int(len(s)),
                "history_from": str(s.index[0].date()), "history_to": str(s.index[-1].date()),
                "roll_window": roll_window, "curve": curve, "exposures": [],
                "performance": {}, "factor_regression": {"available": False},
                "benchmark": {"available": False, "label": bench_label}}

    common = s.index.intersection(ff.index)
    s = s.reindex(common).dropna()
    F = ff.reindex(s.index)
    rf = F["rf"]
    mkt = F["mkt_rf"] + rf

    performance = _performance(s, mkt, rf)
    factor_reg = _factor_regression(s - rf, F[_FACTOR_KEYS])
    strat_eq = (1.0 + s).cumprod()
    bench_eq = (1.0 + mkt).cumprod()
    curve = [
        {"date": str(pd.Timestamp(d).date()), "ret": float(s.loc[d]),
         "equity": float(strat_eq.loc[d]), "bench_equity": float(bench_eq.loc[d])}
        for d in s.index
    ]
    exposures = _rolling_exposures(s - rf, F[_FACTOR_KEYS], roll_window)

    return {
        "available": True,
        "benchmarked": True,
        "n_months": int(len(s)),
        "history_from": str(s.index[0].date()),
        "history_to": str(s.index[-1].date()),
        "roll_window": roll_window,
        "performance": performance,
        "factor_regression": factor_reg,
        "benchmark": {"available": True, "label": bench_label},
        "curve": curve,
        "exposures": exposures,
    }


def _rolling_exposures(y: pd.Series, X: pd.DataFrame, window: int) -> list[dict[str, Any]]:
    """Trailing ``window``-month OLS betas of ``y`` on the FF factors ``X``, one point per
    month-end from the first full window onward — the book's factor exposure *over time*."""
    yv = y.values.astype(float)
    Xv = X.values.astype(float)
    n, k = Xv.shape
    names = list(X.columns)
    dates = y.index
    out: list[dict[str, Any]] = []
    if n < window or window < k + 2:
        return out
    for i in range(window, n + 1):
        yw = yv[i - window:i]
        Xw = Xv[i - window:i]
        A = np.column_stack([np.ones(window), Xw])
        coef, *_ = np.linalg.lstsq(A, yw, rcond=None)
        out.append({
            "date": str(pd.Timestamp(dates[i - 1]).date()),
            "betas": {name: float(coef[1 + j]) for j, name in enumerate(names)},
        })
    return out


def prewarm(jurisdiction: str = "US") -> None:
    """Warm the default backtest (call from a startup thread; the walk-forward is slow)."""
    art = qlib_alpha.get_model(jurisdiction)
    if art is None:
        return
    try:
        import logging
        bt = backtest_alpha(art)
        logging.getLogger("mzqa.quant.backtest").info(
            "backtest prewarm %s: n=%s available=%s", jurisdiction, bt.get("n_periods"), bt.get("available"))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _portfolio_pnl(preds: pd.DataFrame, *, topk: int, long_short: bool) -> pd.Series:
    """Monthly rebalanced P&L series from walk-forward predictions.

    Each month the names are ranked by the model's ``alpha`` (its native horizon), and
    the portfolio books the realized **one-month** return (``ret_1m``) of the top-k —
    long-only, or top-minus-bottom for long/short. Realizing 1m (not the multi-month
    ``ret`` label) is the fix for the overlapping-window compounding that blew the
    equity curve up ~170x. Falls back to ``ret`` only if ``ret_1m`` is absent.
    """
    pnl_col = "ret_1m" if ("ret_1m" in preds.columns and preds["ret_1m"].notna().any()) else "ret"
    min_names = max((2 * topk) if long_short else topk, 10)
    rows: dict[pd.Timestamp, float] = {}
    for dt, g in preds.groupby(level="datetime"):
        if len(g) < min_names:
            continue
        g = g.sort_values("alpha", ascending=False)
        long_ret = g[pnl_col].head(topk).mean()
        pnl = (long_ret - g[pnl_col].tail(topk).mean()) if long_short else long_ret
        if pd.notna(pnl):
            rows[dt] = float(pnl)
    return pd.Series(rows, dtype=float).sort_index()


def _risk_metrics(monthly_returns: pd.Series) -> dict[str, float]:
    from qlib.contrib.evaluate import risk_analysis

    if monthly_returns.empty:
        return {}
    ra = risk_analysis(monthly_returns, N=12)
    series = ra["risk"] if "risk" in getattr(ra, "columns", []) else ra.iloc[:, 0]
    return {str(k): float(v) for k, v in series.to_dict().items()}


def _liquid_instruments(jurisdiction: str, floor: float) -> set[str]:
    """Tickers whose latest market cap is >= ``floor`` USD (investable universe)."""
    try:
        from xbrl_sec.sec.db.connection import connect

        juris = (jurisdiction or "US").upper()
        with connect() as conn:
            df = pd.read_sql(
                """
                SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker) AS ticker, value::float AS mc
                FROM   fact_market_metrics
                WHERE  metric_id = 'market_capitalization' AND value IS NOT NULL
                  AND  (%(j)s = '' OR jurisdiction = %(j)s)
                ORDER  BY UPPER(ticker), market_date DESC NULLS LAST
                """,
                conn, params={"j": juris if juris in ("US", "JP") else ""},
            )
        return set(df.loc[df["mc"] >= floor, "ticker"])
    except Exception:  # noqa: BLE001
        return set()


def _ff_monthly(jurisdiction: str, months: pd.Index) -> pd.DataFrame | None:
    """Monthly Fama-French factors (decimal) aligned to the strategy's month-ends."""
    if len(months) == 0:
        return None
    juris = (jurisdiction or "US").upper()
    five, mom = _FF_DATASETS.get(juris, _FF_DATASETS["US"])
    lo = (pd.Timestamp(min(months)) - pd.offsets.MonthBegin(2)).date()
    hi = (pd.Timestamp(max(months)) + pd.offsets.MonthEnd(1)).date()
    try:
        from xbrl_sec.sec.db.connection import connect

        with connect() as conn:
            raw = pd.read_sql(
                """
                SELECT date, factor, value FROM fact_fama_french
                WHERE dataset = ANY(%(ds)s) AND date BETWEEN %(lo)s AND %(hi)s AND value IS NOT NULL
                """,
                conn, params={"ds": [five, mom], "lo": lo, "hi": hi},
            )
    except Exception:  # noqa: BLE001
        return None
    if raw.empty:
        return None
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["date"])
    wide = raw.pivot_table(index="date", columns="factor", values="value", aggfunc="last").sort_index()
    key = wide.index.to_period("M").to_timestamp("M")
    monthly = (1.0 + wide).groupby(key).prod() - 1.0
    monthly = monthly.rename(columns=_FF_RENAME)
    for k in _FACTOR_KEYS + ["rf"]:
        if k not in monthly.columns:
            monthly[k] = 0.0
    monthly.index.name = "datetime"
    return monthly[_FACTOR_KEYS + ["rf"]]


def _performance(s: pd.Series, mkt: pd.Series, rf: pd.Series) -> dict[str, Any]:
    """Strategy performance + risk metrics, and stats relative to the market."""
    n = len(s)
    if n == 0:
        return {}
    ann = 12.0 / n
    total = float((1.0 + s).prod())
    ann_ret = total ** ann - 1.0
    ann_vol = float(s.std(ddof=1)) * np.sqrt(12.0) if n > 1 else 0.0
    ann_rf = float((1.0 + rf).prod()) ** ann - 1.0
    sharpe = (ann_ret - ann_rf) / ann_vol if ann_vol > 1e-9 else None
    downside = s[s < 0]
    dd_dev = float(downside.std(ddof=1)) * np.sqrt(12.0) if len(downside) > 1 else 0.0
    sortino = (ann_ret - ann_rf) / dd_dev if dd_dev > 1e-9 else None
    eq = (1.0 + s).cumprod()
    max_dd = float((eq / eq.cummax() - 1.0).min())
    bench_ann = float((1.0 + mkt).prod()) ** ann - 1.0
    active = s - mkt
    te = float(active.std(ddof=1)) * np.sqrt(12.0) if n > 1 else 0.0
    ir = (ann_ret - bench_ann) / te if te > 1e-9 else None
    var_m = float(np.var(mkt.values, ddof=1)) if n > 1 else 0.0
    beta = float(np.cov(s.values, mkt.values, ddof=1)[0, 1] / var_m) if var_m > 1e-12 else None
    return {
        "annualized_return": ann_ret, "annualized_vol": ann_vol, "sharpe": sharpe,
        "sortino": sortino, "max_drawdown": max_dd, "hit_rate": float((s > 0).mean()),
        "cumulative_return": total - 1.0, "benchmark_annualized_return": bench_ann,
        "excess_annualized_return": ann_ret - bench_ann, "tracking_error": te,
        "information_ratio": ir, "beta_vs_market": beta, "n_months": n,
    }


def _factor_regression(y: pd.Series, X: pd.DataFrame) -> dict[str, Any]:
    """OLS of strategy excess return on FF factors -> annualized alpha, t-stat, R^2, betas."""
    yv = y.values.astype(float)
    Xv = X.values.astype(float)
    n, k = Xv.shape
    if n < k + 2:
        return {"available": False, "reason": "too few months for a factor regression"}
    A = np.column_stack([np.ones(n), Xv])
    coef, *_ = np.linalg.lstsq(A, yv, rcond=None)
    resid = yv - A @ coef
    ss_res = float(resid @ resid)
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    dof = n - (k + 1)
    alpha_m = float(coef[0])
    alpha_t = None
    try:
        sigma2 = ss_res / dof if dof > 0 else float("nan")
        se = np.sqrt(np.diag(sigma2 * np.linalg.inv(A.T @ A)))
        if se[0] > 0:
            alpha_t = float(alpha_m / se[0])
    except Exception:  # noqa: BLE001
        pass
    return {
        "available": True, "alpha_monthly": alpha_m,
        "alpha_annualized": (1.0 + alpha_m) ** 12 - 1.0, "alpha_tstat": alpha_t,
        "r2": r2, "betas": {name: float(coef[1 + i]) for i, name in enumerate(X.columns)},
        "n_months": n,
    }
