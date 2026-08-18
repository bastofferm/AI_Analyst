"""Point-in-time reference data the research loop groups and neutralizes by.

Three maps, all loaded once per run and shared by the three consumers that need them:

* **GICS sector / industry group** — the sub-population cut the report breaks skill down by,
  and the confounder the robustness rating deconfounds against. Lives in ``dim_company_us`` /
  ``dim_company_jp`` and is used nowhere in ``api/quant`` today.
* **Market capitalization** — the size filter, the size-neutralization regressor, and the
  size buckets. Read point-in-time (``market_date <= month``, forward-filled) rather than
  "latest known", which is what ``qlib_backtest._liquid_instruments`` does; a latest-value
  join would leak today's size into a 2019 cross-section.
* **Fama-French betas** — the second sub-population cut, from ``fact_factor_loadings``, which
  already holds per-ticker rolling FF loadings computed by ``xbrl_sec.sec.sources.factor_model``.
  Selected with ``window_end <= month`` so a bucket assignment never sees a future regression.

Everything is sync (``pd.read_sql`` on the psycopg2 path) because the research loop runs on a
worker thread, not the event loop — the async twin in ``api.quant.risk.fetch_factor_loadings``
cannot be reused there. Every loader degrades to an empty frame rather than raising: a run
without GICS coverage still produces a report, it just reports the breakdown as unavailable.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("mzqa.quant.research.refdata")

# Preference order when several FF model variants are present for a ticker.
_FF_MODEL_PREFERENCE = ("FF6", "FF5", "FF4", "FF3")
FF_BETA_COLUMNS = ("beta_mkt", "beta_smb", "beta_hml", "beta_mom")

# Human labels for the FF exposure cuts, used as table titles in the report.
FF_BETA_LABELS = {
    "beta_mkt": "Market beta",
    "beta_smb": "Size exposure (SMB)",
    "beta_hml": "Value exposure (HML)",
    "beta_mom": "Momentum exposure",
}


def _strip_jp(series: pd.Series) -> pd.Series:
    """JP dimension rows carry a ``.T`` suffix; the panel's instrument does not."""
    return series.astype(str).str.replace(r"\.[Tt]$", "", regex=True)


def _dim_table(jurisdiction: str) -> str | None:
    juris = (jurisdiction or "US").upper().split(":")[0]
    return {"US": "dim_company_us", "JP": "dim_company_jp", "INTL": "dim_company_intl"}.get(juris)


def load_gics(jurisdiction: str = "US") -> pd.DataFrame:
    """``DataFrame[instrument, sector, industry_group]`` — empty if unavailable.

    Indexed by instrument so callers can ``reindex`` a cross-section straight onto it.
    """
    table = _dim_table(jurisdiction)
    if table is None:
        return pd.DataFrame(columns=["sector", "industry_group"]).rename_axis("instrument")
    try:
        from xbrl_sec.sec.db.connection import connect

        with connect() as conn:
            df = pd.read_sql(
                f"""
                SELECT primary_ticker,
                       NULLIF(TRIM(COALESCE(gics_sector_name, '')), '')         AS sector,
                       NULLIF(TRIM(COALESCE(gics_industry_group_name, '')), '') AS industry_group
                FROM   {table}
                WHERE  primary_ticker IS NOT NULL AND primary_ticker <> ''
                """,
                conn,
            )
    except Exception:  # noqa: BLE001 - the breakdown is advisory; never sink a run on it
        logger.warning("GICS load failed for %s", jurisdiction, exc_info=True)
        return pd.DataFrame(columns=["sector", "industry_group"]).rename_axis("instrument")
    if df.empty:
        return pd.DataFrame(columns=["sector", "industry_group"]).rename_axis("instrument")
    df["instrument"] = _strip_jp(df["primary_ticker"]).str.upper()
    df = df.drop(columns=["primary_ticker"]).drop_duplicates(subset=["instrument"])
    return df.set_index("instrument").sort_index()


def load_market_cap(
    jurisdiction: str = "US",
    *,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.Series:
    """Point-in-time market cap indexed by ``(datetime, instrument)`` (month-end).

    ``fact_market_metrics`` stores one row per fiscal period with the ``market_date`` it was
    observed on, so the series is sparse. We month-align, keep the last observation in each
    month, then forward-fill within each instrument — the value that was *knowable* at that
    month-end, never a later one.
    """
    juris = (jurisdiction or "US").upper().split(":")[0]
    try:
        from xbrl_sec.sec.db.connection import connect

        filters = ["metric_id = 'market_capitalization'", "value IS NOT NULL",
                   "market_date IS NOT NULL"]
        params: dict[str, Any] = {}
        if juris in ("US", "JP"):
            filters.append("jurisdiction = %(j)s")
            params["j"] = juris
        if start is not None:
            filters.append("market_date >= %(lo)s")
            params["lo"] = start
        if end is not None:
            filters.append("market_date <= %(hi)s")
            params["hi"] = end
        with connect() as conn:
            df = pd.read_sql(
                f"""
                SELECT ticker, market_date, value::float AS mcap
                FROM   fact_market_metrics
                WHERE  {' AND '.join(filters)}
                """,
                conn, params=params or None,
            )
    except Exception:  # noqa: BLE001
        logger.warning("market-cap load failed for %s", jurisdiction, exc_info=True)
        return pd.Series(dtype=float, name="mcap")
    if df.empty:
        return pd.Series(dtype=float, name="mcap")

    df["instrument"] = _strip_jp(df["ticker"]).str.upper()
    df["datetime"] = pd.to_datetime(df["market_date"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return pd.Series(dtype=float, name="mcap")
    df["datetime"] = df["datetime"].dt.to_period("M").dt.to_timestamp("M")
    # Last observation within each (instrument, month), then forward-fill the gaps.
    wide = (df.sort_values("datetime")
              .groupby(["datetime", "instrument"])["mcap"].last()
              .unstack("instrument")
              .sort_index()
              .ffill())
    out = wide.stack()
    out.index = out.index.set_names(["datetime", "instrument"])
    return out.rename("mcap").astype(float)


def load_ff_betas(
    jurisdiction: str = "US",
    *,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    """Point-in-time FF betas indexed by ``(datetime, instrument)`` (month-end).

    Mirrors the ``DISTINCT ON (ticker) ... window_end <= month`` selection that
    :func:`api.quant.risk.fetch_factor_loadings` performs per request, but vectorized: pull
    every rolling window in range, month-align on ``window_end``, then forward-fill so each
    month carries the most recent regression that had already been estimated by then.
    """
    juris = (jurisdiction or "US").upper().split(":")[0]
    cols = ", ".join(FF_BETA_COLUMNS)
    try:
        from xbrl_sec.sec.db.connection import connect

        filters = ["window_end IS NOT NULL"]
        params: dict[str, Any] = {}
        if juris in ("US", "JP"):
            filters.append("jurisdiction = %(j)s")
            params["j"] = juris
        if start is not None:
            # Reach back so the first month has a prior window to forward-fill from.
            filters.append("window_end >= %(lo)s")
            params["lo"] = (pd.Timestamp(start) - pd.DateOffset(years=2)).date()
        if end is not None:
            filters.append("window_end <= %(hi)s")
            params["hi"] = end
        with connect() as conn:
            df = pd.read_sql(
                f"""
                SELECT ticker, window_end, model, {cols}
                FROM   fact_factor_loadings
                WHERE  {' AND '.join(filters)}
                """,
                conn, params=params or None,
            )
    except Exception:  # noqa: BLE001
        logger.warning("FF-beta load failed for %s", jurisdiction, exc_info=True)
        return pd.DataFrame(columns=list(FF_BETA_COLUMNS))
    if df.empty:
        return pd.DataFrame(columns=list(FF_BETA_COLUMNS))

    # One model variant per ticker: the richest available.
    rank = {m: i for i, m in enumerate(_FF_MODEL_PREFERENCE)}
    df["_rank"] = df["model"].astype(str).str.upper().map(rank).fillna(len(rank))
    df = df.sort_values("_rank").drop_duplicates(subset=["ticker", "window_end"], keep="first")

    df["instrument"] = _strip_jp(df["ticker"]).str.upper()
    df["datetime"] = pd.to_datetime(df["window_end"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return pd.DataFrame(columns=list(FF_BETA_COLUMNS))
    df["datetime"] = df["datetime"].dt.to_period("M").dt.to_timestamp("M")

    frames = {}
    for col in FF_BETA_COLUMNS:
        wide = (df.sort_values("datetime")
                  .groupby(["datetime", "instrument"])[col].last()
                  .unstack("instrument").sort_index().ffill())
        frames[col] = wide.stack()
    out = pd.DataFrame(frames)
    out.index = out.index.set_names(["datetime", "instrument"])
    return out.astype(float).sort_index()


# --------------------------------------------------------------------------- #
# Bucketing
# --------------------------------------------------------------------------- #
def quantile_buckets(values: pd.Series, n: int = 5, *, labels: str = "Q") -> pd.Series:
    """Assign each ``(datetime, instrument)`` a within-date quantile bucket label.

    Bucketing *within each date* is what makes the FF-exposure cut meaningful: a fixed
    global cut point would put the whole panel in one bucket whenever the factor drifts.
    Dates with too few distinct values to split fall back to a single "all" bucket rather
    than raising, which happens on thin INTL cross-sections.
    """
    if values.empty:
        return pd.Series(dtype=object)

    def _cut(group: pd.Series) -> pd.Series:
        clean = group.dropna()
        if clean.nunique() < n:
            return pd.Series(np.nan, index=group.index, dtype=object)
        try:
            codes = pd.qcut(clean, n, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series(np.nan, index=group.index, dtype=object)
        top = int(codes.max()) + 1
        out = codes.map(lambda c: f"{labels}{int(c) + 1} of {top}")
        return out.reindex(group.index)

    return values.groupby(level="datetime", group_keys=False).apply(_cut)
