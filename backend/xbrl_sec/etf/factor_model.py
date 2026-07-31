"""Fama-French factor regressions for ETFs.

Reuses the equity regression engine (xbrl_sec.sec.sources.factor_model): same
Huber-robust OLS, Newey-West SEs, same FF factor library. The differences are:
  - returns come from sec.fact_prices_etf (computed log-returns on the fly)
  - each ETF is mapped to an FF region by its tracked-index name
  - we fit a single most-recent window (not a rolling series) per (isin, model),
    which is the high-value summary for the consumer detail page.

Equity ETFs only — FF equity factors don't describe bond/commodity funds, so
those are skipped (recorded as no-region).
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.factor_model import (
    FF_PRIORITY,
    MODEL_FACTORS,
    _build_factor_frame,
    _load_ff_datasets,
    _run_ols,
    _sf,
    _winsorize,
)

logger = logging.getLogger(__name__)

# Regress on WEEKLY returns, not daily. European-listed ETFs trade on Xetra
# which closes ~6h before the US, so daily ETF returns are non-synchronous with
# the (US/Developed) FF factor day — this attenuates betas toward zero
# (Scholes-Williams/Dimson bias) and the EUR/USD FX overlay adds daily noise.
# Weekly (Friday-anchored) returns capture a full trading week on both sides and
# largely remove both effects, so a broad-equity ETF lands near MKT beta ~1.0.
MODELS = ("FF5", "FF6")
RESAMPLE = "W-FRI"
MIN_OBS = 104            # ~2y of weekly observations
MAX_WINDOW = 312         # cap regression window at ~6y of weekly obs

# Index-name -> FF region. Order matters: most specific first.
_REGION_RULES: list[tuple[tuple[str, ...], str]] = [
    (("japan", "topix", "nikkei"), "JP"),
    (("emerging", "msci em", " em ", "china", "india", "em imi", "em-imi"), "EM"),
    (("s&p 500", "s&p500", "sp 500", "nasdaq", "russell", "crsp us", "dow jones",
      "msci usa", "us large", "united states", "msci us"), "US"),
    # Broad developed / regional developed all map to the Developed library.
    (("world", "all-world", "all world", "acwi", "developed", "eafe", "europe",
      "euro stoxx", "stoxx", "dax", "ftse", "msci world", "eurozone", "emu"), "INTL"),
]

# Bond / commodity / money-market index hints — skip these.
_SKIP_HINTS = ("bond", "treasury", "gilt", "aggregate", "credit", "high yield",
               "govt", "government", "corporate bond", "gold", "silver",
               "commodity", "metal", "money market", "ultrashort", "eonia", "ester")


def _region_for(index_name: str | None, asset_class: str | None) -> str | None:
    if asset_class and asset_class not in ("Equity", None):
        return None
    text = (index_name or "").lower()
    if any(h in text for h in _SKIP_HINTS):
        return None
    for needles, region in _REGION_RULES:
        if any(n in text for n in needles):
            return region
    # Unlabelled equity → assume broad developed exposure.
    return "INTL" if (asset_class == "Equity" or not asset_class) else None


def _resolve_datasets(region: str, loaded: set[str]) -> dict[str, str | None]:
    """For an FF region, pick the first available dataset per factor key."""
    out: dict[str, str | None] = {}
    pri = FF_PRIORITY.get(region, FF_PRIORITY["INTL"])
    for key, candidates in pri.items():
        out[key] = next((c for c in candidates if c in loaded), None)
    return out


def _load_fx_logrets(conn) -> dict[str, pd.Series]:
    """Daily log-returns of USD-per-unit for each currency. USD → all zeros
    (handled implicitly by returning an empty/zero series)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ccy, fx_date, usd_per_unit FROM sec.fact_fx ORDER BY ccy, fx_date")
        rows = cur.fetchall()
    by_ccy: dict[str, list[tuple[Any, float]]] = {}
    for ccy, d, v in rows:
        by_ccy.setdefault(ccy, []).append((d, float(v)))
    out: dict[str, pd.Series] = {}
    for ccy, series in by_ccy.items():
        if len(series) < 2:
            continue
        idx = pd.to_datetime([d for d, _ in series])
        vals = np.array([v for _, v in series], dtype=float)
        out[ccy] = pd.Series(np.diff(np.log(vals)), index=idx[1:])
    return out


def _load_quote_ccy(conn, isins: list[str]) -> dict[str, str]:
    """isin -> quote currency. Prefer captured quote_ccy, fall back to the
    fund's base currency, default EUR (the dominant Xetra quote currency)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.isin, COALESCE(p.quote_ccy, d.fund_currency)
            FROM sec.dim_etf d LEFT JOIN sec.dim_etf_profile p ON p.isin=d.isin
            WHERE d.isin = ANY(%s)
            """,
            (isins,),
        )
        return {isin: (ccy.upper()[:3] if ccy else "EUR") for isin, ccy in cur.fetchall()}


def _load_etf_logrets(
    conn,
    isins: list[str],
    ccy_map: dict[str, str],
    fx_rets: dict[str, pd.Series],
) -> dict[str, pd.Series]:
    """Return {isin: USD daily log-return Series}. Fund returns are quoted in
    the local listing currency; we add the FX log-return to express them in USD
    so they are comparable with the USD Fama-French factors:
        r_usd = r_local + Δlog(USD per local-ccy unit)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT isin, price_date, close
            FROM sec.fact_prices_etf
            WHERE isin = ANY(%s) AND close > 0
            ORDER BY isin, price_date
            """,
            (isins,),
        )
        rows = cur.fetchall()
    out: dict[str, pd.Series] = {}
    by_isin: dict[str, list[tuple[Any, float]]] = {}
    for isin, d, close in rows:
        by_isin.setdefault(isin, []).append((d, float(close)))
    for isin, series in by_isin.items():
        if len(series) < MIN_OBS + 1:
            continue
        idx = pd.to_datetime([d for d, _ in series])
        closes = np.array([c for _, c in series], dtype=float)
        local = pd.Series(np.diff(np.log(closes)), index=idx[1:])
        ccy = ccy_map.get(isin, "EUR")
        if ccy and ccy != "USD" and ccy in fx_rets:
            fx = fx_rets[ccy].reindex(local.index).fillna(0.0)
            out[isin] = local + fx
        else:
            out[isin] = local  # USD-quoted (or unknown FX) → already USD
    return out


def _fit_one(ret_w: pd.Series, factor_w: pd.DataFrame, model: str) -> dict[str, Any] | None:
    """ret_w and factor_w are already resampled to weekly sums."""
    factors = list(MODEL_FACTORS[model])
    if any(f not in factor_w.columns for f in factors):
        return None
    rf = factor_w["RF"].reindex(ret_w.index).fillna(0.0) if "RF" in factor_w.columns else pd.Series(0.0, index=ret_w.index)
    excess = (ret_w - rf).dropna()
    common = excess.index.intersection(factor_w.index)
    if len(common) < MIN_OBS:
        return None
    common = common.sort_values()[-MAX_WINDOW:]
    y = _winsorize(excess.reindex(common).values.astype(float))
    x = factor_w[factors].reindex(common).values.astype(float)
    valid = ~(np.isnan(y) | np.any(np.isnan(x), axis=1))
    y, x = y[valid], x[valid]
    idx = common[valid]
    if len(y) < MIN_OBS:
        return None
    design = np.column_stack([np.ones(len(y)), x])
    res = _run_ols(y, design, factors)
    if res is None:
        return None
    # alpha is per-week; annualize by ×52 at the caller.
    res["window_start"] = idx[0].date() if hasattr(idx[0], "date") else idx[0]
    res["window_end"] = idx[-1].date() if hasattr(idx[-1], "date") else idx[-1]
    return res


def _to_weekly(s: pd.Series) -> pd.Series:
    """Sum log-returns within each ISO week (Friday-anchored)."""
    return s.resample(RESAMPLE).sum(min_count=3)  # need >=3 days in the week


def compute_etf_factors(limit: int | None = None, only_missing: bool = True) -> dict[str, int]:
    """Fit FF5+FF6 for every equity ETF with enough history. Returns counts."""
    with connect() as conn:
        with conn.cursor() as cur:
            where = "WHERE COALESCE(d.is_active,TRUE)=TRUE"
            if only_missing:
                where += (" AND NOT EXISTS (SELECT 1 FROM sec.fact_etf_factor_loadings f "
                          "WHERE f.isin=d.isin)")
            sql = f"SELECT d.isin, d.index_tracked, d.asset_class FROM sec.dim_etf d {where}"
            if limit:
                sql += f" ORDER BY d.aum_eur DESC NULLS LAST LIMIT {int(limit)}"
            cur.execute(sql)
            candidates = cur.fetchall()

        # Bucket ISINs by region so we load each FF library once.
        by_region: dict[str, list[str]] = {}
        for isin, idx_name, ac in candidates:
            region = _region_for(idx_name, ac)
            if region:
                by_region.setdefault(region, []).append(isin)

        loaded_ds = set()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT dataset FROM sec.fact_fama_french")
            loaded_ds = {r[0] for r in cur.fetchall()}

        all_ds_needed: set[str] = set()
        region_datasets: dict[str, dict[str, str | None]] = {}
        for region in by_region:
            ds = _resolve_datasets(region, loaded_ds)
            region_datasets[region] = ds
            all_ds_needed.update(d for d in ds.values() if d)

        ff_cache = _load_ff_datasets(conn, all_ds_needed)
        fx_rets = _load_fx_logrets(conn)

        rows_out: list[tuple] = []
        fitted = skipped = 0
        for region, isins in by_region.items():
            datasets = region_datasets[region]
            frames_daily = {m: _build_factor_frame(ff_cache, datasets, m) for m in MODELS}
            # Resample each factor frame to weekly once per region.
            frames: dict[str, pd.DataFrame | None] = {}
            for m, fdf in frames_daily.items():
                if fdf is None:
                    frames[m] = None
                else:
                    frames[m] = fdf.resample(RESAMPLE).sum(min_count=3)
            if all(f is None for f in frames.values()):
                skipped += len(isins)
                continue
            ccy_map = _load_quote_ccy(conn, isins)
            retmap = _load_etf_logrets(conn, isins, ccy_map, fx_rets)
            for isin in isins:
                ret = retmap.get(isin)
                if ret is None:
                    skipped += 1
                    continue
                ret_w = _to_weekly(ret)
                any_fit = False
                for model in MODELS:
                    fdf = frames[model]
                    if fdf is None:
                        continue
                    res = _fit_one(ret_w, fdf, model)
                    if res is None:
                        continue
                    any_fit = True
                    beta, t = res["beta"], res["t"]
                    rows_out.append((
                        isin, model, res["window_end"], res["window_start"], region,
                        int(res["n_obs"]),
                        _sf(beta.get("alpha", 0.0) * 52.0),   # weekly alpha → annual
                        _sf(beta.get("MKT")), _sf(beta.get("SMB")), _sf(beta.get("HML")),
                        _sf(beta.get("MOM")), _sf(beta.get("RMW")), _sf(beta.get("CMA")),
                        _sf(t.get("MKT")), _sf(t.get("SMB")), _sf(t.get("HML")),
                        _sf(t.get("MOM")), _sf(t.get("RMW")), _sf(t.get("CMA")),
                        _sf(res["r2"]), _sf(res["adj_r2"]),
                    ))
                fitted += 1 if any_fit else 0
                skipped += 0 if any_fit else 1

        if rows_out:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO sec.fact_etf_factor_loadings
                        (isin, model, window_end, window_start, ff_region, n_obs, alpha,
                         beta_mkt, beta_smb, beta_hml, beta_mom, beta_rmw, beta_cma,
                         t_mkt, t_smb, t_hml, t_mom, t_rmw, t_cma, r2, adj_r2)
                    VALUES %s
                    ON CONFLICT (isin, model, window_end) DO UPDATE SET
                        window_start=EXCLUDED.window_start, ff_region=EXCLUDED.ff_region,
                        n_obs=EXCLUDED.n_obs, alpha=EXCLUDED.alpha,
                        beta_mkt=EXCLUDED.beta_mkt, beta_smb=EXCLUDED.beta_smb,
                        beta_hml=EXCLUDED.beta_hml, beta_mom=EXCLUDED.beta_mom,
                        beta_rmw=EXCLUDED.beta_rmw, beta_cma=EXCLUDED.beta_cma,
                        t_mkt=EXCLUDED.t_mkt, t_smb=EXCLUDED.t_smb, t_hml=EXCLUDED.t_hml,
                        t_mom=EXCLUDED.t_mom, t_rmw=EXCLUDED.t_rmw, t_cma=EXCLUDED.t_cma,
                        r2=EXCLUDED.r2, adj_r2=EXCLUDED.adj_r2, updated_at=NOW()
                    """,
                    rows_out,
                )
        return {
            "candidates": len(candidates),
            "equity_regioned": sum(len(v) for v in by_region.values()),
            "fitted": fitted,
            "skipped": skipped,
            "rows": len(rows_out),
        }
