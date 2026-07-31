"""Specialist analyst archetypes for the investment committee.

The core graph already knows how to add PM-defined analysts as extra nodes. This
module defines reusable built-in specialists and a small sector-priority policy
so each run can compile a richer, company-aware tribunal roster.
"""
from __future__ import annotations

from typing import Any


SPECIALIST_ANALYSTS: dict[str, dict[str, Any]] = {
    "growth_extrapolator": {
        "key": "growth_extrapolator",
        "name": "Growth Extrapolator",
        "focus": "Forecasting and extrapolation",
        "mandate": (
            "Specialize in identifying and extending historical performance trends. Focus on top-line "
            "revenue growth, market-share capture, mix shift, and operating leverage. Challenge the "
            "committee's conservative assumptions by testing the aggressive trend-following case where "
            "current growth momentum remains durable. Translate the case into explicit revenue-growth, "
            "margin, and reinvestment assumptions, and state where the historical trend stops being a "
            "usable guide."
        ),
    },
    "qoe_auditor": {
        "key": "qoe_auditor",
        "name": "Quality-of-Earnings Auditor",
        "focus": "Fundamental analysis",
        "mandate": (
            "Dig into the accounting mechanics behind reported performance. Reconcile earnings with "
            "operating cash flow and free cash flow; inspect accruals, working-capital movements, "
            "capitalization policies, SBC, one-offs, and capex intensity. Serve as the technical "
            "counterweight to growth claims by deciding whether growth is backed by high-quality cash "
            "generation or by accounting artifacts."
        ),
    },
    "relative_value_arbitrageur": {
        "key": "relative_value_arbitrageur",
        "name": "Relative-Value Arbitrageur",
        "focus": "Relative value",
        "mandate": (
            "Judge the asset against its peer group rather than in an intrinsic-value vacuum. Focus on "
            "P/E, EV/EBITDA, EV/EBIT, EV/FCF, FCF yield, growth-adjusted spreads, and where the target "
            "sits versus the 10 largest GICS peers. If the DCF says BUY, "
            "argue whether the market multiple is justified relative to similar firms."
        ),
    },
    "macro_regime_strategist": {
        "key": "macro_regime_strategist",
        "name": "Macro-Regime Strategist",
        "focus": "Forecasting and fundamental analysis",
        "mandate": (
            "Evaluate how external macro variables change the company-specific valuation. Connect rates, "
            "inflation, FX, credit spreads, commodity inputs, geopolitical risk, and risk appetite to WACC, "
            "terminal growth, terminal multiple, and scenario weights. Refine the deterministic packet so "
            "the model does not operate in a company-only vacuum."
        ),
    },
    "sensitivity_stress_tester": {
        "key": "sensitivity_stress_tester",
        "name": "Sensitivity Stress-Tester",
        "focus": "Forecasting and risk analysis",
        "mandate": (
            "Systematically break the thesis with what-if scenarios. Stress revenue growth, terminal "
            "margin, WACC, terminal growth, exit multiples, capex intensity, and working-capital needs. "
            "Find the break-even assumptions that flip the recommendation, and force the Lead Analyst to "
            "justify the probability-weighted fair value under small changes to core inputs."
        ),
    },
    "quant_factor_analyst": {
        "key": "quant_factor_analyst",
        "name": "Quantitative Factor Analyst",
        "focus": "Quantitative / statistical",
        "mandate": (
            "Interpret the machine-learned quant signals in the evidence packet's 'quant_signals' block: "
            "the cross-sectional qlib alpha model's expected forward return and its percentile rank in the "
            "universe, the model's out-of-sample rank IC (signal reliability), the factor-structured "
            "forward volatility and factor exposures, and the model-implied portfolio weight. Reconcile "
            "these statistical signals with the fundamental/DCF thesis: when the model's expected return "
            "and the intrinsic-value upside agree, say so and quantify the conviction; when they diverge "
            "(e.g. cheap on DCF but low or negative model alpha, or expensive but high alpha), flag the "
            "disagreement explicitly and reason about which is more trustworthy given the model's IC, the "
            "name's factor exposures, and the current macro regime. Never treat the model as ground truth — "
            "state its confidence and limitations. If 'quant_signals' is unavailable, say so briefly and "
            "defer to the fundamental case."
        ),
    },
}

_DEFAULT_ORDER = (
    "growth_extrapolator",
    "qoe_auditor",
    "relative_value_arbitrageur",
    "macro_regime_strategist",
    "sensitivity_stress_tester",
    "quant_factor_analyst",
)

_GROWTH_TERMS = (
    "technology", "software", "semiconductor", "interactive media", "communication services",
    "consumer discretionary", "health care", "biotechnology", "internet", "media",
)
_QOE_TERMS = (
    "industrial", "materials", "energy", "consumer staples", "financial", "bank", "insurance",
    "real estate", "reit", "utility", "utilities", "manufacturing", "capital goods",
)
_RELATIVE_VALUE_TERMS = (
    "financial", "bank", "insurance", "real estate", "reit", "utility", "utilities",
    "consumer staples", "materials", "energy",
)
_MACRO_TERMS = (
    "energy", "materials", "utility", "utilities", "real estate", "reit", "financial",
    "bank", "insurance", "consumer discretionary", "industrials",
)


def specialist_roster(company: dict[str, Any] | None, config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return built-in specialist analysts for a run.

    Config knobs:
    - enable_specialist_analysts: bool, default True.
    - specialist_analysts: "auto" | "all" | "none" | list[str], optional.
    - specialist_analyst_mode: same values, used when specialist_analysts is unset.
    - max_specialist_analysts: int, default all selected keys.
    """
    cfg = config or {}
    if not cfg.get("enable_specialist_analysts", True):
        return []

    keys = _selected_keys(cfg)
    keys = _sector_priority(keys, company or {})

    max_n = cfg.get("max_specialist_analysts")
    try:
        if max_n is not None:
            keys = keys[:max(0, int(max_n))]
    except (TypeError, ValueError):
        pass

    return [_roster_entry(k, company or {}) for k in keys]


def _selected_keys(cfg: dict[str, Any]) -> list[str]:
    requested = cfg.get("specialist_analysts", None)
    mode = cfg.get("specialist_analyst_mode", "auto")
    if requested is not None:
        if requested is False:
            return []
        if isinstance(requested, str):
            mode = requested
        elif isinstance(requested, (list, tuple)):
            return _valid_keys([str(k) for k in requested])

    mode_s = str(mode or "auto").strip().lower()
    if mode_s in {"none", "off", "false", "0"}:
        return []
    if mode_s in {"auto", "all", "default"}:
        return list(_DEFAULT_ORDER)
    if "," in mode_s:
        return _valid_keys([p.strip() for p in mode_s.split(",")])
    return _valid_keys([mode_s])


def _valid_keys(keys: list[str]) -> list[str]:
    out: list[str] = []
    for key in keys:
        k = key.strip().lower().replace("-", "_").replace(" ", "_")
        if k in SPECIALIST_ANALYSTS and k not in out:
            out.append(k)
    return out


def _sector_priority(keys: list[str], company: dict[str, Any]) -> list[str]:
    descriptor = " ".join(
        str(company.get(k) or "")
        for k in (
            "gics_sector_name", "gics_industry_group_name", "gics_industry_name",
            "gics_sub_industry_name", "mapping_sector",
        )
    ).lower()

    preferred: list[str] = []
    if _has_any(descriptor, _GROWTH_TERMS):
        preferred.append("growth_extrapolator")
    if _has_any(descriptor, _QOE_TERMS):
        preferred.append("qoe_auditor")
    if _has_any(descriptor, _RELATIVE_VALUE_TERMS):
        preferred.append("relative_value_arbitrageur")
    if _has_any(descriptor, _MACRO_TERMS):
        preferred.append("macro_regime_strategist")
    preferred.append("sensitivity_stress_tester")
    preferred.append("quant_factor_analyst")  # sector-agnostic: the quant signals apply to every name

    ordered = [k for k in preferred if k in keys]
    ordered.extend(k for k in _DEFAULT_ORDER if k in keys and k not in ordered)
    return ordered


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _roster_entry(key: str, company: dict[str, Any]) -> dict[str, Any]:
    spec = SPECIALIST_ANALYSTS[key]
    sector = company.get("gics_sector_name") or company.get("mapping_sector") or "unknown sector"
    industry = (
        company.get("gics_industry_name")
        or company.get("gics_industry_group_name")
        or company.get("gics_sub_industry_name")
        or "unknown industry"
    )
    mandate = (
        f"{spec['mandate']}\n\n"
        f"Company context: sector={sector}; industry={industry}. Adjust emphasis to this context while "
        "using only the supplied evidence packet."
    )
    return {
        "key": spec["key"],
        "name": spec["name"],
        "mandate": mandate,
        "focus": spec["focus"],
        "origin": "specialist",
        "emit_structured": True,
    }
