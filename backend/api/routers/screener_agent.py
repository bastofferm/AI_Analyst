"""Default screener agent — "value + sentiment scanner".

One-click scan for interesting, cheap stocks: a deterministic cheap+growing pre-filter
(reusing the screener's SQL) overlaid with sentiment mined from the MD&A text in the XBRL
filings (scored on demand by DeepSeek) plus news sentiment where the warehouse has it.
Returns a composite "interest" ranking with a per-name rationale.

MD&A tone carries the sentiment weight because it has broad coverage; news sentiment is a
best-effort bonus (only watchlisted/ingested tickers are scored in the warehouse).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime
from ..db import acquire
from ..quant import alpha_signal, ic_weights
from .screener import Range, ScreenerRunRequest, Sort, Universe, screener_run

router = APIRouter()
logger = logging.getLogger("mzqa.screener.agent")

# Curated "cheap + growing" preset. Tunable knobs are exposed on the request; the
# _SANITY bounds are fixed guards that drop obviously-bad warehouse rows (e.g. a
# free_cash_flow_yield of 18000% or a P/E of 0.1) so the scan surfaces real names.
_PRESET = {
    "max_pe": 20.0,
    "min_fcf_yield": 0.04,     # 4%
    "min_rev_yoy": 0.03,       # 3%
    "min_market_cap_usd": 1_000_000_000.0,
}
_SANITY = {
    "min_pe": 3.0,             # below this, P/E is almost always mis-scaled bad data
    "max_fcf_yield": 0.5,      # catalogue suggests <=25%; >50% is a data artifact
    "max_rev_yoy": 2.0,        # 200% YoY is a data artifact, not organic growth
}
_MDA_CHARS = 4000
_MDA_MISSING_NOTE = "No MD&A text found in the warehouse for this ticker."
_MDA_SCORE_FAILED_NOTE = "MD&A scoring failed; see backend logs."


class AgentRequest(BaseModel):
    jurisdiction: Literal["US", "JP", "INTL"] = "US"
    country_code: str | None = None   # ISO-2, only meaningful when jurisdiction=INTL
    region: str | None = None         # INTL region bucket (e.g. "Europe"), only meaningful when jurisdiction=INTL
    limit: int = Field(default=12, ge=3, le=25)   # shortlist scored for sentiment
    max_pe: float | None = _PRESET["max_pe"]
    min_fcf_yield: float | None = _PRESET["min_fcf_yield"]
    min_rev_yoy: float | None = _PRESET["min_rev_yoy"]
    min_market_cap_usd: float | None = _PRESET["min_market_cap_usd"]
    include_news: bool = True
    include_alpha: bool = True       # blend the qlib alpha model's expected return
    alpha_weight: float = Field(default=0.4, ge=0.0, le=0.9)  # dominant term when the model is available
    provider: str | None = None      # llm_providers id; None -> server default (DeepSeek)
    api_key: str | None = None
    model: str | None = None


class AgentRow(BaseModel):
    ticker: str
    name: str
    sector: str | None = None
    key_metrics: dict[str, float | None]
    mda_tone: float | None = None            # [-1, 1]
    mda_note: str | None = None
    mda_risk_flags: list[str] = Field(default_factory=list)
    news_sentiment: float | None = None
    alpha: float | None = None               # qlib model expected forward return (monthly)
    alpha_percentile: float | None = None    # rank of alpha within the shortlist (0-100)
    score_components: dict[str, float] = Field(default_factory=dict)  # normalized parts of the composite
    interest_score: float                    # 0-100
    rationale: str


class AgentResponse(BaseModel):
    rows: list[AgentRow]
    universe: dict[str, Any]
    scored_count: int
    scoring: dict[str, Any] = Field(default_factory=dict)  # weights/model actually used
    warnings: list[str] = Field(default_factory=list)


_MDA_SYSTEM = (
    "You are a buy-side analyst reading an excerpt of a company's latest MD&A (management "
    "discussion & analysis). Judge management's tone about the business outlook. Return JSON: "
    '{"tone": <number -1..1>, "guidance": "positive"|"neutral"|"negative", '
    '"risk_flags": [<=3 short strings], "note": "<=1 sentence"}. '
    "Base it ONLY on the text; if the excerpt is empty or boilerplate, tone=0 and guidance=neutral."
)


async def _fetch_mda(ticker: str, jurisdiction: str) -> str:
    # INTL companies have no MD&A source table — the scanner runs an MDA-less
    # composite instead (see _rank has_mda branch).
    if jurisdiction == "INTL":
        return ""
    if jurisdiction == "US":
        sql = """
            SELECT m.section_text
            FROM   fact_mda_sections_us m
            JOIN   dim_company_us d ON d.cik = m.cik
            WHERE  d.primary_ticker = $1 AND m.section_text IS NOT NULL
            ORDER  BY m.filed_date DESC NULLS LAST, m.char_count DESC NULLS LAST
            LIMIT  1
        """
    else:
        sql = """
            SELECT m.section_text
            FROM   fact_mda_sections_jp m
            JOIN   dim_company_jp d ON d.edinet_code = m.edinet_code
            WHERE  d.primary_ticker = $1 AND m.section_text IS NOT NULL
            ORDER  BY m.filed_date DESC NULLS LAST, m.char_count DESC NULLS LAST
            LIMIT  1
        """
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(sql, ticker)
    except Exception:  # noqa: BLE001
        logger.exception("MD&A fetch failed for %s", ticker)
        return ""
    return (row["section_text"] if row and row["section_text"] else "").strip()[:_MDA_CHARS]


async def _score_mda(ticker: str, jurisdiction: str, api_key: str, provider: str, model: str) -> dict[str, Any]:
    text = await _fetch_mda(ticker, jurisdiction)
    if not text:
        return {"tone": None, "note": _MDA_MISSING_NOTE, "risk_flags": [], "guidance": None}
    try:
        data = await llm_runtime.chat_json(
            api_key=api_key, provider=provider, model=model,
            system_prompt=_MDA_SYSTEM, user_prompt=text, temperature=0.1, max_tokens=300)
    except llm_runtime.LLMError as exc:
        logger.warning("MD&A scoring failed for %s: %s", ticker, exc)
        return {"tone": None, "note": _MDA_SCORE_FAILED_NOTE, "risk_flags": [], "guidance": None}
    tone = data.get("tone")
    try:
        tone = max(-1.0, min(1.0, float(tone))) if tone is not None else None
    except (TypeError, ValueError):
        tone = None
    flags = data.get("risk_flags")
    flags = [str(x) for x in flags][:3] if isinstance(flags, list) else []
    note = data.get("note")
    return {"tone": tone, "note": str(note) if note else None, "risk_flags": flags,
            "guidance": data.get("guidance")}


def _news_scores(tickers: list[str]) -> dict[str, float | None]:
    """Best-effort per-ticker avg news sentiment from the warehouse (sync)."""
    try:
        from ai_analyst.committee.newsmacro import news_summary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float | None] = {}
    for t in tickers:
        try:
            ns = news_summary(t)
            out[t] = ns.get("avg_sentiment") if ns.get("available") else None
        except Exception:  # noqa: BLE001
            out[t] = None
    return out


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _minmax(vals: list[float]) -> tuple[float, float]:
    lo, hi = min(vals), max(vals)
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


# Historical hard-coded component weights (keys: fcf, pe, g, tone, news), branched
# on data availability. These remain the exact fallback when neither the qlib alpha
# model nor regime-IC weights are available.
def _base_weights(has_mda: bool, has_news: bool) -> dict[str, float]:
    if has_mda:
        if has_news:
            return {"fcf": 0.28, "pe": 0.17, "g": 0.17, "tone": 0.22, "news": 0.16}
        return {"fcf": 0.32, "pe": 0.19, "g": 0.19, "tone": 0.30}
    # INTL: no MD&A — reweight (mda slice → fcf/pe/g/news).
    if has_news:
        return {"fcf": 0.36, "pe": 0.22, "g": 0.22, "news": 0.20}
    return {"fcf": 0.45, "pe": 0.27, "g": 0.28}


def _apply_ic_weights(weights: dict[str, float], ic_w: dict[str, float] | None) -> dict[str, float]:
    """Re-split the fundamental budget (fcf+pe = value, g = growth) by regime IC.

    Preserves the total fundamental budget and the internal fcf:pe ratio; leaves
    tone/news untouched. No-op when ``ic_w`` lacks both value and growth emphasis.
    """
    if not ic_w:
        return weights
    val_ic, grw_ic = ic_w.get("value"), ic_w.get("growth")
    if not val_ic or not grw_ic or (val_ic + grw_ic) <= 0:
        return weights
    w = dict(weights)
    base_val = w.get("fcf", 0.0) + w.get("pe", 0.0)
    fund = base_val + w.get("g", 0.0)
    if fund <= 0 or base_val <= 0:
        return weights
    val_budget = fund * val_ic / (val_ic + grw_ic)
    w["fcf"] = val_budget * w.get("fcf", 0.0) / base_val
    w["pe"] = val_budget * w.get("pe", 0.0) / base_val
    w["g"] = fund * grw_ic / (val_ic + grw_ic)
    return w


def _rank(
    rows: list[dict[str, Any]],
    tone_map: dict[str, dict],
    news_map: dict[str, float | None],
    *,
    has_mda: bool = True,
    alpha_map: dict[str, float | None] | None = None,
    ic_weights: dict[str, float] | None = None,
    alpha_weight: float = 0.4,
) -> list[AgentRow]:
    def col(key: str) -> dict[str, float]:
        return {r["ticker"]: v for r in rows if (v := _num((r.get("metrics") or {}).get(key))) is not None}

    fcf, pe, g = col("fcf_yield"), col("pe"), col("rev_yoy")
    fcf_lo, fcf_hi = _minmax(list(fcf.values()) or [0.0, 1.0])
    pe_lo, pe_hi = _minmax(list(pe.values()) or [0.0, 1.0])
    g_lo, g_hi = _minmax(list(g.values()) or [0.0, 1.0])

    # Learned alpha: normalize across the shortlist and only blend it in when the
    # model covers a meaningful share of names (else keep the exact legacy composite).
    alpha_map = alpha_map or {}
    alpha_vals = {t: a for t in (r["ticker"] for r in rows) if (a := alpha_map.get(t)) is not None}
    alpha_active = len(alpha_vals) >= max(3, (len(rows) + 1) // 2)
    a_lo, a_hi = _minmax(list(alpha_vals.values()) or [0.0, 1.0])
    ranked_alpha = sorted(alpha_vals.values())

    scored: list[AgentRow] = []
    for r in rows:
        t = r["ticker"]
        m = r.get("metrics") or {}
        tone = (tone_map.get(t) or {}).get("tone")
        news = news_map.get(t)
        # Normalized components in [0,1]; cheaper P/E is better (inverted).
        v = {
            "fcf": (fcf[t] - fcf_lo) / (fcf_hi - fcf_lo) if t in fcf else 0.5,
            "pe": 1.0 - (pe[t] - pe_lo) / (pe_hi - pe_lo) if t in pe else 0.5,
            "g": (g[t] - g_lo) / (g_hi - g_lo) if t in g else 0.5,
            "tone": (tone + 1.0) / 2.0 if tone is not None else 0.5,
            "news": (news + 1.0) / 2.0 if isinstance(news, (int, float)) else None,
        }
        weights = _apply_ic_weights(_base_weights(has_mda, v["news"] is not None), ic_weights)
        base_score = sum(w * v[k] for k, w in weights.items())

        alpha = alpha_map.get(t)
        v_alpha: float | None = None
        alpha_pct: float | None = None
        if alpha_active:
            v_alpha = (alpha - a_lo) / (a_hi - a_lo) if alpha is not None else 0.5
            score = alpha_weight * v_alpha + (1.0 - alpha_weight) * base_score
            if alpha is not None and ranked_alpha:
                below = sum(1 for x in ranked_alpha if x <= alpha)
                alpha_pct = round(100.0 * below / len(ranked_alpha), 1)
        else:
            score = base_score
        interest = round(100.0 * score, 1)

        components = {k: round(val, 4) for k, val in v.items() if val is not None}
        components["base"] = round(base_score, 4)
        if v_alpha is not None:
            components["alpha"] = round(v_alpha, 4)

        scored.append(AgentRow(
            ticker=t,
            name=r.get("name") or t,
            sector=r.get("sector"),
            key_metrics=m,
            mda_tone=tone,
            mda_note=(tone_map.get(t) or {}).get("note"),
            mda_risk_flags=(tone_map.get(t) or {}).get("risk_flags") or [],
            news_sentiment=news,
            alpha=alpha,
            alpha_percentile=alpha_pct,
            score_components=components,
            interest_score=interest,
            rationale=_rationale(m, tone_map.get(t) or {}, news, alpha),
        ))
    scored.sort(key=lambda x: x.interest_score, reverse=True)
    return scored


def _rationale(m: dict[str, Any], tone: dict[str, Any], news: float | None, alpha: float | None = None) -> str:
    bits: list[str] = []
    if alpha is not None:
        bits.append(f"alpha {alpha * 100:+.1f}%/mo")
    pe, fcf, g = _num(m.get("pe")), _num(m.get("fcf_yield")), _num(m.get("rev_yoy"))
    if pe is not None:
        bits.append(f"P/E {pe:.1f}")
    if fcf is not None:
        bits.append(f"FCF yield {fcf * 100:.1f}%")
    if g is not None:
        bits.append(f"rev {g * 100:+.0f}% YoY")
    if tone.get("guidance"):
        bits.append(f"MD&A tone {tone['guidance']}")
    if isinstance(news, (int, float)):
        bits.append(f"news {news:+.2f}")
    return " · ".join(bits) or "insufficient data"


@router.post("/agent/value-sentiment", response_model=AgentResponse)
async def value_sentiment_agent(req: AgentRequest) -> AgentResponse:
    warnings: list[str] = []

    # Tunable thresholds + fixed sanity bounds that drop obviously-bad warehouse rows.
    filters: dict[str, Range] = {}
    if req.max_pe is not None:
        filters["pe"] = Range(min=_SANITY["min_pe"], max=req.max_pe)
    if req.min_fcf_yield is not None:
        filters["fcf_yield"] = Range(min=req.min_fcf_yield, max=_SANITY["max_fcf_yield"])
    if req.min_rev_yoy is not None:
        filters["rev_yoy"] = Range(min=req.min_rev_yoy, max=_SANITY["max_rev_yoy"])
    if req.min_market_cap_usd is not None:
        filters["market_cap_usd"] = Range(min=req.min_market_cap_usd)

    # Sort the shortlist by size (not raw FCF yield): large cheap+growing names are
    # better covered for MD&A and less prone to the FCF-yield metric artifacts that
    # dominate small lenders/financials. The composite re-ranks on value+growth+tone.
    universe = Universe(
        jurisdiction=req.jurisdiction,
        country_code=req.country_code.strip().upper() if (req.country_code and req.jurisdiction == "INTL") else None,
        region=req.region.strip() if (req.region and req.jurisdiction == "INTL") else None,
    )
    run = await screener_run(ScreenerRunRequest(
        universe=universe, filters=filters, sort=Sort(key="market_cap_usd", dir="desc"), limit=req.limit))
    rows = [r.model_dump() for r in run.rows]
    if not rows:
        return AgentResponse(rows=[], universe=universe.model_dump(), scored_count=0,
                             warnings=["No names matched the value+growth preset — try relaxing the thresholds."])

    # MD&A tone (needs an LLM key). News sentiment is a best-effort bonus.
    # INTL has no MD&A source; skip the tone pass entirely and let _rank use its
    # MDA-less composite.
    has_mda_source = req.jurisdiction != "INTL"
    provider = llm_providers.normalize_id(req.provider)
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    tone_map: dict[str, dict] = {}
    if not has_mda_source:
        warnings.append("INTL: no MD&A available — composite uses valuation, growth, and news only.")
    elif api_key:
        model = llm_providers.chat_model(provider, req.model)
        results = await asyncio.gather(
            *[_score_mda(r["ticker"], req.jurisdiction, api_key, provider, model) for r in rows],
            return_exceptions=True,
        )
        for r, res in zip(rows, results):
            tone_map[r["ticker"]] = res if isinstance(res, dict) else {"tone": None}
        missing_text = sum(1 for res in tone_map.values() if res.get("note") == _MDA_MISSING_NOTE)
        score_failed = sum(1 for res in tone_map.values() if res.get("note") == _MDA_SCORE_FAILED_NOTE)
        if missing_text == len(rows):
            warnings.append("No MD&A text was found in the warehouse for this shortlist; run/extend the MD&A extraction pipeline.")
        elif missing_text:
            warnings.append(f"MD&A text is missing in the warehouse for {missing_text} of {len(rows)} shortlisted names.")
        if score_failed:
            warnings.append(f"MD&A scoring failed for {score_failed} shortlisted names; see backend logs.")
    else:
        _plabel = getattr(llm_providers.get(provider), "label", provider)
        warnings.append(f"No {_plabel} key — ranking uses valuation/growth + qlib alpha only (no MD&A tone).")

    news_map: dict[str, float | None] = {}
    if req.include_news:
        news_map = await asyncio.to_thread(_news_scores, [r["ticker"] for r in rows])
        if not any(v is not None for v in news_map.values()) and has_mda_source:
            warnings.append("No scored news in the warehouse for these names (MD&A tone carries sentiment).")

    # qlib learned alpha (expected return) + regime-conditioned factor-IC family weights.
    # Both degrade gracefully: no model / no IC table -> the exact legacy composite.
    tickers = [r["ticker"] for r in rows]
    alpha_map: dict[str, float | None] = {}
    ic_w: dict[str, float] | None = None
    if req.include_alpha:
        alpha_map = await asyncio.to_thread(alpha_signal.expected_returns, req.jurisdiction, tickers)
        ic_w = await asyncio.to_thread(ic_weights.family_weights, req.jurisdiction)
    alpha_covered = sum(1 for v in alpha_map.values() if v is not None)
    if req.include_alpha and alpha_covered == 0:
        warnings.append("No trained qlib alpha model for this jurisdiction — ranking uses the value+sentiment composite only.")

    scored = _rank(
        rows, tone_map, news_map, has_mda=has_mda_source,
        alpha_map=alpha_map, ic_weights=ic_w, alpha_weight=req.alpha_weight,
    )
    alpha_meta = alpha_signal.model_meta(req.jurisdiction) if req.include_alpha else None
    return AgentResponse(
        rows=scored,
        universe=universe.model_dump(),
        scored_count=sum(1 for r in scored if r.mda_tone is not None),
        scoring={
            "alpha_weight": req.alpha_weight if (req.include_alpha and alpha_covered >= max(3, (len(rows) + 1) // 2)) else 0.0,
            "alpha_covered": alpha_covered,
            "alpha_model": alpha_meta,
            "ic_family_weights": ic_w,
            "ic_weighting": "regime_ic" if ic_w else "fixed",
        },
        warnings=warnings,
    )
