"""Branded committee-report renderer (HTML + PDF).

Produces an institutional equity-research note styled with the MZQA frontend
design tokens (warm off-white canvas #F5F4F0, navy #2F4D73, soft #DDD8CD card
borders, uppercase muted eyebrows, tabular numerals, green/red deltas). The PDF
is rendered by headless Chrome/Edge ``--print-to-pdf`` so CSS fidelity matches
the web terminal exactly.

Input is the committee graph's final state dict.
"""
from __future__ import annotations

import base64
import html as _html
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import charts
from . import metrics as metrics_mod


def output_root() -> Path:
    """Committee output directory, always inside the MZQA-Equity-Terminal repo.

    Override with ``MZQA_COMMITTEE_OUTPUT_DIR``. Defaults to ``<repo>/output/committee``
    (``output/`` is git-ignored, so reports live in the repo without polluting git).
    """
    env = os.environ.get("MZQA_COMMITTEE_OUTPUT_DIR")
    if env:
        return Path(env)
    # <repo>/backend/ai_analyst/committee/report_pdf.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3] / "output" / "committee"


def report_dir_for(ticker: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (ticker or "UNKNOWN").upper())
    return output_root() / datetime.now().strftime("%Y%m%d") / safe

# --- MZQA design tokens ---
# Shared with the quant research dossier via ai_analyst.report_style, so both documents
# render in one house style. Re-exported here because this module's templates reference the
# bare names throughout.
# Tribunal palette is deliberately MUTED (institutional, not alarm): Auditor/Base =
# navy (objectivity), Advocate = deep gedecktes green, Challenger = burgundy/brick.
from ..report_style import (  # noqa: E402
    AMBER, BG, BORDER, BORDER_SOFT, GREEN, MUTED, NAVY, NAVY2, NAVY3, PANEL, RED,
)

ADVOCATE = GREEN; CHALLENGER = RED; BASE = NAVY  # tribunal semantic aliases


# --------------------------------------------------------------------------- API

def write_report(state: dict[str, Any], out_dir: Path | str) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = render_html(state)
    html_path = out_dir / "committee_report.html"
    html_path.write_text(html_doc, encoding="utf-8")
    pdf_path = out_dir / "committee_report.pdf"
    ok = _html_to_pdf(html_path, pdf_path)
    return {"html": str(html_path), "pdf": str(pdf_path) if ok else None, "pdf_ok": ok}


# ----------------------------------------------------------------- HTML template

def render_html(state: dict[str, Any]) -> str:
    packet = state.get("financial_ratios") or {}
    company = packet.get("company") or {}
    name = company.get("name") or state.get("ticker") or "Company"
    ticker = company.get("ticker") or state.get("ticker") or ""
    sector = company.get("gics_sector_name") or company.get("mapping_sector") or ""
    juris = company.get("jurisdiction") or state.get("jurisdiction") or ""

    tri = state.get("triangulation") or {}
    primary = tri.get("primary_fair_value")
    price = tri.get("current_price")
    upside = tri.get("implied_upside_pct")
    # Prefer the committee's stated rating (from the memo) over a mechanical mapping,
    # so the hero badge matches the analyst conclusion in the body.
    reco, reco_color = _rating_from_memo(state) or _recommendation(upside)

    hero = _hero_header(state, company, name, ticker, juris, sector, reco, reco_color, primary, price, upside)
    banner = _kpi_banner(state)
    sec = _split_memo((state.get("memo") or {}).get("en") or "")
    executive = _executive_summary_card(state, sec, reco, primary, price, upside)
    story = "\n".join([
        _canonical_card(state),                # authoritative market snapshot & valuation ratios
        _yahoo_cross_check_card(state),        # independent Yahoo reconciliation vs SEC/EDINET
        _investment_case_spread(state, sec),   # thesis text | football-field + scenarios
        _dcf_model_section(state, sec),        # base-case projected IS + FCFF + valuation bridge
        _tribunal_spread(state, sec),          # Advocate/Challenger/Auditor, each paired with its chart
        _extra_analysts_card(state),           # specialist + user-added analysts (if any)
        _quarterly_trend_card(state),          # last-8-quarters revenue/margin momentum + TTM
        _capital_allocation_card(state, sec),  # capital-allocation text | capital-return chart
        _segment_story_card(state, sec),       # segment narrative + table
        _market_structure_card(state, sec),    # macro + 13F + price/multiples, integrated
        _risks_card(state, sec),               # falsification KPIs
    ])
    appendix = "\n".join([
        "<div class='appendix'><div class='appendix-title'>Appendix — auditable inputs</div>",
        _dcf_model_appendix(state),            # upside & downside full projected models
        _wacc_appendix(state),
        _sotp_appendix(state),
        _sensitivity_appendix(state),
        _comps_appendix(state),
        _scenario_appendix(state),
        _cashflow_appendix(state),
        _ownership_appendix(state),
        "</div>",
    ])

    footer = (
        '<div class="footer">Generated by the MZQA multi-agent investment committee (Advocate · Challenger · '
        'Auditor · specialist archetypes · Lead Analyst) on deepseek-reasoner. Headline fair value is the segment '
        'sum-of-the-parts (primary), cross-checked against a consolidated DCF and peer multiples. '
        'WACC is Fama-French / CAPM-derived. Native data from the xbrl_sec warehouse; not investment advice.</div>'
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{_esc(name)} — Investment Committee Memo</title>
<style>{_CSS}</style></head>
<body>{hero}<div class="page">{executive}{banner}{story}{footer}{appendix}</div></body></html>"""


# --------------------------------------------------------------------------- KPI banner

def _kpi_banner(state: dict[str, Any]) -> str:
    a = state.get("analytics") or {}
    w = a.get("wacc") or {}
    rg = state.get("reverse_dcf") or a.get("reverse_dcf") or {}
    rm = a.get("reverse_dcf_margin") or {}
    inc = a.get("incremental_roic") or {}
    boxes = []
    boxes.append(_kpi_box(_pct(w.get("wacc_pct")), "WACC (CAPM)", NAVY))
    if rg.get("implied_growth_pct") is not None:
        boxes.append(_kpi_box(_pct(rg.get("implied_growth_pct")), "Market-implied growth", NAVY))
    if rm.get("implied_margin_pct") is not None:
        suffix = "+" if rm.get("bounded") else ""
        col = CHALLENGER if rm.get("exceeds_peer_max") else NAVY
        boxes.append(_kpi_box(f"{rm['implied_margin_pct']:.0f}%{suffix}", "Implied EBIT margin", col))
    if inc.get("incremental_roic_pct") is not None:
        col = ADVOCATE if inc.get("value_accretive") else CHALLENGER
        boxes.append(_kpi_box(_pct(inc.get("incremental_roic_pct")), "Incremental ROIC", col))
    if not boxes:
        return ""
    return f"<div class='kpi-banner'>{''.join(boxes)}</div>"


def _kpi_box(value: str, label: str, color: str) -> str:
    return (f"<div class='kpi-box'><div class='kpi-v' style='color:{color}'>{value}</div>"
            f"<div class='kpi-l'>{_esc(label)}</div></div>")


def _hero_header(
    state: dict[str, Any],
    company: dict[str, Any],
    name: str,
    ticker: str,
    jurisdiction: str,
    sector: str,
    reco: str,
    reco_color: str,
    primary: Any,
    price: Any,
    upside: Any,
) -> str:
    subtitle = " - ".join(part for part in [ticker, jurisdiction, sector] if part)
    logo = _company_logo_html(company, name, ticker, jurisdiction)
    return f"""
    <div class="hero">
      <div class="hero-top">
        <div class="firm-brand">
          <div class="firm-mark">{_firm_logo_svg()}</div>
          <div>
            <div class="firm-name">MZQA</div>
            <div class="firm-sub">Financial Technologies LLC</div>
          </div>
        </div>
        <div class="asof">Investment Committee Memo<br>{_esc(datetime.now().strftime('%d %b %Y'))}</div>
      </div>
      <div class="hero-body">
        <div class="company-lockup">
          {logo}
          <div class="company-copy">
            <div class="eyebrow-hero">{_esc(subtitle)}</div>
            <div class="hero-title">{_esc(name)}</div>
            <div class="reco" style="color:{reco_color}">{_esc(reco)}</div>
          </div>
        </div>
        <div class="hero-stats">
          {_hero_stat('Fair value - SOTP base', _money(primary), None)}
          {_hero_stat('Current price', _money(price), None)}
          {_hero_stat('Implied upside', _signed_pct(upside), _delta_color(upside))}
        </div>
      </div>
    </div>"""


def _executive_summary_card(
    state: dict[str, Any],
    sec: dict[str, str],
    reco: str,
    primary: Any,
    price: Any,
    upside: Any,
) -> str:
    bullets: list[str] = []
    if primary is not None or upside is not None:
        bullets.append(
            f"{reco}: SOTP-primary fair value is {_money(primary)} versus current price {_money(price)}, "
            f"implying {_signed_pct(upside)} upside/downside."
        )
    rg = state.get("reverse_dcf") or (state.get("analytics") or {}).get("reverse_dcf") or {}
    rm = (state.get("analytics") or {}).get("reverse_dcf_margin") or {}
    if rg.get("implied_growth_pct") is not None or rm.get("implied_margin_pct") is not None:
        bullets.append(
            "Market-implied case: "
            f"{_pct(rg.get('implied_growth_pct'))} revenue growth and "
            f"{_pct(rm.get('implied_margin_pct'))} EBIT margin in the reverse-DCF reads."
        )
    mda = state.get("mda_analysis") or {}
    if mda.get("summary"):
        bullets.append(
            f"Management tone: {mda.get('guidance') or 'unscored'} MD&A guidance "
            f"with tone {mda.get('tone_score') if mda.get('tone_score') is not None else 'n/a'}; "
            f"{mda.get('summary')}"
        )
    capital = _first_plain_sentence(_msec(sec, "capital allocation", "capital"))
    if capital:
        bullets.append(capital)
    segments = _first_plain_sentence(_msec(sec, "segments"))
    if segments:
        bullets.append(segments)
    comments = state.get("specialist_comments") or []
    if comments:
        first = comments[0]
        first_bullet = ((first.get("bullets") or [])[:1] or [None])[0]
        if first_bullet:
            bullets.append(f"Specialist comment - {first.get('analyst')}: {first_bullet}")
    for names in [
        ("recommendation",),
        ("valuation",),
        ("market",),
        ("advocate",),
        ("challenger",),
        ("auditor",),
        ("risks",),
    ]:
        if len(bullets) >= 6:
            break
        sentence = _first_plain_sentence(_msec(sec, *names))
        if sentence and not any(sentence[:80] in existing for existing in bullets):
            bullets.append(sentence)
    if not bullets:
        bullets.append("Committee output is available, but the memo did not provide enough text for a richer executive summary.")
    items = "".join(f"<li>{_esc(b)}</li>" for b in bullets[:6])
    return f"<section class='exec-summary'><div class='exec-h'>Executive summary</div><ul>{items}</ul></section>"


def _firm_logo_svg() -> str:
    path = _terminal_root() / "ops" / "dashboard" / "assets" / "mzqa-nav-mark.svg"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return (
        '<svg width="42" height="28" viewBox="0 0 280 140" fill="none" xmlns="http://www.w3.org/2000/svg">'
        f'<line x1="0" y1="70" x2="280" y2="70" stroke="{NAVY}" stroke-width="2.5"/>'
        f'<path d="M0,70 C35,70 35,10 70,10 C105,10 105,130 140,130 C175,130 175,10 210,10 C245,10 245,70 280,70" stroke="{NAVY}" stroke-width="2.5" fill="none"/>'
        "</svg>"
    )


def _company_logo_html(company: dict[str, Any], name: str, ticker: str, jurisdiction: str) -> str:
    uri = _company_logo_data_uri(company, jurisdiction)
    if uri:
        return f"<div class='company-logo'><img src='{uri}' alt='{_esc(ticker)} logo'></div>"
    return f"<div class='company-logo logo-fallback'>{_esc(_initials(name, ticker))}</div>"


def _company_logo_data_uri(company: dict[str, Any], jurisdiction: str) -> str | None:
    path = _company_logo_path(company, jurisdiction)
    if not path:
        return None
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return None
    return f"data:image/png;base64,{encoded}"


def _company_logo_path(company: dict[str, Any], jurisdiction: str) -> Path | None:
    market = str(jurisdiction or company.get("jurisdiction") or "US").upper()
    folder = "logo_images_jp" if market == "JP" else "logo_images"
    root = _terminal_root()
    logo_dirs = [root / "company_metadata" / folder, root / "backend" / "company_metadata" / folder]
    raw_ids = [
        company.get("edinet_code") if market == "JP" else company.get("cik"),
        company.get("uid"),
        company.get("ticker"),
    ]
    for raw in raw_ids:
        file_id = _logo_file_id(raw, market)
        if not file_id:
            continue
        for logo_dir in logo_dirs:
            path = logo_dir / f"{file_id}.png"
            if path.is_file():
                return path
    return None


def _terminal_root() -> Path:
    env = os.environ.get("MZQA_TERMINAL_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3].parent / "MZQA-Equity-Terminal"


def _logo_file_id(value: Any, market: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if market == "US":
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits.zfill(10) if digits else raw
    return raw


def _initials(name: str, ticker: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return (ticker or "?")[:2].upper()


def _first_plain_sentence(text: str) -> str | None:
    plain = re.sub(r"<[^>]+>", " ", _md_to_html(text or ""))
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain:
        return None
    return re.split(r"(?<=[.!?])\s+", plain)[0][:260].rstrip()


# --------------------------------------------------------------------------- canonical snapshot

def _canonical_card(state: dict[str, Any]) -> str:
    """Authoritative market snapshot & valuation ratios. This is the single source
    of truth the tribunal is instructed to cite — rendered deterministically so the
    reader always sees the correct figures even if an analyst's prose drifts."""
    m = metrics_mod.canonical(state)
    if not m.get("available"):
        return ""

    def tile(val: str, lab: str, color: str | None = None) -> str:
        style = f" style='color:{color}'" if color else ""
        return f"<div class='tile'><div class='tile-v'{style}>{val}</div><div class='tile-l'>{_esc(lab)}</div></div>"

    def money(v: Any) -> str:
        s = _money(v)
        return f"${s}" if s != "—" else "—"

    def x(v: Any) -> str:
        return f"{float(v):.1f}x" if isinstance(v, (int, float)) else "—"

    nd = m.get("net_debt") or 0.0
    cash_tile = tile(money(-nd), "Net cash") if nd < 0 else tile(money(nd), "Net debt")
    inc = m.get("incremental_roic_pct")
    inc_col = ADVOCATE if (isinstance(inc, (int, float)) and m.get("incremental_roic_spread_pct", 0) and m["incremental_roic_spread_pct"] > 0) else None

    market = "".join([
        tile(money(m.get("as_of_price")), "Price"),
        tile(money(m.get("market_cap")), "Market cap"),
        tile(money(m.get("enterprise_value")), "Enterprise value"),
        cash_tile,
    ])
    has_ttm = bool(m.get("has_ttm"))
    pref = (lambda ttm_key, fy_key: m.get(ttm_key)) if has_ttm else (lambda ttm_key, fy_key: m.get(fy_key))
    mult_title = (f"Valuation multiples · TTM ({_esc(m.get('ttm_window'))})") if has_ttm else "Valuation multiples · latest FY"
    mult = "".join([
        tile(x(pref("pe_ttm", "pe")), "P/E"),
        tile(x(pref("ev_ebitda_ttm", "ev_ebitda")), "EV/EBITDA"),
        tile(x(pref("ev_ebit_ttm", "ev_ebit")), "EV/EBIT"),
        tile(x(pref("ev_fcf_ttm", "ev_fcf")), "EV/FCF"),
        tile(_pct(pref("fcf_yield_ttm_pct", "fcf_yield_pct")), "FCF yield"),
        tile(x(pref("p_fcf_ttm", "p_fcf")), "P/FCF"),
    ])
    returns = "".join([
        tile(_pct(m.get("roic_pct")), f"ROIC (FY{m.get('fiscal_year')})"),
        tile(_pct(inc), "Incremental ROIC", inc_col),
        tile(_signed_pct(m.get("incremental_roic_spread_pct")) + "pts", "ROIC − WACC"),
        tile(_pct(m.get("shareholder_yield_pct")), "Shareholder yield"),
        tile(_pct(m.get("wacc_pct")), "WACC"),
    ])
    cash = "".join([
        tile(money(m.get("operating_cash_flow")), f"Operating CF (FY{m.get('fiscal_year')})"),
        tile(money(m.get("capex")), "Capex"),
        tile(money(m.get("free_cash_flow")), "Free cash flow"),
        tile(_pct(m.get("capex_pct_revenue")), "Capex / revenue"),
    ])
    implied = "".join([
        tile(_pct(m.get("reverse_dcf_implied_growth_pct")), "Price implies rev. growth"),
        tile(_pct(m.get("reverse_dcf_implied_margin_pct")) + ("+" if m.get("reverse_dcf_margin_bounded") else ""),
             "Price implies EBIT margin"),
    ])
    body = (
        f"<div class='cap'><b>Authoritative snapshot</b> — every figure below is the committee's canonical value; "
        f"analyst prose is instructed to match it. 13F as of {_esc(m.get('ownership_quarter') or '—')}.</div>"
        f"<div class='mtitle'>Market</div><div class='tiles'>{market}</div>"
        f"<div class='mtitle'>{mult_title}</div><div class='tiles'>{mult}</div>"
        f"<div class='mtitle'>Returns &amp; capital allocation</div><div class='tiles'>{returns}</div>"
        f"<div class='mtitle'>Latest-FY cash flow</div><div class='tiles'>{cash}</div>"
        f"<div class='mtitle'>What the price implies (reverse-DCF)</div><div class='tiles'>{implied}</div>"
    )
    return _card("Market snapshot & valuation ratios", body)


def _yahoo_cross_check_card(state: dict[str, Any]) -> str:
    packet = state.get("financial_ratios") or {}
    check = packet.get("yahoo_cross_check") or {}
    yf = packet.get("yahoo_fundamentals") or {}
    if not check and not yf:
        return ""
    if not check.get("available"):
        note = check.get("note") or yf.get("note") or "No overlapping Yahoo statement rows were available."
        body = (
            "<div class='cap'><b>Advisory source:</b> "
            "Yahoo Finance fundamentals were requested as a cross-check against SEC/EDINET, "
            f"but no usable comparison was produced. {_esc(note)}</div>"
        )
        return _card("Yahoo Finance cross-check", body)

    rows = check.get("rows") or []
    rank = {"material": 0, "currency_mismatch": 1, "watch": 2, "informational": 3, "ok": 4}
    rows = sorted(rows, key=lambda r: (rank.get(str(r.get("severity")), 9), str(r.get("line_item_id") or "")))

    def sev_color(sev: str) -> str:
        if sev == "material":
            return RED
        if sev in {"watch", "currency_mismatch"}:
            return AMBER
        return GREEN

    def amount(value: Any, currency: Any) -> str:
        suffix = f" {_esc(currency)}" if currency else ""
        return f"{_money(value)}{suffix}"

    body_rows = ""
    for row in rows[:10]:
        sev = str(row.get("severity") or "n/a")
        body_rows += (
            f"<tr><td>{_esc(row.get('label') or row.get('line_item_id'))}</td>"
            f"<td class='num'>{_esc(row.get('standardized_fiscal_year'))}</td>"
            f"<td class='num'>{amount(row.get('standardized_value'), row.get('standardized_currency'))}</td>"
            f"<td class='num'>{_esc(row.get('yahoo_fiscal_year'))}</td>"
            f"<td class='num'>{amount(row.get('yahoo_value'), row.get('yahoo_currency'))}</td>"
            f"<td class='num' style='color:{_delta_color(row.get('pct_delta'))}'>{_signed_pct(row.get('pct_delta'))}</td>"
            f"<td><span style='color:{sev_color(sev)}'>{_esc(sev.replace('_', ' '))}</span></td></tr>"
        )
    table = (
        "<table class='grid'><thead><tr><th>Line item</th><th class='num'>SEC/EDINET FY</th>"
        "<th class='num'>SEC/EDINET</th><th class='num'>Yahoo FY</th><th class='num'>Yahoo</th>"
        "<th class='num'>Delta</th><th>Severity</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table>"
    ) if body_rows else "<div class='cap'>No overlapping canonical line items were available.</div>"
    summary = _esc(check.get("summary") or "Yahoo Finance cross-check completed.")
    source_table = check.get("source_table") or "fact_yfinance_fundamental_snapshot"
    body = (
        f"<div class='cap'><b>Advisory reconciliation</b> from sec.{_esc(source_table)} "
        f"as of {_esc(check.get('snapshot_date') or 'n/a')}. {summary}</div>"
        f"{table}"
    )
    return _card("Yahoo Finance cross-check", body)


# --------------------------------------------------------------------------- memo sectioning

_MEMO_HEAD_RE = re.compile(r"(?im)^\s*#{1,3}\s*([A-Za-z][A-Za-z &/\-]{2,30}?)\s*$")


def _split_memo(text: str) -> dict[str, str]:
    """Parse the sectioned memo (## HEADERS) into {lowercased_header: body_markdown}."""
    if not text:
        return {}
    matches = list(_MEMO_HEAD_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        key = m.group(1).strip().lower()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[key] = text[m.end():end].strip()
    return out


def _msec(sec: dict[str, str], *names: str) -> str:
    """Return the first matching memo section as HTML prose (fuzzy header match)."""
    for want in names:
        for k, v in sec.items():
            if want in k:
                return _md_to_html(v)
    return ""


# --------------------------------------------------------------------------- story spreads

def _investment_case_spread(state: dict[str, Any], sec: dict[str, str]) -> str:
    tri = state.get("triangulation") or {}
    rev = state.get("reverse_dcf") or {}
    methods = tri.get("methods") or []
    price = tri.get("current_price")
    prim = tri.get("primary_fair_value")
    football = charts.valuation_range_chart(
        [{"label": m["label"], "low": m.get("low"), "high": m.get("high"), "mid": m.get("mid")} for m in methods],
        price,
    )
    rows = ""
    for m in methods:
        rows += (f"<tr><td>{_esc(m['label'])}{' <span class=pill>primary</span>' if m.get('primary') else ''}</td>"
                 f"<td class='num'>{_money(m.get('low'))}</td><td class='num strong'>{_money(m.get('mid'))}</td>"
                 f"<td class='num'>{_money(m.get('high'))}</td></tr>")
    implied = rev.get("implied_growth_pct")
    rm = (state.get("analytics") or {}).get("reverse_dcf_margin") or {}
    reverse = ""
    if implied is not None:
        margin_clause = ""
        if rm.get("implied_margin_pct") is not None:
            perfect = rm.get("bounded") or rm.get("exceeds_peer_max")
            margin_clause = (
                f" Freeze growth and the price demands a <b>{rm['implied_margin_pct']:.0f}%"
                f"{'+' if rm.get('bounded') else ''} EBIT margin</b> (vs {rm.get('base_margin_pct','—')}% today"
                + (f", above the {rm['peer_max_margin_pct']:.0f}% best peer" if rm.get("peer_max_margin_pct") else "")
                + f") — {'priced for perfection.' if perfect else 'a demanding step-up.'}"
            )
        reverse = (f"<div class='note-box'><b>Reverse-DCF:</b> {_money(price)} implies "
                   f"<b>{implied:.0f}% revenue growth</b> at today's margin.{margin_clause}</div>")
    title = "Investment case"
    if prim and price:
        gap = (prim / price - 1) * 100
        title = (f"Three methods, one gap — SOTP fair value {_money(prim)} sits "
                 f"{abs(gap):.0f}% {'below' if gap < 0 else 'above'} the {_money(price)} price")
    narrative = _msec(sec, "recommendation") + _msec(sec, "valuation")
    if not narrative:
        narrative = _md_to_html(state.get("lead_synthesis") or "")
    mid_note = ("<div class='cap'>Mid = the method's central estimate: Sum-of-the-parts is the <b>base case</b>; "
                "Consolidated DCF is the <b>probability-weighted</b> value; Peer multiples is the median implied value.</div>")
    right = (f"{football}<table class='grid'><thead><tr><th>Method</th><th class='num'>Low</th>"
             f"<th class='num'>Mid</th><th class='num'>High</th></tr></thead><tbody>{rows}</tbody></table>{mid_note}")
    body = (f"<div class='spread-body'><div class='sp-text'>{narrative}{reverse}</div>"
            f"<div class='sp-fig'>{right}</div></div>")
    return _card(title, body)


def _tribunal_spread(state: dict[str, Any], sec: dict[str, str]) -> str:
    """The 3 analysts, each paired with the ONE exhibit that carries their argument."""
    a = state.get("analytics") or {}
    hist = a.get("cashflow_history") or []
    wacc = (a.get("wacc") or {}).get("wacc_pct") or 9.0
    inc = a.get("incremental_roic") or {}
    trend = a.get("segment_trend") or {}
    exhibits = {
        "advocate": charts.segment_trend_chart(trend, w=460, h=210) if trend else "",
        "challenger": charts.capex_history_chart(hist, w=460, h=210),
        "auditor": charts.roic_vs_wacc_chart(hist, wacc, inc, w=460, h=210),
    }
    labels = {"advocate": ("The Advocate", ADVOCATE), "challenger": ("The Challenger", CHALLENGER),
              "auditor": ("The Auditor", NAVY2)}
    rows_html = ""
    for key in ("advocate", "challenger", "auditor"):
        label, color = labels[key]
        text = _msec(sec, key)
        if not text:  # fallback: the agent's full reasoner thesis
            raw = (state.get(f"{key}_analysis") or "").strip()
            text = _md_to_html(raw[:1800] + ("…" if len(raw) > 1800 else ""))
        rows_html += (
            f"<div class='tri-row'>"
            f"<div class='tri-txt'><div class='tri-h' style='color:{color};border-color:{color}'>{label}</div>{text}</div>"
            f"<div class='tri-fig'>{exhibits.get(key, '')}</div></div>"
        )
    return _card("Core tribunal - three mindsets, each with its exhibit", rows_html)


def _quarterly_trend_card(state: dict[str, Any]) -> str:
    """Last-eight-quarters revenue/margin momentum + TTM line, from the quarterly
    series. Empty when the filer has no clean quarterly data (e.g. 20-F filers)."""
    a = state.get("analytics") or {}
    q = a.get("quarterly") or {}
    if not q.get("available"):
        try:
            from . import quarterly as _qm
            juris = state.get("jurisdiction") or ((state.get("financial_ratios") or {}).get("company") or {}).get("jurisdiction") or "US"
            q = _qm.quarterly_series(state.get("cik"), juris)
        except Exception:  # noqa: BLE001
            q = {}
    quarters = (q.get("quarters") or []) if q.get("available") else []
    if len(quarters) < 2:
        return ""

    chart = charts.quarterly_trend_chart(quarters)
    rows = ""
    for qq in quarters[-6:]:
        rows += (
            f"<tr><td>{_esc(qq.get('fiscal_period'))}'{_esc(str(qq.get('fiscal_year',''))[2:])}</td>"
            f"<td class='num'>${_money(qq.get('revenue'))}</td>"
            f"<td class='num' style='color:{_delta_color(qq.get('yoy_rev_growth_pct'))}'>{_signed_pct(qq.get('yoy_rev_growth_pct'))}</td>"
            f"<td class='num'>{_pct(qq.get('ebit_margin_pct'))}</td>"
            f"<td class='num'>{_pct(qq.get('fcf_margin_pct'))}</td></tr>"
        )
    ttm = q.get("ttm") or {}
    ttm_note = ""
    if ttm.get("available"):
        ttm_note = (f"<div class='cap'><b>TTM ({_esc(ttm.get('start_period_end'))}"
                    f"..{_esc(ttm.get('period_end'))}):</b> revenue ${_money(ttm.get('revenue'))}, "
                    f"EBIT margin {_pct(ttm.get('ebit_margin_pct'))}, FCF margin {_pct(ttm.get('fcf_margin_pct'))}.</div>")
    table = (f"<table class='grid'><thead><tr><th>Qtr</th><th class='num'>Revenue</th>"
             f"<th class='num'>YoY</th><th class='num'>EBIT mgn</th><th class='num'>FCF mgn</th></tr></thead>"
             f"<tbody>{rows}</tbody></table>{ttm_note}")
    body = f"<div class='spread-body'><div class='sp-text'>{table}</div><div class='sp-fig'>{chart}</div></div>"
    return _card("Quarterly momentum — the last eight quarters", body)


_BUILTIN_STANCES = {"advocate", "challenger", "auditor"}


def _extra_analysts_card(state: dict[str, Any]) -> str:
    """Render specialist/user-added analysts as compact comments beyond core stances."""
    comments = state.get("specialist_comments") or []
    if not comments:
        try:
            from .. import committee_comments
            comments = committee_comments.build_specialist_comments(state)
        except Exception:
            comments = []
    rows = ""
    for item in comments:
        if not isinstance(item, dict):
            continue
        bullets = item.get("bullets") or []
        if not bullets:
            continue
        bullet_html = "".join(f"<li>{_esc(b)}</li>" for b in bullets[:4])
        label = item.get("analyst") or str(item.get("analyst_key") or "").replace("_", " ").title()
        focus = item.get("focus") or item.get("origin") or "specialist"
        rows += (
            "<div class='specialist-comment'>"
            f"<div class='specialist-name'>{_esc(label)}</div>"
            f"<div class='specialist-focus'>{_esc(focus)}</div>"
            f"<ul>{bullet_html}</ul>"
            "</div>"
        )
    if not rows:
        rows = (
            "<div class='specialist-comment'>"
            "<div class='specialist-name'>No specialist comments captured</div>"
            "<div class='specialist-focus'>run configuration</div>"
            "<ul><li>Automatic specialist comments were not present in this run state. Re-run the standard single-stock analysis after the backend reloads to populate the sector-aware comments.</li></ul>"
            "</div>"
        )
    return _card("Specialist comments - sector-aware lenses", f"<div class='specialist-grid'>{rows}</div>")


def _capital_allocation_card(state: dict[str, Any], sec: dict[str, str]) -> str:
    a = state.get("analytics") or {}
    hist = a.get("cashflow_history") or []
    wacc = (a.get("wacc") or {}).get("wacc_pct") or 9.0
    inc = a.get("incremental_roic") or {}
    ret_chart = charts.capital_return_chart(hist)

    title = "Capital allocation & cash returns"
    if inc.get("available"):
        acc = inc.get("value_accretive")
        title = (f"Incremental ROIC of {inc['incremental_roic_pct']:.0f}% "
                 f"{'clears' if acc else 'trails'} the {wacc:.1f}% WACC — the capex build is "
                 f"{'value-accretive' if acc else 'value-dilutive'}")

    narrative = _msec(sec, "capital allocation", "capital")
    bullets = []
    if inc.get("available"):
        acc = inc.get("value_accretive"); col = ADVOCATE if acc else CHALLENGER
        bullets.append(f"<b style='color:{col}'>Incremental ROIC {inc['incremental_roic_pct']:.0f}%</b> vs "
                       f"WACC {wacc:.1f}% ({inc['spread_vs_wacc_pct']:+.0f}pts).")
    if len(hist) >= 2:
        f, l = hist[0], hist[-1]
        if f.get("fcf_margin") is not None and l.get("fcf_margin") is not None:
            bullets.append(f"<b>FCF margin</b> {_pct(f['fcf_margin'])} → {_pct(l['fcf_margin'])} as the build absorbed cash.")
        if l.get("capital_return"):
            bullets.append(f"<b>{_money(l['capital_return'])}</b> returned in FY{l['fiscal_year']} "
                           f"vs {_money(l.get('free_cash_flow'))} FCF.")
    takeaways = "<ul class='takeaways'>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>" if bullets else ""
    left = narrative + takeaways
    body = (f"<div class='spread-body'><div class='sp-text'>{left}</div>"
            f"<div class='sp-fig'>{ret_chart}</div></div>")
    return _card(title, body)


def _segment_story_card(state: dict[str, Any], sec: dict[str, str]) -> str:
    seg = state.get("segment_data") or {}
    rows = ""
    for r in (seg.get("structured") or [])[:8]:
        m = r.get("operating_margin")
        rows += (f"<tr><td>{_esc(r.get('segment'))}</td><td class='num'>{_money(r.get('revenue'))}</td>"
                 f"<td class='num'>{_money(r.get('operating_income'))}</td>"
                 f"<td class='num' style='color:{_margin_color(m)}'>{_pct(m*100) if m is not None else '—'}</td></tr>")
    narrative = _msec(sec, "segment")
    if not rows and not narrative:
        return ""
    table = (f"<table class='grid'><thead><tr><th>Segment</th><th class='num'>Revenue</th>"
             f"<th class='num'>Op. income</th><th class='num'>Op. margin</th></tr></thead><tbody>{rows}</tbody></table>")
    body = f"<div class='spread-body'><div class='sp-text'>{narrative}</div><div class='sp-fig'>{table}</div></div>"
    return _card("Segment economics — the mix behind the multiple", body)


def _risks_card(state: dict[str, Any], sec: dict[str, str]) -> str:
    prose = _msec(sec, "risk", "falsif", "kpi")
    if not prose:
        # fallback: pull "falsifies if" lines from the agent theses
        kpis = []
        for stance in ("advocate_analysis", "challenger_analysis", "auditor_analysis"):
            for line in (state.get(stance) or "").splitlines():
                if "falsif" in line.lower() and ":" in line:
                    kpis.append(line.split(":", 1)[-1].strip())
        if not kpis:
            return ""
        prose = "<ul>" + "".join(f"<li>{_esc(k)}</li>" for k in kpis[:6]) + "</ul>"
    return _card("What breaks the thesis — falsification KPIs", f"<div class='prose'>{prose}</div>")


def _market_structure_card(state: dict[str, Any], sec: dict[str, str]) -> str:
    a = state.get("analytics") or {}
    own = state.get("ownership") or {}
    macro = state.get("macro") or {}
    sig = macro.get("signal") or {}
    regime = macro.get("regime") or {}
    c = a.get("comps") or {}

    macro_tiles = (f"<div class='tiles'>"
                   f"<div class='tile'><div class='tile-v'>{_esc(regime.get('quadrant') or '—')}</div><div class='tile-l'>Macro regime</div></div>"
                   f"<div class='tile'><div class='tile-v'>{sig.get('ten_year','—')}%</div><div class='tile-l'>US 10Y</div></div>"
                   f"<div class='tile'><div class='tile-v'>{sig.get('yield_curve_2s10s','—')}</div><div class='tile-l'>2s10s (pp)</div></div>"
                   f"<div class='tile'><div class='tile-v'>{_esc(sig.get('tilt') or '—')}</div><div class='tile-l'>Scenario tilt</div></div>"
                   f"</div>")
    price_chart = charts.rebased_price_chart(a.get("price_history") or {}, state.get("ticker") or "")
    narrative = _msec(sec, "market")
    own_note = ""
    if own.get("available"):
        own_note = (f"<div class='cap'>13F: {own.get('holder_count')} filers · net <b>{own.get('net_direction')}</b> · "
                    f"passive {own.get('passive_share_of_reported_pct')}% of reported value (as of {own.get('quarter')}).</div>")
    left = macro_tiles + narrative + own_note
    right = price_chart or ""
    top = f"<div class='spread-body'><div class='sp-text'>{left}</div><div class='sp-fig'>{right}</div></div>"

    # Relative-value + ownership exhibits side by side (no stack).
    mult_chart = ""
    if c.get("available"):
        rows_for_chart = (c.get("sector_peers") or []) + ([c["target"]] if c.get("target") else [])
        med = (c.get("peer_median") or {}).get("ev_ebitda")
        mult_chart = charts.multiples_bar_chart(rows_for_chart, "ev_ebitda", "EV/EBITDA vs GICS peers",
                                                 state.get("ticker") or "", med)
    own_chart = charts.ownership_chart(own) if own.get("available") else ""
    bottom = f"<div class='two-col'><div>{mult_chart}</div><div>{own_chart}</div></div>" if (mult_chart or own_chart) else ""
    return _card("Market structure, macro & relative value", f"{top}{bottom}")


# --------------------------------------------------------------------------- DCF model

_IS_ROWS = [
    ("Revenue", "revenue", "money"), ("  growth", "growth_pct", "pct"),
    ("Cost of revenue", "cogs", "money"), ("Gross profit", "gross_profit", "money"),
    ("  gross margin", "gross_margin_pct", "pct"),
    ("Operating expenses", "operating_expenses", "money"),
    ("EBIT", "ebit", "moneyb"), ("  EBIT margin", "ebit_margin_pct", "pct"),
    ("Interest expense", "interest", "money"), ("Pre-tax income", "pretax", "money"),
    ("Income tax", "tax", "money"), ("Net income", "net_income", "moneyb"), ("EPS", "eps", "eps"),
]
_FCFF_ROWS = [
    ("EBIT", "ebit", "money"), ("NOPAT = EBIT×(1−t)", "nopat", "money"),
    ("+ D&A", "d_a", "money"), ("− Capex", "capex", "money"), ("− ΔNWC", "d_nwc", "money"),
    ("FCFF", "fcff", "moneyb"), ("Discount factor", "discount_factor", "factor"),
    ("PV of FCFF", "pv_fcff", "money"),
]


def _dcf_model_section(state: dict[str, Any], sec: dict[str, str]) -> str:
    m = state.get("dcf_model")
    if not m or not m.get("years"):
        return ""
    a = m.get("assumptions") or {}
    strip = ("<div class='assum'>"
             + _chip("WACC", _pct(a.get("wacc_pct")))
             + _chip("Terminal g", _pct(a.get("terminal_growth_pct")))
             + _chip("Tax rate", _pct(a.get("tax_rate_pct")))
             + _chip("Gross margin", _pct(m.get("gross_margin_pct")))
             + _chip("Capex / rev", _pct(a.get("capex_pct_of_rev")))
             + "</div>")
    walk = _msec(sec, "valuation")
    title = f"DCF model — base case → {_money(m.get('per_share_value'))} / share"
    body = (strip
            + (f"<div class='prose'>{walk}</div>" if walk else "")
            + _model_table("Projected income statement", _IS_ROWS, m["years"])
            + _model_table("Free cash flow to the firm & present value", _FCFF_ROWS, m["years"])
            + _bridge_table(m))
    return _card(title, body)


def _model_table(title: str, rows: list, years: list[dict[str, Any]]) -> str:
    head = "<th></th>" + "".join(f"<th class='num'>{_esc(y.get('year'))}</th>" for y in years)
    body = ""
    for label, key, fmt in rows:
        sub = label.startswith("  ")
        cls = "strong" if fmt.endswith("b") else ("sub" if sub else "")
        cells = "".join(f"<td class='num'>{_fmt_cell(y.get(key), fmt)}</td>" for y in years)
        body += f"<tr class='{cls}'><td class='mrow'>{_esc(label.strip())}</td>{cells}</tr>"
    return (f"<div class='mtitle'>{_esc(title)}</div>"
            f"<table class='grid model'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def _bridge_table(m: dict[str, Any]) -> str:
    sh = m.get("shares")
    nd = m.get("net_debt") or 0.0
    # equity = EV − net_debt. When net_debt < 0 the company holds NET CASH, so the
    # bridge ADDS it — label and sign must reflect that (was mislabelled "− Net debt").
    bridge_lbl, bridge_val = ("+ Net cash", _money(-nd)) if nd < 0 else ("− Net debt", _money(nd))
    rows = [
        ("Σ PV of explicit FCFF", _money(m.get("sum_pv_fcff")), False),
        ("+ PV of terminal value", _money(m.get("terminal_pv")), False),
        ("= Enterprise value", _money(m.get("enterprise_value")), True),
        (bridge_lbl, bridge_val, False),
        ("= Equity value", _money(m.get("equity_value")), True),
        (f"÷ Diluted shares ({_money(sh)})", "", False),
        ("= Value per share", _money(m.get("per_share_value")), True),
    ]
    body = "".join(
        f"<tr class='{'strong' if tot else ''}'><td class='mrow'>{_esc(lbl)}</td>"
        f"<td class='num'>{val}</td></tr>" for lbl, val, tot in rows)
    tv_share = ""
    if m.get("terminal_pv") and m.get("enterprise_value"):
        tv_share = f"<div class='cap'>Terminal value is {m['terminal_pv']/m['enterprise_value']*100:.0f}% of enterprise value.</div>"
    return f"<div class='mtitle'>Valuation bridge</div><table class='grid bridge'><tbody>{body}</tbody></table>{tv_share}"


def _dcf_model_appendix(state: dict[str, Any]) -> str:
    models = state.get("dcf_models") or {}
    rows = [("Revenue", "revenue", "money"), ("EBIT", "ebit", "money"),
            ("Net income", "net_income", "money"), ("EPS", "eps", "eps"),
            ("FCFF", "fcff", "money"), ("PV of FCFF", "pv_fcff", "money")]
    out = ""
    for label in ("upside", "downside"):
        m = models.get(label)
        if not m or not m.get("years"):
            continue
        out += (f"<div class='cap'><b>{label.upper()}</b> → {_money(m.get('per_share_value'))}/share "
                f"(WACC {_pct((m.get('assumptions') or {}).get('wacc_pct'))}, "
                f"EBIT margin {_pct((m.get('assumptions') or {}).get('ebit_margin_pct'))})</div>"
                + _model_table("", rows, m["years"]))
    return _card("A0 · Upside & downside DCF models", out) if out else ""


def _chip(label: str, value: str) -> str:
    return f"<span class='achip'><b>{value}</b> {_esc(label)}</span>"


def _fmt_cell(v: Any, fmt: str) -> str:
    if v is None:
        return "—"
    if fmt == "pct":
        return _pct(v)
    if fmt == "eps":
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return "—"
    if fmt == "factor":
        try:
            return f"{float(v):.3f}"
        except (TypeError, ValueError):
            return "—"
    return _money(v)


def _scenario_appendix(state: dict[str, Any]) -> str:
    val = state.get("scenarios") or {}
    pwfv = val.get("probability_weighted_fair_value")
    rows = ""
    for s in (val.get("scenarios") or []):
        ps = s.get("per_share_value")
        w = s.get("weight")
        g = s.get("rev_growth_pct")
        g_disp = (f"{sum(g)/len(g):.1f}%" if isinstance(g, list) and g else "—")
        rows += (
            f"<tr><td class='lbl'><span class='tag tag-{s.get('label')}'>{_esc(str(s.get('label','')).upper())}</span></td>"
            f"<td class='num'>{_money(ps)}</td>"
            f"<td class='num'>{_pct(w*100) if isinstance(w,(int,float)) else '—'}</td>"
            f"<td class='num'>{g_disp}</td>"
            f"<td class='num'>{_pct(s.get('wacc_pct')) if s.get('wacc_pct') is not None else '—'}</td>"
            f"<td class='num'>{_pct(s.get('ebit_margin_pct')) if s.get('ebit_margin_pct') is not None else '—'}</td></tr>"
        )
    return _card("Scenario valuation", f"""
      <table class="grid">
        <thead><tr><th>Scenario</th><th class='num'>Value / sh</th><th class='num'>Weight</th>
          <th class='num'>Rev g (avg)</th><th class='num'>WACC</th><th class='num'>EBIT margin</th></tr></thead>
        <tbody>{rows}</tbody>
        <tfoot><tr><td class='lbl'>PROBABILITY-WEIGHTED</td><td class='num strong'>{_money(pwfv)}</td>
          <td class='num'>100%</td><td></td><td></td><td></td></tr></tfoot>
      </table>""")


def _segment_card(seg: dict[str, Any]) -> str:
    rows = seg.get("structured") or []
    if not rows:
        return _card("Reportable segments", "<div class='empty'>No off-statement segment disclosure available for this filer.</div>")
    total = sum(r.get("revenue") or 0 for r in rows) or None
    body = ""
    for r in rows[:10]:
        rev = r.get("revenue"); m = r.get("operating_margin")
        mix = (rev / total * 100) if (rev and total) else None
        bar = ""
        if mix is not None:
            bar = f"<div class='bar'><span style='width:{min(100,mix):.1f}%'></span></div>"
        body += (
            f"<tr><td>{_esc(r.get('segment',''))}</td>"
            f"<td class='num'>{_money(rev)}</td>"
            f"<td class='num'>{_pct(mix) if mix is not None else '—'}{bar}</td>"
            f"<td class='num'>{_money(r.get('operating_income'))}</td>"
            f"<td class='num' style='color:{_margin_color(m)}'>{_pct(m*100) if m is not None else '—'}</td></tr>"
        )
    src = seg.get("source")
    note = f"<span class='src'>source: {_esc(str(src))}</span>" if src else ""
    return _card(f"Reportable segments {note}", f"""
      <table class="grid">
        <thead><tr><th>Segment</th><th class='num'>Revenue</th><th class='num'>Mix</th>
          <th class='num'>Op. income</th><th class='num'>Op. margin</th></tr></thead>
        <tbody>{body}</tbody></table>""")


def _snapshot_card(packet: dict[str, Any]) -> str:
    metrics = _latest_metrics(packet)
    tiles = ""
    order = [
        ("revenue_growth_year_over_year", "Revenue growth YoY", "pct"),
        ("earnings_before_interest_taxes_depreciation_amortization_margin", "EBITDA margin", "pct"),
        ("operating_margin", "Operating margin", "pct"),
        ("net_profit_margin", "Net margin", "pct"),
        ("return_on_invested_capital", "ROIC", "pct"),
        ("free_cash_flow_margin", "FCF margin", "pct"),
    ]
    for mid, lab, kind in order:
        v = metrics.get(mid)
        if v is None:
            continue
        disp = _pct(v * 100) if abs(v) <= 5 else _pct(v)
        tiles += f"<div class='tile'><div class='tile-v'>{disp}</div><div class='tile-l'>{_esc(lab)}</div></div>"
    if not tiles:
        tiles = "<div class='empty'>No derived metrics available.</div>"
    return _card("Financial snapshot (latest FY)", f"<div class='tiles'>{tiles}</div>")


def _tribunal_card(state: dict[str, Any]) -> str:
    cols = ""
    for stance, color, label in [("advocate_analysis", GREEN, "The Advocate"),
                                 ("challenger_analysis", RED, "The Challenger"),
                                 ("auditor_analysis", NAVY2, "The Auditor")]:
        txt = state.get(stance) or "—"
        cols += (
            f"<div class='col'><div class='col-h' style='border-color:{color};color:{color}'>{label}</div>"
            f"<div class='col-b'>{_md_to_html(txt)}</div></div>"
        )
    return _card("The tribunal", f"<div class='tri'>{cols}</div>")


def _synthesis_card(state: dict[str, Any]) -> str:
    syn = state.get("lead_synthesis") or "—"
    return _card("Lead analyst synthesis", f"<div class='prose'>{_md_to_html(syn)}</div>")


def _kpi_card(state: dict[str, Any]) -> str:
    kpis = []
    for hist in (state.get("committee_chat_history") or []):
        content = hist.get("content") or ""
        for line in content.splitlines():
            if line.lower().startswith("falsifies if"):
                kpis.append((hist.get("role", ""), line.split(":", 1)[-1].strip()))
    if not kpis:
        return ""
    items = "".join(
        f"<li><span class='tag tag-{r}'>{_esc(r.upper())}</span> {_esc(k)}</li>" for r, k in kpis[:9]
    )
    return _card("Thesis-falsification KPIs", f"<ul class='kpi'>{items}</ul>")


def _commentary_card(state: dict[str, Any]) -> str:
    memo = (state.get("memo") or {}).get("en") or ""
    if not memo:
        return ""
    return _card("Analyst commentary", f"<div class='prose'>{_md_to_html(memo)}</div>")


# --------------------------------------------------------------------------- appendix

def _wacc_appendix(state: dict[str, Any]) -> str:
    w = (state.get("analytics") or {}).get("wacc") or {}
    if not w:
        return ""
    trail = "".join(f"<li>{_esc(x)}</li>" for x in (w.get("audit_trail") or []))
    betas = w.get("betas") or {}
    beta_line = ", ".join(f"{k}={v:.2f}" for k, v in betas.items() if isinstance(v, (int, float)))
    prem = w.get("premia_pct") or {}
    prem_line = ", ".join(f"{k}={v:.2f}%" for k, v in prem.items() if isinstance(v, (int, float)))
    return _card("A1 · WACC derivation (Fama-French / CAPM)", f"""
      <ol class='audit'>{trail}</ol>
      <div class='cap'>Factor model: {_esc(w.get('factor_model'))} (adj R²={_esc(round(w.get('adj_r2') or 0,2))}). Betas: {_esc(beta_line)}.
      Annualized premia: {_esc(prem_line)}. FF5 multi-factor Re (exhibit) = {_pct(w.get('cost_of_equity_ff5_pct'))}.</div>""")


def _sotp_appendix(state: dict[str, Any]) -> str:
    sotp = state.get("sotp") or {}
    segs = sotp.get("segments_base") or []
    if not segs:
        return ""
    rows = ""
    for s in segs:
        rows += (f"<tr><td>{_esc(s.get('segment'))}</td><td class='num'>{_money(s.get('revenue'))}</td>"
                 f"<td class='num'>{_pct(s.get('growth_pct'))}</td><td class='num'>{_pct(s.get('operating_margin_pct'))}</td>"
                 f"<td class='num'>{s.get('fcf_conversion','—')}</td><td class='num'>{_pct(s.get('wacc_pct'))}</td>"
                 f"<td class='num strong'>{_money(s.get('enterprise_value'))}</td></tr>")
    ps = sotp.get("per_share") or {}
    footer = (f"<div class='cap'>Per-share by scenario: upside {_money(ps.get('upside'))} · base {_money(ps.get('base'))} · "
              f"downside {_money(ps.get('downside'))} · weighted {_money(sotp.get('weighted_per_share'))}.</div>")
    return _card("A2 · Sum-of-the-parts (base case, per segment)", f"""
      <table class="grid"><thead><tr><th>Segment</th><th class='num'>Revenue</th><th class='num'>Growth</th>
        <th class='num'>Op. margin</th><th class='num'>FCF conv.</th><th class='num'>Segment WACC</th><th class='num'>EV</th></tr></thead>
      <tbody>{rows}</tbody></table>{footer}""")


def _sensitivity_appendix(state: dict[str, Any]) -> str:
    grid = (state.get("analytics") or {}).get("sensitivity_grid") or {}
    price = (state.get("triangulation") or {}).get("current_price")
    heat = charts.sensitivity_heat(grid, price)
    if not heat:
        return ""
    return _card("A3 · DCF sensitivity — revenue growth × WACC (per share)",
                 f"{heat}<div class='cap'>Cells at/above the current price are shown in bold.</div>")


def _comps_appendix(state: dict[str, Any]) -> str:
    c = (state.get("analytics") or {}).get("comps") or {}
    if not c.get("available"):
        return ""
    def tbl(rows, title):
        body = ""
        for r in rows:
            body += (f"<tr><td class='lbl'>{_esc(r.get('ticker'))}</td>"
                     f"<td class='num'>{_xx(r.get('pe'))}</td><td class='num'>{_xx(r.get('ev_ebitda'))}</td>"
                     f"<td class='num'>{_xx(r.get('ev_ebit'))}</td><td class='num'>{_xx(r.get('ev_revenue'))}</td>"
                     f"<td class='num'>{_xx(r.get('ev_fcf'))}</td><td class='num'>{_xx(r.get('peg'))}</td></tr>")
        return (f"<div class='cap'>{title}</div><table class='grid'><thead><tr><th></th><th class='num'>P/E</th>"
                f"<th class='num'>EV/EBITDA</th><th class='num'>EV/EBIT</th><th class='num'>EV/Rev</th>"
                f"<th class='num'>EV/FCF</th><th class='num'>PEG</th></tr></thead><tbody>{body}</tbody></table>")
    rows = ([c["target"]] if c.get("target") else []) + (c.get("sector_peers") or [])
    note = f"<div class='cap'>{_esc(c.get('selection_rule') or 'Largest GICS peers by latest market cap.')}</div>"
    return _card("A4 - Comparable companies", note + tbl(rows, "Target and 10 largest GICS peers"))


def _cashflow_appendix(state: dict[str, Any]) -> str:
    hist = (state.get("analytics") or {}).get("cashflow_history") or []
    if not hist:
        return ""
    rows = ""
    for r in hist:
        rows += (f"<tr><td class='lbl'>FY{r.get('fiscal_year')}</td>"
                 f"<td class='num'>{_money(r.get('revenue'))}</td><td class='num'>{_money(r.get('operating_cash_flow'))}</td>"
                 f"<td class='num'>{_money(r.get('capex'))}</td><td class='num'>{_money(r.get('free_cash_flow'))}</td>"
                 f"<td class='num'>{_money(r.get('sbc'))}</td><td class='num'>{_money(r.get('buybacks'))}</td>"
                 f"<td class='num'>{_money(r.get('dividends'))}</td><td class='num'>{_money(r.get('invested_capital'))}</td>"
                 f"<td class='num'>{_pct(r.get('roic_pct'))}</td></tr>")
    projections = _cashflow_dcf_projection_block(state)
    return _card("A5 · Cash-flow & capital history ($ / %)", f"""
      <table class="grid"><thead><tr><th></th><th class='num'>Revenue</th><th class='num'>OCF</th><th class='num'>Capex</th>
        <th class='num'>FCF</th><th class='num'>SBC</th><th class='num'>Buybacks</th><th class='num'>Dividends</th>
        <th class='num'>Inv. capital</th><th class='num'>ROIC</th></tr></thead><tbody>{rows}</tbody></table>{projections}""")


def _cashflow_dcf_projection_block(state: dict[str, Any]) -> str:
    model = state.get("dcf_model") or {}
    years = model.get("years") or []
    if not years:
        return ""
    rows = [
        ("Revenue", "revenue", "money"),
        ("  growth", "growth_pct", "pct"),
        ("EBIT", "ebit", "moneyb"),
        ("  EBIT margin", "ebit_margin_pct", "pct"),
        ("FCFF", "fcff", "moneyb"),
        ("PV of FCFF", "pv_fcff", "money"),
    ]
    assumptions = model.get("assumptions") or {}
    note = (
        "<div class='cap dcf-proj-note'>Base-case DCF projections appended to the cash-flow history "
        f"(WACC {_pct(assumptions.get('wacc_pct'))}, terminal growth {_pct(assumptions.get('terminal_growth_pct'))}, "
        f"value/share {_money(model.get('per_share_value'))}).</div>"
    )
    return note + _model_table("Base-case DCF projections", rows, years)


def _ownership_appendix(state: dict[str, Any]) -> str:
    own = state.get("ownership") or {}
    holders = own.get("top_holders") or []
    if not holders:
        return ""
    rows = ""
    for h in holders[:12]:
        chg = h.get("shares_changed")
        col = GREEN if (chg and chg > 0) else RED if (chg and chg < 0) else MUTED
        rows += (f"<tr><td>{_esc(h.get('manager'))}</td><td class='lbl'>{'passive' if h.get('is_passive') else 'active'}</td>"
                 f"<td class='num'>{_money(h.get('market_value_usd'))}</td><td class='num'>{_pct(h.get('weight_pct'))}</td>"
                 f"<td class='num' style='color:{col}'>{_money(chg) if chg is not None else '—'}</td></tr>")
    return _card(f"A6 · Top 13F holders (as of {_esc(own.get('quarter'))})", f"""
      <table class="grid"><thead><tr><th>Manager</th><th>Type</th><th class='num'>Value</th>
        <th class='num'>Weight</th><th class='num'>QoQ Δ shares</th></tr></thead><tbody>{rows}</tbody></table>""")


def _xx(v: Any) -> str:
    return f"{float(v):.1f}x" if isinstance(v, (int, float)) else "—"


# --------------------------------------------------------------------------- bits

def _card(title: str, body: str) -> str:
    return (f"<section class='card'><div class='card-h'>{title}</div>"
            f"<div class='card-b'>{body}</div></section>")


def _hero_stat(label: str, value: str, color: str | None) -> str:
    style = f" style='color:{color}'" if color else ""
    return (f"<div class='hstat'><div class='hstat-v'{style}>{value}</div>"
            f"<div class='hstat-l'>{_esc(label)}</div></div>")


def _recommendation(upside: Any) -> tuple[str, str]:
    if not isinstance(upside, (int, float)):
        return "UNDER REVIEW", NAVY3
    if upside >= 20: return "BUY", GREEN
    if upside >= 5: return "ACCUMULATE", GREEN
    if upside <= -20: return "SELL", RED
    if upside <= -5: return "REDUCE", RED
    return "HOLD", AMBER


# Ordered longest-first so multi-word ratings match before their substrings.
_RATING_COLORS = {
    "OVERWEIGHT": GREEN, "OUTPERFORM": GREEN, "ACCUMULATE": GREEN, "STRONG BUY": GREEN,
    "BUY": GREEN, "ADD": GREEN,
    "MARKET PERFORM": AMBER, "SECTOR PERFORM": AMBER, "MARKETWEIGHT": AMBER,
    "NEUTRAL": AMBER, "HOLD": AMBER, "PERFORM": AMBER,
    "UNDERPERFORM": RED, "UNDERWEIGHT": RED, "REDUCE": RED, "SELL": RED, "TRIM": RED,
}


def _rating_from_memo(state: dict[str, Any]) -> tuple[str, str] | None:
    """Extract the committee's stated rating from the memo, so the hero badge matches."""
    memo = (state.get("memo") or {}).get("en") or ""
    sec = _split_memo(memo)
    reco = sec.get("recommendation", memo)
    # Prefer an explicit "Rating: X" or the leading word of the recommendation section.
    m = re.search(r"(?im)\brating\b[\s:*]*([A-Za-z][A-Za-z \-]{2,20})", reco) \
        or re.match(r"(?is)\s*\**([A-Za-z][A-Za-z \-]{2,20})", reco)
    keys_by_len = sorted(_RATING_COLORS, key=len, reverse=True)  # UNDERPERFORM before PERFORM
    if m:
        word = m.group(1).strip().upper()
        for key in keys_by_len:
            if word.startswith(key):
                return key, _RATING_COLORS[key]
    for key in keys_by_len:  # fallback: first rating keyword mentioned in the recommendation
        if re.search(rf"(?im)\b{re.escape(key)}\b", reco):
            return key, _RATING_COLORS[key]
    return None


# ----------------------------------------------------------------- formatting

def _latest_metrics(packet: dict[str, Any]) -> dict[str, float]:
    rows = (packet.get("metrics") or {}).get("rows") or []
    best: dict[str, tuple[int, float]] = {}
    for r in rows:
        mid = r.get("metric_id"); v = r.get("value"); fy = r.get("fiscal_year") or 0
        if mid is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if mid not in best or fy > best[mid][0]:
            best[mid] = (fy, v)
    return {k: v for k, (_, v) in best.items()}


def _money(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    if a >= 1e12: return f"{v/1e12:,.2f}T"
    if a >= 1e9:  return f"{v/1e9:,.2f}B"
    if a >= 1e6:  return f"{v/1e6:,.1f}M"
    return f"{v:,.2f}"


def _pct(v: Any) -> str:
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _signed_pct(v: Any) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.1f}%"


def _delta_color(v: Any) -> str:
    try:
        return GREEN if float(v) >= 0 else RED
    except (TypeError, ValueError):
        return NAVY3


def _margin_color(m: Any) -> str:
    try:
        m = float(m)
    except (TypeError, ValueError):
        return NAVY
    if m >= 0.25: return GREEN
    if m < 0: return RED
    return NAVY


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _md_to_html(text: str) -> str:
    """Minimal, safe Markdown -> HTML for prose blocks (headings, lists, tables)."""
    text = _html.escape(text or "")
    lines = text.split("\n")
    out: list[str] = []
    in_ul = False
    i = 0

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>"); in_ul = False

    while i < len(lines):
        line = lines[i].rstrip()
        # Markdown table: a "| ... |" row followed by a |---|---| separator row.
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            close_ul()
            header = _split_row(line)
            i += 2
            body_rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                body_rows.append(_split_row(lines[i])); i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in header)
            trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body_rows)
            out.append(f"<table class='grid'><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
            continue
        if not line.strip():
            close_ul(); i += 1; continue
        h = re.match(r"^(#{1,4})\s+(.*)$", line)
        if h:
            close_ul()
            lvl = min(len(h.group(1)) + 2, 5)
            out.append(f"<h{lvl}>{_inline(h.group(2))}</h{lvl}>"); i += 1; continue
        if re.match(r"^\s*[-*•]\s+", line):
            if not in_ul: out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(re.sub(r'^\s*[-*•]\s+', '', line))}</li>"); i += 1; continue
        close_ul()
        out.append(f"<p>{_inline(line)}</p>"); i += 1
    close_ul()
    return "\n".join(out)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", s)
    return s


# --------------------------------------------------------------------------- PDF

from ..report_style import find_browser as _find_browser  # noqa: E402
from ..report_style import html_to_pdf as _html_to_pdf  # noqa: E402


# --------------------------------------------------------------------------- CSS

_CSS = f"""
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; background:{BG}; color:{NAVY};
  font-family: Inter, "Segoe UI", -apple-system, sans-serif; font-size:11px;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
@page {{ size: A4; margin: 0; }}
.page {{ padding: 18px 26px 26px; }}
.hero {{ background:{PANEL}; color:{NAVY}; padding:18px 26px 20px; border-bottom:1px solid {BORDER}; }}
.hero-top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }}
.firm-brand {{ display:flex; align-items:center; gap:10px; }}
.firm-mark {{ width:34px; height:22px; display:flex; align-items:center; }}
.firm-mark svg {{ width:34px; height:22px; }}
.firm-name {{ font-size:13px; font-weight:700; letter-spacing:0.08em; color:{AMBER}; line-height:1; }}
.firm-sub {{ font-size:8px; text-transform:uppercase; letter-spacing:0.12em; color:{MUTED}; margin-top:3px; }}
.asof {{ font-size:8px; line-height:1.45; letter-spacing:0.12em; text-transform:uppercase; color:{MUTED}; text-align:right; }}
.hero-body {{ display:flex; justify-content:space-between; align-items:stretch; gap:18px; }}
.company-lockup {{ flex:1; min-width:0; display:flex; align-items:center; gap:14px; border:1px solid {BORDER_SOFT}; background:#fff; border-radius:8px; padding:14px 16px; }}
.company-logo {{ width:58px; height:58px; flex:0 0 58px; border:1px solid {BORDER}; border-radius:8px; background:{BG}; display:flex; align-items:center; justify-content:center; overflow:hidden; }}
.company-logo img {{ max-width:50px; max-height:50px; object-fit:contain; display:block; }}
.logo-fallback {{ color:{AMBER}; font-weight:800; letter-spacing:0.08em; font-size:16px; }}
.company-copy {{ min-width:0; }}
.eyebrow-hero {{ font-size:8px; font-weight:600; letter-spacing:0.14em; text-transform:uppercase;
  color:{MUTED}; margin-bottom:7px; }}
.hero-title {{ font-size:25px; font-weight:650; line-height:1.05; color:{NAVY}; margin:0 0 8px; }}
.reco {{ font-size:11px; font-weight:800; letter-spacing:0.1em; text-transform:uppercase; }}
.hero-stats {{ flex:0 0 250px; display:grid; grid-template-columns:1fr; gap:8px; }}
.hstat {{ text-align:left; background:#fff; border:1px solid {BORDER_SOFT}; border-radius:8px; padding:10px 12px; }}
.hstat-v {{ font-size:20px; font-weight:800; color:{NAVY}; font-variant-numeric:tabular-nums; line-height:1; }}
.hstat-l {{ font-size:8px; text-transform:uppercase; letter-spacing:0.1em; color:{MUTED}; margin-top:5px; }}

.exec-summary {{ background:#fff; border:1px solid {BORDER}; border-radius:6px; padding:13px 15px; margin-bottom:14px; break-inside:avoid; }}
.exec-h {{ font-size:12px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:{NAVY}; margin-bottom:8px; }}
.exec-summary ul {{ margin:0; padding-left:16px; }}
.exec-summary li {{ font-size:11px; line-height:1.5; margin:4px 0; color:{NAVY}; }}

.card {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:6px; margin-top:16px;
  break-inside: avoid; }}
/* Action titles read as headlines, not uppercase dummy labels. */
.card-h {{ font-size:12.5px; font-weight:600; letter-spacing:0.005em; text-transform:none;
  color:{NAVY}; padding:11px 15px; border-bottom:1px solid {BORDER_SOFT}; line-height:1.3; }}
.card-h .src {{ float:right; font-weight:400; letter-spacing:0.04em; text-transform:none; color:{MUTED}; font-size:8px; }}
.card-b {{ padding:13px 15px; }}

table.grid {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
table.grid th {{ font-size:8px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase;
  color:{MUTED}; text-align:left; padding:4px 8px; border-bottom:1px solid {BORDER}; }}
table.grid td {{ font-size:11px; padding:5px 8px; border-bottom:1px solid {BORDER_SOFT}; color:{NAVY}; }}
table.grid td.num, table.grid th.num {{ text-align:right; }}
table.grid td.lbl {{ color:{MUTED}; }}
table.grid .strong {{ font-weight:700; }}
table.grid tfoot td {{ border-top:2px solid {BORDER}; border-bottom:none; font-weight:600; }}

.tag {{ display:inline-block; font-size:8px; font-weight:700; letter-spacing:0.06em;
  padding:2px 6px; border-radius:3px; color:#fff; }}
.tag-advocate {{ background:{GREEN}; }} .tag-base {{ background:{NAVY2}; }} .tag-challenger {{ background:{RED}; }}
.tag-auditor {{ background:{NAVY2}; }}

.bar {{ display:block; height:3px; background:{BORDER_SOFT}; border-radius:2px; margin-top:3px; }}
.bar span {{ display:block; height:3px; background:{NAVY2}; border-radius:2px; }}

.tiles {{ display:flex; flex-wrap:wrap; gap:10px; }}
.tile {{ flex:1 1 100px; min-width:100px; background:#fff; border:1px solid {BORDER_SOFT};
  border-radius:5px; padding:10px 12px; }}
.tile-v {{ font-size:18px; font-weight:700; color:{NAVY}; font-variant-numeric:tabular-nums; }}
.tile-l {{ font-size:8px; text-transform:uppercase; letter-spacing:0.08em; color:{MUTED}; margin-top:4px; }}

.tri {{ display:flex; gap:12px; }}
.col {{ flex:1; background:#fff; border:1px solid {BORDER_SOFT}; border-radius:5px; overflow:hidden; }}
.col-h {{ font-size:10px; font-weight:700; letter-spacing:0.06em; padding:7px 10px;
  border-bottom:2px solid; }}
.col-b {{ padding:8px 10px; font-size:10px; line-height:1.5; color:{NAVY}; }}
.col-b p {{ margin:0 0 6px; }} .col-b ul {{ margin:4px 0; padding-left:14px; }} .col-b li {{ margin:2px 0; }}

.prose {{ font-size:11px; line-height:1.55; color:{NAVY}; }}
.prose p {{ margin:0 0 7px; }} .prose h3,.prose h4,.prose h5 {{ font-size:10px; text-transform:uppercase;
  letter-spacing:0.08em; color:{NAVY2}; margin:10px 0 5px; }}
.prose ul {{ margin:5px 0; padding-left:16px; }} .prose li {{ margin:2px 0; }}

ul.kpi {{ list-style:none; margin:0; padding:0; }}
ul.kpi li {{ font-size:10px; padding:5px 0; border-bottom:1px solid {BORDER_SOFT}; color:{NAVY}; }}

.empty {{ font-size:10px; color:{MUTED}; font-style:italic; }}
.footer {{ margin-top:16px; padding-top:10px; border-top:1px solid {BORDER};
  font-size:8px; color:{MUTED}; line-height:1.5; }}

.two-col {{ display:flex; gap:12px; }} .two-col > div {{ flex:1; }}
.note-box {{ background:#fff; border:1px solid {BORDER_SOFT}; border-left:3px solid {NAVY2};
  border-radius:4px; padding:8px 10px; margin:8px 0; font-size:10px; line-height:1.5; color:{NAVY}; }}
.specialist-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
.specialist-comment {{ background:#fff; border:1px solid {BORDER_SOFT}; border-radius:5px; padding:9px 10px; }}
.specialist-name {{ font-size:10px; font-weight:700; color:{NAVY}; }}
.specialist-focus {{ font-size:8px; text-transform:uppercase; letter-spacing:0.08em; color:{MUTED}; margin-top:2px; }}
.specialist-comment ul {{ margin:7px 0 0; padding-left:15px; }}
.specialist-comment li {{ font-size:10px; line-height:1.45; margin:3px 0; color:{NAVY}; }}
.cap {{ font-size:8px; color:{MUTED}; margin:5px 0 2px; }}
.pill {{ display:inline-block; font-size:7px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase;
  background:{NAVY}; color:#fff; padding:1px 5px; border-radius:3px; vertical-align:middle; }}
ol.audit {{ margin:4px 0; padding-left:18px; font-size:10px; color:{NAVY}; font-variant-numeric:tabular-nums; }}
ol.audit li {{ margin:2px 0; }}
.appendix {{ break-before: page; margin-top:20px; }}
.appendix-title {{ font-size:13px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase;
  color:{NAVY}; padding:8px 0; border-bottom:2px solid {NAVY}; margin-bottom:8px; }}
svg {{ display:block; max-width:100%; }}

/* KPI banner — anchor numbers up top, break the text mass. */
.kpi-banner {{ display:flex; gap:10px; margin:2px 0 4px; }}
.kpi-box {{ flex:1; background:#fff; border:1px solid {BORDER}; border-radius:6px; padding:13px 14px; text-align:center; }}
.kpi-v {{ font-size:23px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1; }}
.kpi-l {{ font-size:8px; text-transform:uppercase; letter-spacing:0.1em; color:{MUTED}; margin-top:6px; }}

/* 2-column: analysis left (~60%), chart/exhibit right. */
.grid2 {{ display:flex; gap:16px; align-items:center; }}
.g-left {{ flex:0 0 56%; }} .g-right {{ flex:1; }}
ul.takeaways {{ list-style:none; margin:0; padding:0; }}
ul.takeaways li {{ font-size:10.5px; line-height:1.5; color:{NAVY}; padding:7px 0 7px 15px; position:relative;
  border-bottom:1px solid {BORDER_SOFT}; }}
ul.takeaways li:last-child {{ border-bottom:none; }}
ul.takeaways li:before {{ content:'▸'; position:absolute; left:0; top:7px; color:{NAVY3}; }}

/* Integrated spreads: narrative beside its exhibit, never stacked as a dump. */
.spread-body {{ display:flex; gap:16px; align-items:flex-start; }}
.sp-text {{ flex:0 0 47%; }} .sp-fig {{ flex:1; min-width:0; }}
.sp-text p {{ font-size:10.5px; line-height:1.5; margin:0 0 7px; color:{NAVY}; }}
.sp-text ul {{ margin:5px 0; padding-left:15px; }} .sp-text li {{ font-size:10.5px; line-height:1.45; margin:2px 0; }}
.sp-fig table.grid {{ margin-top:6px; }}

/* Tribunal: one row per analyst — thesis text next to the exhibit that carries it. */
.tri-row {{ display:flex; gap:16px; align-items:center; padding:11px 0; border-bottom:1px solid {BORDER_SOFT}; break-inside:avoid; }}
.tri-row:last-child {{ border-bottom:none; padding-bottom:2px; }}
.tri-txt {{ flex:0 0 48%; }}
.tri-h {{ font-size:11px; font-weight:700; letter-spacing:0.03em; padding-bottom:4px; margin-bottom:6px;
  border-bottom:2px solid; display:inline-block; }}
.tri-txt p {{ font-size:10px; line-height:1.5; margin:0 0 5px; color:{NAVY}; }}
.tri-txt ul {{ margin:4px 0; padding-left:14px; }} .tri-txt li {{ font-size:10px; line-height:1.4; margin:2px 0; }}
.tri-fig {{ flex:1; min-width:0; }}

/* DCF model — dense financial tables, years as columns. */
.assum {{ display:flex; flex-wrap:wrap; gap:8px; margin:2px 0 10px; }}
.achip {{ font-size:9px; color:{MUTED}; background:#fff; border:1px solid {BORDER_SOFT}; border-radius:4px; padding:4px 9px; }}
.achip b {{ color:{NAVY}; font-size:12px; font-variant-numeric:tabular-nums; margin-right:3px; }}
.mtitle {{ font-size:9px; font-weight:600; letter-spacing:0.08em; text-transform:uppercase; color:{NAVY2}; margin:13px 0 4px; }}
table.model td, table.bridge td {{ font-size:9.5px; padding:3px 9px; }}
table.model th {{ font-size:8px; }}
table.model td.mrow, table.bridge td.mrow {{ text-align:left; color:{NAVY}; }}
table.model tr.sub td {{ color:{MUTED}; font-size:9px; border-bottom:none; }}
table.model tr.sub td.mrow {{ padding-left:18px; font-style:italic; }}
table.model tr.strong td, table.bridge tr.strong td {{ font-weight:700; border-top:1px solid {BORDER}; }}
table.bridge {{ width:auto; min-width:300px; }}
"""
