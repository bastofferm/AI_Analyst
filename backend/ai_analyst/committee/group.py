"""Relative-value GROUP committee.

Runs ONE committee-style deliberation over a set of names (an industry or an
AI-screen result) and returns a ranked verdict + group thesis. The evidence is
the deterministic screener metric table (valuation / growth / profitability);
the tribunal only argues the cross-section on top of it. Offline-safe: with no
LLM key (or MZQA_COMMITTEE_DISABLE_LLM=1) it returns a deterministic composite
ranking so the endpoint always responds.

Entry point: ``run_group_committee(rows, universe, config, api_key, model, provider)``.
"""
from __future__ import annotations

import html as _html
import logging
import os
import statistics
import time
from typing import Any

import llm_providers

from . import archetypes, group_prompts

logger = logging.getLogger("mzqa.committee.group")

# metric_key -> weight. Positive = higher is more attractive; negative = cheaper
# (lower) is more attractive. Only keys present in the screener rows are used.
_SCORE_WEIGHTS: dict[str, float] = {
    "pe": -1.0,             # cheaper P/E better
    "ev_ebitda": -1.0,      # cheaper EV/EBITDA better
    "pb": -0.5,             # cheaper P/B better
    "fcf_yield": 1.0,       # higher FCF yield better
    "dividend_yield": 0.25,
    "rev_yoy": 0.9,         # faster growth better
    "rev_cagr_3y": 0.9,
    "gross_margin": 0.4,    # higher quality better (guards value traps)
    "operating_margin": 0.5,
}
_PCT_KEYS = {"fcf_yield", "dividend_yield", "rev_yoy", "rev_cagr_3y", "gross_margin", "operating_margin"}

# "Cheaper is better" multiples (negative weights). A NEGATIVE value here does not
# mean cheap — it means the denominator is negative: loss-making earnings, negative
# EBITDA, negative book equity. Ranking those on a cheapness scale rewards the most
# distressed name in the group (a P/B of -68 would z-score as the biggest bargain
# on the board), so they are excluded from the z-score population and reported as
# unscored instead. Metrics where a negative reading IS meaningful — a negative FCF
# yield really is worse than a positive one — keep their sign.
_POSITIVE_ONLY = {"pe", "ev_ebitda", "pb"}


def _scorable(key: str, value: float) -> bool:
    """False when the value exists but cannot be ranked on this metric's scale."""
    return not (key in _POSITIVE_ONLY and value <= 0)

# Display metadata for the per-name score breakdown the UI renders next to the
# ranking. `metric_id` is the warehouse metric the screener actually read
# (fact_market_metrics / fact_metrics_*), so the panel can name its own source.
_METRIC_META: dict[str, dict[str, str]] = {
    "pe":               {"label": "P/E (trailing)",   "metric_id": "price_to_earnings_trailing",                  "group": "Valuation"},
    "ev_ebitda":        {"label": "EV / EBITDA",      "metric_id": "enterprise_value_to_ebitda",                  "group": "Valuation"},
    "pb":               {"label": "P/B",              "metric_id": "price_to_book",                               "group": "Valuation"},
    "fcf_yield":        {"label": "FCF yield",        "metric_id": "free_cash_flow_yield",                        "group": "Valuation"},
    "dividend_yield":   {"label": "Dividend yield",   "metric_id": "dividend_yield",                              "group": "Valuation"},
    "rev_yoy":          {"label": "Revenue YoY",      "metric_id": "revenue_growth_year_over_year",               "group": "Growth"},
    "rev_cagr_3y":      {"label": "Revenue CAGR 3Y",  "metric_id": "revenue_compound_annual_growth_rate_3_year",  "group": "Growth"},
    "gross_margin":     {"label": "Gross margin",     "metric_id": "gross_margin",                                "group": "Quality"},
    "operating_margin": {"label": "Operating margin", "metric_id": "operating_margin",                            "group": "Quality"},
}


def _disabled() -> bool:
    return os.environ.get("MZQA_COMMITTEE_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes"}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _zscores(values: dict[str, float]) -> dict[str, float]:
    """Z-score a {ticker: value} map; returns {} when there is too little spread."""
    vals = list(values.values())
    if len(vals) < 2:
        return {}
    mean = statistics.fmean(vals)
    try:
        sd = statistics.pstdev(vals)
    except statistics.StatisticsError:
        return {}
    if not sd:
        return {}
    return {t: (v - mean) / sd for t, v in values.items()}


def deterministic_ranking(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Composite relative-value ranking from the screener metrics. Always available."""
    # Per-metric z-scores across the group.
    z_by_key: dict[str, dict[str, float]] = {}
    for key in _SCORE_WEIGHTS:
        present = {}
        for r in rows:
            v = _num((r.get("metrics") or {}).get(key))
            if v is not None and _scorable(key, v):
                present[r["ticker"]] = v
        zs = _zscores(present)
        if zs:
            z_by_key[key] = zs

    scores: dict[str, float] = {}
    for r in rows:
        t = r["ticker"]
        s = 0.0
        for key, w in _SCORE_WEIGHTS.items():
            z = z_by_key.get(key, {}).get(t)
            if z is not None:
                s += w * z
        scores[t] = s

    ordered = sorted(rows, key=lambda r: scores.get(r["ticker"], 0.0), reverse=True)
    n = len(ordered)
    out: list[dict[str, Any]] = []
    for i, r in enumerate(ordered):
        # Terciles → stance.
        if n >= 3 and i < max(1, n // 3):
            stance = "attractive"
        elif n >= 3 and i >= n - max(1, n // 3):
            stance = "expensive"
        else:
            stance = "fair"
        out.append({
            "ticker": r["ticker"],
            "name": r.get("name") or r["ticker"],
            "sector": r.get("sector"),
            "stance": stance,
            "composite_score": round(scores.get(r["ticker"], 0.0), 3),
            "rationale": _auto_rationale(r),
            "metrics": r.get("metrics") or {},
            "score_inputs": _score_inputs(r, z_by_key),
        })
    return out


def _score_inputs(row: dict[str, Any], z_by_key: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Per-metric audit trail for one name: the warehouse value, its z-score in
    this peer group, the weight applied, and the resulting contribution.

    The composite is just the sum of `contribution`, so the UI can show exactly
    why a name ranked where it did — including the metrics that were missing
    from the warehouse and therefore scored nothing.
    """
    metrics = row.get("metrics") or {}
    ticker = row["ticker"]
    inputs: list[dict[str, Any]] = []
    for key, weight in _SCORE_WEIGHTS.items():
        meta = _METRIC_META.get(key, {})
        value = _num(metrics.get(key))
        z = z_by_key.get(key, {}).get(ticker)
        if z is not None:
            note = None
        elif value is None:
            note = "missing"
        elif not _scorable(key, value):
            note = "negative"
        else:
            note = "no_spread"
        inputs.append({
            "key": key,
            "label": meta.get("label", key),
            "group": meta.get("group", "Other"),
            "metric_id": meta.get("metric_id"),
            "unit": "pct" if key in _PCT_KEYS else "ratio",
            "value": value,
            "z": round(z, 3) if z is not None else None,
            "weight": weight,
            "contribution": round(weight * z, 3) if z is not None else None,
            # Why this metric did not score: missing from the warehouse, negative
            # (so the multiple is meaningless), or flat across the whole group.
            "note": note,
        })
    # Biggest movers first; unscored metrics sink to the bottom.
    inputs.sort(key=lambda d: abs(d["contribution"]) if d["contribution"] is not None else -1, reverse=True)
    return inputs


def _auto_rationale(row: dict[str, Any]) -> str:
    m = row.get("metrics") or {}
    bits: list[str] = []
    pe, ev, fcf, g = _num(m.get("pe")), _num(m.get("ev_ebitda")), _num(m.get("fcf_yield")), _num(m.get("rev_yoy"))
    # Quote the cheapness multiples only when they are meaningful. A negative
    # P/E or EV/EBITDA is loss-making, not cheap, and it is excluded from the
    # score — printing it here would contradict the breakdown panel. Flag the
    # loss instead, which is the fact that actually matters.
    if pe is not None and _scorable("pe", pe):
        bits.append(f"P/E {pe:.1f}")
    if ev is not None and _scorable("ev_ebitda", ev):
        bits.append(f"EV/EBITDA {ev:.1f}")
    elif ev is not None:
        bits.append("EBITDA negative")
    if fcf is not None:
        bits.append(f"FCF yield {fcf * 100:.1f}%")
    if g is not None:
        bits.append(f"rev {g * 100:+.0f}% YoY")
    return " · ".join(bits) or "insufficient metrics"


# ------------------------------------------------------------------- LLM verdict

def _table_text(ranking: list[dict[str, Any]]) -> str:
    """Compact fixed-columns table the LLM reasons over (seeded by the composite)."""
    lines = ["ticker | name | sector | P/E | EV/EBITDA | P/B | FCF_yld% | rev_YoY% | op_margin% | seed_stance"]
    for r in ranking:
        m = r.get("metrics") or {}
        def g(k, pct=False):
            v = _num(m.get(k))
            if v is None:
                return "—"
            return f"{v * 100:.1f}" if pct else f"{v:.1f}"
        lines.append(" | ".join([
            r["ticker"], str(r.get("name") or "")[:28], str(r.get("sector") or ""),
            g("pe"), g("ev_ebitda"), g("pb"), g("fcf_yield", True), g("rev_yoy", True),
            g("operating_margin", True), r["stance"],
        ]))
    return "\n".join(lines)


def _extra_lenses(config: dict[str, Any]) -> str:
    cfg = config or {}
    extras = [*archetypes.specialist_roster(None, cfg), *(cfg.get("extra_analysts") or [])]
    out = []
    seen: set[str] = set()
    for a in extras:
        name = str((a or {}).get("name") or "").strip()
        mandate = str((a or {}).get("mandate") or "").strip()
        key = name.lower()
        if name and mandate and key not in seen:
            seen.add(key)
            out.append(f"- {name.upper()}: {mandate}")
    return ("\n".join(out) + "\n") if out else ""


def _llm_verdict(api_key: str, model: str, label: str, jurisdiction: str,
                 ranking: list[dict[str, Any]], config: dict[str, Any],
                 *, provider: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """One structured deliberation → ({thesis, ranking:[{ticker,stance,rationale}]}, None),
    or (None, reason) when every attempt fails. The reason (provider errors are already
    key-redacted) is surfaced in the response warnings so a silent verdict failure is
    diagnosable from the UI rather than only the server log.

    Structured output goes through JSON mode (response_format=json_object), NOT a forced
    tool call. Our OpenAI-dialect wire layer downgrades a forced tool_choice to "auto" to
    survive DeepSeek's thinking mode; after that a reasoning model — notably Kimi — answers
    in prose and the tool call never comes, so with_structured_output fails outright
    ("refused to issue a tool call"). JSON mode is honored by every OpenAI-dialect provider
    and is exactly what the screener uses, so it is the reliable path for the ranked verdict."""
    from pydantic import BaseModel, Field

    from .. import llm_runtime

    # Canonical ticker per upper-cased key, so we can tolerate a model that echoes the
    # ticker in a different case/whitespace and still map it back to a real row.
    canon = {r["ticker"].upper(): r["ticker"] for r in ranking}

    class RankItem(BaseModel):
        ticker: str
        stance: str = "fair"
        rationale: str = ""

    class GroupVerdict(BaseModel):
        thesis: str = ""
        ranking: list[RankItem] = Field(default_factory=list)

    system = group_prompts.GROUP_SYSTEM.format(
        label=label, jurisdiction=jurisdiction, extra_lenses=_extra_lenses(config))
    system += (
        "\n\nReturn ONLY a JSON object (no prose, no code fence) with this exact shape:\n"
        '{"thesis": "<3-6 sentence relative-value thesis>", '
        '"ranking": [{"ticker": "<one ticker copied from the table>", '
        '"stance": "attractive" | "fair" | "expensive", '
        '"rationale": "<one terse, evidence-anchored sentence>"}]}\n'
        "Include EVERY ticker from the table exactly once, ordered most → least attractive."
    )
    user = "EVIDENCE TABLE (seed_stance is the quantitative composite):\n" + _table_text(ranking)

    last: Exception | None = None
    for i in range(3):
        try:
            data = llm_runtime.chat_json(
                api_key=api_key, provider=provider, model=model,
                system_prompt=system, user_prompt=user,
                temperature=0.2, max_tokens=2400,
            )
            gv = GroupVerdict.model_validate(data)
            items: list[RankItem] = []
            for it in gv.ranking:
                key = (it.ticker or "").strip().upper()
                if key in canon:
                    it.ticker = canon[key]
                    items.append(it)
            if not items:
                last = ValueError(
                    f"model returned no table tickers (keys={list(data.keys())}, "
                    f"rows={len(gv.ranking)})")
                continue
            return {"thesis": (gv.thesis or "").strip(), "ranking": items}, None
        except Exception as exc:  # noqa: BLE001 — any failure falls back to the composite
            last = exc
            logger.warning("group verdict attempt %d/3 failed: %s", i + 1, exc)
            time.sleep(1.2 * (i + 1))
    logger.warning("group LLM verdict unavailable, using deterministic ranking: %s", last)
    reason = f"{last.__class__.__name__}: {last}" if last else "no verdict returned"
    return None, reason


def _merge_llm(base: list[dict[str, Any]], verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Reorder/annotate the deterministic ranking with the LLM's stances + rationales."""
    by_ticker = {r["ticker"]: r for r in base}
    order = [it.ticker for it in verdict["ranking"] if it.ticker in by_ticker]
    order += [t for t in by_ticker if t not in order]  # any names the LLM dropped go last
    llm_by = {it.ticker: it for it in verdict["ranking"]}
    out = []
    for t in order:
        r = dict(by_ticker[t])
        it = llm_by.get(t)
        if it:
            r["stance"] = (it.stance or r["stance"]).strip().lower()
            r["rationale"] = (it.rationale or r["rationale"]).strip()
        out.append(r)
    return out


# ----------------------------------------------------------------- HTML report

_STANCE_COLOR = {"attractive": "#16A34A", "fair": "#6F7890", "expensive": "#DC2626"}


def render_group_html(label: str, jurisdiction: str, ranking: list[dict[str, Any]], memo: str) -> str:
    def esc(s: Any) -> str:
        return _html.escape(str(s if s is not None else ""))

    def cell(v: Any, pct: bool = False) -> str:
        n = _num(v)
        if n is None:
            return "—"
        return f"{n * 100:.1f}%" if pct else f"{n:.1f}"

    rows_html = []
    for i, r in enumerate(ranking, 1):
        m = r.get("metrics") or {}
        color = _STANCE_COLOR.get(str(r.get("stance")), "#6F7890")
        rows_html.append(
            f"<tr>"
            f"<td style='text-align:right;color:#6F7890'>{i}</td>"
            f"<td><b>{esc(r['ticker'])}</b><div style='font-size:11px;color:#6F7890'>{esc(r.get('name'))}</div></td>"
            f"<td><span style='color:{color};font-weight:600;text-transform:capitalize'>{esc(r.get('stance'))}</span></td>"
            f"<td style='text-align:right'>{cell(m.get('pe'))}</td>"
            f"<td style='text-align:right'>{cell(m.get('ev_ebitda'))}</td>"
            f"<td style='text-align:right'>{cell(m.get('fcf_yield'), True)}</td>"
            f"<td style='text-align:right'>{cell(m.get('rev_yoy'), True)}</td>"
            f"<td style='font-size:12px;color:#2F4D73'>{esc(r.get('rationale'))}</td>"
            f"</tr>"
        )

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body{{font-family:Inter,'Segoe UI',sans-serif;color:#2F4D73;margin:0;padding:16px;background:#fff}}
      h1{{font-size:16px;margin:0 0 4px}} .label{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#6F7890}}
      .memo{{font-size:13px;line-height:1.5;margin:8px 0 16px;max-width:70ch;white-space:pre-wrap}}
      table{{border-collapse:collapse;width:100%;font-size:13px}}
      th{{text-align:left;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#6F7890;border-bottom:1px solid #DDD8CD;padding:6px 8px}}
      td{{border-bottom:1px solid #EEECE5;padding:6px 8px;vertical-align:top}}
    </style></head><body>
      <div class="label">Relative-value committee verdict</div>
      <h1>{esc(label)} · {esc(jurisdiction)} · {len(ranking)} names</h1>
      <div class="memo">{esc(memo)}</div>
      <table><thead><tr>
        <th>#</th><th>Name</th><th>Stance</th><th style="text-align:right">P/E</th>
        <th style="text-align:right">EV/EBITDA</th><th style="text-align:right">FCF yld</th>
        <th style="text-align:right">Rev YoY</th><th>Rationale</th>
      </tr></thead><tbody>{''.join(rows_html)}</tbody></table>
    </body></html>"""


# --------------------------------------------------------------------- entry

def run_group_committee(*, rows: list[dict[str, Any]], universe: dict[str, Any],
                        config: dict[str, Any] | None = None,
                        api_key: str | None = None, model: str | None = None,
                        provider: str | None = None) -> dict[str, Any]:
    config = config or {}
    warnings: list[str] = []
    label = _universe_label(universe)
    jurisdiction = str(universe.get("jurisdiction") or "US")
    prov = llm_providers.get(provider)

    ranking = deterministic_ranking(rows)
    memo = group_prompts.GROUP_MEMO_OFFLINE.format(label=label, jurisdiction=jurisdiction, n=len(rows))

    if api_key and not _disabled():
        structured_model = llm_providers.chat_model(prov.id, model or config.get("structured_model"))
        verdict, reason = _llm_verdict(api_key, structured_model, label, jurisdiction, ranking, config,
                                       provider=prov.id)
        if verdict:
            ranking = _merge_llm(ranking, verdict)
            memo = verdict["thesis"] or memo
        else:
            detail = f" ({reason})" if reason else ""
            warnings.append(f"{prov.label} verdict unavailable{detail}; showing the deterministic composite ranking.")
    elif not api_key:
        warnings.append(f"No {prov.label} key; showing the deterministic composite ranking only.")

    report_html = render_group_html(label, jurisdiction, ranking, memo)
    return {"ranking": ranking, "group_memo": memo, "report_html": report_html, "warnings": warnings}


def _universe_label(universe: dict[str, Any]) -> str:
    parts: list[str] = []
    if universe.get("industries"):
        parts.append("industries " + ", ".join(map(str, universe["industries"])))
    elif universe.get("sectors"):
        parts.append("sectors " + ", ".join(map(str, universe["sectors"])))
    if universe.get("exchanges"):
        parts.append("on " + ", ".join(map(str, universe["exchanges"])))
    return " ".join(parts) or "Screen result"
