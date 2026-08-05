"""Historical-simulation return distribution for an optimized book.

Given the optimizer's weights and a daily return panel of the same names, reconstruct
the portfolio's realized daily P&L and roll it up into overlapping horizon-length
windows — a non-parametric ("historical simulation") view of the distribution of
*possible returns over the forecast horizon* for that exact book. Summarized by its
first four moments (mean, variance, skewness, excess kurtosis), percentiles, a
histogram, and a Gaussian-KDE curve for a smooth overlay.

Pure numpy/scipy and DB-free, so it is unit-testable in isolation: the router loads
the long price history (``qlib_risk.load_price_returns``) and passes ``R`` + aligned
``weights`` here.
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

# Fixed calendar-to-trading-day factor. ~21 trading days/month; the horizon window is
# ``horizon_months * TRADING_DAYS_PER_MONTH`` days. Kept a constant (not data-derived)
# so the same book yields the same window regardless of the sampled date range.
TRADING_DAYS_PER_MONTH = 21.0


def _moment_summary(samples: np.ndarray) -> dict[str, float]:
    """Mean, (sample) variance, std, skewness and *excess* kurtosis of ``samples``."""
    from scipy import stats

    n = samples.shape[0]
    mean = float(np.mean(samples))
    var = float(np.var(samples, ddof=1)) if n > 1 else 0.0
    std = float(np.sqrt(var))
    # Bias-corrected skew/kurtosis; both need a non-degenerate spread and enough points.
    skew = float(stats.skew(samples, bias=False)) if std > 1e-12 and n > 2 else 0.0
    kurt = float(stats.kurtosis(samples, fisher=True, bias=False)) if std > 1e-12 and n > 3 else 0.0
    return {"mean": mean, "variance": var, "std": std, "skewness": skew, "kurtosis": kurt}


def _density_curve(samples: np.ndarray, grid: np.ndarray, std: float) -> np.ndarray:
    """Gaussian-KDE density on ``grid``; degrade to a matched normal if the KDE is
    singular (near-constant samples)."""
    from scipy import stats

    if std <= 1e-12:
        return np.zeros_like(grid)
    try:
        return stats.gaussian_kde(samples)(grid)
    except Exception:  # noqa: BLE001 - singular covariance etc. -> normal fallback
        return stats.norm.pdf(grid, loc=float(np.mean(samples)), scale=std)


def portfolio_return_distribution(
    R: np.ndarray,
    weights: Sequence[float],
    horizon_months: int,
    *,
    trading_days_per_month: float = TRADING_DAYS_PER_MONTH,
    num_bins: int = 36,
    kde_points: int = 96,
    min_samples: int = 30,
) -> dict[str, Any]:
    """Distribution of the book's horizon return by overlapping historical windows.

    ``R`` is a (T, N) daily simple-return matrix; ``weights`` (N,) must be aligned to
    ``R``'s columns. Returns ``{"available": False, "reason": ...}`` when there is too
    little overlapping history to form a horizon window.
    """
    R = np.asarray(R, dtype=float)
    w = np.asarray(weights, dtype=float).reshape(-1)
    if R.ndim != 2 or R.shape[0] < 2 or R.shape[1] != w.shape[0] or w.shape[0] == 0:
        return {"available": False, "reason": "no overlapping return history for these weights"}

    # Daily portfolio simple return under the fixed book (weights held constant).
    rp = R @ w
    rp = rp[np.isfinite(rp)]
    T = int(rp.shape[0])
    h = max(1, int(horizon_months))
    H = max(1, int(round(trading_days_per_month * h)))
    if T < H + min_samples:
        return {"available": False,
                "reason": f"need >= {H + min_samples} trading days for a {h}-month window; have {T}"}

    # Overlapping H-day compounded returns via a cumulative-log difference (O(T)).
    log1p = np.log1p(np.clip(rp, -0.999999, None))
    cs = np.concatenate([[0.0], np.cumsum(log1p)])
    samples = np.expm1(cs[H:] - cs[:-H])          # length T - H + 1
    samples = samples[np.isfinite(samples)]
    n = int(samples.shape[0])
    if n < min_samples:
        return {"available": False, "reason": "too few horizon windows for a distribution"}

    moments = _moment_summary(samples)
    std = moments["std"]
    mean = moments["mean"]

    percentiles = {f"p{k}": float(np.percentile(samples, k)) for k in (1, 5, 25, 50, 75, 95, 99)}

    # Annualized context: geometric mean return, sqrt-time volatility.
    ann_mean = ((1.0 + mean) ** (12.0 / h) - 1.0) if mean > -1.0 else -1.0
    ann_vol = std * float(np.sqrt(12.0 / h))

    # Histogram as a probability *density* so the KDE overlays on one shared y-axis.
    dens, edges = np.histogram(samples, bins=num_bins, density=True)
    raw, _ = np.histogram(samples, bins=edges)
    histogram = [
        {"x0": float(edges[i]), "x1": float(edges[i + 1]),
         "mid": float(0.5 * (edges[i] + edges[i + 1])),
         "density": float(dens[i]), "count": int(raw[i])}
        for i in range(len(dens))
    ]

    lo, hi = float(edges[0]), float(edges[-1])
    pad = 0.05 * ((hi - lo) or 1.0)
    grid = np.linspace(lo - pad, hi + pad, kde_points)
    curve = [{"x": float(x), "y": float(y)} for x, y in zip(grid, _density_curve(samples, grid, std))]

    return {
        "available": True,
        "method": "historical_simulation",
        "horizon_months": h,
        "n_obs": T,
        "n_samples": n,
        "window_days": H,
        "moments": moments,
        "annualized": {"mean": float(ann_mean), "vol": float(ann_vol)},
        "percentiles": percentiles,
        "histogram": histogram,
        "curve": curve,
    }
