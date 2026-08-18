"""Render a research run as a branded HTML/PDF model-validation dossier.

Same mechanics as the committee memo — MZQA design tokens, headless Chrome ``--print-to-pdf``
— so a printed dossier and the web terminal are visibly one product. Structure differs
deliberately: the document is sectioned by *quality attribute* and led by the robustness
rating, rather than by whichever metric was computed first.

That ordering is the point. Lewis et al. (arXiv:2602.05043) find that ML evaluation
overwhelmingly reports predictive accuracy and little else, and that testing across
attributes surfaces a wider range of defects. A dossier whose first page is a rating and
whose sections are named for attributes makes an empty section legible as a gap — which a
list of forty floats does not.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_analyst.report_style import (
    AMBER, BG, BORDER, BORDER_SOFT, GREEN, MUTED, NAVY, NAVY2, NAVY3, PANEL, RED,
    esc, html_to_pdf,
)

_RATING_COLOR = {1: GREEN, 2: AMBER, 3: RED}
_STATUS_COLOR = {"pass": GREEN, "warn": AMBER, "fail": RED}


def output_root() -> Path:
    """Where dossiers are written. ``output/`` is git-ignored."""
    env = os.environ.get("MZQA_RESEARCH_OUTPUT_DIR")
    if env:
        return Path(env)
    # backend/api/quant/research/report_pdf.py -> parents[4] == project root
    return Path(__file__).resolve().parents[4] / "output" / "quant_research"


def write_report(run: dict[str, Any], out_dir: Path | str | None = None) -> dict[str, Any]:
    """Render ``run`` to HTML and PDF. Returns ``{html, pdf, pdf_ok}``.

    Mirrors ``committee.report_pdf.write_report`` so the router can treat both identically.
    A box without Chrome or Edge still gets the HTML, which is self-contained and readable.
    """
    run_id = str(run.get("run_id") or "unknown")
    out_dir = Path(out_dir) if out_dir else (
        output_root() / datetime.now().strftime("%Y%m%d") / run_id[:12])
    out_dir.mkdir(parents=True, exist_ok=True)

    html_doc = render_html(run)
    html_path = out_dir / "alpha_research_report.html"
    html_path.write_text(html_doc, encoding="utf-8")
    pdf_path = out_dir / "alpha_research_report.pdf"
    ok = html_to_pdf(html_path, pdf_path)
    return {"html": str(html_path), "pdf": str(pdf_path) if ok else None, "pdf_ok": ok}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _n(v: Any, d: int = 4) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:.{d}f}"


def _pct(v: Any, d: int = 1) -> str:
    try:
        return f"{float(v) * 100:.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def _date(v: Any) -> str:
    return esc(str(v)[:19].replace("T", " ")) if v else "—"


def _meta(label: str, value: str, sub: str = "", color: str = NAVY) -> str:
    sub_html = f'<div class="sub">{esc(sub)}</div>' if sub else ""
    return (f'<div class="meta"><div class="lbl">{esc(label)}</div>'
            f'<div class="val" style="color:{color}">{value}</div>{sub_html}</div>')


def _table(headers: list[str], rows: list[list[str]], *, right_from: int = 1) -> str:
    if not rows:
        return '<div class="empty">No data for this section.</div>'
    head = "".join(
        f'<th style="text-align:{"right" if i >= right_from else "left"}">{esc(h)}</th>'
        for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(
            f'<td style="text-align:{"right" if i >= right_from else "left"}">{c}</td>'
            for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def _hero(run: dict[str, Any]) -> str:
    summary = run.get("summary_json") or {}
    champ = (summary.get("champion") or {}) if isinstance(summary, dict) else {}
    promoted = bool(run.get("promoted"))
    rating, rating_label = _champion_rating(run)
    color = _RATING_COLOR.get(rating, MUTED)

    return f"""
    <div class="hero">
      <div class="hero-top">
        <div>
          <div class="firm">MZQA</div>
          <div class="firm-sub">AI Investment Committee &middot; Quantitative Desk</div>
        </div>
        <div class="asof">
          Run {esc(str(run.get('run_id'))[:12])}<br>
          {_date(run.get('started_at'))}
        </div>
      </div>
      <div class="eyebrow">Model validation dossier</div>
      <h1>Alpha research &mdash; {esc(run.get('model_key'))} {esc(run.get('label'))}</h1>
      <div class="hero-grid">
        {_meta("Robustness rating",
               f"{rating} &mdash; {esc(rating_label)}" if rating else "&mdash;",
               # Literal characters, not HTML entities: `sub` is escaped by _meta, so an
               # entity here would render as the raw "&middot;" text.
               "1 robust · 2 moderate · 3 fragile", color)}
        {_meta("Rounds", esc(run.get("iterations_done")),
               f"of {esc(run.get('max_iterations'))} budgeted")}
        {_meta("Champion",
               (f"round {esc(champ.get('iteration'))}" if champ.get("available") else "none"),
               esc(champ.get("kind") or ""))}
        {_meta("Promoted to production", "Yes" if promoted else "No",
               "", GREEN if promoted else MUTED)}
        {_meta("Status", esc(run.get("status")), esc(run.get("stop_reason") or ""))}
      </div>
      <div class="promo">{esc(run.get('promotion_reason') or '')}</div>
    </div>
    """


def _champion_rating(run: dict[str, Any]) -> tuple[int | None, str]:
    champ_iter = run.get("champion_iteration")
    for it in run.get("iterations") or []:
        if it.get("iteration") == champ_iter:
            r = it.get("rating_json") or {}
            return r.get("rating"), r.get("rating_label") or ""
    for it in reversed(run.get("iterations") or []):
        r = it.get("rating_json") or {}
        if r.get("rating"):
            return r.get("rating"), r.get("rating_label") or ""
    return None, ""


def _timeline(run: dict[str, Any]) -> str:
    rows = []
    for it in run.get("iterations") or []:
        rep = it.get("report_json") or {}
        head = rep.get("headline") or {}
        val = it.get("validation_json") or {}
        pm = it.get("pm_json") or {}
        changes = ", ".join((it.get("patch_json") or {}).get("changes") or []) or "baseline"
        status = str(val.get("status") or "—")
        rows.append([
            esc(it.get("iteration")),
            f'<span class="chg">{esc(changes[:110])}</span>',
            _n(head.get("rank_ic")),
            _n(head.get("r2_oos"), 5),
            f'<span style="color:{_RATING_COLOR.get(head.get("robustness_rating"), MUTED)}">'
            f'{esc(head.get("robustness_rating") or "—")}</span>',
            _n(head.get("long_short_sharpe"), 2),
            _pct(head.get("turnover"), 0),
            f'<span style="color:{_STATUS_COLOR.get(status, MUTED)}">{esc(status)}</span>',
            esc(pm.get("decision") or "—"),
        ])
    return _table(
        ["#", "What changed", "rank-IC", "R² OOS", "Rating", "L/S Sharpe", "Turnover",
         "Validation", "PM"], rows, right_from=2)


def _quality_sections(it: dict[str, Any]) -> str:
    """One round's report, laid out by quality attribute."""
    rep = it.get("report_json") or {}
    sec = rep.get("sections") or {}
    fc = sec.get("functional_correctness") or {}
    rq = sec.get("ranking_quality") or {}
    econ = sec.get("economic_value") or {}
    rob = sec.get("robustness") or {}
    expl = sec.get("explainability") or {}
    fh = sec.get("factor_hygiene") or {}
    cons = sec.get("consistency") or {}
    mon = sec.get("monitorability") or {}
    ci = fc.get("rank_ic_ci95") or [None, None]

    blocks = [
        ("Functional correctness", _table(
            ["Metric", "Value"],
            [["Rank-IC (mean)", _n(fc.get("rank_ic_mean"))],
             ["95% confidence interval", f"[{_n(ci[0])}, {_n(ci[1])}]"],
             ["Rank-IC t-statistic", _n(fc.get("rank_ic_t_stat"), 2)],
             ["Rank-IC p-value", _n(fc.get("rank_ic_p_value"), 4)],
             ["Rank-ICIR (annualized)", _n(fc.get("rank_icir_annualized"), 3)],
             ["Months with positive IC", _pct(fc.get("ic_hit_rate"), 0)],
             ["R² out-of-sample (zero-benchmarked)",
              _n((fc.get("r2_oos") or {}).get("zero_benchmarked"), 5)],
             ["R² out-of-sample (mean-benchmarked)",
              _n((fc.get("r2_oos") or {}).get("mean_benchmarked"), 5)],
             ["Evaluation months", esc(fc.get("n_dates"))]])),
        ("Ranking quality", _table(
            ["Metric", "Value"],
            [["Top-minus-bottom decile spread", _pct(rq.get("top_minus_bottom"), 2)],
             ["Spread t-statistic", _n(rq.get("top_minus_bottom_tstat"), 2)],
             ["Monotonicity across deciles", _n(rq.get("monotonicity"), 2)],
             ["Top-decile hit rate", _pct(rq.get("top_decile_hit_rate"), 0)]])),
        ("Economic value", _table(
            ["Metric", "Value"],
            [["Long/short annualized return", _pct(econ.get("long_short_annualized_return"))],
             ["Annualized volatility", _pct(econ.get("long_short_annualized_vol"))],
             ["Sharpe", _n(econ.get("long_short_sharpe"), 2)],
             ["Maximum drawdown", _pct(econ.get("max_drawdown"))],
             ["Monthly turnover of the top-k book", _pct(econ.get("turnover"), 0)]])),
        ("Robustness &mdash; perturbation battery", _perturbation_table(
            rob.get("perturbation_rating") or {})),
        ("Robustness &mdash; stability over time", _table(
            ["Metric", "Value"],
            [["Train-minus-OOS rank-IC gap", _n(rob.get("train_oos_gap"))],
             ["Positive years", f"{esc(rob.get('positive_years'))} of "
                               f"{esc(rob.get('total_years'))}"],
             ["Worst year", f"{esc(rob.get('worst_year'))} "
                            f"({_n(rob.get('worst_year_rank_ic'))})"],
             ["Up-market rank-IC",
              _n((rob.get("regime_split") or {}).get("up_market_rank_ic"))],
             ["Down-market rank-IC",
              _n((rob.get("regime_split") or {}).get("down_market_rank_ic"))]])),
        ("Factor hygiene", _factor_table(fh)),
        ("Explainability", _explain_table(expl)),
        ("Consistency &amp; reproducibility", _table(
            ["Metric", "Value"],
            [["Spec hash", f'<span class="mono">{esc(cons.get("spec_hash"))}</span>'],
             ["Seed", esc(cons.get("seed"))],
             ["Hyperparameter stability (CV)",
              _n((cons.get("hyperparameter_stability") or {}).get(
                  "coefficient_of_variation"), 3)],
             ["Complexity stable across windows",
              esc((cons.get("hyperparameter_stability") or {}).get("stable"))],
             ["Walk-forward refits",
              esc(len((cons.get("refit_months") or [])))]])),
        ("Monitorability &amp; sample", _table(
            ["Metric", "Value"],
            [["Panel rows used", esc(mon.get("rows_out"))],
             ["Features used", esc(mon.get("features_out"))],
             ["Distinct names", esc(mon.get("names"))],
             ["Months", esc(mon.get("months"))],
             ["Row retention after filters", _pct(mon.get("row_retention"), 0)],
             ["Window", f"{esc(mon.get('first_month'))} &rarr; {esc(mon.get('last_month'))}"],
             ["Fit seconds", _n(mon.get("fit_seconds"), 1)]])),
    ]
    return "".join(f'<div class="block"><h3>{title}</h3>{body}</div>'
                   for title, body in blocks)


def _perturbation_table(rating: dict[str, Any]) -> str:
    if not rating.get("available"):
        return f'<div class="empty">{esc(rating.get("reason", "Not available."))}</div>'
    rows = [[esc(p.get("id")), esc(p.get("label")), esc(p.get("stands_for")),
             _pct(p.get("rank_ic_degradation"), 1),
             _n(p.get("confounding_share_pct"), 1) + "%"]
            for p in rating.get("perturbations") or [] if p.get("available")]
    worst = rating.get("worst_case") or {}
    note = (f'<div class="note">Worst case: {esc(worst.get("label"))} '
            f'({_pct(worst.get("rank_ic_degradation"), 1)} of rank-IC lost). '
            f'Deconfounded by {esc(rating.get("deconfounding"))}. '
            f'Rating aggregates the worst case, not the average.</div>')
    return _table(["ID", "Perturbation", "Stands for", "Rank-IC lost", "Confounding share"],
                  rows, right_from=3) + note


def _factor_table(fh: dict[str, Any]) -> str:
    fr = fh.get("factor_regression") or {}
    if not fr.get("available"):
        return f'<div class="empty">{esc(fr.get("reason", "Not available."))}</div>'
    betas = fr.get("betas") or {}
    rows = [["Annualized alpha", _pct(fr.get("alpha_annualized"))],
            ["Alpha t-statistic", _n(fr.get("alpha_tstat"), 2)],
            ["Regression R²", _n(fr.get("r2"), 3)]]
    rows += [[f"Beta &middot; {esc(k)}", _n(v, 3)] for k, v in betas.items()]
    fn = fh.get("factor_neutral_ic") or {}
    if fn.get("available"):
        rows.append(["Rank-IC after neutralizing FF exposure",
                     _n(fn.get("factor_neutral_rank_ic"))])
    return _table(["Metric", "Value"], rows) + (
        '<div class="note">A small alpha t-statistic beside a large R² means the spread is '
        'Fama-French factor exposure rather than stock selection.</div>')


def _explain_table(expl: dict[str, Any]) -> str:
    if not expl.get("available"):
        return '<div class="empty">Not available.</div>'
    gain = expl.get("gain_importance") or {}
    perm = expl.get("permutation_importance_rank_ic_drop") or {}
    shap = expl.get("mean_abs_shap") or {}
    feats = list(gain)[:10] or list(perm)[:10]
    rows = [[f'<span class="mono">{esc(f)}</span>', _n(gain.get(f), 1),
             _n(perm.get(f), 4), _n(shap.get(f), 4)] for f in feats]
    stab = expl.get("stability") or {}
    note = (f'<div class="note">Importance stability across refits: top-k Jaccard '
            f'{_n(stab.get("mean_top_k_jaccard"), 2)}, rank correlation '
            f'{_n(stab.get("mean_rank_correlation"), 2)} &mdash; '
            f'{"stable" if stab.get("stable") else "UNSTABLE, do not reason from this ranking"}.'
            f'</div>')
    return _table(["Feature", "Gain", "Permutation (rank-IC drop)", "Mean |SHAP|"],
                  rows) + note


def _breakdowns(it: dict[str, Any]) -> str:
    data = it.get("breakdown_json") or {}
    if not data.get("available"):
        return ('<div class="block"><h3>Where the model works</h3>'
                '<div class="empty">No classification or factor-loading coverage available '
                'for this market.</div></div>')
    out = []
    for cut, rows in (data.get("cuts") or {}).items():
        body = _table(
            ["Bucket", "Names", "Months", "Rank-IC", "t-stat", "R² OOS", "Decile spread"],
            [[esc(r.get("bucket")) + (' <span class="thin">thin</span>' if r.get("thin") else ""),
              esc(r.get("n_names")), esc(r.get("n_months")), _n(r.get("rank_ic")),
              _n(r.get("rank_ic_t_stat"), 2), _n(r.get("r2_oos"), 5),
              _pct(r.get("top_decile_spread"), 2)] for r in rows])
        out.append(f'<div class="block"><h3>Where the model works &mdash; {esc(cut)}</h3>'
                   f'{body}</div>')
    return "".join(out) + (
        '<div class="note">Buckets marked <span class="thin">thin</span> hold too few names '
        'to support a conclusion. A headline that depends on one is a finding, not a result.'
        '</div>')


def _agents(it: dict[str, Any]) -> str:
    val = it.get("validation_json") or {}
    pm = it.get("pm_json") or {}
    adv = it.get("advisor_json") or {}
    res = it.get("researcher_json") or {}
    status = str(val.get("status") or "—")

    findings = "".join(
        f'<li><b>{esc(f.get("category"))}</b> ({esc(f.get("severity"))}) &mdash; '
        f'{esc(f.get("detail"))}</li>' for f in (val.get("findings") or []))
    findings_html = f"<ul>{findings}</ul>" if findings else "<p>No findings raised.</p>"
    concerns = "".join(f"<li>{esc(c)}</li>" for c in (pm.get("concerns") or []))

    return f"""
    <div class="block">
      <h3>The committee</h3>
      <div class="agent" style="border-left-color:{_STATUS_COLOR.get(status, MUTED)}">
        <div class="who">Model Validation &mdash;
          <span style="color:{_STATUS_COLOR.get(status, MUTED)}">{esc(status.upper())}</span>
          {' &middot; BLOCKING' if val.get("blocking") else ''}</div>
        <p>{esc(val.get("summary") or "")}</p>{findings_html}
      </div>
      <div class="agent" style="border-left-color:{NAVY2}">
        <div class="who">Portfolio Manager &mdash; {esc(pm.get("decision") or "—")}</div>
        <p>{esc(pm.get("reasoning") or "")}</p>
        {f"<ul>{concerns}</ul>" if concerns else ""}
      </div>
      <div class="agent" style="border-left-color:{AMBER}">
        <div class="who">External Advisor</div>
        <p><b>Contrarian read.</b> {esc(adv.get("contrarian_read") or "")}</p>
        <p><b>Orthogonal direction.</b> {esc(adv.get("orthogonal_direction") or "")}</p>
      </div>
      <div class="agent" style="border-left-color:{NAVY3}">
        <div class="who">Quantitative Researcher &mdash; next proposal</div>
        <p>{esc(res.get("rationale") or "")}</p>
        <p><i>{esc(res.get("hypothesis") or "")}</i></p>
      </div>
    </div>
    """


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #
def render_html(run: dict[str, Any]) -> str:
    iterations = run.get("iterations") or []
    rounds = "".join(
        f'<div class="page"><div class="round-head">Round {esc(it.get("iteration"))} '
        f'&mdash; {esc(", ".join((it.get("patch_json") or {}).get("changes") or []) or "baseline spec")}'
        f'</div>{_quality_sections(it)}{_breakdowns(it)}{_agents(it)}</div>'
        for it in iterations)

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Alpha research — {esc(run.get('model_key'))}</title>
<style>{_CSS}</style></head><body>
{_hero(run)}
<div class="page">
  <div class="block"><h3>The search</h3>{_timeline(run)}
  <div class="note">Every figure is purged, expanding-window, out-of-sample: at each
  prediction month the model saw only labels realized by that month minus the horizon
  embargo. These numbers are therefore not comparable with the production model's
  single-block figures, which were measured without an embargo.</div></div>
</div>
{rounds}
<div class="foot">MZQA AI Investment Committee &middot; generated
{_date(datetime.now().isoformat())} &middot; run {esc(str(run.get('run_id'))[:12])}</div>
</body></html>"""


_CSS = f"""
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; background:{BG}; color:{NAVY};
  font-family: Inter, "Segoe UI", -apple-system, sans-serif; font-size:11px;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
@page {{ size: A4; margin: 0; }}
.hero {{ background:{PANEL}; padding:20px 26px; border-bottom:1px solid {BORDER}; }}
.hero-top {{ display:flex; justify-content:space-between; align-items:flex-start;
  margin-bottom:16px; }}
.firm {{ font-size:13px; font-weight:700; letter-spacing:0.08em; color:{AMBER}; }}
.firm-sub {{ font-size:8px; text-transform:uppercase; letter-spacing:0.12em;
  color:{MUTED}; margin-top:3px; }}
.asof {{ font-size:8px; letter-spacing:0.1em; text-transform:uppercase; color:{MUTED};
  text-align:right; line-height:1.5; }}
.eyebrow {{ font-size:8px; font-weight:600; letter-spacing:0.14em; text-transform:uppercase;
  color:{MUTED}; margin-bottom:6px; }}
h1 {{ font-size:23px; font-weight:650; margin:0 0 16px; color:{NAVY}; }}
.hero-grid {{ display:flex; gap:26px; flex-wrap:wrap; }}
.meta .lbl {{ font-size:8px; font-weight:600; letter-spacing:0.12em; text-transform:uppercase;
  color:{MUTED}; }}
.meta .val {{ font-size:15px; font-weight:650; margin-top:2px; font-variant-numeric:tabular-nums; }}
.meta .sub {{ font-size:8.5px; color:{MUTED}; margin-top:1px; }}
.promo {{ margin-top:14px; font-size:9.5px; color:{MUTED}; border-top:1px solid {BORDER_SOFT};
  padding-top:8px; }}
.page {{ padding:18px 26px; page-break-inside:auto; }}
.round-head {{ font-size:13px; font-weight:650; color:{NAVY}; border-bottom:2px solid {NAVY};
  padding-bottom:5px; margin-bottom:12px; page-break-after:avoid; }}
.block {{ margin-bottom:16px; page-break-inside:avoid; }}
h3 {{ font-size:10px; font-weight:600; letter-spacing:0.1em; text-transform:uppercase;
  color:{MUTED}; margin:0 0 6px; }}
table {{ width:100%; border-collapse:collapse; font-size:9.5px; }}
th {{ font-size:8px; text-transform:uppercase; letter-spacing:0.07em; color:{MUTED};
  border-bottom:1px solid {BORDER}; padding:4px 6px; font-weight:600; }}
td {{ border-bottom:1px solid {BORDER_SOFT}; padding:3.5px 6px;
  font-variant-numeric:tabular-nums; }}
.mono {{ font-family:Consolas,monospace; font-size:9px; }}
.chg {{ color:{NAVY2}; }}
.thin {{ font-size:7.5px; text-transform:uppercase; letter-spacing:0.06em; color:{RED};
  border:1px solid {RED}; border-radius:3px; padding:0 3px; }}
.note {{ font-size:8.5px; color:{MUTED}; margin-top:6px; line-height:1.5; }}
.empty {{ font-size:9px; color:{MUTED}; font-style:italic; padding:5px 0; }}
.agent {{ border-left:3px solid {NAVY}; padding:2px 0 2px 10px; margin-bottom:10px; }}
.agent .who {{ font-size:9px; font-weight:650; letter-spacing:0.04em; margin-bottom:3px; }}
.agent p {{ margin:0 0 5px; font-size:9.5px; line-height:1.55; }}
.agent ul {{ margin:3px 0 5px 14px; padding:0; font-size:9px; line-height:1.5; }}
.foot {{ padding:12px 26px; border-top:1px solid {BORDER}; font-size:8px; color:{MUTED};
  letter-spacing:0.08em; text-transform:uppercase; }}
"""
