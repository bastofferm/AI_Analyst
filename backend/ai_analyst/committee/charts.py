"""Inline SVG charts for the committee report, styled with the MZQA palette.

All functions return a self-contained ``<svg>`` string that embeds directly in the
report HTML (and therefore prints crisply via headless Chrome). No JS, no external
assets. Every chart degrades to an empty string when it has no data.
"""
from __future__ import annotations

from typing import Any

NAVY = "#2F4D73"; NAVY2 = "#476D99"; NAVY3 = "#6B86A8"; MUTED = "#6F7890"
BORDER = "#DDD8CD"; BORDER_SOFT = "#EEECE5"; GREEN = "#1F7A52"; RED = "#8C2F39"; AMBER = "#B7791F"
PANEL = "#FBFAF7"; ADVOCATE = GREEN; CHALLENGER = RED; BASE = NAVY
_SERIES_COLORS = [NAVY, "#C2410C", "#0E7490", "#7C3AED", "#B45309", "#9333EA"]


def _t(x, y, s, size=9, color=MUTED, weight=400, anchor="start"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="Inter,Segoe UI,sans-serif">{s}</text>')


# ---------------------------------------------------- rebased relative price

def rebased_price_chart(series: dict[str, list[dict[str, Any]]], highlight: str,
                        w: int = 720, h: int = 260) -> str:
    series = {k: v for k, v in (series or {}).items() if v and len(v) > 2}
    if not series:
        return ""
    pad_l, pad_r, pad_t, pad_b = 38, 90, 14, 22
    rebased: dict[str, list[float]] = {}
    for tk, pts in series.items():
        base = pts[0]["close"] or 1.0
        rebased[tk] = [(p["close"] / base) * 100.0 for p in pts]
    all_vals = [v for arr in rebased.values() for v in arr]
    lo, hi = min(all_vals), max(all_vals)
    rng = (hi - lo) or 1.0
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b

    def x(i, n): return pad_l + (i / max(1, n - 1)) * plot_w
    def y(v): return pad_t + (1 - (v - lo) / rng) * plot_h

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    # gridlines + y labels at 100 / max / min
    for gv in sorted({round(lo), 100, round(hi)}):
        yy = y(gv)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l+plot_w}" y2="{yy:.1f}" stroke="{BORDER_SOFT}" stroke-width="1"/>')
        parts.append(_t(pad_l - 4, yy + 3, f"{gv:.0f}", 8, MUTED, anchor="end"))
    ordered = [highlight.upper()] + [t for t in rebased if t != highlight.upper()]
    series_list = [t for t in ordered if t in rebased]
    # Pre-compute end-label y positions and de-collide (min 10px vertical gap).
    label_specs = sorted(({"tk": tk, "y": y(rebased[tk][-1]), "val": rebased[tk][-1]} for tk in series_list),
                         key=lambda d: d["y"])
    for i in range(1, len(label_specs)):
        if label_specs[i]["y"] - label_specs[i - 1]["y"] < 10:
            label_specs[i]["y"] = label_specs[i - 1]["y"] + 10
    label_y = {s["tk"]: s["y"] for s in label_specs}
    for idx, tk in enumerate(series_list):
        arr = rebased[tk]; n = len(arr)
        pts = " ".join(f"{x(i,n):.1f},{y(v):.1f}" for i, v in enumerate(arr))
        is_hl = tk == highlight.upper()
        color = NAVY if is_hl else _SERIES_COLORS[(idx) % len(_SERIES_COLORS)]
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="{2.4 if is_hl else 1.2}" opacity="{1 if is_hl else 0.8}"/>')
        parts.append(_t(pad_l + plot_w + 6, label_y[tk] + 3, f"{tk} {arr[-1]:.0f}", 8,
                        color, 700 if is_hl else 500))
    parts.append(_t(pad_l, h - 6, series[list(series)[0]][0]["date"][:7], 8, MUTED))
    parts.append(_t(pad_l + plot_w, h - 6, series[list(series)[0]][-1]["date"][:7], 8, MUTED, anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- capex / cash-flow history

def capex_history_chart(hist: list[dict[str, Any]], w: int = 720, h: int = 260) -> str:
    hist = [r for r in (hist or []) if r.get("capex")]
    if len(hist) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 44, 16, 26
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    caps = [r["capex"] / 1e9 for r in hist]
    fcf = [(r.get("free_cash_flow") or 0) / 1e9 for r in hist]
    pct = [r.get("capex_pct_revenue") or 0 for r in hist]
    ymax = max(caps + fcf) * 1.15 or 1
    pmax = max(pct) * 1.3 or 1
    n = len(hist)
    slot = plot_w / n
    bw = slot * 0.32

    def yb(v): return pad_t + (1 - v / ymax) * plot_h
    def yp(v): return pad_t + (1 - v / pmax) * plot_h

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="{BORDER}" stroke-width="1"/>')
    for i, r in enumerate(hist):
        cx = pad_l + slot * i + slot / 2
        # capex bar (navy) + fcf bar (green), side by side
        parts.append(f'<rect x="{cx-bw:.1f}" y="{yb(caps[i]):.1f}" width="{bw:.1f}" height="{pad_t+plot_h-yb(caps[i]):.1f}" fill="{NAVY}"/>')
        parts.append(f'<rect x="{cx:.1f}" y="{yb(fcf[i]):.1f}" width="{bw:.1f}" height="{pad_t+plot_h-yb(fcf[i]):.1f}" fill="{GREEN}" opacity="0.8"/>')
        parts.append(_t(cx, pad_t + plot_h + 12, f"FY{str(r['fiscal_year'])[2:]}", 8, MUTED, anchor="middle"))
    # capex % of revenue line (amber)
    line = " ".join(f"{pad_l+slot*i+slot/2:.1f},{yp(pct[i]):.1f}" for i in range(n))
    parts.append(f'<polyline points="{line}" fill="none" stroke="{AMBER}" stroke-width="2"/>')
    for i in range(n):
        parts.append(f'<circle cx="{pad_l+slot*i+slot/2:.1f}" cy="{yp(pct[i]):.1f}" r="2.2" fill="{AMBER}"/>')
    parts.append(_t(pad_l+slot*(n-1)+slot/2+4, yp(pct[-1]), f"{pct[-1]:.0f}% of rev", 8, AMBER, 700))
    # legend
    parts.append(f'<rect x="{pad_l}" y="2" width="8" height="8" fill="{NAVY}"/>' + _t(pad_l+11, 9, "Capex ($B)", 8, MUTED))
    parts.append(f'<rect x="{pad_l+80}" y="2" width="8" height="8" fill="{GREEN}"/>' + _t(pad_l+91, 9, "FCF ($B)", 8, MUTED))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- ROIC vs WACC

def roic_vs_wacc_chart(hist: list[dict[str, Any]], wacc: float, incremental: dict[str, Any] | None = None,
                       w: int = 720, h: int = 250) -> str:
    rows = [r for r in (hist or []) if r.get("roic_pct") is not None]
    if len(rows) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 60, 28, 24
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    roics = [r["roic_pct"] for r in rows]
    ymax = max(max(roics), wacc) * 1.15 or 1
    n = len(rows)

    def x(i): return pad_l + (i / max(1, n - 1)) * plot_w
    def y(v): return pad_t + (1 - v / ymax) * plot_h

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    # WACC band (destruction below is shaded faint red up to WACC line? keep simple: WACC line)
    yw = y(wacc)
    parts.append(f'<rect x="{pad_l}" y="{yw:.1f}" width="{plot_w}" height="{pad_t+plot_h-yw:.1f}" fill="{RED}" opacity="0.06"/>')
    parts.append(f'<line x1="{pad_l}" y1="{yw:.1f}" x2="{pad_l+plot_w}" y2="{yw:.1f}" stroke="{RED}" stroke-width="1.4" stroke-dasharray="5 3"/>')
    parts.append(_t(pad_l + plot_w + 4, yw + 3, f"WACC {wacc:.1f}%", 8, RED, 700))
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(roics))
    parts.append(f'<polyline points="{line}" fill="none" stroke="{NAVY}" stroke-width="2.4"/>')
    for i, r in enumerate(rows):
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(roics[i]):.1f}" r="2.6" fill="{NAVY}"/>')
        parts.append(_t(x(i), y(roics[i]) - 6, f"{roics[i]:.0f}%", 8, NAVY, 600, anchor="middle"))
        parts.append(_t(x(i), pad_t + plot_h + 12, f"FY{str(r['fiscal_year'])[2:]}", 8, MUTED, anchor="middle"))
    parts.append(_t(pad_l, 12, "ROIC (%) vs cost of capital", 9, MUTED, 600))
    if incremental and incremental.get("available"):
        acc = incremental.get("value_accretive")
        col = GREEN if acc else RED
        parts.append(_t(pad_l + plot_w + 4, pad_t + 8,
                        f"Incr. ROIC {incremental['incremental_roic_pct']:.0f}%", 8, col, 700, anchor="end"))
        parts.append(_t(pad_l + plot_w + 4, pad_t + 20,
                        f"(+{incremental['spread_vs_wacc_pct']:.0f} vs WACC)" if acc else f"({incremental['spread_vs_wacc_pct']:.0f} vs WACC)",
                        8, col, 500, anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- capital return / FCF

def capital_return_chart(hist: list[dict[str, Any]], w: int = 720, h: int = 250) -> str:
    rows = [r for r in (hist or []) if r.get("free_cash_flow") is not None]
    if len(rows) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 20, 22, 26
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    div = [(r.get("dividends") or 0) / 1e9 for r in rows]
    buy = [(r.get("buybacks") or 0) / 1e9 for r in rows]
    fcf = [(r.get("free_cash_flow") or 0) / 1e9 for r in rows]
    ymax = max([d + b for d, b in zip(div, buy)] + fcf) * 1.15 or 1
    n = len(rows); slot = plot_w / n; bw = slot * 0.5

    def yb(v): return pad_t + (1 - v / ymax) * plot_h
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="{BORDER}" stroke-width="1"/>')
    for i, r in enumerate(rows):
        cx = pad_l + slot * i + slot / 2
        y_div = yb(div[i]); base = pad_t + plot_h
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{y_div:.1f}" width="{bw:.1f}" height="{base-y_div:.1f}" fill="{NAVY2}"/>')
        y_buy = yb(div[i] + buy[i])
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{y_buy:.1f}" width="{bw:.1f}" height="{y_div-y_buy:.1f}" fill="{NAVY3}"/>')
        parts.append(_t(cx, pad_t + plot_h + 12, f"FY{str(r['fiscal_year'])[2:]}", 8, MUTED, anchor="middle"))
    fl = " ".join(f"{pad_l+slot*i+slot/2:.1f},{yb(fcf[i]):.1f}" for i in range(n))
    parts.append(f'<polyline points="{fl}" fill="none" stroke="{GREEN}" stroke-width="2"/>')
    for i in range(n):
        parts.append(f'<circle cx="{pad_l+slot*i+slot/2:.1f}" cy="{yb(fcf[i]):.1f}" r="2.2" fill="{GREEN}"/>')
    parts.append(f'<rect x="{pad_l}" y="2" width="8" height="8" fill="{NAVY2}"/>' + _t(pad_l+11, 9, "Dividends", 8, MUTED))
    parts.append(f'<rect x="{pad_l+70}" y="2" width="8" height="8" fill="{NAVY3}"/>' + _t(pad_l+81, 9, "Buybacks", 8, MUTED))
    parts.append(f'<line x1="{pad_l+145}" y1="6" x2="{pad_l+157}" y2="6" stroke="{GREEN}" stroke-width="2"/>' + _t(pad_l+160, 9, "FCF ($B)", 8, MUTED))
    parts.append("</svg>")
    return "".join(parts)


def quarterly_trend_chart(quarters: list[dict[str, Any]], w: int = 460, h: int = 210) -> str:
    """Discrete quarterly revenue ($B, bars) with the YoY revenue-growth line (%)."""
    rows = [q for q in (quarters or []) if q.get("revenue") is not None][-8:]
    if len(rows) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 40, 40, 24, 26
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    rev = [q["revenue"] / 1e9 for q in rows]
    ymax = (max(rev) * 1.15) or 1
    gy = [q.get("yoy_rev_growth_pct") for q in rows]
    gvals = [g for g in gy if g is not None] or [0.0]
    gmax, gmin = max(gvals + [0.0]), min(gvals + [0.0])
    grange = (gmax - gmin) or 1.0
    n = len(rows); slot = plot_w / n; bw = slot * 0.5

    def yb(v): return pad_t + (1 - v / ymax) * plot_h
    def yg(v): return pad_t + (1 - (v - gmin) / grange) * plot_h

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="{BORDER}" stroke-width="1"/>')
    for i, q in enumerate(rows):
        cx = pad_l + slot * i + slot / 2
        yv = yb(rev[i]); base = pad_t + plot_h
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{yv:.1f}" width="{bw:.1f}" height="{base-yv:.1f}" fill="{NAVY2}"/>')
        lbl = f"{q.get('fiscal_period','')}'{str(q.get('fiscal_year',''))[2:]}"
        parts.append(_t(cx, base + 12, lbl, 8, MUTED, anchor="middle"))
    pts = [(pad_l + slot * i + slot / 2, yg(gy[i])) for i in range(n) if gy[i] is not None]
    if len(pts) >= 2:
        pl = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        parts.append(f'<polyline points="{pl}" fill="none" stroke="{GREEN}" stroke-width="2"/>')
        for x, y in pts:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{GREEN}"/>')
    parts.append(f'<rect x="{pad_l}" y="4" width="8" height="8" fill="{NAVY2}"/>' + _t(pad_l+11, 11, "Revenue ($B)", 8, MUTED))
    parts.append(f'<line x1="{pad_l+95}" y1="8" x2="{pad_l+107}" y2="8" stroke="{GREEN}" stroke-width="2"/>' + _t(pad_l+110, 11, "YoY growth", 8, MUTED))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- segment revenue trend

def segment_trend_chart(series: dict[str, list[dict[str, Any]]], w: int = 720, h: int = 250) -> str:
    """series: {segment_name: [{fiscal_year, revenue}, ...]}."""
    series = {k: [p for p in v if p.get("revenue")] for k, v in (series or {}).items()}
    series = {k: v for k, v in series.items() if len(v) >= 2}
    if not series:
        return ""
    pad_l, pad_r, pad_t, pad_b = 42, 120, 16, 24
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    years = sorted({p["fiscal_year"] for v in series.values() for p in v})
    allv = [p["revenue"] / 1e9 for v in series.values() for p in v]
    ymax = max(allv) * 1.1 or 1

    def x(fy): return pad_l + (years.index(fy) / max(1, len(years) - 1)) * plot_w
    def y(v): return pad_t + (1 - v / ymax) * plot_h

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for fy in years:
        parts.append(_t(x(fy), pad_t + plot_h + 12, f"FY{str(fy)[2:]}", 8, MUTED, anchor="middle"))
    for idx, (seg, pts) in enumerate(sorted(series.items(), key=lambda kv: -kv[1][-1]["revenue"])):
        color = _SERIES_COLORS[idx % len(_SERIES_COLORS)]
        pts = sorted(pts, key=lambda p: p["fiscal_year"])
        line = " ".join(f"{x(p['fiscal_year']):.1f},{y(p['revenue']/1e9):.1f}" for p in pts)
        parts.append(f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2"/>')
        last = pts[-1]
        parts.append(_t(x(last["fiscal_year"]) + 5, y(last["revenue"]/1e9) + 3,
                        f"{seg[:20]} {last['revenue']/1e9:.0f}", 8, color, 600))
    parts.append(_t(pad_l, 12, "Segment revenue ($B)", 9, MUTED, 600))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- valuation football field

def valuation_range_chart(items: list[dict[str, Any]], current_price: float | None,
                          w: int = 720, h: int = 220) -> str:
    items = [it for it in (items or []) if it.get("low") is not None and it.get("high") is not None]
    if not items:
        return ""
    pad_l, pad_r, pad_t, pad_b = 150, 30, 16, 26
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    lows = [it["low"] for it in items] + ([current_price] if current_price else [])
    highs = [it["high"] for it in items] + ([current_price] if current_price else [])
    lo, hi = min(lows), max(highs)
    rng = (hi - lo) or 1.0
    lo -= rng * 0.05; hi += rng * 0.05; rng = hi - lo

    def x(v): return pad_l + (v - lo) / rng * plot_w
    row_h = plot_h / len(items)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for i, it in enumerate(items):
        cy = pad_t + row_h * i + row_h / 2
        parts.append(_t(pad_l - 8, cy + 3, it["label"], 9, NAVY, 500, anchor="end"))
        x1, x2 = x(it["low"]), x(it["high"])
        parts.append(f'<rect x="{x1:.1f}" y="{cy-6:.1f}" width="{max(2,x2-x1):.1f}" height="12" rx="2" fill="{NAVY2}" opacity="0.30"/>')
        if it.get("mid") is not None:
            parts.append(f'<line x1="{x(it["mid"]):.1f}" y1="{cy-7:.1f}" x2="{x(it["mid"]):.1f}" y2="{cy+7:.1f}" stroke="{NAVY}" stroke-width="2.5"/>')
            parts.append(_t(x(it["mid"]), cy - 9, f"{it['mid']:.0f}", 8, NAVY, 700, anchor="middle"))
        parts.append(_t(x1 - 3, cy + 3, f"{it['low']:.0f}", 7, MUTED, anchor="end"))
        parts.append(_t(x2 + 3, cy + 3, f"{it['high']:.0f}", 7, MUTED))
    if current_price:
        px = x(current_price)
        parts.append(f'<line x1="{px:.1f}" y1="{pad_t-2:.1f}" x2="{px:.1f}" y2="{pad_t+plot_h+2:.1f}" stroke="{RED}" stroke-width="1.5" stroke-dasharray="4 3"/>')
        parts.append(_t(px, pad_t + plot_h + 14, f"Price {current_price:.0f}", 8, RED, 700, anchor="middle"))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- multiples bar (vs peer group)

def multiples_bar_chart(rows: list[dict[str, Any]], metric: str, label: str, highlight: str,
                        median: float | None = None, w: int = 720, h: int = 220) -> str:
    rows = [r for r in (rows or []) if isinstance(r.get(metric), (int, float)) and r[metric] > 0]
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: r[metric], reverse=True)
    pad_l, pad_r, pad_t, pad_b = 30, 20, 20, 30
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    vmax = max(r[metric] for r in rows) * 1.15 or 1
    n = len(rows); slot = plot_w / n; bw = slot * 0.6

    def yb(v): return pad_t + (1 - v / vmax) * plot_h
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{pad_l+plot_w}" y2="{pad_t+plot_h}" stroke="{BORDER}" stroke-width="1"/>')
    for i, r in enumerate(rows):
        cx = pad_l + slot * i + slot / 2
        v = r[metric]
        is_hl = str(r.get("ticker", "")).upper() == highlight.upper()
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{yb(v):.1f}" width="{bw:.1f}" height="{pad_t+plot_h-yb(v):.1f}" fill="{NAVY if is_hl else NAVY3}" opacity="{1 if is_hl else 0.7}"/>')
        parts.append(_t(cx, yb(v) - 3, f"{v:.1f}", 8, NAVY, 700 if is_hl else 400, anchor="middle"))
        parts.append(_t(cx, pad_t + plot_h + 12, r.get("ticker", ""), 8, NAVY if is_hl else MUTED, 700 if is_hl else 400, anchor="middle"))
    if median:
        ym = yb(median)
        parts.append(f'<line x1="{pad_l}" y1="{ym:.1f}" x2="{pad_l+plot_w}" y2="{ym:.1f}" stroke="{AMBER}" stroke-width="1.2" stroke-dasharray="5 3"/>')
        parts.append(_t(pad_l + plot_w, ym - 3, f"median {median:.1f}", 8, AMBER, 700, anchor="end"))
    parts.append(_t(pad_l, 12, label, 9, MUTED, 600))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- 13F ownership

def ownership_chart(summary: dict[str, Any], w: int = 720, h: int = 250) -> str:
    holders = (summary or {}).get("top_holders") or []
    holders = [x for x in holders if x.get("weight_pct")][:8]
    if not holders:
        return ""
    pad_l, pad_r, pad_t, pad_b = 160, 60, 14, 14
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    vmax = max(x["weight_pct"] for x in holders) * 1.1 or 1
    row_h = plot_h / len(holders)

    def bw(v): return (v / vmax) * plot_w
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for i, x in enumerate(holders):
        cy = pad_t + row_h * i + row_h / 2
        name = (x.get("manager") or "")[:26]
        parts.append(_t(pad_l - 8, cy + 3, name, 8, NAVY, 500, anchor="end"))
        chg = x.get("shares_changed")
        color = GREEN if (chg and chg > 0) else RED if (chg and chg < 0) else NAVY3
        parts.append(f'<rect x="{pad_l}" y="{cy-5:.1f}" width="{bw(x["weight_pct"]):.1f}" height="10" rx="2" fill="{color}" opacity="0.85"/>')
        tag = "passive" if x.get("is_passive") else "active"
        parts.append(_t(pad_l + bw(x["weight_pct"]) + 4, cy + 3, f"{x['weight_pct']:.1f}%", 8, MUTED, anchor="start"))
    parts.append(_t(pad_l, 10, "Top 13F holders (bar = % of shares; green=adding, red=reducing)", 8, MUTED, 600))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------- growth×WACC heat grid

def sensitivity_heat(grid: dict[str, Any], current_price: float | None,
                     w: int = 720, h: int = 200) -> str:
    if not grid or not grid.get("per_share"):
        return ""
    g_axis, w_axis, m = grid["growth_axis"], grid["wacc_axis"], grid["per_share"]
    flat = [v for row in m for v in row if isinstance(v, (int, float))]
    if not flat:
        return ""
    lo, hi = min(flat), max(flat); rng = (hi - lo) or 1
    pad_l, pad_t = 60, 34
    cw = (w - pad_l - 12) / len(w_axis)
    ch = (h - pad_t - 20) / len(g_axis)

    def shade(v):
        t = (v - lo) / rng  # 0..1 -> red..green
        r = int(220 + (22 - 220) * t); g = int(38 + (163 - 38) * t); b = int(38 + (74 - 38) * t)
        return f"rgb({r},{g},{b})"

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    parts.append(_t(pad_l - 6, pad_t - 18, "Rev growth \\ WACC", 8, MUTED, 600, anchor="start"))
    for j, wv in enumerate(w_axis):
        parts.append(_t(pad_l + cw * j + cw / 2, pad_t - 6, f"{wv:.1f}%", 8, MUTED, 500, anchor="middle"))
    for i, gv in enumerate(g_axis):
        parts.append(_t(pad_l - 6, pad_t + ch * i + ch / 2 + 3, f"{gv:.1f}%", 8, MUTED, 500, anchor="end"))
        for j, wv in enumerate(w_axis):
            v = m[i][j]
            xx, yy = pad_l + cw * j, pad_t + ch * i
            fill = shade(v) if isinstance(v, (int, float)) else BORDER_SOFT
            parts.append(f'<rect x="{xx:.1f}" y="{yy:.1f}" width="{cw:.1f}" height="{ch:.1f}" fill="{fill}" stroke="{PANEL}" stroke-width="1"/>')
            if isinstance(v, (int, float)):
                over = current_price and v >= current_price
                parts.append(_t(xx + cw / 2, yy + ch / 2 + 3, f"{v:.0f}", 8, "#FBFAF7", 700 if over else 500, anchor="middle"))
    parts.append("</svg>")
    return "".join(parts)
