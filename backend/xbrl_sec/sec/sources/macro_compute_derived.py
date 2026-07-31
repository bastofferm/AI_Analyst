"""Derived macro series — written under source_id='compute'.

Produces derived series in ``fact_macro`` after upstream raw series have
refreshed. Each output series_id must already be present in ``ref_macro_series``
(see migration 070_macro_nowcasts_liquidity.sql).

Outputs:
  * ``COMPUTE:NETLIQ``            — Fed net liquidity   = WALCL − WTREGEN − RRPONTSYD (weekly, USD mln)
  * ``COMPUTE:GLOBAL_LIQ``        — Global CB liquidity = Fed + ECB + BOJ (USD-converted, weekly, USD trn)
  * ``COMPUTE:JP_TANKAN_FACTOR``  — PCA of BOJ Tankan large mfg/non-mfg DIs (quarterly, std units)
  * ``COMPUTE:JP_FSI``            — Japan financial-stress index (z-mean of JGB 2s10s spread, USD/JPY return vol, BOJ HY proxy if available)

The job is incremental by default: each output is recomputed only for dates
where a new input observation has appeared since the last write. ``--full``
recomputes the entire history.

Run:
    python -m xbrl_sec.sec.sources.macro_compute_derived
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
from datetime import date, datetime

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running
from xbrl_sec.sec.sources.boj_ingest import _upsert

logger = logging.getLogger("mzqa.macro_compute_derived")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _series(series_id: str) -> pd.Series:
    """Pull a single series from fact_macro as a date-indexed Series."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT date, value FROM fact_macro WHERE series_id = %s ORDER BY date",
            (series_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.Series([float(r[1]) if r[1] is not None else np.nan for r in rows], index=idx, name=series_id).dropna()


def _write(series_id: str, s: pd.Series) -> int:
    rows = [(d.date(), float(v)) for d, v in s.items() if not pd.isna(v)]
    if rows:
        _upsert(series_id, rows)
    return len(rows)


def _delete_after(series_id: str, cutoff: date) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM fact_macro WHERE series_id = %s AND date > %s",
            (series_id, cutoff),
        )


def _yoy_percent(s: pd.Series) -> pd.Series:
    """Year-over-year percent change using the latest observation at or before T-1Y."""
    if s.empty:
        return pd.Series(dtype=float)
    s = s.sort_index().dropna()
    out: list[tuple[pd.Timestamp, float]] = []
    for dt, value in s.items():
        prev_window = s.loc[: dt - pd.DateOffset(years=1)]
        if prev_window.empty:
            continue
        prev = float(prev_window.iloc[-1])
        if prev == 0 or pd.isna(prev):
            continue
        out.append((dt, (float(value) - prev) / abs(prev) * 100.0))
    if not out:
        return pd.Series(dtype=float)
    return pd.Series([v for _, v in out], index=pd.to_datetime([d for d, _ in out]))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _probability_0_1(s: pd.Series) -> pd.Series:
    """Normalize percent/decimal probabilities to 0-1 and clamp outliers."""
    if s.empty:
        return pd.Series(dtype=float)
    out = s.astype(float).copy()
    if out.dropna().max() > 1.5:
        out = out / 100.0
    return out.clip(lower=0.0, upper=1.0).dropna()


def _month_end(ts: pd.Timestamp | date | datetime) -> date:
    d = ts.date() if hasattr(ts, "date") else ts
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _latest_value(series_id: str) -> tuple[date | None, float | None]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT date, value FROM fact_macro WHERE series_id = %s ORDER BY date DESC LIMIT 1",
            (series_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], float(row[1]) if row[1] is not None else None


# ---------------------------------------------------------------------------
# 1. Fed net liquidity
# ---------------------------------------------------------------------------

def compute_netliq() -> int:
    """COMPUTE:NETLIQ = WALCL − WTREGEN − RRPONTSYD.

    WALCL/WTREGEN are weekly (USD mln); RRPONTSYD is daily (USD bln). We
    align on WALCL's index (weekly Wednesdays) by forward-filling the other
    series. RRPONTSYD is converted to USD millions (* 1000) for unit
    consistency. Output unit: millions USD.
    """
    walcl = _series("FRED:WALCL")
    tga = _series("FRED:WTREGEN")
    rrp = _series("FRED:RRPONTSYD")
    if walcl.empty:
        logger.warning("compute_netliq: WALCL series empty — skipping")
        return 0
    rrp_mn = rrp * 1000.0
    aligned = pd.concat({"walcl": walcl, "tga": tga, "rrp": rrp_mn}, axis=1).sort_index().ffill()
    aligned = aligned.dropna(subset=["walcl"])
    netliq = aligned["walcl"] - aligned["tga"].fillna(0) - aligned["rrp"].fillna(0)
    return _write("COMPUTE:NETLIQ", netliq)


# ---------------------------------------------------------------------------
# 2. Global central-bank liquidity
# ---------------------------------------------------------------------------

def compute_global_liq() -> int:
    """COMPUTE:GLOBAL_LIQ = Fed (WALCL) + ECB (BS_TOTAL) + BOJ (BS_TOTAL), USD-converted, USD trillions.

    Unit conventions (must match ref_macro_series.units for each input):
      - FRED:WALCL       = USD millions
      - ECB:BS_TOTAL     = EUR millions (ECB SDMX ILM.W.U2.C.A050000.U2.EUR)
      - BOJ:BS_TOTAL     = 100 million JPY (FRED proxy JPNASSETS)
      - FRED:DEXUSEU     = USD per EUR
      - FRED:DEXJPUS     = JPY per USD
    """
    fed = _series("FRED:WALCL")              # USD millions
    ecb = _series("ECB:BS_TOTAL")            # EUR millions
    boj = _series("BOJ:BS_TOTAL")            # 100 million JPY (FRED JPNASSETS)
    eur_usd = _series("FRED:DEXUSEU")        # USD per EUR
    jpy_usd = _series("FRED:DEXJPUS")        # JPY per USD

    if fed.empty:
        logger.warning("compute_global_liq: WALCL empty — skipping")
        return 0

    # Re-align all auxiliary series on fed's DatetimeIndex. An empty input
    # has a default RangeIndex (int64) which would crash .reindex() against a
    # DatetimeIndex — replace any empty with a same-index NaN series first.
    def _align(s: pd.Series) -> pd.Series:
        if s.empty:
            return pd.Series(np.nan, index=fed.index)
        return s.reindex(fed.index, method="ffill")

    eur_usd = _align(eur_usd)
    jpy_usd = _align(jpy_usd).replace(0, np.nan)
    ecb = _align(ecb)
    boj = _align(boj)

    fed_trn = fed / 1e6                       # USD trillions
    ecb_trn = (ecb * eur_usd) / 1e6           # USD trillions
    boj_trn = (boj * 1e8 / jpy_usd) / 1e12    # 100M JPY → JPY → USD → trn

    glob = fed_trn.fillna(0) + ecb_trn.fillna(0) + boj_trn.fillna(0)
    return _write("COMPUTE:GLOBAL_LIQ", glob)


# ---------------------------------------------------------------------------
# 3. JP Tankan factor (PCA of large-mfg + large-non-mfg DIs)
# ---------------------------------------------------------------------------

def compute_jp_tankan_factor() -> int:
    """First principal component of two Tankan diffusion indices.

    Both series are quarterly. We z-score then take the equal-weighted sum
    (sign-corrected so that higher = stronger business conditions) as a
    cheap PCA proxy — the 2-series PCA reduces to ±(z1+z2)/√2 with sign
    governed by sample variance, indistinguishable from the simple mean at
    this dimensionality.
    """
    lmfg = _series("BOJ:TANKAN_LMFG")
    lnmfg = _series("BOJ:TANKAN_LNMFG")
    if lmfg.empty and lnmfg.empty:
        logger.warning("compute_jp_tankan_factor: no Tankan inputs — skipping")
        return 0
    wide = pd.concat({"lmfg": lmfg, "lnmfg": lnmfg}, axis=1).dropna(how="all")
    z = (wide - wide.mean()) / wide.std(ddof=0).replace(0, np.nan)
    z = z.fillna(0)
    factor = z.mean(axis=1)
    return _write("COMPUTE:JP_TANKAN_FACTOR", factor)


# ---------------------------------------------------------------------------
# 4. Japan financial stress index
# ---------------------------------------------------------------------------

def compute_jp_fsi() -> int:
    """Japan FSI = mean z-score of:
      - JGB 10Y - call rate spread (inverted: tight = stress)
      - 20-day realised vol of USD/JPY return
      - 20-day realised vol of TOPIX (via index proxy)

    All inputs daily. Output unit: standard deviations.
    """
    jgb10 = _series("MOF_JP:JGB_10Y")
    call = _series("BOJ:IR01_OCRT")
    usdjpy = _series("FRED:DEXJPUS")

    components: dict[str, pd.Series] = {}

    if not jgb10.empty and not call.empty:
        spread = (jgb10 - call.reindex(jgb10.index, method="ffill")).dropna()
        components["spread_inv"] = -spread

    if not usdjpy.empty:
        ret = np.log(usdjpy / usdjpy.shift(1)).dropna()
        rv = ret.rolling(20).std() * np.sqrt(252)
        components["usdjpy_rv"] = rv.dropna()

    if not components:
        logger.warning("compute_jp_fsi: no inputs available — skipping")
        return 0

    aligned = pd.concat(components, axis=1).dropna(how="all").ffill()
    z = (aligned - aligned.mean()) / aligned.std(ddof=0).replace(0, np.nan)
    fsi = z.mean(axis=1).dropna()
    return _write("COMPUTE:JP_FSI", fsi)


# ---------------------------------------------------------------------------
# 5. Yield-curve slopes
# ---------------------------------------------------------------------------

def compute_jp_2s10s() -> int:
    two_y = _series("MOF_JP:JGB_2Y")
    ten_y = _series("MOF_JP:JGB_10Y")
    if two_y.empty or ten_y.empty:
        logger.warning("compute_jp_2s10s: missing JGB 2Y or 10Y inputs - skipping")
        return 0
    spread = ten_y - two_y.reindex(ten_y.index, method="ffill")
    return _write("COMPUTE:JP_2S10S", spread.dropna())


def compute_ez_2s10s() -> int:
    two_y = _series("ECB:BUND_2Y")
    ten_y = _series("ECB:BUND_10Y")
    if two_y.empty or ten_y.empty:
        logger.warning("compute_ez_2s10s: missing EA 2Y or 10Y inputs - skipping")
        return 0
    spread = ten_y - two_y.reindex(ten_y.index, method="ffill")
    return _write("COMPUTE:EZ_2S10S", spread.dropna())


# ---------------------------------------------------------------------------
# 6. Japan YoY derived rates
# ---------------------------------------------------------------------------

def compute_jp_cgpi_yoy() -> int:
    return _write("COMPUTE:JP_CGPI_YOY", _yoy_percent(_series("BOJ:CGPI_ALL")))


def compute_jp_sppi_yoy() -> int:
    return _write("COMPUTE:JP_SPPI_YOY", _yoy_percent(_series("BOJ:SPPI_ALL")))


def compute_jp_bank_loans_yoy() -> int:
    return _write("COMPUTE:JP_BANK_LOANS_YOY", _yoy_percent(_series("BOJ:BANK_LOANS_TOTAL")))


def compute_jp_public_debt_yoy() -> int:
    return _write("COMPUTE:JP_PUBLIC_DEBT_YOY", _yoy_percent(_series("BOJ:NATIONAL_GOV_DEBT")))


# ---------------------------------------------------------------------------
# 7. State probability / proxy outputs
# ---------------------------------------------------------------------------

def compute_us_recession_prob_ms_dfm() -> int:
    return _write("COMPUTE:US_RECESSION_PROB_MS_DFM", _probability_0_1(_series("FRED:RECPROUSM156N")))


def compute_us_recession_prob_gdp_hamilton() -> int:
    return _write("COMPUTE:US_RECESSION_PROB_GDP_HAMILTON", _probability_0_1(_series("FRED:JHGDPBRINDX")))


def compute_us_recession_prob_12m_nyfed() -> int:
    raw = _probability_0_1(_series("NYFED:YC_RECESSION_12M_RAW"))
    if raw.empty:
        return 0
    # NY Fed's file dates the probability to the 12-month-ahead target. The
    # dashboard needs the model vintage/as-of month, so shift the visible
    # series back one year while preserving the raw target-date row.
    vintage = raw.copy()
    vintage.index = vintage.index - pd.DateOffset(years=1)
    vintage = vintage.groupby(vintage.index).last().sort_index()
    if not vintage.empty:
        _delete_after("COMPUTE:US_RECESSION_PROB_12M_NYFED", vintage.index.max().date())
    return _write("COMPUTE:US_RECESSION_PROB_12M_NYFED", vintage)


def compute_ez_state_risk_ciss() -> int:
    ciss = _probability_0_1(_series("ECB:CISS_EA_NEW"))
    if ciss.empty:
        return 0
    monthly = ciss.resample("ME").last().dropna()
    return _write("COMPUTE:EZ_STATE_RISK_CISS", monthly)


def compute_jp_ci_recession_proxy() -> int:
    ci = _series("CAO_JP:CI_COIN").sort_index().dropna()
    if ci.empty:
        logger.warning("compute_jp_ci_recession_proxy: CAO_JP:CI_COIN empty - skipping")
        return 0
    monthly_change = ci.diff()
    rolling_change = monthly_change.rolling(3).mean()
    persistent_negative = (rolling_change < 0).rolling(3).sum() >= 3
    below_diffusion_trigger = ci < 50
    proxy = (persistent_negative | below_diffusion_trigger).astype(float).dropna()
    return _write("COMPUTE:JP_CI_RECESSION_PROXY", proxy)


# ---------------------------------------------------------------------------
# 8. Unified regional cycle assessment
# ---------------------------------------------------------------------------

CYCLE_REGIONS = ("US", "JP", "EZ")
CYCLE_CATEGORY_TOPICS: dict[str, str] = {
    "liquidity": "liquidity",
    "money_supply": "liquidity",
    "rates": "interest_rates",
    "credit": "debt",
    "inflation": "inflation",
    "growth": "growth",
    "activity": "growth",
    "nowcast": "growth",
    "sentiment": "growth",
    "debt": "debt",
    "labor": "labor",
    "state_probability": "business_cycle",
    "state_proxy": "business_cycle",
    "financial_stress": "business_cycle",
}


def _signal_rows(jurisdiction: str) -> list[dict]:
    juris = jurisdiction.upper()
    if juris == "GLOBAL":
        juris_filter = "s.jurisdiction IN ('US','JP','EZ','XX')"
        params: tuple = ()
    else:
        juris_filter = "s.jurisdiction = %s"
        params = (juris,)
    sql = f"""
        WITH targets AS (
            SELECT series_id, story_tile_slot, name, jurisdiction, category, units, frequency
            FROM   ref_macro_series s
            WHERE  s.is_active = TRUE
              AND  s.importance <= 3
              AND  s.story_tile_slot IS NOT NULL
              AND  {juris_filter}
        ),
        latest AS (
            SELECT DISTINCT ON (f.series_id) f.series_id, f.date, f.value
            FROM   fact_macro f
            WHERE  f.series_id IN (SELECT series_id FROM targets)
              AND  f.value IS NOT NULL
            ORDER  BY f.series_id, f.date DESC
        ),
        prev AS (
            SELECT DISTINCT ON (f.series_id) f.series_id, f.value AS prev_value
            FROM   fact_macro f
            JOIN   latest l ON l.series_id = f.series_id
            WHERE  f.date <= l.date - INTERVAL '1 year'
            ORDER  BY f.series_id, f.date DESC
        ),
        history AS (
            SELECT f.series_id, array_agg(f.value ORDER BY f.date ASC) AS values
            FROM   fact_macro f
            JOIN   latest l ON l.series_id = f.series_id
            WHERE  f.date >= l.date - INTERVAL '3 years'
              AND  f.value IS NOT NULL
            GROUP  BY f.series_id
        )
        SELECT t.series_id, t.story_tile_slot, t.name, t.jurisdiction, t.category,
               t.units, t.frequency, l.date, l.value, p.prev_value, h.values
        FROM   targets t
        LEFT   JOIN latest l ON l.series_id = t.series_id
        LEFT   JOIN prev p ON p.series_id = t.series_id
        LEFT   JOIN history h ON h.series_id = t.series_id
        WHERE  l.value IS NOT NULL
        ORDER  BY t.jurisdiction, t.category, t.story_tile_slot
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _feature_score(row: dict) -> float:
    value = row.get("value")
    if value is None:
        return 0.0
    history = [float(v) for v in (row.get("values") or []) if v is not None]
    current = float(value)
    if len(history) >= 6:
        pctile = sum(1 for v in history if v <= current) / len(history)
        raw = pctile * 2.0 - 1.0
    else:
        prev = row.get("prev_value")
        if prev in (None, 0):
            raw = 0.0
        else:
            raw = _clamp((current - float(prev)) / abs(float(prev)), -0.25, 0.25) * 4.0

    category = str(row.get("category") or "").lower()
    slot = str(row.get("story_tile_slot") or "").lower()
    name = str(row.get("name") or "").lower()

    invert = category in {"inflation", "debt", "credit", "state_probability", "state_proxy", "financial_stress"}
    if category == "rates":
        invert = not ("2s10s" in slot or "curve" in slot or "spread" in name and "yield spread" in name)
    if category == "labor":
        invert = "unemployment" in slot or "unemployment" in name
    if category == "liquidity" or category == "money_supply":
        invert = False
    if category in {"growth", "activity", "nowcast", "sentiment"}:
        invert = False

    return _clamp(-raw if invert else raw, -1.0, 1.0)


def _bucket_for(row: dict) -> str:
    category = str(row.get("category") or "").lower()
    if category in {"liquidity", "money_supply"}:
        return "liquidity"
    if category in {"rates", "credit"}:
        return "rates"
    if category == "inflation":
        return "inflation"
    if category in {"growth", "activity", "nowcast", "sentiment"}:
        return "growth"
    if category == "debt":
        return "debt_credit"
    if category == "labor":
        return "labor"
    if category in {"state_probability", "state_proxy", "financial_stress"}:
        return "state"
    return "other"


def _format_driver_value(row: dict) -> str:
    value = row.get("value")
    if value is None:
        return "-"
    units = str(row.get("units") or "").lower()
    category = str(row.get("category") or "").lower()
    v = float(value)
    if "probability" in units or "0-1" in units or category in {"state_probability", "state_proxy", "financial_stress"}:
        return f"{v * 100.0:.1f}%"
    if "percent" in units or "%" in units:
        return f"{v:.2f}%"
    if "bp" in units:
        return f"{v:.0f} bp"
    if "index" in units or "normalised" in units or "amp" in units:
        return f"{v:.1f}"
    if abs(v) >= 1e12:
        return f"{v / 1e12:.1f}T"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    return f"{v:,.2f}"


def _tone(score: float) -> str:
    if score > 0.18:
        return "green"
    if score < -0.18:
        return "red"
    return "amber"


def _phase(score: float, recession_probability: float | None, buckets: dict[str, float]) -> str:
    growth = buckets.get("growth", 0.0)
    pressure = np.nanmean([buckets.get("rates", np.nan), buckets.get("inflation", np.nan), buckets.get("debt_credit", np.nan)])
    if recession_probability is not None and recession_probability >= 0.67:
        return "contraction"
    if score <= 32.0:
        return "contraction"
    if growth < -0.25 or score < 44.0:
        return "slowdown"
    if growth > 0.18 and pressure < -0.18:
        return "late_cycle"
    if score >= 58.0 and growth >= 0.0:
        return "expansion"
    if score >= 52.0 and (recession_probability is None or recession_probability < 0.35):
        return "recovery"
    return "mixed"


def _normalize_prob(value: float | None) -> float | None:
    if value is None:
        return None
    v = float(value)
    if v > 1.5:
        v = v / 100.0
    return _clamp(v, 0.0, 1.0)


def _regional_recession_probability(jurisdiction: str, assessments: dict[str, dict] | None = None) -> float | None:
    j = jurisdiction.upper()
    if j == "GLOBAL":
        vals = [
            a.get("recession_probability")
            for key, a in (assessments or {}).items()
            if key in CYCLE_REGIONS and a.get("recession_probability") is not None
        ]
        return float(np.mean(vals)) if vals else None

    if j == "US":
        families = [
            ("ms_dfm", 0.45, ("COMPUTE:US_RECESSION_PROB_MS_DFM", "FRED:RECPROUSM156N")),
            ("hamilton", 0.25, ("COMPUTE:US_RECESSION_PROB_GDP_HAMILTON", "FRED:JHGDPBRINDX")),
            ("nyfed", 0.30, ("COMPUTE:US_RECESSION_PROB_12M_NYFED", "NYFED:YC_RECESSION_12M_RAW")),
        ]
        total_weight = 0.0
        weighted = 0.0
        for _family, weight, series_ids in families:
            p = None
            for sid in series_ids:
                _, raw = _latest_value(sid)
                p = _normalize_prob(raw)
                if p is not None:
                    break
            if p is not None:
                total_weight += weight
                weighted += p * weight
        if total_weight:
            return _clamp(weighted / total_weight, 0.0, 1.0)
    if j == "EZ":
        for sid in ("COMPUTE:EZ_STATE_RISK_CISS", "ECB:CISS_EA_NEW"):
            _, raw = _latest_value(sid)
            p = _normalize_prob(raw)
            if p is not None:
                return p
    if j == "JP":
        _, raw = _latest_value("COMPUTE:JP_CI_RECESSION_PROXY")
        return _normalize_prob(raw)
    return None


def _summary_text(jurisdiction: str, phase: str, score: float, recession_probability: float | None, drivers: list[dict]) -> str:
    if not drivers:
        return "Cycle assessment is not yet populated for this region."
    tone = "green" if score >= 58 else "red" if score < 44 else "amber"
    headline = f"[[{tone}:{phase.replace('_', ' ').title()}]]"
    risk = "state-risk proxy" if jurisdiction in {"JP", "EZ"} else "recession probability"
    prob = f"{recession_probability * 100.0:.1f}%" if recession_probability is not None else "not available"
    top = drivers[0]
    second = drivers[1] if len(drivers) > 1 else None
    if second:
        return f"{headline} cycle score is **{score:.0f}/100** with {risk} at **{prob}**. The main drivers are **{top['label']}** and **{second['label']}**."
    return f"{headline} cycle score is **{score:.0f}/100** with {risk} at **{prob}**. The main driver is **{top['label']}**."


def _assess_region(jurisdiction: str, prior: dict[str, dict] | None = None) -> dict:
    rows = _signal_rows(jurisdiction)
    buckets_raw: dict[str, list[float]] = {}
    driver_candidates: list[dict] = []
    latest_dates: list[date] = []

    for row in rows:
        score = _feature_score(row)
        bucket = _bucket_for(row)
        if bucket == "other":
            continue
        buckets_raw.setdefault(bucket, []).append(score)
        if row.get("date"):
            latest_dates.append(row["date"])
        topic = CYCLE_CATEGORY_TOPICS.get(str(row.get("category") or "").lower(), bucket)
        driver_candidates.append({
            "topic": topic,
            "bucket": bucket,
            "series_id": row.get("series_id"),
            "slot": row.get("story_tile_slot"),
            "label": row.get("name"),
            "value": float(row["value"]) if row.get("value") is not None else None,
            "value_str": _format_driver_value(row),
            "as_of": row["date"].isoformat() if row.get("date") else None,
            "score": round(score, 4),
            "tone": _tone(score),
            "text": f"**{row.get('name')}:** {_format_driver_value(row)}",
        })

    bucket_scores = {k: float(np.mean(v)) for k, v in buckets_raw.items() if v}
    core_keys = ["liquidity", "rates", "inflation", "growth", "debt_credit", "labor"]
    available_core = [bucket_scores[k] for k in core_keys if k in bucket_scores]
    aggregate = float(np.mean(available_core)) if available_core else 0.0
    score = _clamp(50.0 + aggregate * 35.0, 0.0, 100.0)
    recession_probability = _regional_recession_probability(jurisdiction, prior)
    if recession_probability is None and available_core:
        recession_probability = _clamp(0.5 - aggregate * 0.35, 0.0, 1.0)

    phase = _phase(score, recession_probability, bucket_scores)
    driver_candidates.sort(key=lambda d: abs(float(d["score"])), reverse=True)
    drivers = driver_candidates[:4]
    coverage = len([k for k in core_keys if k in bucket_scores]) / len(core_keys)
    confidence = _clamp(0.25 + coverage * 0.6 + (0.15 if recession_probability is not None else 0.0), 0.0, 1.0)
    as_of = max(latest_dates) if latest_dates else date.today()
    period_end = _month_end(as_of)
    summary = _summary_text(jurisdiction, phase, score, recession_probability, drivers)

    return {
        "jurisdiction": jurisdiction,
        "period_end": period_end,
        "phase": phase,
        "score": round(score, 2),
        "recession_probability": round(float(recession_probability), 6) if recession_probability is not None else None,
        "confidence": round(confidence, 4),
        "drivers_json": {
            "drivers": drivers,
            "category_scores": {k: round(v, 4) for k, v in bucket_scores.items()},
            "coverage": round(coverage, 4),
            "summary": summary,
        },
    }


def compute_cycle_assessments() -> int:
    assessments: dict[str, dict] = {}
    for jurisdiction in CYCLE_REGIONS:
        assessments[jurisdiction] = _assess_region(jurisdiction)
    assessments["GLOBAL"] = _assess_region("GLOBAL", assessments)

    rows = list(assessments.values())
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM fact_macro_cycle_assessment WHERE period_end > CURRENT_DATE")
        for item in rows:
            cur.execute(
                """
                INSERT INTO fact_macro_cycle_assessment
                    (jurisdiction, period_end, phase, score, recession_probability, confidence, drivers_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (jurisdiction, period_end) DO UPDATE SET
                    phase = EXCLUDED.phase,
                    score = EXCLUDED.score,
                    recession_probability = EXCLUDED.recession_probability,
                    confidence = EXCLUDED.confidence,
                    drivers_json = EXCLUDED.drivers_json,
                    updated_at = now()
                """,
                (
                    item["jurisdiction"],
                    item["period_end"],
                    item["phase"],
                    item["score"],
                    item["recession_probability"],
                    item["confidence"],
                    json.dumps(item["drivers_json"]),
                ),
            )
    return len(rows)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run(full: bool = False) -> dict[str, int]:
    """Run derived computations. ``full`` is informational — the
    upserts are idempotent so a full rerun is equivalent to overwriting all
    rows back to history start."""
    out: dict[str, int] = {}
    with market_run("compute", full, {"derived_series": 16}) as ctx:
        for fn, sid in [
            (compute_netliq,             "COMPUTE:NETLIQ"),
            (compute_global_liq,         "COMPUTE:GLOBAL_LIQ"),
            (compute_jp_tankan_factor,   "COMPUTE:JP_TANKAN_FACTOR"),
            (compute_jp_fsi,             "COMPUTE:JP_FSI"),
            (compute_jp_2s10s,           "COMPUTE:JP_2S10S"),
            (compute_ez_2s10s,           "COMPUTE:EZ_2S10S"),
            (compute_jp_cgpi_yoy,        "COMPUTE:JP_CGPI_YOY"),
            (compute_jp_sppi_yoy,        "COMPUTE:JP_SPPI_YOY"),
            (compute_jp_bank_loans_yoy,  "COMPUTE:JP_BANK_LOANS_YOY"),
            (compute_jp_public_debt_yoy, "COMPUTE:JP_PUBLIC_DEBT_YOY"),
            (compute_us_recession_prob_ms_dfm, "COMPUTE:US_RECESSION_PROB_MS_DFM"),
            (compute_us_recession_prob_gdp_hamilton, "COMPUTE:US_RECESSION_PROB_GDP_HAMILTON"),
            (compute_us_recession_prob_12m_nyfed, "COMPUTE:US_RECESSION_PROB_12M_NYFED"),
            (compute_ez_state_risk_ciss, "COMPUTE:EZ_STATE_RISK_CISS"),
            (compute_jp_ci_recession_proxy, "COMPUTE:JP_CI_RECESSION_PROXY"),
            (compute_cycle_assessments, "CYCLE_ASSESSMENT"),
        ]:
            mark_item_running(ctx, "compute", sid)
            try:
                n = fn()
                out[sid] = n
                mark_item_done(ctx, "compute", sid, status="succeeded" if n else "skipped", rows_in=n, rows_out=n)
            except Exception as exc:
                logger.exception("macro_compute_derived %s failed: %s", sid, exc)
                out[sid] = 0
                mark_item_done(ctx, "compute", sid, status="failed", error=str(exc)[:4000])
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="Reload all history (informational)")
    args = p.parse_args()
    out = run(full=args.full)
    import json as _json
    print(_json.dumps(out))


if __name__ == "__main__":
    main()
