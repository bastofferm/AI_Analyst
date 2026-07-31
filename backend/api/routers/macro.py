"""Macro signals endpoints — DB-driven slot resolver over fact_macro.

Replaces the previous 6-series FRED hardcode. Now serves bilingual tiles,
essays, regime, calendar, sector-beta and curve via ref_macro_series.
"""
from __future__ import annotations

import logging
import json
import calendar
from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime
from ..db import acquire
from xbrl_sec.sec.cycle.phase import NBER_US_RECESSION_WINDOWS, apply_display_label_policy

router = APIRouter()
logger = logging.getLogger("mzqa.macro")

Lang = Literal["en", "de"]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class MacroSignal(BaseModel):
    series_id: str
    slot: str | None = None
    label: str
    jurisdiction: str
    category: str
    value: float | None
    value_str: str
    direction: Literal["up", "dn", "neu"]
    note: str
    as_of: str
    units: str | None = None
    frequency: str | None = None
    trend_window_label: str
    value_series: list[float] = []
    caption: str | None = None   # bilingual LLM caption (en|de via ?lang)
    # Optional dual-view fields (populated for inflation-category index series
    # so the home tile can toggle IDX <-> % Chg).
    value_label: str | None = None       # label for value_str (e.g. "IDX")
    value_alt_str: str | None = None     # alternate display (e.g. "+2.43%")
    value_alt_label: str | None = None   # label for value_alt_str (e.g. "% Chg")
    state_active: bool | None = None
    state_threshold: float | None = None
    state_label: str | None = None


class MacroEssay(BaseModel):
    scope: str
    scope_key: str
    lang: str
    text: str
    body_md: str | None = None
    bullets: list[dict] = Field(default_factory=list)
    generated_at: str | None
    model: str | None


class MacroRegime(BaseModel):
    factor_id: str
    date: str | None
    value: float | None
    percentile: float | None
    regime_label: str | None
    top_loadings: list[dict] = Field(default_factory=list)


class MacroCurvePoint(BaseModel):
    tenor: str
    yield_pct: float | None


class MacroCurve(BaseModel):
    jurisdiction: str
    as_of: str
    points: list[MacroCurvePoint]
    two_s_ten_s_bp: float | None
    flow_caption: str | None = None


class MacroCurveHistory(BaseModel):
    jurisdiction: str
    as_of: str | None
    dates: list[str]
    tenors: list[str]
    # grid[i][j] = yield (in %) on dates[i] at tenors[j]; None if missing.
    grid: list[list[float | None]]


class MacroCalendarItem(BaseModel):
    series_id: str
    label: str
    jurisdiction: str
    release_at: str
    period_end: str
    value: float | None


class MacroSectorBetaCell(BaseModel):
    sector: str
    factor: str
    beta: float | None
    t_stat: float | None


class MacroCycleDriver(BaseModel):
    topic: str | None = None
    bucket: str | None = None
    series_id: str | None = None
    slot: str | None = None
    label: str
    value: float | None = None
    value_str: str | None = None
    as_of: str | None = None
    score: float | None = None
    tone: Literal["green", "red", "amber", "blue"] | None = None
    text: str | None = None


class MacroCycleAssessment(BaseModel):
    jurisdiction: str
    phase: Literal["expansion", "late_cycle", "slowdown", "contraction", "recovery", "mixed"]
    score: float | None = None
    recession_probability: float | None = None
    confidence: float | None = None
    as_of: str | None = None
    drivers: list[MacroCycleDriver] = Field(default_factory=list)
    summary: str | None = None
    category_scores: dict[str, float] = Field(default_factory=dict)


class MacroDashboardIndexPoint(BaseModel):
    date: str
    close: float


class MacroDashboardIndexSeries(BaseModel):
    ticker: str | None = None
    name: str | None = None
    points: list[MacroDashboardIndexPoint] = Field(default_factory=list)


class MacroDashboardOverlayPeriod(BaseModel):
    kind: str
    label: str
    start: str
    end: str
    tone: Literal["green", "red", "amber", "blue"]
    source_series_id: str | None = None
    source_label: str | None = None


class MacroDashboardRegion(BaseModel):
    jurisdiction: str
    label: str
    cycle: MacroCycleAssessment | None = None
    signals: list[MacroSignal] = Field(default_factory=list)
    index: MacroDashboardIndexSeries = Field(default_factory=MacroDashboardIndexSeries)
    overlays: list[MacroDashboardOverlayPeriod] = Field(default_factory=list)
    story_bullets: list[str] = Field(default_factory=list)


class MacroDashboardResponse(BaseModel):
    regions: list[MacroDashboardRegion]
    generated_at: str


class CycleAnchorLabels(BaseModel):
    quadrant: str | None = None
    quadrant_as_of: str | None = None
    quadrant_growth_z: float | None = None
    quadrant_inflation_z: float | None = None
    pca_factor: float | None = None
    pca_percentile: float | None = None
    pca_label: str | None = None
    pca_as_of: str | None = None
    rule_phase: str | None = None
    rule_score: float | None = None
    rule_recession_probability: float | None = None
    rule_confidence: float | None = None
    rule_as_of: str | None = None


class CycleStatePoint(BaseModel):
    date: str
    phase_label: str | None = None
    phase_probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    uncertainty: float | None = None
    latent_cycle: list[float] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    modality_contributions: dict[str, Any] = Field(default_factory=dict)


class CycleStateResponse(BaseModel):
    jurisdiction: str
    run_id: str
    model_family: str
    model_version: str
    trained_at: str | None = None
    state: CycleStatePoint
    anchors: CycleAnchorLabels = Field(default_factory=CycleAnchorLabels)


class CycleModelRun(BaseModel):
    run_id: str
    jurisdiction: str
    model_family: str
    model_version: str
    trained_at: str | None = None
    train_start: str | None = None
    train_end: str | None = None
    feature_set_version: str | None = None
    hyperparams: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_path: str | None = None
    status: str


class RegimeFactorIcRow(BaseModel):
    date: str
    jurisdiction: str
    run_id: str
    regime_source: str
    regime_label: str
    metric_id: str
    forward_return_window: str
    spearman_ic: float | None = None
    p_value: float | None = None
    n_obs: int | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class CycleComparisonRun(BaseModel):
    model_family: str
    run_id: str
    model_version: str
    trained_at: str | None = None
    train_start: str | None = None
    train_end: str | None = None
    n_months: int | None = None
    n_features: int | None = None
    engine: str | None = None
    modalities: list[str] = Field(default_factory=list)
    base_run_id: str | None = None
    selected_factor: str | None = None
    benchmark_correlation: float | None = None
    benchmark_points: int | None = None
    benchmark_series: list[str] = Field(default_factory=list)
    governance_status: str | None = None


class CycleComparisonPeriod(BaseModel):
    date: str
    consensus_label: str | None = None
    benchmark_label: str | None = None
    benchmark_value: float | None = None
    benchmark_series: list[str] = Field(default_factory=list)
    agreement: float | None = None
    models_reporting: int
    labels: dict[str, str | None] = Field(default_factory=dict)
    raw_labels: dict[str, str | None] = Field(default_factory=dict)
    display_label: str | None = None
    display_labels: dict[str, str | None] = Field(default_factory=dict)
    label_sources: dict[str, str | None] = Field(default_factory=dict)
    switch_probabilities: dict[str, float | None] = Field(default_factory=dict)
    override_reasons: dict[str, str | None] = Field(default_factory=dict)
    phase_probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)
    confidences: dict[str, float | None] = Field(default_factory=dict)
    raw_stress: dict[str, float | None] = Field(default_factory=dict)
    smoothed_stress: dict[str, float | None] = Field(default_factory=dict)
    nber_recession_target: bool | None = None
    benchmark_agreement: bool | None = None


class CycleComparisonResponse(BaseModel):
    jurisdiction: str
    period: Literal["M", "Q", "2Q"]
    runs: list[CycleComparisonRun] = Field(default_factory=list)
    periods: list[CycleComparisonPeriod] = Field(default_factory=list)
    lineage: dict[str, Any] = Field(default_factory=dict)


class CycleMetricOption(BaseModel):
    metric_id: str
    plain_label: str | None = None
    driver_group: str | None = None
    investor_description: str | None = None
    rows: int
    regimes: int
    avg_ic: float | None = None
    avg_abs_ic: float | None = None
    n_obs: int | None = None


class CycleStockMetricPoint(BaseModel):
    date: str
    metric_id: str
    plain_label: str | None = None
    driver_group: str | None = None
    value: float | None = None
    percentile: float | None = None
    value_z: float | None = None
    peer_count: int | None = None
    phase_label: str | None = None
    confidence: float | None = None


class CycleIcSummaryRow(BaseModel):
    regime_label: str
    metric_id: str
    plain_label: str | None = None
    driver_group: str | None = None
    rows: int
    avg_ic: float | None = None
    avg_abs_ic: float | None = None
    total_obs: int | None = None


class CycleIcJobStatus(BaseModel):
    job_key: str | None = None
    jurisdiction: str
    run_id: str
    status: str
    metric_family: str = "all"
    horizons: list[str] = Field(default_factory=list)
    chunk_size: int | None = None
    total_metrics: int = 0
    completed_metrics: int = 0
    failed_metrics: int = 0
    rows_written: int = 0
    hard_rows_written: int = 0
    probability_rows_written: int = 0
    ic_table_rows: int = 0
    state_start: str | None = None
    state_end: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    elapsed_seconds: float | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class CyclePeerDistributionBucket(BaseModel):
    bucket: str
    count: int


class CycleDriverGroupSummary(BaseModel):
    driver_group: str
    metric_count: int
    avg_abs_ic: float | None = None
    avg_ic: float | None = None
    stock_score: float | None = None
    confidence_label: str = "Insufficient data"


class CycleRegimeDriverCell(BaseModel):
    regime_label: str
    driver_group: str
    avg_ic: float | None = None
    avg_abs_ic: float | None = None
    total_obs: int | None = None


class CycleStockLensResponse(BaseModel):
    jurisdiction: str
    run_id: str
    ticker: str
    company_name: str | None = None
    gics_sector_code: str | None = None
    gics_sector_name: str | None = None
    gics_industry_group_code: str | None = None
    gics_industry_group_name: str | None = None
    selected_metric_id: str | None = None
    selected_metric_plain_label: str | None = None
    selected_metric_driver_group: str | None = None
    metric_description: str | None = None
    metric_warning: str | None = None
    metric_selection_reason: str | None = None
    ic_available: bool = False
    ic_confidence_label: str = "Insufficient data"
    sample_size: int | None = None
    investor_summary: str | None = None
    warnings: list[str] = Field(default_factory=list)
    peer_group_label: str | None = None
    peer_distribution: list[CyclePeerDistributionBucket] = Field(default_factory=list)
    driver_groups: list[CycleDriverGroupSummary] = Field(default_factory=list)
    regime_driver_grid: list[CycleRegimeDriverCell] = Field(default_factory=list)
    ic_status: CycleIcJobStatus | None = None
    current_regime: str | None = None
    metric_options: list[CycleMetricOption] = Field(default_factory=list)
    stock_metrics: list[CycleStockMetricPoint] = Field(default_factory=list)
    ic_rows: list[CycleIcSummaryRow] = Field(default_factory=list)
    lineage: dict[str, Any] = Field(default_factory=dict)


class CycleDashboardChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CycleDashboardChatRequest(BaseModel):
    messages: list[CycleDashboardChatMessage] = Field(default_factory=list)
    jurisdiction: Literal["US", "JP"] = "US"
    run_id: str | None = None
    ticker: str | None = None
    metric_id: str | None = None
    period: Literal["M", "Q", "2Q"] = "M"
    provider: str | None = None      # llm_providers id; None -> server default (DeepSeek)
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 1800


class CycleDashboardChatResponse(BaseModel):
    text: str
    model: str
    has_env_key: bool


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_value(category: str, units: str | None, value: float | None, prev: float | None) -> tuple[str, Literal["up", "dn", "neu"], str, dict | None]:
    """Returns (value_str, direction, note, alt_info | None).

    alt_info is populated only for inflation-category index series, so the home
    tile can toggle between the IDX level and the YoY % change. Schema:
        { "value_label": "IDX",
          "value_alt_str": "+2.4%",
          "value_alt_label": "% Chg" }
    """
    if value is None:
        return "—", "neu", "no data", None

    u = (units or "").lower()

    if category in {"state_probability", "state_proxy", "financial_stress"} or "probability" in u or "0-1" in u:
        display = value * 100 if abs(value) <= 1.5 else value
        s = f"{display:.1f}%"
        if prev is not None:
            prev_display = prev * 100 if abs(prev) <= 1.5 else prev
            delta_pp = display - prev_display
            d = "dn" if delta_pp > 0.1 else "up" if delta_pp < -0.1 else "neu"
            note = f"{delta_pp:+.1f} pp YoY" if abs(delta_pp) >= 0.1 else "flat YoY"
            return s, d, note, None
        return s, "neu", "", None

    # Percent or yield
    if "percent" in u or "%" in u:
        s = f"{value:.2f}%"
        # For inflation-category percent series (CPI YoY, breakeven, PPI YoY,
        # CGPI YoY, etc.) we expose a synthesized IDX alt — rebased so the
        # observation a year ago equals 100. Lets the tile toggle between the
        # YoY rate and an implied index level.
        alt = None
        if category == "inflation":
            idx = 100.0 * (1.0 + value / 100.0)
            alt = {
                "value_label": "% Chg",
                "value_alt_str": f"{idx:.2f}",
                "value_alt_label": "IDX",
            }
        if prev is not None:
            d = "up" if value > prev + 0.01 else "dn" if value < prev - 0.01 else "neu"
            delta_bp = (value - prev) * 100
            note = f"{delta_bp:+.0f} bp YoY" if abs(delta_bp) >= 1 else "flat YoY"
            return s, d, note, alt
        return s, "neu", "", alt

    # Spread (basis points)
    if "bp" in u:
        s = f"{value:.0f} bp"
        d = "up" if (prev is not None and value > prev * 1.02) else "dn" if (prev is not None and value < prev * 0.98) else "neu"
        return s, d, "", None

    # Index-style
    if "index" in u or "normalised" in u or "amp" in u:
        s = f"{value:.1f}"
        if prev is not None and prev != 0:
            yoy = (value - prev) / abs(prev) * 100
            d = "up" if yoy > 0.5 else "dn" if yoy < -0.5 else "neu"
            # For inflation index series, expose the YoY% as a swappable alt view.
            alt = None
            if category == "inflation":
                alt = {
                    "value_label": "IDX",
                    "value_alt_str": f"{yoy:+.2f}%",
                    "value_alt_label": "% Chg",
                }
            return s, d, f"{yoy:+.1f}% YoY", alt
        return s, "neu", "", None

    # CPI as YoY
    if category == "inflation" and ("annual" in u or "yoy" in u or "%" in u):
        s = f"{value:+.2f}%"
        d = "up" if value > 2.5 else "dn" if value < 1.5 else "neu"
        return s, d, "vs 2% target", None

    # FX
    if "jpy per usd" in u or "per usd" in u:
        s = f"{value:.2f}"
        d = "up" if (prev is not None and value > prev * 1.01) else "dn" if (prev is not None and value < prev * 0.99) else "neu"
        return s, d, "", None

    # Big monetary aggregates — just compact
    if "100 million" in u and ("yen" in u or "jpy" in u):
        s = f"JPY {value/10000:.1f}T"
        if prev is not None and prev != 0:
            yoy = (value - prev) / abs(prev) * 100
            d = "up" if yoy > 0.5 else "dn" if yoy < -0.5 else "neu"
            return s, d, f"{yoy:+.1f}% YoY", None
        return s, "neu", "", None

    if "million" in u and ("yen" in u or "jpy" in u):
        s = f"JPY {value/1_000_000:.2f}T"
        if prev is not None and prev != 0:
            yoy = (value - prev) / abs(prev) * 100
            d = "up" if yoy > 0.5 else "dn" if yoy < -0.5 else "neu"
            return s, d, f"{yoy:+.1f}% YoY", None
        return s, "neu", "", None

    if "jpy" in u or "usd" in u:
        if abs(value) >= 1e12:
            s = f"{value/1e12:.1f}T"
        elif abs(value) >= 1e9:
            s = f"{value/1e9:.1f}B"
        else:
            s = f"{value:,.0f}"
        if prev is not None and prev != 0:
            yoy = (value - prev) / abs(prev) * 100
            d = "up" if yoy > 0.5 else "dn" if yoy < -0.5 else "neu"
            return s, d, f"{yoy:+.1f}% YoY", None
        return s, "neu", "", None

    s = f"{value:,.2f}"
    return s, "neu", "", None


def _state_meta(series_id: str, category: str, units: str | None, value: float | None) -> tuple[bool | None, float | None, str | None]:
    u = (units or "").lower()
    if value is None or not (category in {"state_probability", "state_proxy", "financial_stress"} or "probability" in u or "0-1" in u):
        return None, None, None
    threshold = 0.67 if "HAMILTON" in series_id.upper() or "JHGDPBRINDX" in series_id.upper() else 0.50
    normalized = value if abs(value) <= 1.5 else value / 100.0
    active = normalized >= threshold
    label = "proxy active" if category in {"state_proxy", "financial_stress"} and active else "active" if active else "watch"
    return active, threshold, label


# ---------------------------------------------------------------------------
# /signals — DB-driven slot resolver
# ---------------------------------------------------------------------------

@router.get("/signals", response_model=list[MacroSignal])
async def macro_signals(
    jurisdiction: Optional[str] = Query(None, description="US, JP, EZ, CH, AU, SG, HK, XX, or GLOBAL"),
    importance: int = Query(2, ge=1, le=3),
    category: Optional[str] = Query(None, description="Filter by ref_macro_series.category (e.g. 'nowcast', 'liquidity', 'rates')"),
    lang: Lang = "en",
    limit: int = Query(40, ge=1, le=200),
) -> list[MacroSignal]:
    juris = (jurisdiction or "").upper()
    where = ["s.is_active = TRUE", "s.importance <= $1", "s.story_tile_slot IS NOT NULL"]
    args: list = [importance]
    if juris and juris != "GLOBAL":
        args.append(juris)
        where.append(f"s.jurisdiction = ${len(args)}")
    if category:
        args.append(category.lower())
        where.append(f"s.category = ${len(args)}")
    where_sql = " AND ".join(where)

    sql = f"""
        WITH targets AS (
            SELECT series_id, story_tile_slot, name AS label, jurisdiction,
                   category, units, frequency, importance
            FROM   ref_macro_series s
            WHERE  {where_sql}
            ORDER  BY importance, jurisdiction, story_tile_slot
            LIMIT  {int(limit)}
        ),
        latest AS (
            SELECT DISTINCT ON (f.series_id) f.series_id, f.date, f.value
            FROM   fact_macro f
            WHERE  f.series_id IN (SELECT series_id FROM targets)
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
            SELECT f.series_id, array_agg(f.value ORDER BY f.date ASC) AS value_series
            FROM   fact_macro f
            JOIN   targets t ON t.series_id = f.series_id
            WHERE  f.date >= CURRENT_DATE - INTERVAL '3 years'
              AND  f.value IS NOT NULL
            GROUP  BY f.series_id
        ),
        captions AS (
            SELECT scope_key, text
            FROM   fact_macro_story
            WHERE  scope = 'tile' AND lang = $%d
        )
        SELECT t.series_id, t.story_tile_slot, t.label, t.jurisdiction, t.category,
               t.units, t.frequency,
               l.date, l.value, p.prev_value,
               COALESCE(h.value_series, ARRAY[]::double precision[]) AS value_series,
               c.text AS caption
        FROM   targets t
        LEFT   JOIN latest  l ON l.series_id = t.series_id
        LEFT   JOIN prev    p ON p.series_id = t.series_id
        LEFT   JOIN history h ON h.series_id = t.series_id
        LEFT   JOIN captions c ON c.scope_key = 'tile:' || t.story_tile_slot
        ORDER  BY t.importance, t.jurisdiction, t.story_tile_slot
    """ % (len(args) + 1)
    args.append(lang)

    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except Exception as exc:
        logger.warning("macro.signals query failed: %s", exc)
        return []

    out: list[MacroSignal] = []
    for r in rows:
        val_str, direction, note, alt = _format_value(r["category"], r["units"], r["value"], r["prev_value"])
        state_active, state_threshold, state_label = _state_meta(r["series_id"], r["category"], r["units"], r["value"])
        out.append(MacroSignal(
            series_id=r["series_id"],
            slot=r["story_tile_slot"],
            label=r["label"],
            jurisdiction=r["jurisdiction"],
            category=r["category"],
            value=r["value"],
            value_str=val_str,
            direction=direction,
            note=note,
            as_of=r["date"].isoformat() if r["date"] else "",
            units=r["units"],
            frequency=r["frequency"],
            trend_window_label="3Y",
            value_series=[float(v) for v in (r["value_series"] or []) if v is not None],
            caption=r["caption"],
            value_label=alt["value_label"] if alt else None,
            value_alt_str=alt["value_alt_str"] if alt else None,
            value_alt_label=alt["value_alt_label"] if alt else None,
            state_active=state_active,
            state_threshold=state_threshold,
            state_label=state_label,
        ))
    return out


# ---------------------------------------------------------------------------
# /tile/{slot} — single tile detail
# ---------------------------------------------------------------------------

@router.get("/tile/{slot}", response_model=MacroSignal | None)
async def macro_tile(slot: str, lang: Lang = "en") -> MacroSignal | None:
    sql = """
        SELECT s.series_id, s.story_tile_slot, s.name AS label, s.jurisdiction,
               s.category, s.units, s.frequency,
               l.date, l.value, p.prev_value, h.value_series, c.text AS caption
        FROM   ref_macro_series s
        LEFT   JOIN LATERAL (
                  SELECT date, value FROM fact_macro
                  WHERE series_id = s.series_id ORDER BY date DESC LIMIT 1
               ) l ON TRUE
        LEFT   JOIN LATERAL (
                  SELECT value AS prev_value FROM fact_macro
                  WHERE series_id = s.series_id AND date <= l.date - INTERVAL '1 year'
                  ORDER BY date DESC LIMIT 1
               ) p ON TRUE
        LEFT   JOIN LATERAL (
                  SELECT array_agg(value ORDER BY date ASC) AS value_series
                  FROM fact_macro
                  WHERE series_id = s.series_id
                    AND date >= CURRENT_DATE - INTERVAL '5 years'
                    AND value IS NOT NULL
               ) h ON TRUE
        LEFT   JOIN fact_macro_story c
                  ON c.scope = 'tile'
                 AND c.scope_key = 'tile:' || s.story_tile_slot
                 AND c.lang = $2
        WHERE  s.story_tile_slot = $1
        LIMIT  1
    """
    try:
        async with acquire() as conn:
            r = await conn.fetchrow(sql, slot, lang)
    except Exception as exc:
        logger.warning("macro.tile query failed: %s", exc)
        return None
    if r is None or r["series_id"] is None:
        return None
    val_str, direction, note, alt = _format_value(r["category"], r["units"], r["value"], r["prev_value"])
    state_active, state_threshold, state_label = _state_meta(r["series_id"], r["category"], r["units"], r["value"])
    return MacroSignal(
        series_id=r["series_id"],
        slot=r["story_tile_slot"],
        label=r["label"],
        jurisdiction=r["jurisdiction"],
        category=r["category"],
        value=r["value"],
        value_str=val_str,
        direction=direction,
        note=note,
        as_of=r["date"].isoformat() if r["date"] else "",
        units=r["units"],
        frequency=r["frequency"],
        trend_window_label="5Y",
        value_series=[float(v) for v in (r["value_series"] or []) if v is not None],
        caption=r["caption"],
        value_label=alt["value_label"] if alt else None,
        value_alt_str=alt["value_alt_str"] if alt else None,
        value_alt_label=alt["value_alt_label"] if alt else None,
        state_active=state_active,
        state_threshold=state_threshold,
        state_label=state_label,
    )


# ---------------------------------------------------------------------------
# /essay — daily LLM macro essay
# ---------------------------------------------------------------------------

@router.get("/essay", response_model=MacroEssay | None)
async def macro_essay(
    scope: str = Query("GLOBAL", description="GLOBAL | US | JP | EZ"),
    date_: Optional[str] = Query(None, alias="date"),
    lang: Lang = "en",
) -> MacroEssay | None:
    if date_:
        like = f"essay:{scope}-{date_}%"
    else:
        like = f"essay:{scope}-%"
    sql = """
        SELECT scope, scope_key, lang, text, structured_json, generated_at, model
        FROM   fact_macro_story
        WHERE  scope = 'essay'
          AND  scope_key LIKE $1
          AND  lang IN ($2, 'en')
        ORDER  BY (lang = $2) DESC, generated_at DESC
        LIMIT  1
    """
    try:
        async with acquire() as conn:
            r = await conn.fetchrow(sql, like, lang)
    except Exception as exc:
        logger.warning("macro.essay query failed: %s", exc)
        return None
    if r is None:
        return None
    body_md, bullets = _extract_story_payload(r["structured_json"])
    return MacroEssay(
        scope=r["scope"],
        scope_key=r["scope_key"],
        lang=r["lang"],
        text=r["text"],
        body_md=body_md,
        bullets=bullets,
        generated_at=r["generated_at"].isoformat() if r["generated_at"] else None,
        model=r["model"],
    )


def _extract_story_payload(raw: object) -> tuple[str | None, list[dict]]:
    if not raw:
        return None, []
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except _json.JSONDecodeError:
            return None, []
    if not isinstance(raw, dict):
        return None, []

    body = raw.get("body_md") or raw.get("text_md") or raw.get("body")
    body_md = str(body).strip() if body is not None and str(body).strip() else None

    candidates = raw.get("bullets") or raw.get("heavy_hitters") or raw.get("heavyHitters") or []
    bullets: list[dict] = []
    if isinstance(candidates, list):
        for item in candidates:
            if isinstance(item, str) and item.strip():
                bullets.append({"text": item.strip(), "tone": None})
            elif isinstance(item, dict):
                text = item.get("text") or item.get("label") or item.get("title")
                if text is not None and str(text).strip():
                    tone = item.get("tone") or item.get("color")
                    bullets.append({"text": str(text).strip(), "tone": str(tone) if tone else None})
            if len(bullets) >= 4:
                break
    return body_md, bullets


# ---------------------------------------------------------------------------
# /regime — current business-cycle factor
# ---------------------------------------------------------------------------

@router.get("/regime", response_model=MacroRegime | None)
async def macro_regime(factor: str = Query("us_cycle")) -> MacroRegime | None:
    sql = """
        SELECT factor_id, date, value, percentile, regime_label, top_loadings
        FROM   fact_macro_factor
        WHERE  factor_id = $1
        ORDER  BY date DESC LIMIT 1
    """
    try:
        async with acquire() as conn:
            r = await conn.fetchrow(sql, factor)
    except Exception as exc:
        logger.warning("macro.regime query failed: %s", exc)
        return None
    if r is None:
        return None
    raw_loadings = r["top_loadings"]
    if isinstance(raw_loadings, str):
        import json as _json
        try:
            raw_loadings = _json.loads(raw_loadings)
        except _json.JSONDecodeError:
            raw_loadings = []
    return MacroRegime(
        factor_id=r["factor_id"],
        date=r["date"].isoformat() if r["date"] else None,
        value=r["value"],
        percentile=r["percentile"],
        regime_label=r["regime_label"],
        top_loadings=list(raw_loadings or []),
    )


# ---------------------------------------------------------------------------
# /cycle - unified regional business-cycle synthesis
# ---------------------------------------------------------------------------

@router.get("/cycle", response_model=MacroCycleAssessment | None)
async def macro_cycle(jurisdiction: str = Query("US", description="US | JP | EZ | GLOBAL")) -> MacroCycleAssessment | None:
    juris = _normalize_cycle_jurisdiction(jurisdiction)
    sql = """
        SELECT jurisdiction, period_end, phase, score, recession_probability,
               confidence, drivers_json
        FROM   fact_macro_cycle_assessment
        WHERE  jurisdiction = $1
        ORDER  BY period_end DESC
        LIMIT  1
    """
    try:
        async with acquire() as conn:
            r = await conn.fetchrow(sql, juris)
    except Exception as exc:
        logger.warning("macro.cycle cached query failed: %s", exc)
        r = None

    if r is not None:
        return _cycle_from_cached_row(r)
    return await _cycle_from_live_signals(juris)


def _normalize_cycle_jurisdiction(value: str) -> str:
    key = (value or "").upper().strip()
    return key if key in {"US", "JP", "EZ", "GLOBAL"} else "US"


def _cycle_from_cached_row(r) -> MacroCycleAssessment:
    raw = r["drivers_json"] or {}
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except _json.JSONDecodeError:
            raw = {}
    if isinstance(raw, list):
        drivers = raw
        category_scores = {}
        summary = None
    else:
        drivers = raw.get("drivers") or []
        category_scores = raw.get("category_scores") or {}
        summary = raw.get("summary")
    return MacroCycleAssessment(
        jurisdiction=str(r["jurisdiction"]).strip(),
        phase=r["phase"],
        score=float(r["score"]) if r["score"] is not None else None,
        recession_probability=float(r["recession_probability"]) if r["recession_probability"] is not None else None,
        confidence=float(r["confidence"]) if r["confidence"] is not None else None,
        as_of=r["period_end"].isoformat() if r["period_end"] else None,
        drivers=[_cycle_driver(item) for item in drivers if isinstance(item, dict)],
        summary=str(summary) if summary else None,
        category_scores={str(k): float(v) for k, v in dict(category_scores).items() if v is not None},
    )


def _cycle_driver(item: dict) -> MacroCycleDriver:
    return MacroCycleDriver(
        topic=item.get("topic"),
        bucket=item.get("bucket"),
        series_id=item.get("series_id"),
        slot=item.get("slot"),
        label=str(item.get("label") or item.get("series_id") or "Macro signal"),
        value=float(item["value"]) if item.get("value") is not None else None,
        value_str=str(item.get("value_str")) if item.get("value_str") is not None else None,
        as_of=str(item.get("as_of")) if item.get("as_of") is not None else None,
        score=float(item["score"]) if item.get("score") is not None else None,
        tone=item.get("tone") if item.get("tone") in {"green", "red", "amber", "blue"} else None,
        text=str(item.get("text")) if item.get("text") else None,
    )


async def _cycle_from_live_signals(juris: str) -> MacroCycleAssessment:
    if juris == "GLOBAL":
        where = "s.jurisdiction IN ('US','JP','EZ','XX')"
        args: list = []
    else:
        where = "s.jurisdiction = $1"
        args = [juris]
    sql = f"""
        WITH targets AS (
            SELECT series_id, story_tile_slot, name AS label, jurisdiction,
                   category, units, frequency, importance
            FROM   ref_macro_series s
            WHERE  s.is_active = TRUE
              AND  s.importance <= 3
              AND  s.story_tile_slot IS NOT NULL
              AND  {where}
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
        )
        SELECT t.series_id, t.story_tile_slot, t.label, t.jurisdiction, t.category,
               t.units, t.frequency, l.date, l.value, p.prev_value
        FROM   targets t
        JOIN   latest l ON l.series_id = t.series_id
        LEFT   JOIN prev p ON p.series_id = t.series_id
        ORDER  BY t.importance, t.jurisdiction, t.category
        LIMIT  120
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except Exception as exc:
        logger.warning("macro.cycle live fallback failed: %s", exc)
        return MacroCycleAssessment(
            jurisdiction=juris,
            phase="mixed",
            score=50,
            recession_probability=None,
            confidence=0,
            as_of=None,
            drivers=[],
            summary="Cycle assessment is not yet populated.",
        )
    if not rows:
        return MacroCycleAssessment(
            jurisdiction=juris,
            phase="mixed",
            score=50,
            recession_probability=None,
            confidence=0,
            as_of=None,
            drivers=[],
            summary="Cycle assessment is not yet populated.",
        )

    drivers: list[MacroCycleDriver] = []
    raw_scores: list[float] = []
    state_probs: list[float] = []
    as_of = ""
    for r in rows:
        val_str, _direction, _note, _alt = _format_value(r["category"], r["units"], r["value"], r["prev_value"])
        score = _live_cycle_score(r["category"], r["label"], r["story_tile_slot"], r["value"], r["prev_value"])
        raw_scores.append(score)
        if r["category"] in {"state_probability", "state_proxy", "financial_stress"} and r["value"] is not None:
            state_probs.append(_normalize_probability(float(r["value"])))
        if r["date"] and r["date"].isoformat() > as_of:
            as_of = r["date"].isoformat()
        tone = "green" if score > 0.18 else "red" if score < -0.18 else "amber"
        topic = _cycle_topic_for_category(r["category"])
        drivers.append(MacroCycleDriver(
            topic=topic,
            bucket=topic,
            series_id=r["series_id"],
            slot=r["story_tile_slot"],
            label=r["label"],
            value=float(r["value"]) if r["value"] is not None else None,
            value_str=val_str,
            as_of=r["date"].isoformat() if r["date"] else None,
            score=score,
            tone=tone,
            text=f"**{r['label']}:** {val_str}",
        ))

    avg = sum(raw_scores) / len(raw_scores) if raw_scores else 0.0
    score_out = max(0.0, min(100.0, 50.0 + avg * 35.0))
    recession_probability = max(state_probs) if state_probs else max(0.0, min(1.0, 0.5 - avg * 0.35))
    phase = _phase_from_score(score_out, recession_probability)
    drivers.sort(key=lambda d: abs(d.score or 0), reverse=True)
    visible_drivers = drivers[:4]
    summary = _cycle_summary(juris, phase, score_out, recession_probability, visible_drivers)
    return MacroCycleAssessment(
        jurisdiction=juris,
        phase=phase,
        score=score_out,
        recession_probability=recession_probability,
        confidence=min(1.0, 0.25 + min(1.0, len(rows) / 18.0) * 0.65 + (0.1 if state_probs else 0.0)),
        as_of=as_of or None,
        drivers=visible_drivers,
        summary=summary,
    )


def _normalize_probability(value: float) -> float:
    return max(0.0, min(1.0, value / 100.0 if abs(value) > 1.5 else value))


def _cycle_topic_for_category(category: str) -> str:
    c = (category or "").lower()
    if c in {"liquidity", "money_supply"}:
        return "liquidity"
    if c in {"rates", "credit"}:
        return "interest_rates"
    if c in {"growth", "activity", "nowcast", "sentiment"}:
        return "growth"
    if c == "debt":
        return "debt"
    if c == "labor":
        return "labor"
    if c == "inflation":
        return "inflation"
    return "business_cycle"


def _live_cycle_score(category: str, label: str, slot: str | None, value: float | None, prev: float | None) -> float:
    if value is None or prev in (None, 0):
        return 0.0
    c = (category or "").lower()
    text = f"{label or ''} {slot or ''}".lower()
    raw = max(-1.0, min(1.0, (float(value) - float(prev)) / abs(float(prev)) * 4.0))
    invert = c in {"inflation", "debt", "credit", "state_probability", "state_proxy", "financial_stress"}
    if c == "rates":
        invert = "2s10s" not in text and "curve" not in text
    if c == "labor":
        invert = "unemployment" in text
    if c in {"liquidity", "money_supply", "growth", "activity", "nowcast", "sentiment"}:
        invert = False
    return max(-1.0, min(1.0, -raw if invert else raw))


def _phase_from_score(score: float, recession_probability: float | None) -> Literal["expansion", "late_cycle", "slowdown", "contraction", "recovery", "mixed"]:
    if recession_probability is not None and recession_probability >= 0.67:
        return "contraction"
    if score <= 32:
        return "contraction"
    if score < 44:
        return "slowdown"
    if score >= 62:
        return "expansion"
    if score >= 55:
        return "recovery"
    return "mixed"


def _cycle_summary(juris: str, phase: str, score: float, recession_probability: float | None, drivers: list[MacroCycleDriver]) -> str:
    tone = "green" if score >= 58 else "red" if score < 44 else "amber"
    risk = "state-risk proxy" if juris in {"JP", "EZ"} else "recession probability"
    prob = f"{recession_probability * 100.0:.1f}%" if recession_probability is not None else "not available"
    if drivers:
        labels = " and ".join(f"**{d.label}**" for d in drivers[:2])
        return f"[[{tone}:{phase.replace('_', ' ').title()}]] cycle score is **{score:.0f}/100** with {risk} at **{prob}**. The main drivers are {labels}."
    return f"[[{tone}:{phase.replace('_', ' ').title()}]] cycle score is **{score:.0f}/100** with {risk} at **{prob}**."


# ---------------------------------------------------------------------------
# /dashboard - compact consumer macro dashboard bundle
# ---------------------------------------------------------------------------

_DASHBOARD_REGIONS = {
    "US": ("United States", ("SPY", "^GSPC", "IVV")),
    "EZ": ("Eurozone", ("FEZ", "EZU", "VGK", "EXSA.DE")),
    "JP": ("Japan", ("EWJ", "1321.T", "^N225", "DXJ")),
}

_DASHBOARD_OVERLAY_SPECS: tuple[tuple[str, str, Literal["green", "red", "amber", "blue"], str, str], ...] = (
    ("growth_high", "High growth", "green", "high", "(s.category IN ('growth','nowcast','activity') OR s.name ILIKE '%GDP%' OR s.name ILIKE '%activity%')"),
    ("growth_low", "Low growth", "red", "low", "(s.category IN ('growth','nowcast','activity') OR s.name ILIKE '%GDP%' OR s.name ILIKE '%activity%')"),
    ("inflation_high", "High inflation", "red", "high", "(s.category = 'inflation' OR s.name ILIKE '%inflation%' OR s.name ILIKE '%CPI%')"),
    ("unemployment_high", "High unemployment", "amber", "high", "(s.category IN ('labor','employment') OR s.name ILIKE '%unemployment%')"),
    ("liquidity_low", "Low liquidity", "amber", "low", "(s.category = 'liquidity' OR s.name ILIKE '%liquidity%')"),
    ("funding_stress", "Funding stress", "red", "high", "(s.category IN ('credit','financial_stress','funding') OR s.name ILIKE '%spread%' OR s.name ILIKE '%stress%')"),
)


@router.get("/dashboard", response_model=MacroDashboardResponse)
async def macro_dashboard(
    jurisdictions: str = Query("US,EZ,JP", description="Comma-separated US, EU/EZ, JP"),
    lang: Lang = "en",
) -> MacroDashboardResponse:
    requested = [_normalize_dashboard_jurisdiction(item) for item in jurisdictions.split(",")]
    regions = [region for region in requested if region in _DASHBOARD_REGIONS]
    if not regions:
        regions = ["US", "EZ", "JP"]
    out: list[MacroDashboardRegion] = []
    for region in dict.fromkeys(regions):
        cycle = await macro_cycle(region)
        signals = await macro_signals(jurisdiction=region, importance=2, category=None, lang=lang, limit=60)
        async with acquire() as conn:
            index = await _dashboard_index_series(conn, region)
            overlays = await _dashboard_overlay_periods(conn, region)
        out.append(MacroDashboardRegion(
            jurisdiction=region,
            label=_DASHBOARD_REGIONS[region][0],
            cycle=cycle,
            signals=signals,
            index=index,
            overlays=overlays,
            story_bullets=_dashboard_story_bullets(region, cycle, signals, overlays, lang),
        ))
    return MacroDashboardResponse(regions=out, generated_at=date.today().isoformat())


def _normalize_dashboard_jurisdiction(value: str) -> str:
    v = (value or "").strip().upper()
    if v in {"EU", "EUR", "EUROPE", "EUROZONE"}:
        return "EZ"
    return v


async def _dashboard_index_series(conn, jurisdiction: str) -> MacroDashboardIndexSeries:
    tickers = list(_DASHBOARD_REGIONS.get(jurisdiction, _DASHBOARD_REGIONS["US"])[1])
    rows = await conn.fetch(
        """
        WITH candidates AS (
            SELECT ticker, ord
            FROM unnest($1::text[]) WITH ORDINALITY AS c(ticker, ord)
        ),
        chosen AS (
            SELECT c.ticker, COALESCE(d.name, c.ticker) AS name, c.ord
            FROM candidates c
            LEFT JOIN sec.dim_cross_asset d ON d.ticker = c.ticker
            WHERE EXISTS (
                SELECT 1 FROM sec.fact_cross_asset f
                WHERE f.ticker = c.ticker
                  AND f.close IS NOT NULL
                  AND f.date >= CURRENT_DATE - INTERVAL '10 years'
            )
            ORDER BY c.ord
            LIMIT 1
        ),
        sampled AS (
            SELECT f.date, COALESCE(f.adj_close, f.close) AS close,
                   c.ticker, c.name,
                   row_number() OVER (ORDER BY f.date) AS rn,
                   count(*) OVER () AS n
            FROM chosen c
            JOIN sec.fact_cross_asset f ON f.ticker = c.ticker
            WHERE f.close IS NOT NULL
              AND f.date >= CURRENT_DATE - INTERVAL '10 years'
        )
        SELECT date, close, ticker, name
        FROM sampled
        WHERE rn = 1 OR rn = n OR rn % GREATEST(1, (n / 180)::int) = 0
        ORDER BY date
        """,
        tickers,
    )
    if not rows:
        return MacroDashboardIndexSeries()
    return MacroDashboardIndexSeries(
        ticker=rows[0]["ticker"],
        name=rows[0]["name"],
        points=[MacroDashboardIndexPoint(date=row["date"].isoformat(), close=float(row["close"])) for row in rows],
    )


async def _dashboard_overlay_periods(conn, jurisdiction: str) -> list[MacroDashboardOverlayPeriod]:
    overlays = _nber_dashboard_overlays(jurisdiction)
    for kind, label, tone, direction, condition in _DASHBOARD_OVERLAY_SPECS:
        series = await conn.fetchrow(
            f"""
            SELECT series_id, name
            FROM sec.ref_macro_series s
            WHERE s.is_active = TRUE
              AND s.jurisdiction = $1
              AND {condition}
            ORDER BY s.importance, s.story_tile_slot NULLS LAST, s.series_id
            LIMIT 1
            """,
            jurisdiction,
        )
        if not series:
            continue
        rows = await conn.fetch(
            """
            SELECT date, value
            FROM sec.fact_macro
            WHERE series_id = $1
              AND value IS NOT NULL
              AND date >= CURRENT_DATE - INTERVAL '10 years'
            ORDER BY date
            """,
            series["series_id"],
        )
        overlays.extend(_zscore_periods(rows, kind, label, tone, direction, series["series_id"], series["name"]))
    overlays.sort(key=lambda item: item.start)
    return overlays[-80:]


def _nber_dashboard_overlays(jurisdiction: str) -> list[MacroDashboardOverlayPeriod]:
    if jurisdiction != "US":
        return []
    cutoff = date.today() - timedelta(days=3650)
    out: list[MacroDashboardOverlayPeriod] = []
    for peak, trough in NBER_US_RECESSION_WINDOWS:
        start = date.fromisoformat(f"{peak}-01")
        if start.month == 12:
            start = date(start.year + 1, 1, 1)
        else:
            start = date(start.year, start.month + 1, 1)
        end = date.fromisoformat(f"{trough}-01")
        if end < cutoff:
            continue
        out.append(MacroDashboardOverlayPeriod(
            kind="nber_recession",
            label="NBER recession",
            start=start.isoformat(),
            end=end.isoformat(),
            tone="red",
            source_series_id="NBER_USREC",
            source_label="NBER recession windows",
        ))
    return out


def _zscore_periods(
    rows: list[Any],
    kind: str,
    label: str,
    tone: Literal["green", "red", "amber", "blue"],
    direction: str,
    source_series_id: str,
    source_label: str,
) -> list[MacroDashboardOverlayPeriod]:
    values = [float(row["value"]) for row in rows if row["value"] is not None]
    if len(values) < 12:
        return []
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
    std = variance ** 0.5
    if std <= 0:
        return []
    active_dates: list[date] = []
    threshold = 0.85
    for row in rows:
        z = (float(row["value"]) - mean) / std
        active = z >= threshold if direction == "high" else z <= -threshold
        if active:
            active_dates.append(row["date"])
    if not active_dates:
        return []
    periods: list[MacroDashboardOverlayPeriod] = []
    start = prev = active_dates[0]
    for current in active_dates[1:]:
        if (current - prev).days > 95:
            periods.append(MacroDashboardOverlayPeriod(
                kind=kind, label=label, start=start.isoformat(), end=prev.isoformat(),
                tone=tone, source_series_id=source_series_id, source_label=source_label,
            ))
            start = current
        prev = current
    periods.append(MacroDashboardOverlayPeriod(
        kind=kind, label=label, start=start.isoformat(), end=prev.isoformat(),
        tone=tone, source_series_id=source_series_id, source_label=source_label,
    ))
    return periods[-10:]


def _dashboard_story_bullets(
    jurisdiction: str,
    cycle: MacroCycleAssessment | None,
    signals: list[MacroSignal],
    overlays: list[MacroDashboardOverlayPeriod],
    lang: Lang,
) -> list[str]:
    region = _DASHBOARD_REGIONS.get(jurisdiction, (jurisdiction, ()))[0]
    phase = cycle.phase.replace("_", " ") if cycle else "mixed"
    score = f"{cycle.score:.0f}/100" if cycle and cycle.score is not None else "n/a"
    recession = f"{cycle.recession_probability * 100:.1f}%" if cycle and cycle.recession_probability is not None else "n/a"
    top_signals = ", ".join(signal.label for signal in signals[:3]) or "no fresh macro tiles"
    active_overlay = overlays[-1].label if overlays else "no active stress marker"
    if lang == "de":
        return [
            f"{region}: Regime {phase}, Score {score}.",
            f"Rezessions-/Stress-Signal: {recession}; wichtigste Treiber: {top_signals}.",
            f"Markierte Periode zuletzt: {active_overlay}.",
            "Kurz lesen: Wachstum, Inflation, Arbeitsmarkt und Funding gemeinsam betrachten.",
        ]
    return [
        f"{region}: {phase} regime, score {score}.",
        f"Recession/stress signal: {recession}; key drivers: {top_signals}.",
        f"Latest highlighted period: {active_overlay}.",
        "Read growth, inflation, labour and funding conditions together.",
    ]


# ---------------------------------------------------------------------------
# /calendar — upcoming releases (best-effort; uses fact_macro_release)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Learned cycle model endpoints - US/JP only
# ---------------------------------------------------------------------------

_LEARNED_CYCLE_JURISDICTIONS = {"US", "JP"}
_CYCLE_FACTOR_ID_BY_JURISDICTION = {"US": "us_cycle", "JP": "jp_cycle"}


@router.get("/cycle-state", response_model=CycleStateResponse | None)
async def macro_cycle_state(
    jurisdiction: str = Query(..., description="US | JP"),
    model_family: Optional[str] = Query(None, description="Optional pca | dfm | hmm | vae filter"),
    run_id: Optional[str] = Query(None),
) -> CycleStateResponse | None:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=run_id, model_family=family)
            if run is None:
                return None
            state = await conn.fetchrow(
                """
                SELECT date, phase_label, phase_probabilities, confidence, uncertainty,
                       latent_cycle, diagnostics_json, modality_contrib_json
                FROM   fact_cycle_state_monthly
                WHERE  jurisdiction = $1
                  AND  run_id = $2
                ORDER  BY date DESC
                LIMIT  1
                """,
                juris,
                run["run_id"],
            )
            if state is None:
                return None
            anchors = await _fetch_cycle_anchors(conn, juris)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.cycle_state query failed: %s", exc)
        return None

    return CycleStateResponse(
        jurisdiction=juris,
        run_id=run["run_id"],
        model_family=run["model_family"],
        model_version=run["model_version"],
        trained_at=run["trained_at"].isoformat() if run["trained_at"] else None,
        state=_cycle_state_point(state),
        anchors=anchors,
    )


@router.get("/cycle-state/history", response_model=list[CycleStatePoint])
async def macro_cycle_state_history(
    jurisdiction: str = Query(..., description="US | JP"),
    model_family: Optional[str] = Query(None, description="Optional pca | dfm | hmm | vae filter"),
    run_id: Optional[str] = Query(None),
    months: int = Query(120, ge=1, le=360),
) -> list[CycleStatePoint]:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=run_id, model_family=family)
            if run is None:
                return []
            rows = await conn.fetch(
                """
                SELECT date, phase_label, phase_probabilities, confidence, uncertainty,
                       latent_cycle, diagnostics_json, modality_contrib_json
                FROM   fact_cycle_state_monthly
                WHERE  jurisdiction = $1
                  AND  run_id = $2
                  AND  date >= CURRENT_DATE - ($3::int || ' months')::interval
                ORDER  BY date ASC
                """,
                juris,
                run["run_id"],
                months,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.cycle_state_history query failed: %s", exc)
        return []
    return [_cycle_state_point(row) for row in rows]


@router.get("/cycle-model-runs", response_model=list[CycleModelRun])
async def macro_cycle_model_runs(
    jurisdiction: str = Query(..., description="US | JP"),
    model_family: Optional[str] = Query(None, description="Optional pca | dfm | hmm | vae filter"),
    limit: int = Query(25, ge=1, le=200),
) -> list[CycleModelRun]:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    where = ["jurisdiction = $1"]
    args: list[Any] = [juris]
    if family:
        args.append(family)
        where.append(f"model_family = ${len(args)}")
    sql = f"""
        SELECT run_id, jurisdiction, model_family, model_version, trained_at,
               train_start, train_end, feature_set_version, hyperparams_json,
               metrics_json, artifact_path, status
        FROM   fact_cycle_model_run
        WHERE  {' AND '.join(where)}
        ORDER  BY trained_at DESC
        LIMIT  {int(limit)}
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.cycle_model_runs query failed: %s", exc)
        return []
    return [_cycle_model_run(row) for row in rows]


@router.get("/regime-factor-ic", response_model=list[RegimeFactorIcRow])
async def macro_regime_factor_ic(
    jurisdiction: str = Query(..., description="US | JP"),
    run_id: Optional[str] = Query(None),
    model_family: Optional[str] = Query(None, description="Optional pca | dfm | hmm | vae filter"),
    forward_return_window: Optional[str] = Query(None),
    limit: int = Query(250, ge=1, le=5000),
) -> list[RegimeFactorIcRow]:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=run_id, model_family=family)
            if run is None:
                return []
            args: list[Any] = [juris, run["run_id"]]
            where = ["jurisdiction = $1", "run_id = $2"]
            if forward_return_window:
                args.append(forward_return_window)
                where.append(f"forward_return_window = ${len(args)}")
            sql = f"""
                SELECT date, jurisdiction, run_id, regime_source, regime_label, metric_id,
                       forward_return_window, spearman_ic, p_value, n_obs, diagnostics_json
                FROM   fact_equity_factor_ic_regime
                WHERE  {' AND '.join(where)}
                ORDER  BY date DESC, regime_label, abs(COALESCE(spearman_ic, 0)) DESC, metric_id
                LIMIT  {int(limit)}
            """
            rows = await conn.fetch(sql, *args)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.regime_factor_ic query failed: %s", exc)
        return []
    return [
        RegimeFactorIcRow(
            date=r["date"].isoformat(),
            jurisdiction=str(r["jurisdiction"]).strip(),
            run_id=r["run_id"],
            regime_source=r["regime_source"],
            regime_label=r["regime_label"],
            metric_id=r["metric_id"],
            forward_return_window=r["forward_return_window"],
            spearman_ic=float(r["spearman_ic"]) if r["spearman_ic"] is not None else None,
            p_value=float(r["p_value"]) if r["p_value"] is not None else None,
            n_obs=int(r["n_obs"]) if r["n_obs"] is not None else None,
            diagnostics=_json_payload(r["diagnostics_json"], {}),
        )
        for r in rows
    ]


_CYCLE_APP_MODEL_FAMILIES = ("vae", "pca", "dfm", "hmm")
_CYCLE_PHASE_ORDER = ("contraction", "late_cycle", "mid_expansion", "early_expansion")
_CYCLE_APP_BENCHMARK_SERIES = {
    "US": (
        "COMPUTE:US_RECESSION_PROB_MS_DFM",
        "COMPUTE:US_RECESSION_PROB_GDP_HAMILTON",
    ),
    "JP": (
        "COMPUTE:JP_CI_RECESSION_PROXY",
    ),
}


@router.get("/cycle-dashboard/comparison", response_model=CycleComparisonResponse)
async def macro_cycle_dashboard_comparison(
    jurisdiction: str = Query(..., description="US | JP"),
    period: Literal["M", "Q", "2Q"] = Query("M"),
) -> CycleComparisonResponse:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    try:
        async with acquire() as conn:
            runs = await _fetch_cycle_comparison_runs(conn, juris)
            if not runs:
                return CycleComparisonResponse(jurisdiction=juris, period=period, lineage=_cycle_dashboard_lineage(juris))
            states = await conn.fetch(
                """
                SELECT s.run_id, r.model_family, r.model_version, s.date, s.phase_label,
                       s.phase_probabilities, s.confidence, s.uncertainty, s.diagnostics_json
                FROM   fact_cycle_state_monthly s
                JOIN   fact_cycle_model_run r ON r.run_id = s.run_id
                WHERE  s.jurisdiction = $1
                  AND  s.run_id = ANY($2::text[])
                ORDER  BY s.date, r.model_family
                """,
                juris,
                [r["run_id"] for r in runs],
            )
            benchmark_rows = await _fetch_cycle_benchmark_rows(conn, juris)
            override_rows = await _fetch_cycle_label_overrides(conn, juris, [r["run_id"] for r in runs])
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.cycle_dashboard.comparison query failed: %s", exc)
        return CycleComparisonResponse(jurisdiction=juris, period=period, lineage=_cycle_dashboard_lineage(juris))

    return CycleComparisonResponse(
        jurisdiction=juris,
        period=period,
        runs=[_cycle_comparison_run(row) for row in runs],
        periods=_cycle_comparison_periods(states, period, benchmark_rows, override_rows),
        lineage=_cycle_dashboard_lineage(juris),
    )


@router.get("/cycle-dashboard/stock-lens", response_model=CycleStockLensResponse)
async def macro_cycle_dashboard_stock_lens(
    jurisdiction: str = Query(..., description="US | JP"),
    ticker: str = Query(..., min_length=1, max_length=32),
    run_id: Optional[str] = Query(None),
    model_family: Optional[str] = Query("vae"),
    metric_id: Optional[str] = Query(None),
    horizon: str = Query("1m", pattern="^(1m|3m)$"),
    metric_family: str = Query("accounting", description="accounting | all | market_factor | quality | value | growth"),
) -> CycleStockLensResponse:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    tables = _cycle_jurisdiction_tables(juris)
    metric_filter = _cycle_metric_family_filter(metric_family)
    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=run_id, model_family=family)
            if run is None:
                return CycleStockLensResponse(jurisdiction=juris, run_id="", ticker=ticker, lineage=_cycle_dashboard_lineage(juris))
            company_ctx = await _fetch_cycle_company_context(conn, juris, ticker, tables)
            ic_status = await _cycle_ic_status_response(conn, juris, run["run_id"], metric_family="all", horizon=horizon)
            ic_ready = ic_status.status in {"complete", "legacy_rows"} and ic_status.ic_table_rows > 0
            metric_options = await _fetch_cycle_metric_options(conn, juris, run["run_id"], horizon, metric_filter) if ic_ready else []
            if not metric_options:
                metric_options = await _fetch_cycle_metric_fallback_options(conn, juris, run["run_id"], ticker, tables, metric_filter)
            selected_metric = metric_id if metric_id and any(row["metric_id"] == metric_id for row in metric_options) else (
                metric_options[0]["metric_id"] if metric_options else metric_id
            )
            ic_rows = await _fetch_cycle_ic_summary(conn, juris, run["run_id"], horizon, metric_filter)
            metric_ids_for_meta = sorted({
                *(str(row["metric_id"]) for row in metric_options),
                *(str(row["metric_id"]) for row in ic_rows),
                *([selected_metric] if selected_metric else []),
            })
            metric_meta = await _fetch_cycle_metric_dictionary(conn, metric_ids_for_meta)
            stock_rows = await _fetch_cycle_stock_metrics(conn, juris, run["run_id"], ticker, selected_metric, metric_options, tables, company_ctx)
            state = await conn.fetchrow(
                """
                SELECT phase_label
                FROM   fact_cycle_state_monthly
                WHERE  jurisdiction = $1 AND run_id = $2
                ORDER  BY date DESC
                LIMIT  1
                """,
                juris,
                run["run_id"],
            )
            current_regime = state["phase_label"] if state else None
            peer_distribution = await _fetch_cycle_peer_distribution(conn, juris, ticker, selected_metric, tables, company_ctx)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("macro.cycle_dashboard.stock_lens query failed: %s", exc)
        return CycleStockLensResponse(jurisdiction=juris, run_id=run_id or "", ticker=ticker, lineage=_cycle_dashboard_lineage(juris))

    selected_meta = metric_meta.get(selected_metric or "", _cycle_metric_meta(selected_metric or "", None))
    selected_ic = _selected_ic_summary(ic_rows, selected_metric, current_regime)
    selection_reason = (
        "valid_regime_ic"
        if ic_ready and selected_ic is not None
        else "ic_incomplete_descriptive_fallback"
        if not ic_ready
        else "fallback_metric"
    )
    sample_size = int(selected_ic["total_obs"]) if selected_ic and selected_ic["total_obs"] is not None else None
    ic_confidence = _cycle_ic_confidence_label(sample_size, selected_ic["avg_abs_ic"] if selected_ic else None)
    warnings = _cycle_stock_lens_warnings(ic_ready, ic_status, selected_meta, selection_reason)
    investor_summary = _cycle_investor_summary(
        ticker=ticker,
        metric_meta=selected_meta,
        current_regime=current_regime,
        stock_rows=stock_rows,
        selected_metric=selected_metric,
        selected_ic=selected_ic,
        ic_ready=ic_ready,
        confidence_label=ic_confidence,
    )
    decorated_options = [_decorate_metric_option(row, metric_meta) for row in metric_options]
    decorated_ic_rows = [_decorate_ic_row(row, metric_meta) for row in ic_rows]
    decorated_stock_rows = [_decorate_stock_metric_row(row, metric_meta) for row in stock_rows]
    driver_groups = _cycle_driver_group_summaries(decorated_options, decorated_stock_rows, ic_ready)
    regime_driver_grid = _cycle_regime_driver_grid(decorated_ic_rows)

    return CycleStockLensResponse(
        jurisdiction=juris,
        run_id=run["run_id"],
        ticker=ticker,
        company_name=company_ctx.get("name"),
        gics_sector_code=company_ctx.get("gics_sector_code"),
        gics_sector_name=company_ctx.get("gics_sector_name"),
        gics_industry_group_code=company_ctx.get("gics_industry_group_code"),
        gics_industry_group_name=company_ctx.get("gics_industry_group_name"),
        selected_metric_id=selected_metric,
        selected_metric_plain_label=selected_meta["plain_label"],
        selected_metric_driver_group=selected_meta["driver_group"],
        metric_description=selected_meta["investor_description"],
        metric_warning=selected_meta.get("warning_text"),
        metric_selection_reason=selection_reason,
        ic_available=ic_ready and selected_ic is not None,
        ic_confidence_label=ic_confidence,
        sample_size=sample_size,
        investor_summary=investor_summary,
        warnings=warnings,
        peer_group_label=_cycle_peer_group_label(company_ctx),
        peer_distribution=peer_distribution,
        driver_groups=driver_groups,
        regime_driver_grid=regime_driver_grid,
        ic_status=ic_status,
        current_regime=current_regime,
        metric_options=decorated_options,
        stock_metrics=decorated_stock_rows,
        ic_rows=decorated_ic_rows,
        lineage=_cycle_dashboard_lineage(juris),
    )


@router.get("/cycle-dashboard/ic-status", response_model=CycleIcJobStatus)
async def macro_cycle_dashboard_ic_status(
    jurisdiction: str = Query(..., description="US | JP"),
    run_id: Optional[str] = Query(None),
    model_family: Optional[str] = Query("vae"),
    metric_family: str = Query("all"),
    horizon: str = Query("3m", pattern="^(1m|3m)$"),
) -> CycleIcJobStatus:
    juris = _normalize_learned_cycle_jurisdiction(jurisdiction)
    family = _normalize_model_family(model_family)
    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=run_id, model_family=family)
            resolved_run_id = run["run_id"] if run else (run_id or "")
            return await _cycle_ic_status_response(conn, juris, resolved_run_id, metric_family=metric_family, horizon=horizon)
    except Exception as exc:
        logger.warning("macro.cycle_dashboard.ic_status query failed: %s", exc)
        return CycleIcJobStatus(jurisdiction=juris, run_id=run_id or "", status="unavailable", metric_family=metric_family, horizons=[horizon])


@router.post("/cycle-dashboard/chat", response_model=CycleDashboardChatResponse)
async def macro_cycle_dashboard_chat(req: CycleDashboardChatRequest) -> CycleDashboardChatResponse:
    juris = _normalize_learned_cycle_jurisdiction(req.jurisdiction)
    try:
        prov = llm_providers.get(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    api_key = (req.api_key or llm_runtime.resolve_env_key(prov.id) or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"No {prov.label} API key. Set {prov.env[0]} on the API server or pass api_key.",
        )
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last chat message must be a user message.")

    try:
        async with acquire() as conn:
            run = await _fetch_cycle_run(conn, juris, run_id=req.run_id, model_family="vae")
            runs = await _fetch_cycle_comparison_runs(conn, juris)
            state = None
            if run:
                state = await conn.fetchrow(
                    """
                    SELECT date, phase_label, phase_probabilities, confidence, uncertainty,
                           latent_cycle, diagnostics_json, modality_contrib_json
                    FROM   fact_cycle_state_monthly
                    WHERE  jurisdiction = $1 AND run_id = $2
                    ORDER  BY date DESC
                    LIMIT  1
                    """,
                    juris,
                    run["run_id"],
                )
    except Exception as exc:
        logger.warning("macro.cycle_dashboard.chat context query failed: %s", exc)
        run = None
        runs = []
        state = None

    context = {
        "jurisdiction": juris,
        "selected_run_id": run["run_id"] if run else req.run_id,
        "selected_ticker": req.ticker,
        "selected_metric_id": req.metric_id,
        "period": req.period,
        "latest_state": _cycle_state_point(state).model_dump() if state else None,
        "comparison_runs": [_cycle_comparison_run(row).model_dump() for row in runs],
        "database_lineage": _cycle_dashboard_lineage(juris),
        "model_notes": [
            "US and JP are separate model parametrizations; there is no pooled global learned-cycle model.",
            "Production VAE/PCA/DFM/HMM comparison runs use macro and market modalities only.",
            "Macro labels are model outputs from fact_cycle_state_monthly, not raw fact_macro observations.",
            "IC is a cross-sectional regime-conditioned Spearman statistic from fact_equity_factor_ic_regime.",
        ],
    }
    system_prompt = (
        "You are the Macro Cycle App analyst. Answer from the supplied context and general modeling knowledge. "
        "When asked about chart provenance, cite exact table and column names from database_lineage. "
        "Connect VAE labels, comparison models, stock metric percentiles, fundamentals, and IC carefully. "
        "Do not give personalized investment advice."
    )
    history = [{"role": m.role, "content": m.content} for m in req.messages[:-1]]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": "Current app context:\n" + json.dumps(context, default=str, ensure_ascii=False, indent=2)},
        *history,
        {"role": "user", "content": req.messages[-1].content},
    ]
    try:
        answer = await llm_runtime.chat_once(
            api_key=api_key,
            provider=prov.id,
            base_url=req.base_url,
            model=llm_providers.chat_model(prov.id, req.model),
            messages=messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except llm_runtime.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return CycleDashboardChatResponse(
        text=str(answer.get("content") or "").strip(),
        model=llm_providers.chat_model(prov.id, req.model),
        has_env_key=bool(llm_runtime.resolve_env_key(prov.id)),
    )


def _normalize_learned_cycle_jurisdiction(value: str) -> str:
    juris = (value or "").upper().strip()
    if juris not in _LEARNED_CYCLE_JURISDICTIONS:
        raise HTTPException(status_code=400, detail="Learned cycle models are available for US and JP only.")
    return juris


async def _fetch_cycle_comparison_runs(conn, jurisdiction: str):
    return await conn.fetch(
        """
        WITH ranked AS (
            SELECT run_id, jurisdiction, model_family, model_version, trained_at,
                   train_start, train_end, hyperparams_json, metrics_json, status,
                   ROW_NUMBER() OVER (
                       PARTITION BY model_family
                       ORDER BY
                           CASE
                               WHEN model_version ILIKE '%macro_only_nber_smooth%' THEN 0
                               WHEN model_version ILIKE '%macro_market_prod%' THEN 0
                               WHEN model_version ILIKE '%macro_market%' THEN 1
                               ELSE 2
                           END,
                           trained_at DESC
                   ) AS rn
            FROM   fact_cycle_model_run
            WHERE  jurisdiction = $1
              AND  status = 'complete'
              AND  (train_end IS NULL OR train_end <= CURRENT_DATE)
              AND  model_family = ANY($2::text[])
        )
        SELECT run_id, jurisdiction, model_family, model_version, trained_at,
               train_start, train_end, hyperparams_json, metrics_json, status
        FROM   ranked
        WHERE  rn = 1
        ORDER  BY CASE model_family
                    WHEN 'vae' THEN 1
                    WHEN 'pca' THEN 2
                    WHEN 'dfm' THEN 3
                    WHEN 'hmm' THEN 4
                    ELSE 9
                  END
        """,
        jurisdiction,
        list(_CYCLE_APP_MODEL_FAMILIES),
    )


def _cycle_comparison_run(row) -> CycleComparisonRun:
    metrics = _json_payload(row["metrics_json"], {})
    hyperparams = _json_payload(row["hyperparams_json"], {})
    modalities = hyperparams.get("modalities") or metrics.get("modalities") or []
    if not isinstance(modalities, list):
        modalities = [str(modalities)] if modalities else []
    phase_calibration = metrics.get("phase_calibration") if isinstance(metrics, dict) else None
    if not isinstance(phase_calibration, dict):
        phase_calibration = {}
    benchmark_series = phase_calibration.get("benchmark_series") or []
    if not isinstance(benchmark_series, list):
        benchmark_series = [str(benchmark_series)] if benchmark_series else []
    governance = metrics.get("model_governance") if isinstance(metrics, dict) else None
    governance_status = None
    if isinstance(governance, dict):
        governance_status = governance.get("display_status") or governance.get("status")
    return CycleComparisonRun(
        model_family=row["model_family"],
        run_id=row["run_id"],
        model_version=row["model_version"],
        trained_at=row["trained_at"].isoformat() if row["trained_at"] else None,
        train_start=row["train_start"].isoformat() if row["train_start"] else None,
        train_end=row["train_end"].isoformat() if row["train_end"] else None,
        n_months=_int_or_none(metrics.get("n_months")),
        n_features=_int_or_none(metrics.get("n_features")),
        engine=metrics.get("engine"),
        modalities=[str(v) for v in modalities],
        base_run_id=metrics.get("base_run_id"),
        selected_factor=phase_calibration.get("selected_factor"),
        benchmark_correlation=_float_or_none(phase_calibration.get("benchmark_correlation")),
        benchmark_points=_int_or_none(phase_calibration.get("benchmark_points")),
        benchmark_series=[str(v) for v in benchmark_series],
        governance_status=str(governance_status or metrics.get("display_status") or "research"),
    )


async def _fetch_cycle_benchmark_rows(conn, jurisdiction: str):
    series_ids = list(_CYCLE_APP_BENCHMARK_SERIES.get(jurisdiction, ()))
    if not series_ids:
        return []
    return await conn.fetch(
        """
        SELECT f.date, f.series_id, s.name, s.units, f.value::float AS value
        FROM   fact_macro f
        JOIN   ref_macro_series s ON s.series_id = f.series_id
        WHERE  s.jurisdiction = $1
          AND  s.is_active = TRUE
          AND  f.series_id = ANY($2::text[])
          AND  f.value IS NOT NULL
        ORDER  BY f.date, f.series_id
        """,
        jurisdiction,
        series_ids,
    )


async def _fetch_cycle_label_overrides(conn, jurisdiction: str, run_ids: list[str]):
    try:
        return await conn.fetch(
            """
            SELECT jurisdiction, run_id, model_family, effective_start, effective_end,
                   override_label, reason, source, author
            FROM   fact_cycle_label_override
            WHERE  jurisdiction = $1
              AND  is_active = TRUE
              AND  (run_id IS NULL OR run_id = ANY($2::text[]))
            ORDER  BY effective_start, override_id
            """,
            jurisdiction,
            run_ids,
        )
    except Exception as exc:
        logger.info("cycle label overrides unavailable: %s", exc)
        return []


def _cycle_benchmark_periods(rows, period: str) -> dict[date, dict[str, Any]]:
    by_period: dict[date, dict[str, Any]] = {}
    for row in rows:
        value = _normalize_benchmark_probability(row["value"], row["units"])
        if value is None:
            continue
        period_date = _cycle_period_end(row["date"], period)
        item = by_period.setdefault(period_date, {"values": [], "series": set()})
        item["values"].append(value)
        item["series"].add(str(row["series_id"]))

    out: dict[date, dict[str, Any]] = {}
    for period_date, item in by_period.items():
        values = item["values"]
        if not values:
            continue
        avg = sum(values) / len(values)
        out[period_date] = {
            "value": avg,
            "label": _cycle_benchmark_label(avg),
            "series": sorted(item["series"]),
        }
    return out


def _normalize_benchmark_probability(value: Any, units: str | None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(out, float) or not (-1e12 < out < 1e12):
        return None
    unit = (units or "").lower()
    if "percent" in unit or "percentage" in unit or abs(out) > 1.5:
        out = out / 100.0
    return max(0.0, min(1.0, out))


def _cycle_benchmark_label(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 0.50:
        return "contraction"
    if value >= 0.25:
        return "late_cycle"
    if value >= 0.10:
        return "mid_expansion"
    return "early_expansion"


def _cycle_comparison_periods(rows, period: str, benchmark_rows=None, override_rows=None) -> list[CycleComparisonPeriod]:
    by_model_period: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        family = row["model_family"]
        period_date = _cycle_period_end(row["date"], period)
        key = (family, period_date)
        item = by_model_period.setdefault(
            key,
            {
                "labels": [],
                "confidences": [],
                "prob_sums": {},
                "prob_count": 0,
                "raw_labels": [],
                "raw_stress": [],
                "smoothed_stress": [],
                "nber_recession_target": [],
            },
        )
        if row["phase_label"]:
            item["labels"].append(str(row["phase_label"]))
        if row["confidence"] is not None:
            item["confidences"].append(float(row["confidence"]))
        diagnostics = _json_payload(row["diagnostics_json"], {})
        if isinstance(diagnostics, dict):
            if diagnostics.get("raw_phase_label"):
                item["raw_labels"].append(str(diagnostics.get("raw_phase_label")))
            raw_stress = _float_or_none(diagnostics.get("raw_stress_percentile"))
            smoothed_stress = _float_or_none(diagnostics.get("smoothed_stress_percentile"))
            if raw_stress is not None:
                item["raw_stress"].append(raw_stress)
            if smoothed_stress is not None:
                item["smoothed_stress"].append(smoothed_stress)
            if diagnostics.get("nber_recession_target") is not None:
                item["nber_recession_target"].append(bool(diagnostics.get("nber_recession_target")))
        probs = _float_payload(row["phase_probabilities"])
        if probs:
            item["prob_count"] += 1
            for phase, value in probs.items():
                item["prob_sums"][phase] = item["prob_sums"].get(phase, 0.0) + float(value)

    model_labels: dict[date, dict[str, str | None]] = {}
    model_raw_labels: dict[date, dict[str, str | None]] = {}
    model_conf: dict[date, dict[str, float | None]] = {}
    model_raw_stress: dict[date, dict[str, float | None]] = {}
    model_smoothed_stress: dict[date, dict[str, float | None]] = {}
    model_probabilities: dict[date, dict[str, dict[str, float]]] = {}
    nber_targets: dict[date, bool | None] = {}
    for (family, period_date), item in by_model_period.items():
        label = None
        if item["prob_sums"] and item["prob_count"]:
            avg_probs = {k: v / item["prob_count"] for k, v in item["prob_sums"].items()}
            label = max(avg_probs, key=avg_probs.get)
            model_probabilities.setdefault(period_date, {})[family] = avg_probs
        elif item["labels"]:
            label = _mode_label(item["labels"])
            model_probabilities.setdefault(period_date, {})[family] = {}
        model_labels.setdefault(period_date, {})[family] = label
        model_raw_labels.setdefault(period_date, {})[family] = _mode_label(item["raw_labels"]) if item["raw_labels"] else None
        confidences = item["confidences"]
        model_conf.setdefault(period_date, {})[family] = (sum(confidences) / len(confidences)) if confidences else None
        model_raw_stress.setdefault(period_date, {})[family] = (
            sum(item["raw_stress"]) / len(item["raw_stress"]) if item["raw_stress"] else None
        )
        model_smoothed_stress.setdefault(period_date, {})[family] = (
            sum(item["smoothed_stress"]) / len(item["smoothed_stress"]) if item["smoothed_stress"] else None
        )
        if item["nber_recession_target"]:
            nber_targets[period_date] = sum(1 for v in item["nber_recession_target"] if v) >= (len(item["nber_recession_target"]) / 2)

    benchmarks = _cycle_benchmark_periods(benchmark_rows or [], period)
    period_dates = sorted(set(model_labels) | set(benchmarks))
    out: list[CycleComparisonPeriod] = []
    for period_date in period_dates:
        period_model_labels = model_labels.get(period_date, {})
        labels = {family: period_model_labels.get(family) for family in _CYCLE_APP_MODEL_FAMILIES}
        values = [v for v in labels.values() if v]
        consensus = _mode_label(values) if values else None
        agreement = (values.count(consensus) / len(values)) if values and consensus else None
        benchmark = benchmarks.get(period_date, {})
        display_labels, label_sources, switch_probs, override_reasons = _cycle_display_label_decisions(
            period_date,
            labels,
            model_probabilities,
            out,
            override_rows or [],
        )
        display_values = [v for v in display_labels.values() if v]
        display_consensus = _mode_label(display_values) if display_values else consensus
        benchmark_label = benchmark.get("label")
        out.append(
            CycleComparisonPeriod(
                date=period_date.isoformat(),
                consensus_label=consensus,
                benchmark_label=benchmark_label,
                benchmark_value=benchmark.get("value"),
                benchmark_series=benchmark.get("series", []),
                agreement=agreement,
                models_reporting=len(values),
                labels=labels,
                display_label=display_consensus,
                display_labels=display_labels,
                label_sources=label_sources,
                switch_probabilities=switch_probs,
                override_reasons=override_reasons,
                phase_probabilities={family: model_probabilities.get(period_date, {}).get(family, {}) for family in _CYCLE_APP_MODEL_FAMILIES},
                raw_labels={family: model_raw_labels.get(period_date, {}).get(family) for family in _CYCLE_APP_MODEL_FAMILIES},
                confidences={family: model_conf.get(period_date, {}).get(family) for family in _CYCLE_APP_MODEL_FAMILIES},
                raw_stress={family: model_raw_stress.get(period_date, {}).get(family) for family in _CYCLE_APP_MODEL_FAMILIES},
                smoothed_stress={family: model_smoothed_stress.get(period_date, {}).get(family) for family in _CYCLE_APP_MODEL_FAMILIES},
                nber_recession_target=nber_targets.get(period_date),
                benchmark_agreement=(display_consensus == benchmark_label) if display_consensus and benchmark_label else None,
            )
        )
    return out


def _cycle_period_end(value: Any, period: str) -> date:
    dt = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if period == "Q":
        month = ((dt.month - 1) // 3 + 1) * 3
        return date(dt.year, month, calendar.monthrange(dt.year, month)[1])
    if period == "2Q":
        month = 6 if dt.month <= 6 else 12
        return date(dt.year, month, calendar.monthrange(dt.year, month)[1])
    return date(dt.year, dt.month, calendar.monthrange(dt.year, dt.month)[1])


def _cycle_display_label_decisions(
    period_date: date,
    labels: dict[str, str | None],
    probabilities_by_period: dict[date, dict[str, dict[str, float]]],
    previous_periods: list[CycleComparisonPeriod],
    override_rows,
) -> tuple[dict[str, str | None], dict[str, str | None], dict[str, float | None], dict[str, str | None]]:
    display_labels: dict[str, str | None] = {}
    label_sources: dict[str, str | None] = {}
    switch_probs: dict[str, float | None] = {}
    override_reasons: dict[str, str | None] = {}
    previous = previous_periods[-1].display_labels if previous_periods else {}
    for family in _CYCLE_APP_MODEL_FAMILIES:
        proposed = labels.get(family)
        probs = probabilities_by_period.get(period_date, {}).get(family, {})
        family_overrides = [
            _cycle_override_payload(row)
            for row in (override_rows or [])
            if _override_applies_to_family(row, family)
        ]
        decisions = apply_display_label_policy(
            [
                {"date": period_date, "label": previous.get(family), "probabilities": {}},
                {"date": period_date, "label": proposed, "probabilities": probs},
            ]
            if previous.get(family)
            else [{"date": period_date, "label": proposed, "probabilities": probs}],
            overrides=family_overrides,
            probability_threshold=0.55,
        )
        decision = decisions[-1] if decisions else None
        display_labels[family] = decision.display_label if decision else proposed
        label_sources[family] = decision.label_source if decision else "model"
        switch_probs[family] = decision.switch_probability if decision else _float_or_none(probs.get(proposed or ""))
        override_reasons[family] = decision.override_reason if decision else None
    return display_labels, label_sources, switch_probs, override_reasons


def _cycle_override_payload(row) -> dict[str, Any]:
    return {
        "effective_start": row["effective_start"],
        "effective_end": row["effective_end"],
        "override_label": row["override_label"],
        "reason": row["reason"],
        "source": row["source"] or "manual_override",
    }


def _override_applies_to_family(row, family: str) -> bool:
    model_family = (row["model_family"] or "").lower().strip()
    return not model_family or model_family == family


def _mode_label(labels: list[str]) -> str | None:
    if not labels:
        return None
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    max_count = max(counts.values())
    candidates = {label for label, count in counts.items() if count == max_count}
    for phase in _CYCLE_PHASE_ORDER:
        if phase in candidates:
            return phase
    return sorted(candidates)[0]


def _cycle_jurisdiction_tables(jurisdiction: str) -> dict[str, str]:
    if jurisdiction == "JP":
        return {
            "metrics": "fact_metrics_jp",
            "prices": "fact_prices_jp",
            "company": "dim_company_jp",
        }
    return {
        "metrics": "fact_metrics_us",
        "prices": "fact_prices_us",
        "company": "dim_company_us",
    }


def _cycle_metric_family_filter(metric_family: str) -> tuple[str, ...] | None:
    family = (metric_family or "accounting").lower().strip()
    if family == "all":
        return None
    patterns = {
        "market_factor": ("_ff3", "_ff4", "_ff5", "_ff6", "market_beta", "beta", "volatility", "momentum", "residual", "abnormal_return"),
        "quality": ("return_on", "roe", "roa", "margin", "profit", "accrual", "quality", "cash_conversion", "asset_turnover"),
        "value": ("book_to", "earnings_yield", "free_cash_flow_yield", "dividend", "ev_", "enterprise_value", "valuation", "value"),
        "growth": ("growth", "investment", "capex", "research_and_development", "r_and_d", "sales_change", "revenue_change"),
    }
    return patterns.get(family, ("__exclude_market_factor__",))


async def _fetch_cycle_metric_options(conn, jurisdiction: str, run_id: str, horizon: str, patterns: tuple[str, ...] | None):
    rows = await conn.fetch(
        """
        SELECT metric_id,
               COUNT(*) AS rows,
               COUNT(DISTINCT regime_label) AS regimes,
               AVG(spearman_ic) AS avg_ic,
               AVG(ABS(spearman_ic)) AS avg_abs_ic,
               SUM(COALESCE(n_obs, 0)) AS n_obs
        FROM   fact_equity_factor_ic_regime
        WHERE  jurisdiction = $1
          AND  run_id = $2
          AND  regime_source = 'cycle_model_probability'
          AND  forward_return_window = $3
          AND  spearman_ic IS NOT NULL
        GROUP  BY metric_id
        ORDER  BY avg_abs_ic DESC NULLS LAST, rows DESC, metric_id
        LIMIT  500
        """,
        jurisdiction,
        run_id,
        horizon,
    )
    return _filter_metric_rows(rows, patterns)[:80]


async def _fetch_cycle_metric_fallback_options(conn, jurisdiction: str, run_id: str, ticker: str, tables: dict[str, str], patterns: tuple[str, ...] | None):
    metrics_table = tables["metrics"]
    month_expr = "(date_trunc('month', m.period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date"
    sql = f"""
        WITH state_window AS (
            SELECT MIN(date)::date AS start_date, MAX(date)::date AS end_date
            FROM   fact_cycle_state_monthly
            WHERE  jurisdiction = $1 AND run_id = $2
        )
        SELECT m.metric_id,
               COUNT(DISTINCT {month_expr})::int AS rows,
               0::int AS regimes,
               NULL::float AS avg_ic,
               NULL::float AS avg_abs_ic,
               COUNT(*)::int AS n_obs
        FROM   {metrics_table} m, state_window w
        WHERE  m.ticker = $3
          AND  m.period_end IS NOT NULL
          AND  m.value IS NOT NULL
          AND  COALESCE(m.importance, 9) <= 2
          AND  {month_expr} BETWEEN w.start_date AND w.end_date
        GROUP  BY m.metric_id
        ORDER  BY COUNT(DISTINCT {month_expr}) DESC, m.metric_id
        LIMIT  80
    """
    rows = await conn.fetch(sql, jurisdiction, run_id, ticker)
    return _filter_metric_rows(rows, patterns)[:80]


async def _cycle_ic_status_response(
    conn,
    jurisdiction: str,
    run_id: str,
    *,
    metric_family: str,
    horizon: str,
) -> CycleIcJobStatus:
    if not run_id:
        return CycleIcJobStatus(jurisdiction=jurisdiction, run_id="", status="missing_run", metric_family=metric_family, horizons=[horizon])
    try:
        job = await conn.fetchrow(
            """
            SELECT job_key, jurisdiction, run_id, status, metric_family, horizons_json,
                   chunk_size, total_metrics, completed_metrics, failed_metrics,
                   rows_written, hard_rows_written, probability_rows_written,
                   state_start, state_end, started_at, updated_at, completed_at,
                   elapsed_seconds, diagnostics_json
            FROM   fact_cycle_ic_job_status
            WHERE  jurisdiction = $1
              AND  run_id = $2
              AND  metric_family = $3
            ORDER  BY updated_at DESC
            LIMIT  1
            """,
            jurisdiction,
            run_id,
            (metric_family or "all").lower(),
        )
    except Exception:
        job = None
    try:
        ic_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM   fact_equity_factor_ic_regime
            WHERE  jurisdiction = $1
              AND  run_id = $2
              AND  regime_source = 'cycle_model_probability'
              AND  forward_return_window = $3
            """,
            jurisdiction,
            run_id,
            horizon,
        )
    except Exception:
        ic_count = 0
    ic_rows = int(ic_count or 0)
    if not job:
        return CycleIcJobStatus(
            jurisdiction=jurisdiction,
            run_id=run_id,
            status="legacy_rows" if ic_rows > 0 else "not_started",
            metric_family=metric_family,
            horizons=[horizon],
            ic_table_rows=ic_rows,
        )
    horizons = _json_payload(job["horizons_json"], [horizon])
    diagnostics = _json_payload(job["diagnostics_json"], {})
    if job["status"] == "complete" and isinstance(diagnostics, dict):
        diagnostics = {key: value for key, value in diagnostics.items() if key != "error"}
    return CycleIcJobStatus(
        job_key=job["job_key"],
        jurisdiction=job["jurisdiction"],
        run_id=job["run_id"],
        status=job["status"],
        metric_family=job["metric_family"],
        horizons=[str(v) for v in horizons] if isinstance(horizons, list) else [horizon],
        chunk_size=int(job["chunk_size"]) if job["chunk_size"] is not None else None,
        total_metrics=int(job["total_metrics"] or 0),
        completed_metrics=int(job["completed_metrics"] or 0),
        failed_metrics=int(job["failed_metrics"] or 0),
        rows_written=int(job["rows_written"] or 0),
        hard_rows_written=int(job["hard_rows_written"] or 0),
        probability_rows_written=int(job["probability_rows_written"] or 0),
        ic_table_rows=ic_rows,
        state_start=job["state_start"].isoformat() if job["state_start"] else None,
        state_end=job["state_end"].isoformat() if job["state_end"] else None,
        started_at=job["started_at"].isoformat() if job["started_at"] else None,
        updated_at=job["updated_at"].isoformat() if job["updated_at"] else None,
        completed_at=job["completed_at"].isoformat() if job["completed_at"] else None,
        elapsed_seconds=float(job["elapsed_seconds"]) if job["elapsed_seconds"] is not None else None,
        diagnostics=diagnostics if isinstance(diagnostics, dict) else {},
    )


async def _fetch_cycle_company_context(conn, jurisdiction: str, ticker: str, tables: dict[str, str]) -> dict[str, Any]:
    company_table = tables["company"]
    try:
        row = await conn.fetchrow(
            f"""
            SELECT name, primary_ticker, gics_sector_code, gics_sector_name,
                   gics_industry_group_code, gics_industry_group_name
            FROM   {company_table}
            WHERE  primary_ticker = $1
            LIMIT  1
            """,
            ticker,
        )
    except Exception:
        row = None
    if not row:
        return {"ticker": ticker, "name": ticker}
    return {
        "ticker": ticker,
        "name": row["name"] or ticker,
        "gics_sector_code": row["gics_sector_code"],
        "gics_sector_name": row["gics_sector_name"],
        "gics_industry_group_code": row["gics_industry_group_code"],
        "gics_industry_group_name": row["gics_industry_group_name"],
    }


async def _fetch_cycle_metric_dictionary(conn, metric_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not metric_ids:
        return {}
    try:
        rows = await conn.fetch(
            """
            SELECT d.metric_id,
                   COALESCE(i.plain_label, d.name, d.metric_id) AS plain_label,
                   COALESCE(i.driver_group, d.category, '') AS driver_group,
                   COALESCE(i.investor_description, d.interpretation, d.note, d.formula, '') AS investor_description,
                   i.interpretation_high,
                   i.interpretation_low,
                   COALESCE(i.sector_applicability, d.sector_scope, 'universal') AS sector_applicability,
                   i.warning_text,
                   d.name,
                   d.category
            FROM   ref_metric_definitions d
            LEFT   JOIN ref_cycle_metric_investor_dictionary i ON i.metric_id = d.metric_id
            WHERE  d.metric_id = ANY($1::text[])
            """,
            metric_ids,
        )
    except Exception:
        rows = await conn.fetch(
            """
            SELECT metric_id, name AS plain_label, category AS driver_group,
                   COALESCE(interpretation, note, formula, '') AS investor_description,
                   NULL::text AS interpretation_high,
                   NULL::text AS interpretation_low,
                   COALESCE(sector_scope, 'universal') AS sector_applicability,
                   NULL::text AS warning_text,
                   name,
                   category
            FROM   ref_metric_definitions
            WHERE  metric_id = ANY($1::text[])
            """,
            metric_ids,
        )
    out = {metric_id: _cycle_metric_meta(metric_id, None) for metric_id in metric_ids}
    for row in rows:
        metric_id = str(row["metric_id"])
        out[metric_id] = _cycle_metric_meta(
            metric_id,
            {
                "plain_label": row["plain_label"],
                "driver_group": row["driver_group"],
                "investor_description": row["investor_description"],
                "interpretation_high": row["interpretation_high"],
                "interpretation_low": row["interpretation_low"],
                "sector_applicability": row["sector_applicability"],
                "warning_text": row["warning_text"],
                "category": row["category"],
                "name": row["name"],
            },
        )
    return out


def _cycle_metric_meta(metric_id: str, row: dict[str, Any] | None) -> dict[str, Any]:
    raw = row or {}
    plain = str(raw.get("plain_label") or raw.get("name") or _title_from_metric_id(metric_id))
    driver = str(raw.get("driver_group") or _driver_group_for_metric(metric_id, raw.get("category")))
    if driver not in _CYCLE_DRIVER_GROUPS:
        driver = _driver_group_for_metric(metric_id, driver)
    description = str(raw.get("investor_description") or f"Shows how {plain.lower()} compares across peers.")
    return {
        "metric_id": metric_id,
        "plain_label": plain,
        "driver_group": driver,
        "investor_description": description,
        "interpretation_high": raw.get("interpretation_high"),
        "interpretation_low": raw.get("interpretation_low"),
        "sector_applicability": raw.get("sector_applicability") or "universal",
        "warning_text": raw.get("warning_text"),
    }


_CYCLE_DRIVER_GROUPS = (
    "Profitability & Quality",
    "Growth & Reinvestment",
    "Valuation & Yield",
    "Balance Sheet & Risk",
    "Capital Efficiency",
    "Market & Momentum",
)


def _driver_group_for_metric(metric_id: str | None, category: Any = None) -> str:
    metric = str(metric_id or "").lower()
    cat = str(category or "").lower()
    text = f"{metric} {cat}"
    if any(key in text for key in ("growth", "capex", "investment", "research", "revenue_change", "sales_change")):
        return "Growth & Reinvestment"
    if any(key in text for key in ("yield", "book_to", "valuation", "ev_", "enterprise", "dividend", "value", "earnings_yield")):
        return "Valuation & Yield"
    if any(key in text for key in ("debt", "leverage", "liquidity", "coverage", "solvency", "risk")):
        return "Balance Sheet & Risk"
    if any(key in text for key in ("turnover", "capital", "asset", "invested_capital")):
        return "Capital Efficiency"
    if any(key in text for key in ("momentum", "beta", "volatility", "residual", "market", "pead", "abnormal_return")):
        return "Market & Momentum"
    return "Profitability & Quality"


def _title_from_metric_id(metric_id: str | None) -> str:
    return str(metric_id or "Metric").replace("_", " ").replace("-", " ").title()


def _decorate_metric_option(row, meta: dict[str, dict[str, Any]]) -> CycleMetricOption:
    item = meta.get(str(row["metric_id"]), _cycle_metric_meta(str(row["metric_id"]), None))
    return CycleMetricOption(
        metric_id=row["metric_id"],
        plain_label=item["plain_label"],
        driver_group=item["driver_group"],
        investor_description=item["investor_description"],
        rows=int(row["rows"]),
        regimes=int(row["regimes"]),
        avg_ic=float(row["avg_ic"]) if row["avg_ic"] is not None else None,
        avg_abs_ic=float(row["avg_abs_ic"]) if row["avg_abs_ic"] is not None else None,
        n_obs=int(row["n_obs"]) if row["n_obs"] is not None else None,
    )


def _decorate_ic_row(row, meta: dict[str, dict[str, Any]]) -> CycleIcSummaryRow:
    item = meta.get(str(row["metric_id"]), _cycle_metric_meta(str(row["metric_id"]), None))
    return CycleIcSummaryRow(
        regime_label=row["regime_label"],
        metric_id=row["metric_id"],
        plain_label=item["plain_label"],
        driver_group=item["driver_group"],
        rows=int(row["rows"]),
        avg_ic=float(row["avg_ic"]) if row["avg_ic"] is not None else None,
        avg_abs_ic=float(row["avg_abs_ic"]) if row["avg_abs_ic"] is not None else None,
        total_obs=int(row["total_obs"]) if row["total_obs"] is not None else None,
    )


def _decorate_stock_metric_row(row, meta: dict[str, dict[str, Any]]) -> CycleStockMetricPoint:
    item = meta.get(str(row["metric_id"]), _cycle_metric_meta(str(row["metric_id"]), None))
    return CycleStockMetricPoint(
        date=row["date"].isoformat() if row["date"] else "",
        metric_id=row["metric_id"],
        plain_label=item["plain_label"],
        driver_group=item["driver_group"],
        value=float(row["value"]) if row["value"] is not None else None,
        percentile=float(row["percentile"]) if row["percentile"] is not None else None,
        value_z=float(row["value_z"]) if row["value_z"] is not None else None,
        peer_count=int(row["peer_count"]) if row["peer_count"] is not None else None,
        phase_label=row["phase_label"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


async def _fetch_cycle_peer_distribution(conn, jurisdiction: str, ticker: str, selected_metric: str | None, tables: dict[str, str], ctx: dict[str, Any]) -> list[CyclePeerDistributionBucket]:
    if not selected_metric:
        return []
    metrics_table = tables["metrics"]
    company_table = tables["company"]
    filters = ["m.metric_id = $1", "m.value IS NOT NULL", "m.period_end IS NOT NULL"]
    args: list[Any] = [selected_metric, ticker]
    if ctx.get("gics_industry_group_code"):
        args.append(ctx["gics_industry_group_code"])
        filters.append(f"d.gics_industry_group_code = ${len(args)}")
    elif ctx.get("gics_sector_code"):
        args.append(ctx["gics_sector_code"])
        filters.append(f"d.gics_sector_code = ${len(args)}")
    sql = f"""
        WITH latest AS (
            SELECT MAX(period_end) AS period_end
            FROM   {metrics_table}
            WHERE  ticker = $2 AND metric_id = $1 AND value IS NOT NULL
        ),
        peers AS (
            SELECT m.ticker, m.value::float AS value,
                   percent_rank() OVER (ORDER BY m.value) AS percentile
            FROM   {metrics_table} m
            JOIN   {company_table} d ON d.primary_ticker = m.ticker
            JOIN   latest l ON l.period_end = m.period_end
            WHERE  {' AND '.join(filters)}
        )
        SELECT
            CASE
                WHEN percentile < 0.2 THEN '0-20'
                WHEN percentile < 0.4 THEN '20-40'
                WHEN percentile < 0.6 THEN '40-60'
                WHEN percentile < 0.8 THEN '60-80'
                ELSE '80-100'
            END AS bucket,
            COUNT(*)::int AS count
        FROM peers
        GROUP BY 1
        ORDER BY 1
    """
    try:
        rows = await conn.fetch(sql, *args)
    except Exception:
        return []
    return [CyclePeerDistributionBucket(bucket=row["bucket"], count=int(row["count"])) for row in rows]


def _cycle_peer_group_label(ctx: dict[str, Any]) -> str:
    if ctx.get("gics_industry_group_name"):
        return f"GICS industry group: {ctx['gics_industry_group_name']}"
    if ctx.get("gics_sector_name"):
        return f"GICS sector: {ctx['gics_sector_name']}"
    return "Full covered market"


def _selected_ic_summary(rows, selected_metric: str | None, current_regime: str | None):
    if not selected_metric:
        return None
    candidates = [row for row in rows if row["metric_id"] == selected_metric]
    if current_regime:
        exact = [row for row in candidates if row["regime_label"] == current_regime]
        if exact:
            return exact[0]
    return candidates[0] if candidates else None


def _cycle_ic_confidence_label(sample_size: int | None, avg_abs_ic: float | None) -> str:
    if not sample_size or avg_abs_ic is None:
        return "Insufficient data"
    if sample_size >= 500 and abs(avg_abs_ic) >= 0.04:
        return "High"
    if sample_size >= 150 and abs(avg_abs_ic) >= 0.02:
        return "Medium"
    return "Low"


def _cycle_stock_lens_warnings(ic_ready: bool, status: CycleIcJobStatus, meta: dict[str, Any], selection_reason: str) -> list[str]:
    warnings: list[str] = []
    if not ic_ready:
        warnings.append("IC computation incomplete; stock charts are descriptive until the regime-conditioned IC backfill completes.")
    if selection_reason != "valid_regime_ic":
        warnings.append("Selected metric was chosen as a descriptive fallback, not as a validated regime signal.")
    if meta.get("warning_text"):
        warnings.append(str(meta["warning_text"]))
    if status.failed_metrics:
        warnings.append(f"{status.failed_metrics} metric chunks failed in the latest IC job.")
    return warnings


def _cycle_investor_summary(
    *,
    ticker: str,
    metric_meta: dict[str, Any],
    current_regime: str | None,
    stock_rows,
    selected_metric: str | None,
    selected_ic,
    ic_ready: bool,
    confidence_label: str,
) -> str:
    latest = None
    if selected_metric:
        selected_rows = [row for row in stock_rows if row["metric_id"] == selected_metric and row["percentile"] is not None]
        latest = selected_rows[-1] if selected_rows else None
    pct = f"{float(latest['percentile']) * 100:.0f}th percentile" if latest else "an unavailable percentile"
    regime = phase_title(current_regime or "unknown")
    label = metric_meta["plain_label"]
    if not ic_ready or selected_ic is None:
        return f"{ticker} is at {pct} for {label}. No complete regime-conditioned return evidence is available yet for the selected VAE run, so this view is descriptive only."
    direction = "positive" if (selected_ic["avg_ic"] or 0) > 0 else "negative" if (selected_ic["avg_ic"] or 0) < 0 else "unclear"
    sample = int(selected_ic["total_obs"] or 0)
    return f"{ticker} is at {pct} for {label}. In the current {regime} regime, this driver has a {direction} historical relationship with forward returns. Sample size: {sample}. Confidence: {confidence_label.lower()}."


def _cycle_driver_group_summaries(options: list[CycleMetricOption], stock_rows: list[CycleStockMetricPoint], ic_ready: bool) -> list[CycleDriverGroupSummary]:
    latest_by_metric: dict[str, CycleStockMetricPoint] = {}
    for row in stock_rows:
        if row.percentile is not None:
            latest_by_metric[row.metric_id] = row
    by_group: dict[str, list[tuple[CycleMetricOption, CycleStockMetricPoint | None]]] = {}
    for option in options:
        by_group.setdefault(option.driver_group or "Profitability & Quality", []).append((option, latest_by_metric.get(option.metric_id)))
    out: list[CycleDriverGroupSummary] = []
    for group, items in by_group.items():
        abs_ics = [item.avg_abs_ic for item, _ in items if item.avg_abs_ic is not None]
        ics = [item.avg_ic for item, _ in items if item.avg_ic is not None]
        scores = []
        for option, stock_row in items:
            if not ic_ready or stock_row is None or stock_row.percentile is None or option.avg_ic is None:
                continue
            scores.append(stock_row.percentile if option.avg_ic >= 0 else 1.0 - stock_row.percentile)
        avg_abs = sum(abs_ics) / len(abs_ics) if abs_ics else None
        out.append(
            CycleDriverGroupSummary(
                driver_group=group,
                metric_count=len(items),
                avg_abs_ic=avg_abs,
                avg_ic=sum(ics) / len(ics) if ics else None,
                stock_score=sum(scores) / len(scores) if scores else None,
                confidence_label=_cycle_ic_confidence_label(sum((item.n_obs or 0) for item, _ in items), avg_abs) if ic_ready else "Insufficient data",
            )
        )
    return sorted(out, key=lambda item: (item.stock_score is None, -(item.stock_score or 0), item.driver_group))


def _cycle_regime_driver_grid(rows: list[CycleIcSummaryRow]) -> list[CycleRegimeDriverCell]:
    grouped: dict[tuple[str, str], list[CycleIcSummaryRow]] = {}
    for row in rows:
        grouped.setdefault((row.regime_label, row.driver_group or "Profitability & Quality"), []).append(row)
    out: list[CycleRegimeDriverCell] = []
    for (regime, group), items in grouped.items():
        ics = [item.avg_ic for item in items if item.avg_ic is not None]
        abs_ics = [item.avg_abs_ic for item in items if item.avg_abs_ic is not None]
        obs = [item.total_obs for item in items if item.total_obs is not None]
        out.append(
            CycleRegimeDriverCell(
                regime_label=regime,
                driver_group=group,
                avg_ic=sum(ics) / len(ics) if ics else None,
                avg_abs_ic=sum(abs_ics) / len(abs_ics) if abs_ics else None,
                total_obs=sum(obs) if obs else None,
            )
        )
    return sorted(out, key=lambda item: (item.regime_label, item.driver_group))


def phase_title(value: str) -> str:
    return str(value).replace("_", " ").replace("-", " ").title()


async def _fetch_cycle_ic_summary(conn, jurisdiction: str, run_id: str, horizon: str, patterns: tuple[str, ...] | None):
    rows = await conn.fetch(
        """
        SELECT regime_label, metric_id,
               COUNT(*) AS rows,
               AVG(spearman_ic) AS avg_ic,
               AVG(ABS(spearman_ic)) AS avg_abs_ic,
               SUM(COALESCE(n_obs, 0)) AS total_obs
        FROM   fact_equity_factor_ic_regime
        WHERE  jurisdiction = $1
          AND  run_id = $2
          AND  regime_source = 'cycle_model_probability'
          AND  forward_return_window = $3
          AND  spearman_ic IS NOT NULL
        GROUP  BY regime_label, metric_id
        ORDER  BY avg_abs_ic DESC NULLS LAST, total_obs DESC
        LIMIT  500
        """,
        jurisdiction,
        run_id,
        horizon,
    )
    return _filter_metric_rows(rows, patterns)[:80]


async def _fetch_cycle_stock_metrics(conn, jurisdiction: str, run_id: str, ticker: str, selected_metric: str | None, metric_options, tables: dict[str, str], ctx: dict[str, Any] | None = None):
    metric_ids = []
    if selected_metric:
        metric_ids.append(selected_metric)
    for row in metric_options[:6]:
        metric = row["metric_id"]
        if metric not in metric_ids:
            metric_ids.append(metric)
    if not metric_ids:
        return []
    metrics_table = tables["metrics"]
    company_table = tables["company"]
    ctx = ctx or {}
    peer_filters = []
    args: list[Any] = [jurisdiction, run_id, metric_ids, ticker]
    if ctx.get("gics_industry_group_code"):
        args.append(ctx["gics_industry_group_code"])
        peer_filters.append(f"d.gics_industry_group_code = ${len(args)}")
    elif ctx.get("gics_sector_code"):
        args.append(ctx["gics_sector_code"])
        peer_filters.append(f"d.gics_sector_code = ${len(args)}")
    peer_where = " AND " + " AND ".join(peer_filters) if peer_filters else ""
    month_expr = "(date_trunc('month', m.period_end + INTERVAL '90 days') + INTERVAL '1 month - 1 day')::date"
    sql = f"""
        WITH state_window AS (
            SELECT MIN(date)::date AS start_date, MAX(date)::date AS end_date
            FROM   fact_cycle_state_monthly
            WHERE  jurisdiction = $1 AND run_id = $2
        ),
        states AS (
            SELECT date, phase_label, confidence
            FROM   fact_cycle_state_monthly
            WHERE  jurisdiction = $1 AND run_id = $2
        ),
        raw_metrics AS (
            SELECT {month_expr} AS date,
                   m.ticker,
                   m.metric_id,
                   m.value::float AS value,
                   m.period_end
            FROM   {metrics_table} m
            CROSS  JOIN state_window w
            LEFT   JOIN {company_table} d ON d.primary_ticker = m.ticker
            WHERE  m.period_end IS NOT NULL
              AND  m.value IS NOT NULL
              AND  m.metric_id = ANY($3::text[])
              AND  {month_expr} BETWEEN w.start_date AND w.end_date
              {peer_where}
        ),
        dedup AS (
            SELECT DISTINCT ON (date, ticker, metric_id)
                   date, ticker, metric_id, value, period_end
            FROM   raw_metrics
            ORDER  BY date, ticker, metric_id, period_end DESC
        ),
        ranked AS (
            SELECT d.*,
                   percent_rank() OVER (PARTITION BY date, metric_id ORDER BY value) AS percentile,
                   (value - AVG(value) OVER (PARTITION BY date, metric_id))
                     / NULLIF(STDDEV_SAMP(value) OVER (PARTITION BY date, metric_id), 0) AS value_z,
                   COUNT(*) OVER (PARTITION BY date, metric_id) AS peer_count
            FROM   dedup d
        )
        SELECT r.date, r.metric_id, r.value, r.percentile, r.value_z, r.peer_count,
               s.phase_label, s.confidence
        FROM   ranked r
        JOIN   states s ON s.date = r.date
        WHERE  r.ticker = $4
        ORDER  BY r.date, r.metric_id
    """
    return await conn.fetch(sql, *args)


def _filter_metric_rows(rows, patterns: tuple[str, ...] | None):
    if patterns is None:
        return list(rows)
    out = []
    for row in rows:
        metric = str(row["metric_id"]).lower()
        is_market = any(p in metric for p in ("_ff3", "_ff4", "_ff5", "_ff6", "market_beta", "beta", "volatility", "momentum", "residual", "abnormal_return"))
        if patterns == ("__exclude_market_factor__",):
            if not is_market:
                out.append(row)
        elif any(pattern in metric for pattern in patterns):
            out.append(row)
    return out


def _cycle_dashboard_lineage(jurisdiction: str) -> dict[str, Any]:
    tables = _cycle_jurisdiction_tables(jurisdiction)
    return {
        "macro_label_timeline": {
            "source_table": "fact_cycle_state_monthly",
            "columns": ["run_id", "date", "jurisdiction", "phase_label", "phase_probabilities", "confidence", "uncertainty", "latent_cycle"],
            "grain": "monthly model output; quarter and six-month views are app aggregations of phase_probabilities",
        },
        "benchmark_labels": {
            "source_tables": ["fact_macro", "ref_macro_series"],
            "series_ids": list(_CYCLE_APP_BENCHMARK_SERIES.get(jurisdiction, ())),
            "grain": "macro recession/stress proxy aggregated to the selected dashboard period",
            "note": "US benchmark excludes forward-looking NY Fed 12M recession probability in the dashboard row; Japan uses the Cabinet Office CI proxy available in the local store.",
        },
        "model_runs": {
            "source_table": "fact_cycle_model_run",
            "columns": ["run_id", "model_family", "model_version", "train_start", "train_end", "hyperparams_json", "metrics_json", "artifact_path", "status"],
        },
        "feature_store": {
            "source_table": "fact_cycle_feature_monthly",
            "production_modalities": ["macro", "market"],
            "raw_sources": {
                "macro": ["fact_macro", "ref_macro_series"],
                "market": [tables["prices"], "fact_sector_returns", "fact_fama_french"],
                "fundamentals": [tables["metrics"], "not used in the macro-market production VAE/baselines"],
            },
        },
        "stock_lens": {
            "metric_source_table": tables["metrics"],
            "ic_source_table": "fact_equity_factor_ic_regime",
            "state_join_table": "fact_cycle_state_monthly",
        },
    }


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if -1e12 < out < 1e12 else None
    except (TypeError, ValueError):
        return None


def _normalize_model_family(value: str | None) -> str | None:
    if not value:
        return None
    family = value.lower().strip()
    if family not in {"pca", "dfm", "hmm", "vae"}:
        raise HTTPException(status_code=400, detail="model_family must be one of pca, dfm, hmm, vae.")
    return family


async def _fetch_cycle_run(conn, jurisdiction: str, *, run_id: str | None, model_family: str | None):
    if run_id:
        return await conn.fetchrow(
            """
            SELECT run_id, jurisdiction, model_family, model_version, trained_at
            FROM   fact_cycle_model_run
            WHERE  jurisdiction = $1
              AND  run_id = $2
              AND  ($3::text IS NULL OR model_family = $3)
              AND  status = 'complete'
            """,
            jurisdiction,
            run_id,
            model_family,
        )
    return await conn.fetchrow(
        """
        SELECT run_id, jurisdiction, model_family, model_version, trained_at
        FROM   fact_cycle_model_run
        WHERE  jurisdiction = $1
          AND  ($2::text IS NULL OR model_family = $2)
          AND  status = 'complete'
        ORDER  BY trained_at DESC
        LIMIT  1
        """,
        jurisdiction,
        model_family,
    )


async def _fetch_cycle_anchors(conn, jurisdiction: str) -> CycleAnchorLabels:
    out = CycleAnchorLabels()
    try:
        r = await conn.fetchrow(
            """
            SELECT period_end, quadrant, growth_z, inflation_z
            FROM   fact_macro_regime
            WHERE  jurisdiction = $1
            ORDER  BY period_end DESC
            LIMIT  1
            """,
            jurisdiction,
        )
        if r is not None:
            out.quadrant = r["quadrant"]
            out.quadrant_as_of = r["period_end"].isoformat() if r["period_end"] else None
            out.quadrant_growth_z = float(r["growth_z"]) if r["growth_z"] is not None else None
            out.quadrant_inflation_z = float(r["inflation_z"]) if r["inflation_z"] is not None else None
    except Exception as exc:
        logger.debug("macro.cycle quadrant anchor unavailable: %s", exc)

    try:
        r = await conn.fetchrow(
            """
            SELECT date, value, percentile, regime_label
            FROM   fact_macro_factor
            WHERE  factor_id = $1
            ORDER  BY date DESC
            LIMIT  1
            """,
            _CYCLE_FACTOR_ID_BY_JURISDICTION[jurisdiction],
        )
        if r is not None:
            out.pca_factor = float(r["value"]) if r["value"] is not None else None
            out.pca_percentile = float(r["percentile"]) if r["percentile"] is not None else None
            out.pca_label = r["regime_label"]
            out.pca_as_of = r["date"].isoformat() if r["date"] else None
    except Exception as exc:
        logger.debug("macro.cycle PCA anchor unavailable: %s", exc)

    try:
        r = await conn.fetchrow(
            """
            SELECT period_end, phase, score, recession_probability, confidence
            FROM   fact_macro_cycle_assessment
            WHERE  jurisdiction = $1
            ORDER  BY period_end DESC
            LIMIT  1
            """,
            jurisdiction,
        )
        if r is not None:
            out.rule_phase = r["phase"]
            out.rule_score = float(r["score"]) if r["score"] is not None else None
            out.rule_recession_probability = (
                float(r["recession_probability"]) if r["recession_probability"] is not None else None
            )
            out.rule_confidence = float(r["confidence"]) if r["confidence"] is not None else None
            out.rule_as_of = r["period_end"].isoformat() if r["period_end"] else None
    except Exception as exc:
        logger.debug("macro.cycle rule anchor unavailable: %s", exc)
    return out


def _cycle_state_point(row) -> CycleStatePoint:
    return CycleStatePoint(
        date=row["date"].isoformat() if row["date"] else "",
        phase_label=row["phase_label"],
        phase_probabilities=_float_payload(row["phase_probabilities"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        uncertainty=float(row["uncertainty"]) if row["uncertainty"] is not None else None,
        latent_cycle=[float(v) for v in (row["latent_cycle"] or [])],
        diagnostics=_json_payload(row["diagnostics_json"], {}),
        modality_contributions=_json_payload(row["modality_contrib_json"], {}),
    )


def _cycle_model_run(row) -> CycleModelRun:
    return CycleModelRun(
        run_id=row["run_id"],
        jurisdiction=str(row["jurisdiction"]).strip(),
        model_family=row["model_family"],
        model_version=row["model_version"],
        trained_at=row["trained_at"].isoformat() if row["trained_at"] else None,
        train_start=row["train_start"].isoformat() if row["train_start"] else None,
        train_end=row["train_end"].isoformat() if row["train_end"] else None,
        feature_set_version=row["feature_set_version"],
        hyperparams=_json_payload(row["hyperparams_json"], {}),
        metrics=_json_payload(row["metrics_json"], {}),
        artifact_path=row["artifact_path"],
        status=row["status"],
    )


def _json_payload(raw: object, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def _float_payload(raw: object) -> dict[str, float]:
    payload = _json_payload(raw, {})
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in payload.items():
        if value is None:
            continue
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


@router.get("/calendar", response_model=list[MacroCalendarItem])
async def macro_calendar(
    days: int = Query(14, ge=1, le=60),
    jurisdiction: Optional[str] = None,
) -> list[MacroCalendarItem]:
    juris = (jurisdiction or "").upper()
    where = ["r.release_at >= now() - INTERVAL '7 days'", f"r.release_at <= now() + INTERVAL '{int(days)} days'"]
    args: list = []
    if juris and juris != "GLOBAL":
        args.append(juris)
        where.append(f"s.jurisdiction = ${len(args)}")
    sql = f"""
        SELECT r.series_id, s.name AS label, s.jurisdiction,
               r.release_at, r.period_end, r.value
        FROM   fact_macro_release r
        JOIN   ref_macro_series   s ON s.series_id = r.series_id
        WHERE  {' AND '.join(where)}
        ORDER  BY r.release_at ASC
        LIMIT  200
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except Exception as exc:
        logger.warning("macro.calendar query failed: %s", exc)
        return []
    return [
        MacroCalendarItem(
            series_id=r["series_id"],
            label=r["label"],
            jurisdiction=r["jurisdiction"],
            release_at=r["release_at"].isoformat(),
            period_end=r["period_end"].isoformat(),
            value=r["value"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /curve — UST / JGB yield curve points + 2s10s + flow caption
# ---------------------------------------------------------------------------

_CURVE_SLOTS: dict[str, list[tuple[str, str]]] = {
    "US": [
        ("1M",  "us_1m_yield"),
        ("3M",  "us_3m_yield"),
        ("6M",  "us_6m_yield"),
        ("1Y",  "us_1y_yield"),
        ("2Y",  "us_2y_yield"),
        ("3Y",  "us_3y_yield"),
        ("5Y",  "us_5y_yield"),
        ("7Y",  "us_7y_yield"),
        ("10Y", "us_10y_yield"),
        ("20Y", "us_20y_yield"),
        ("30Y", "us_30y_yield"),
    ],
    "JP": [
        ("ON",  "jp_call_rate"),
        ("1Y",  "jp_1y_yield"),
        ("2Y",  "jp_2y_yield"),
        ("5Y",  "jp_5y_yield"),
        ("10Y", "jp_10y_yield"),
        ("20Y", "jp_20y_yield"),
        ("30Y", "jp_30y_yield"),
    ],
    "EZ": [
        ("Policy", "ez_policy_rate"),
        ("DFR",    "ez_deposit_rate"),
        ("1Y",     "ez_1y_yield"),
        ("2Y",     "ez_2y_yield"),
        ("5Y",     "ez_5y_yield"),
        ("10Y",    "ez_10y_yield"),
        ("20Y",    "ez_20y_yield"),
        ("30Y",    "ez_30y_yield"),
    ],
    "CH": [
        ("Policy", "ch_policy_rate"),
        ("10Y",    "ch_10y_yield"),
    ],
}


def _curve_caption(j: str, two_ten: float | None) -> str | None:
    if two_ten is None:
        return None
    if j == "US":
        if two_ten < 0:
            return "Inverted curve — banks pressured, defensives bid"
        if two_ten < 50:
            return "Flat — late-cycle posture; growth headwind"
        if two_ten > 150:
            return "Steep — cyclicals and financials favoured"
        return "Normal slope — neutral sector implications"
    if j == "JP":
        if two_ten < 0:
            return "Inverted JGB curve — BOJ tightening risk to financials"
        return "JGB curve steepening — Japanese banks earnings tailwind"
    return None


# /curve/history — last N days, used by the home curve card's History animation
_CURVE_HISTORY_CACHE: dict[tuple[str, int], tuple[float, MacroCurveHistory]] = {}
_CURVE_HISTORY_TTL = 600  # 10 minutes


@router.get("/curve/history", response_model=MacroCurveHistory | None)
async def macro_curve_history(
    jurisdiction: str = Query("US"),
    days: int = Query(252, ge=30, le=750),
) -> MacroCurveHistory | None:
    j = jurisdiction.upper()
    slots = _CURVE_SLOTS.get(j)
    if not slots:
        return None
    import time as _time
    cache_key = (j, days)
    cached = _CURVE_HISTORY_CACHE.get(cache_key)
    if cached and cached[0] > _time.time():
        return cached[1]

    slot_keys = [s for _, s in slots]
    tenor_by_slot = {s: t for t, s in slots}
    # Step 1: resolve story_tile_slot → series_id for the active slots.
    # Step 2: fetch last N days of (series_id, date, value), then pivot.
    sql = """
        WITH series AS (
            SELECT s.story_tile_slot AS slot, s.series_id
            FROM   ref_macro_series s
            WHERE  s.is_active = TRUE
              AND  s.story_tile_slot = ANY($1)
        )
        SELECT  s.slot, f.date, f.value
        FROM    series s
        JOIN    fact_macro f ON f.series_id = s.series_id
        WHERE   f.date >= CURRENT_DATE - ($2::int || ' days')::interval
          AND   f.value IS NOT NULL
        ORDER   BY f.date, s.slot
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, slot_keys, days)
    except Exception as exc:
        logger.warning("macro.curve_history query failed: %s", exc)
        return None

    if not rows:
        return MacroCurveHistory(
            jurisdiction=j, as_of=None, dates=[], tenors=[t for t, _ in slots], grid=[]
        )

    # Pivot: by_date[iso_date] = { slot: value }
    from collections import defaultdict
    by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        d = r["date"].isoformat()
        by_date[d][r["slot"]] = float(r["value"])

    # Keep only dates with full tenor coverage (rectangular matrix).
    tenors = [t for t, _ in slots]
    full_dates = sorted(d for d, m in by_date.items() if all(s in m for s in slot_keys))

    grid: list[list[float | None]] = []
    for d in full_dates:
        row_vals = by_date[d]
        grid.append([row_vals.get(slot) for _, slot in slots])

    as_of = full_dates[-1] if full_dates else None
    result = MacroCurveHistory(
        jurisdiction=j, as_of=as_of, dates=full_dates, tenors=tenors, grid=grid
    )
    _CURVE_HISTORY_CACHE[cache_key] = (_time.time() + _CURVE_HISTORY_TTL, result)
    return result


@router.get("/curve", response_model=MacroCurve | None)
async def macro_curve(jurisdiction: str = Query("US")) -> MacroCurve | None:
    j = jurisdiction.upper()
    slots = _CURVE_SLOTS.get(j)
    if not slots:
        return None
    sql = """
        WITH candidates AS (
            SELECT s.story_tile_slot AS slot, l.value, l.date
            FROM   ref_macro_series s
            LEFT   JOIN LATERAL (
                      SELECT value, date FROM fact_macro
                      WHERE  series_id = s.series_id ORDER BY date DESC LIMIT 1
                   ) l ON TRUE
            WHERE  s.is_active = TRUE
              AND  s.story_tile_slot = ANY($1)
        )
        SELECT DISTINCT ON (slot) slot, value, date
        FROM   candidates
        ORDER  BY slot, (date IS NULL), date DESC
    """
    slot_keys = [s for _, s in slots]
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, slot_keys)
    except Exception as exc:
        logger.warning("macro.curve query failed: %s", exc)
        return None
    by_slot = {r["slot"]: (r["value"], r["date"]) for r in rows}
    points = []
    as_of = ""
    for tenor, slot in slots:
        v, d = by_slot.get(slot, (None, None))
        if d and not as_of:
            as_of = d.isoformat()
        points.append(MacroCurvePoint(tenor=tenor, yield_pct=v))

    by_tenor = {p.tenor: p.yield_pct for p in points if p.yield_pct is not None}
    short_y = by_tenor.get("2Y") or by_tenor.get("3M") or by_tenor.get("ON")
    ten_y = by_tenor.get("10Y")
    two_ten_bp = (ten_y - short_y) * 100 if (short_y is not None and ten_y is not None) else None
    return MacroCurve(
        jurisdiction=j,
        as_of=as_of,
        points=points,
        two_s_ten_s_bp=two_ten_bp,
        flow_caption=_curve_caption(j, two_ten_bp),
    )


# ---------------------------------------------------------------------------
# /sector-beta — rolling betas of sectors to macro factors
# ---------------------------------------------------------------------------

@router.get("/sector-beta", response_model=list[MacroSectorBetaCell])
async def macro_sector_beta(jurisdiction: str = Query("US")) -> list[MacroSectorBetaCell]:
    sql = """
        SELECT DISTINCT ON (sector, factor) sector, factor, beta, t_stat
        FROM   mv_sector_macro_beta
        WHERE  jurisdiction = $1
        ORDER  BY sector, factor, date DESC
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, jurisdiction.upper())
    except Exception as exc:
        logger.warning("macro.sector_beta query failed (mv may not exist yet): %s", exc)
        return []
    return [
        MacroSectorBetaCell(sector=r["sector"], factor=r["factor"], beta=r["beta"], t_stat=r["t_stat"])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /earnings-revision — aggregate breadth overlay
# ---------------------------------------------------------------------------

class EarningsRevisionPoint(BaseModel):
    date: str
    jurisdiction: str
    breadth: float | None
    cycle_factor: float | None = None
    forward_pe: float | None = None
    # pe_method is "cap_weighted" when the snapshot has >=50 tickers with both
    # a valid forward_pe and market_cap; otherwise we fall back to the equal-
    # weighted trimmed mean. n_tickers reports the total universe size for the
    # snapshot (informational; the cap-weighted subset may be smaller).
    pe_method: Literal["cap_weighted", "equal_weighted"] | None = None
    n_tickers: int | None = None


@router.get("/earnings-revision", response_model=list[EarningsRevisionPoint])
async def macro_earnings_revision(months: int = Query(36, ge=6, le=120)) -> list[EarningsRevisionPoint]:
    """Aggregate NTM EPS revision breadth + forward P/E (from yfinance snapshots).

    Reads the v_earnings_revision_aggregate view introduced by migration 057.
    Returns [] until the yfinance snapshot pipeline has run at least once.
    """
    # avg_forward_pe is cap-weighted (added in migration 062). When market_cap
    # coverage is too thin (e.g. legacy rows from before 062 or a snapshot run
    # that hit broad yfinance rate-limits), avg_forward_pe is NULL and we fall
    # back to avg_forward_pe_eq, the equal-weighted trimmed mean. The 50-ticker
    # floor avoids reporting a noisy cap-weighted number from a tiny sample.
    # pe_method tells the frontend which branch fired so the chart can label
    # itself honestly.
    sql = """
        SELECT
            date,
            jurisdiction,
            breadth,
            cycle_factor,
            n_tickers,
            CASE
                WHEN n_cap_weighted >= 50 AND avg_forward_pe IS NOT NULL
                    THEN avg_forward_pe
                ELSE avg_forward_pe_eq
            END AS forward_pe,
            CASE
                WHEN n_cap_weighted >= 50 AND avg_forward_pe IS NOT NULL
                    THEN 'cap_weighted'
                ELSE 'equal_weighted'
            END AS pe_method
        FROM   v_earnings_revision_aggregate
        WHERE  date >= CURRENT_DATE - ($1::int || ' months')::interval
        ORDER  BY date ASC
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, months)
    except Exception as exc:
        logger.warning("macro.earnings_revision query failed: %s", exc)
        return []
    return [
        EarningsRevisionPoint(
            date=r["date"].isoformat(),
            jurisdiction=r["jurisdiction"],
            breadth=float(r["breadth"]) if r["breadth"] is not None else None,
            cycle_factor=float(r["cycle_factor"]) if r["cycle_factor"] is not None else None,
            forward_pe=float(r["forward_pe"]) if r["forward_pe"] is not None else None,
            pe_method=r["pe_method"],
            n_tickers=int(r["n_tickers"]) if r["n_tickers"] is not None else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /ticker-exposure — individual equity × macro
# ---------------------------------------------------------------------------

class TickerMacroFactor(BaseModel):
    factor: str           # 'growth' | 'inflation' | 'policy' | 'usd'
    beta: float | None
    t_stat: float | None
    r2: float | None = None


class TickerRegimeReturn(BaseModel):
    quadrant: str         # 'goldilocks' | 'reflation' | 'stagflation' | 'deflation'
    avg_monthly_return: float | None
    n_months: int


class TickerDrawdownPoint(BaseModel):
    date: str
    drawdown: float       # negative number, 0 = at peak
    quadrant: str | None  # cycle quadrant at this date (regime band)


class TickerMacroExposure(BaseModel):
    ticker: str
    jurisdiction: str
    as_of: str | None
    factors: list[TickerMacroFactor]
    regime_returns: list[TickerRegimeReturn]
    drawdown: list[TickerDrawdownPoint]
    caption: str | None = None


_QUADRANTS = ("goldilocks", "reflation", "stagflation", "deflation")


@router.get("/ticker-exposure", response_model=TickerMacroExposure | None)
async def macro_ticker_exposure(
    ticker: str = Query(..., min_length=1, max_length=24),
    jurisdiction: str = Query("US"),
    lang: Lang = "en",
) -> TickerMacroExposure | None:
    """Macro-factor exposure of a single ticker.

    Returns:
      - Latest 24M-rolling beta to each of the 4 macro factors (growth, inflation,
        policy, usd) from mv_ticker_macro_beta.
      - Avg monthly return broken down by cycle quadrant (Goldilocks / Reflation /
        Stagflation / Deflation), computed inline from fact_prices_* joined to
        fact_macro_regime (jurisdiction-scoped).
      - 5Y drawdown series with regime quadrant per point.

    Degrades to empty arrays when the supporting tables are empty (e.g. the
    compute job hasn't run yet) — never 500s.
    """
    juris = jurisdiction.upper()
    if juris not in ("US", "JP"):
        return None
    prices_table = "fact_prices_us" if juris == "US" else "fact_prices_jp"

    out = TickerMacroExposure(
        ticker=ticker,
        jurisdiction=juris,
        as_of=None,
        factors=[],
        regime_returns=[],
        drawdown=[],
        caption=None,
    )

    # ── 1. Latest factor betas ────────────────────────────────────────────
    factor_sql = """
        SELECT DISTINCT ON (factor) factor, beta, t_stat, r2, date
        FROM   mv_ticker_macro_beta
        WHERE  jurisdiction = $1 AND ticker = $2
        ORDER  BY factor, date DESC
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(factor_sql, juris, ticker)
            for r in rows:
                out.factors.append(TickerMacroFactor(
                    factor=r["factor"],
                    beta=float(r["beta"]) if r["beta"] is not None else None,
                    t_stat=float(r["t_stat"]) if r["t_stat"] is not None else None,
                    r2=float(r["r2"]) if r["r2"] is not None else None,
                ))
                if out.as_of is None and r["date"] is not None:
                    out.as_of = r["date"].isoformat()
    except Exception as exc:
        logger.warning("macro.ticker_exposure factor query failed (mv may not exist yet): %s", exc)

    # ── 2. Regime-conditioned returns + drawdown ─────────────────────────
    # Join monthly compound price returns to the cycle quadrant from
    # fact_macro_regime (migration 058). Fall back gracefully if either side
    # is empty.
    series_sql = f"""
        WITH px AS (
            SELECT date::date AS date, close
            FROM   {prices_table}
            WHERE  ticker = $1
              AND  date >= CURRENT_DATE - INTERVAL '5 years'
              AND  close IS NOT NULL
            ORDER  BY date
        ),
        monthly AS (
            SELECT DISTINCT ON (DATE_TRUNC('month', date))
                   DATE_TRUNC('month', date)::date AS month, close
            FROM   px
            ORDER  BY DATE_TRUNC('month', date), date DESC
        ),
        with_ret AS (
            SELECT month,
                   close,
                   close / LAG(close) OVER (ORDER BY month) - 1.0 AS ret
            FROM   monthly
        ),
        regime AS (
            SELECT period_end, quadrant
            FROM   fact_macro_regime
            WHERE  jurisdiction = $2
        ),
        joined AS (
            SELECT w.month, w.ret,
                   (SELECT r.quadrant
                      FROM regime r
                      WHERE r.period_end <= w.month
                      ORDER BY r.period_end DESC
                      LIMIT 1) AS quadrant
            FROM   with_ret w
            WHERE  w.ret IS NOT NULL
        )
        SELECT month, ret, quadrant FROM joined ORDER BY month
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(series_sql, ticker, juris)
    except Exception as exc:
        logger.warning("macro.ticker_exposure series query failed: %s", exc)
        rows = []

    # Regime-conditioned avg returns
    bucket: dict[str, list[float]] = {q: [] for q in _QUADRANTS}
    daily_for_dd: list[tuple] = []  # (date, ret, quadrant)
    for r in rows:
        q = (r["quadrant"] or "").lower()
        if q in bucket and r["ret"] is not None:
            bucket[q].append(float(r["ret"]))
        daily_for_dd.append((r["month"], float(r["ret"]) if r["ret"] is not None else 0.0, q if q in _QUADRANTS else None))

    for q in _QUADRANTS:
        vals = bucket[q]
        out.regime_returns.append(TickerRegimeReturn(
            quadrant=q,
            avg_monthly_return=(sum(vals) / len(vals)) if vals else None,
            n_months=len(vals),
        ))

    # Drawdown
    peak = 1.0
    cum = 1.0
    for d, ret, q in daily_for_dd:
        cum *= (1.0 + ret)
        if cum > peak:
            peak = cum
        dd = (cum / peak) - 1.0 if peak > 0 else 0.0
        out.drawdown.append(TickerDrawdownPoint(
            date=d.isoformat() if hasattr(d, "isoformat") else str(d),
            drawdown=dd,
            quadrant=q,
        ))

    # ── 3. Caption (bilingual, pre-cached) ───────────────────────────────
    caption_sql = """
        SELECT text FROM fact_macro_story
        WHERE  scope = 'ticker_macro'
          AND  scope_key = $1
          AND  lang IN ($2, 'en')
        ORDER  BY (lang = $2) DESC, generated_at DESC
        LIMIT  1
    """
    try:
        async with acquire() as conn:
            r = await conn.fetchrow(caption_sql, f"ticker:{ticker}", lang)
            if r and r["text"]:
                out.caption = r["text"]
    except Exception as exc:
        logger.warning("macro.ticker_exposure caption query failed: %s", exc)

    return out
