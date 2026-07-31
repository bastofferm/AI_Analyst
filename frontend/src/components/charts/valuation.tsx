"use client";

// Bespoke valuation charts — React/SVG ports of the institutional print charts in
// backend/ai_analyst/committee/charts.py (football field, sensitivity heat grid,
// peer multiples, 13F ownership) plus web-only additions (waterfall, scenario bars,
// FCF bars, SOTP segment bars). Every chart returns null when its data is missing.

import type {
  CompsData,
  OwnershipSummary,
  Scenario,
  SensitivityGrid,
  SotpSegment,
  TriMethod,
  WaterfallItem,
} from "@/lib/api";
import {
  BORDER,
  BORDER_SOFT,
  CHART_AMBER,
  CHART_FONT,
  CHART_GREEN,
  CHART_RED,
  MUTED,
  NAVY,
  NAVY2,
  NAVY3,
  NUM_FONT,
  PANEL,
  SERIES_COLORS,
  fmtShort,
  heatShade,
  isNum,
} from "./theme";

const FONT_PROPS = { fontFamily: CHART_FONT } as const;

// ---------------------------------------------------------------- Football field

/** Valuation ranges per method with the live price as a dashed red line. */
export function FootballField({
  methods,
  currentPrice,
}: {
  methods: TriMethod[] | null | undefined;
  currentPrice?: number | null;
}) {
  const items = (methods || []).filter((m) => isNum(m.low) && isNum(m.high));
  if (items.length === 0) return null;
  const W = 720;
  const padL = 150;
  const padR = 30;
  const padT = 16;
  const padB = 30;
  const rowH = 44;
  const H = padT + rowH * items.length + padB;
  const plotW = W - padL - padR;

  const lows = items.map((m) => m.low as number).concat(isNum(currentPrice) ? [currentPrice] : []);
  const highs = items.map((m) => m.high as number).concat(isNum(currentPrice) ? [currentPrice] : []);
  let lo = Math.min(...lows);
  let hi = Math.max(...highs);
  let rng = hi - lo || 1;
  lo -= rng * 0.06;
  hi += rng * 0.06;
  rng = hi - lo;
  const x = (v: number) => padL + ((v - lo) / rng) * plotW;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label="Valuation ranges by method">
      {items.map((m, i) => {
        const cy = padT + rowH * i + rowH / 2;
        const x1 = x(m.low as number);
        const x2 = x(m.high as number);
        return (
          <g key={i}>
            <text x={padL - 8} y={cy + 3} fontSize={10} fill={NAVY} fontWeight={m.primary ? 700 : 500} textAnchor="end">
              {m.label}
              {m.primary ? " ★" : ""}
            </text>
            <rect x={x1} y={cy - 7} width={Math.max(2, x2 - x1)} height={14} rx={2} fill={NAVY2} opacity={0.3}>
              <title>{`${m.label}: ${fmtShort(m.low as number)} – ${fmtShort(m.high as number)}`}</title>
            </rect>
            {isNum(m.mid) ? (
              <>
                <line x1={x(m.mid)} y1={cy - 9} x2={x(m.mid)} y2={cy + 9} stroke={NAVY} strokeWidth={2.5} />
                <text
                  x={x(m.mid)}
                  y={cy - 12}
                  fontSize={9.5}
                  fill={NAVY}
                  fontWeight={700}
                  textAnchor="middle"
                  style={{ fontFamily: NUM_FONT }}
                >
                  {fmtShort(m.mid)}
                </text>
              </>
            ) : null}
            <text x={x1 - 4} y={cy + 3.5} fontSize={8.5} fill={MUTED} textAnchor="end" style={{ fontFamily: NUM_FONT }}>
              {fmtShort(m.low as number)}
            </text>
            <text x={x2 + 4} y={cy + 3.5} fontSize={8.5} fill={MUTED} style={{ fontFamily: NUM_FONT }}>
              {fmtShort(m.high as number)}
            </text>
          </g>
        );
      })}
      {isNum(currentPrice) ? (
        <>
          <line
            x1={x(currentPrice)}
            y1={padT - 2}
            x2={x(currentPrice)}
            y2={H - padB + 4}
            stroke={CHART_RED}
            strokeWidth={1.5}
            strokeDasharray="4 3"
          />
          <text
            x={x(currentPrice)}
            y={H - padB + 16}
            fontSize={9}
            fill={CHART_RED}
            fontWeight={700}
            textAnchor="middle"
            style={{ fontFamily: NUM_FONT }}
          >
            Price {fmtShort(currentPrice)}
          </text>
        </>
      ) : null}
    </svg>
  );
}

// ---------------------------------------------------------------- Waterfall

/**
 * Enterprise-value → equity-value bridge. `shares` (optional) adds a per-share
 * footnote for the final total.
 */
export function WaterfallChart({
  items,
  shares,
  currency = "$",
}: {
  items: WaterfallItem[] | null | undefined;
  shares?: number | null;
  currency?: string;
}) {
  const rows = (items || []).filter((it) => isNum(it.value));
  if (rows.length < 2) return null;
  const W = 680;
  const H = 250;
  const padL = 16;
  const padR = 16;
  const padT = 26;
  const padB = 40;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  // Compute running levels: totals reset the baseline, steps float.
  let running = 0;
  const bars = rows.map((it) => {
    if (it.is_total) {
      const bar = { ...it, from: 0, to: it.value };
      running = it.value;
      return bar;
    }
    const from = running;
    running += it.value;
    return { ...it, from, to: running };
  });
  const allLevels = bars.flatMap((b) => [b.from, b.to, 0]);
  const lo = Math.min(...allLevels);
  const hi = Math.max(...allLevels) * 1.08 || 1;
  const rng = hi - lo || 1;
  const y = (v: number) => padT + (1 - (v - lo) / rng) * plotH;
  const n = bars.length;
  const slot = plotW / n;
  const bw = Math.min(72, slot * 0.62);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label="Valuation bridge">
      <line x1={padL} y1={y(0)} x2={W - padR} y2={y(0)} stroke={BORDER} strokeWidth={1} />
      {bars.map((b, i) => {
        const cx = padL + slot * i + slot / 2;
        const yTop = y(Math.max(b.from, b.to));
        const hPix = Math.max(2, Math.abs(y(b.from) - y(b.to)));
        const fill = b.is_total ? NAVY : b.value >= 0 ? CHART_GREEN : CHART_RED;
        const next = bars[i + 1];
        return (
          <g key={i}>
            {next ? (
              <line
                x1={cx + bw / 2}
                y1={y(b.to)}
                x2={padL + slot * (i + 1) + slot / 2 - bw / 2}
                y2={y(next.is_total ? next.to : next.from)}
                stroke={MUTED}
                strokeWidth={1}
                strokeDasharray="3 3"
                opacity={0.6}
              />
            ) : null}
            <rect x={cx - bw / 2} y={yTop} width={bw} height={hPix} rx={2} fill={fill} opacity={b.is_total ? 1 : 0.85}>
              <title>{`${b.name}: ${b.value >= 0 ? "+" : ""}${currency}${fmtShort(b.value)}`}</title>
            </rect>
            <text
              x={cx}
              y={yTop - 5}
              fontSize={9.5}
              fill={b.is_total ? NAVY : b.value >= 0 ? CHART_GREEN : CHART_RED}
              fontWeight={700}
              textAnchor="middle"
              style={{ fontFamily: NUM_FONT }}
            >
              {b.is_total ? "" : b.value >= 0 ? "+" : "−"}
              {currency}
              {fmtShort(Math.abs(b.is_total ? b.to : b.value))}
            </text>
            <text x={cx} y={H - padB + 14} fontSize={8.5} fill={MUTED} textAnchor="middle">
              {b.name.length > 16 ? `${b.name.slice(0, 15)}…` : b.name}
            </text>
          </g>
        );
      })}
      {isNum(shares) && shares > 0 && bars[n - 1] ? (
        <text x={W - padR} y={16} fontSize={9.5} fill={NAVY} fontWeight={600} textAnchor="end">
          ÷ {fmtShort(shares)} shares = {currency}
          {fmtShort(bars[n - 1].to / shares)} per share
        </text>
      ) : null}
    </svg>
  );
}

// ---------------------------------------------------------------- Scenario bars

/** Upside / base / downside per-share bars with probability weights, price + weighted value lines. */
export function ScenarioBars({
  scenarios,
  currentPrice,
  weightedValue,
  currency = "$",
}: {
  scenarios: Scenario[] | null | undefined;
  currentPrice?: number | null;
  weightedValue?: number | null;
  currency?: string;
}) {
  const rows = (scenarios || []).filter((s) => isNum(s.per_share_value));
  if (rows.length === 0) return null;
  const order = { upside: 0, base: 1, downside: 2 } as Record<string, number>;
  const sorted = [...rows].sort(
    (a, b) => (order[a.label?.toLowerCase() || ""] ?? 9) - (order[b.label?.toLowerCase() || ""] ?? 9)
  );
  const colorFor = (label: string | null | undefined) => {
    const l = (label || "").toLowerCase();
    if (l.includes("upside")) return CHART_GREEN;
    if (l.includes("downside")) return CHART_RED;
    return NAVY;
  };
  const W = 640;
  const H = 240;
  const padL = 40;
  const padR = 120;
  const padT = 18;
  const padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const vals = sorted.map((s) => s.per_share_value as number);
  const extra = [currentPrice, weightedValue].filter(isNum) as number[];
  const hi = Math.max(...vals, ...extra) * 1.12 || 1;
  const y = (v: number) => padT + (1 - v / hi) * plotH;
  const n = sorted.length;
  const slot = plotW / n;
  const bw = Math.min(88, slot * 0.52);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label="Scenario values">
      <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke={BORDER} strokeWidth={1} />
      {sorted.map((s, i) => {
        const cx = padL + slot * i + slot / 2;
        const v = s.per_share_value as number;
        const col = colorFor(s.label);
        const label = (s.label || "").charAt(0).toUpperCase() + (s.label || "").slice(1);
        return (
          <g key={i}>
            <rect x={cx - bw / 2} y={y(v)} width={bw} height={padT + plotH - y(v)} rx={3} fill={col} opacity={0.88}>
              <title>{`${label}: ${currency}${fmtShort(v)} per share${isNum(s.weight) ? ` · weight ${(s.weight * 100).toFixed(0)}%` : ""}`}</title>
            </rect>
            <text
              x={cx}
              y={y(v) - 6}
              fontSize={11}
              fill={col}
              fontWeight={700}
              textAnchor="middle"
              style={{ fontFamily: NUM_FONT }}
            >
              {currency}
              {fmtShort(v)}
            </text>
            <text x={cx} y={padT + plotH + 14} fontSize={10} fill={NAVY} fontWeight={600} textAnchor="middle">
              {label}
            </text>
            {isNum(s.weight) ? (
              <text x={cx} y={padT + plotH + 26} fontSize={8.5} fill={MUTED} textAnchor="middle" style={{ fontFamily: NUM_FONT }}>
                {(s.weight * 100).toFixed(0)}% likely
              </text>
            ) : null}
          </g>
        );
      })}
      {isNum(weightedValue) ? (
        <>
          <line x1={padL} y1={y(weightedValue)} x2={padL + plotW} y2={y(weightedValue)} stroke={CHART_AMBER} strokeWidth={1.4} strokeDasharray="5 3" />
          <text x={padL + plotW + 6} y={y(weightedValue) + 3} fontSize={9} fill={CHART_AMBER} fontWeight={700}>
            Weighted {currency}
            {fmtShort(weightedValue)}
          </text>
        </>
      ) : null}
      {isNum(currentPrice) ? (
        <>
          <line x1={padL} y1={y(currentPrice)} x2={padL + plotW} y2={y(currentPrice)} stroke={CHART_RED} strokeWidth={1.4} strokeDasharray="4 3" />
          <text x={padL + plotW + 6} y={y(currentPrice) + 3} fontSize={9} fill={CHART_RED} fontWeight={700}>
            Price {currency}
            {fmtShort(currentPrice)}
          </text>
        </>
      ) : null}
    </svg>
  );
}

/** Stacked 100% strip of scenario probability weights. */
export function WeightStrip({ scenarios }: { scenarios: Scenario[] | null | undefined }) {
  const rows = (scenarios || []).filter((s) => isNum(s.weight) && (s.weight as number) > 0);
  if (rows.length === 0) return null;
  const total = rows.reduce((acc, s) => acc + (s.weight as number), 0) || 1;
  const colorFor = (label: string | null | undefined) => {
    const l = (label || "").toLowerCase();
    if (l.includes("upside")) return CHART_GREEN;
    if (l.includes("downside")) return CHART_RED;
    return NAVY;
  };
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full">
        {rows.map((s, i) => (
          <div
            key={i}
            style={{ width: `${((s.weight as number) / total) * 100}%`, background: colorFor(s.label), opacity: 0.85 }}
            title={`${s.label}: ${(((s.weight as number) / total) * 100).toFixed(0)}%`}
          />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
        {rows.map((s, i) => (
          <span key={i} className="flex items-center gap-1.5 text-[10px] text-muted">
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: colorFor(s.label) }} />
            <span className="capitalize">{s.label}</span>
            <span className="num font-semibold text-navy">{(((s.weight as number) / total) * 100).toFixed(0)}%</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Projected FCF bars

/** Projected free cash flow vs its discounted (present) value, year by year. */
export function FcfBars({
  fcfs,
  discounted,
  currency = "$",
}: {
  fcfs: number[] | null | undefined;
  discounted?: number[] | null;
  currency?: string;
}) {
  const proj = (fcfs || []).filter(isNum);
  if (proj.length < 2) return null;
  const disc = (discounted || []).filter(isNum);
  const W = 640;
  const H = 210;
  const padL = 16;
  const padR = 16;
  const padT = 26;
  const padB = 26;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const hi = Math.max(...proj, ...(disc.length ? disc : [0])) * 1.1 || 1;
  const lo = Math.min(0, ...proj, ...(disc.length ? disc : [0]));
  const rng = hi - lo || 1;
  const y = (v: number) => padT + (1 - (v - lo) / rng) * plotH;
  const n = proj.length;
  const slot = plotW / n;
  const bw = Math.min(26, slot * 0.3);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label="Projected free cash flows">
      <line x1={padL} y1={y(0)} x2={padL + plotW} y2={y(0)} stroke={BORDER} strokeWidth={1} />
      {proj.map((v, i) => {
        const cx = padL + slot * i + slot / 2;
        const d = disc[i];
        return (
          <g key={i}>
            <rect x={cx - bw} y={Math.min(y(0), y(v))} width={bw} height={Math.max(2, Math.abs(y(v) - y(0)))} fill={NAVY2} opacity={0.4} rx={1.5}>
              <title>{`Year ${i + 1} projected FCF: ${currency}${fmtShort(v)}`}</title>
            </rect>
            {isNum(d) ? (
              <rect x={cx} y={Math.min(y(0), y(d))} width={bw} height={Math.max(2, Math.abs(y(d) - y(0)))} fill={NAVY} rx={1.5}>
                <title>{`Year ${i + 1} discounted to today: ${currency}${fmtShort(d)}`}</title>
              </rect>
            ) : null}
            <text x={cx} y={padT + plotH + 14} fontSize={8.5} fill={MUTED} textAnchor="middle">
              Y{i + 1}
            </text>
          </g>
        );
      })}
      <rect x={padL} y={6} width={8} height={8} fill={NAVY2} opacity={0.4} />
      <text x={padL + 12} y={13} fontSize={9} fill={MUTED}>
        Projected FCF
      </text>
      <rect x={padL + 100} y={6} width={8} height={8} fill={NAVY} />
      <text x={padL + 112} y={13} fontSize={9} fill={MUTED}>
        Discounted to today
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------- SOTP segment bars

/** Horizontal enterprise-value bars per business segment. */
export function SegmentBars({ segments, currency = "$" }: { segments: SotpSegment[] | null | undefined; currency?: string }) {
  const rows = (segments || []).filter((s) => isNum(s.enterprise_value) && (s.enterprise_value as number) > 0);
  if (rows.length === 0) return null;
  const sorted = [...rows].sort((a, b) => (b.enterprise_value as number) - (a.enterprise_value as number));
  const total = sorted.reduce((acc, s) => acc + (s.enterprise_value as number), 0) || 1;
  const max = sorted[0].enterprise_value as number;
  return (
    <div className="flex flex-col gap-2">
      {sorted.map((s, i) => {
        const v = s.enterprise_value as number;
        const meta: string[] = [];
        if (isNum(s.growth_pct)) meta.push(`${s.growth_pct.toFixed(0)}% growth`);
        if (isNum(s.operating_margin_pct)) meta.push(`${s.operating_margin_pct.toFixed(0)}% margin`);
        return (
          <div key={i} className="flex items-center gap-3">
            <div className="w-40 shrink-0 sm:w-52">
              <div className="truncate text-[12px] font-semibold text-navy">{s.segment || `Segment ${i + 1}`}</div>
              <div className="truncate text-[10px] text-muted">{meta.join(" · ")}</div>
            </div>
            <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-border-soft">
              <div
                className="h-full rounded-sm"
                style={{ width: `${Math.max(2, (v / max) * 100)}%`, background: SERIES_COLORS[i % SERIES_COLORS.length], opacity: 0.85 }}
                title={`${s.segment}: ${currency}${fmtShort(v)} enterprise value`}
              />
            </div>
            <div className="num w-24 shrink-0 text-right text-[11px] font-bold text-navy">
              {currency}
              {fmtShort(v)}
              <span className="ml-1 font-normal text-muted">{((v / total) * 100).toFixed(0)}%</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- Sensitivity heatmap

/** Growth × WACC per-share heat grid, red→green, bold when ≥ current price. */
export function SensitivityHeatmap({
  grid,
  currentPrice,
}: {
  grid: SensitivityGrid | null | undefined;
  currentPrice?: number | null;
}) {
  const g = grid || {};
  const gAxis = (g.growth_axis || []).filter(isNum);
  const wAxis = (g.wacc_axis || []).filter(isNum);
  const m = g.per_share || [];
  if (gAxis.length === 0 || wAxis.length === 0 || m.length === 0) return null;
  const flat = m.flat().filter(isNum);
  if (flat.length === 0) return null;
  const lo = Math.min(...flat);
  const hi = Math.max(...flat);
  const rng = hi - lo || 1;

  const W = 720;
  const padL = 64;
  const padT = 40;
  const padR = 12;
  const ch = 27;
  const H = padT + ch * gAxis.length + 22;
  const cw = (W - padL - padR) / wAxis.length;

  const nearestIdx = (axis: number[], v: number | null | undefined) => {
    if (!isNum(v)) return -1;
    let best = -1;
    let bestD = Infinity;
    axis.forEach((a, i) => {
      const d = Math.abs(a - v);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    });
    return bestD < 0.35 ? best : -1;
  };
  const baseI = nearestIdx(gAxis, g.base_growth);
  const baseJ = nearestIdx(wAxis, g.base_wacc);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label="Sensitivity heatmap">
      <text x={4} y={14} fontSize={9} fill={MUTED} fontWeight={600}>
        Revenue growth ↓ · Discount rate (WACC) →
      </text>
      {wAxis.map((wv, j) => (
        <text key={j} x={padL + cw * j + cw / 2} y={padT - 8} fontSize={9} fill={MUTED} fontWeight={500} textAnchor="middle" style={{ fontFamily: NUM_FONT }}>
          {wv.toFixed(1)}%
        </text>
      ))}
      {gAxis.map((gv, i) => (
        <g key={i}>
          <text x={padL - 8} y={padT + ch * i + ch / 2 + 3} fontSize={9} fill={MUTED} fontWeight={500} textAnchor="end" style={{ fontFamily: NUM_FONT }}>
            {gv.toFixed(1)}%
          </text>
          {wAxis.map((wv, j) => {
            const v = m[i]?.[j];
            const xx = padL + cw * j;
            const yy = padT + ch * i;
            const numeric = isNum(v);
            const over = numeric && isNum(currentPrice) && (v as number) >= currentPrice;
            return (
              <g key={j}>
                <rect
                  x={xx}
                  y={yy}
                  width={cw}
                  height={ch}
                  fill={numeric ? heatShade(((v as number) - lo) / rng) : BORDER_SOFT}
                  stroke={PANEL}
                  strokeWidth={1}
                >
                  <title>
                    {numeric
                      ? `${gv.toFixed(1)}% growth × ${wv.toFixed(1)}% WACC → ${fmtShort(v as number)} per share`
                      : "not available"}
                  </title>
                </rect>
                {numeric ? (
                  <text
                    x={xx + cw / 2}
                    y={yy + ch / 2 + 3.5}
                    fontSize={9}
                    fill="#FBFAF7"
                    fontWeight={over ? 700 : 500}
                    textAnchor="middle"
                    style={{ fontFamily: NUM_FONT }}
                  >
                    {fmtShort(v as number)}
                  </text>
                ) : null}
                {i === baseI && j === baseJ ? (
                  <rect x={xx + 1} y={yy + 1} width={cw - 2} height={ch - 2} fill="none" stroke="#F59E0B" strokeWidth={2} rx={2} />
                ) : null}
              </g>
            );
          })}
        </g>
      ))}
    </svg>
  );
}

// ---------------------------------------------------------------- Peer multiples

/** Vertical peer-multiple bars, target highlighted navy, amber dashed median. */
export function PeerMultiplesBar({
  comps,
  metric = "ev_ebitda",
  label = "EV/EBITDA vs sector peers",
  highlight,
}: {
  comps: CompsData | null | undefined;
  metric?: string;
  label?: string;
  highlight: string;
}) {
  const c = comps || {};
  const raw = [...(c.sector_peers || []), ...(c.target ? [c.target] : [])];
  const rows = raw
    .map((r) => ({ ticker: String(r?.ticker || ""), v: r?.[metric] }))
    .filter((r) => isNum(r.v) && (r.v as number) > 0) as { ticker: string; v: number }[];
  if (rows.length === 0) return null;
  rows.sort((a, b) => b.v - a.v);
  const median = c.peer_median?.[metric];

  const W = 720;
  const H = 220;
  const padL = 30;
  const padR = 20;
  const padT = 20;
  const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const vmax = rows[0].v * 1.15 || 1;
  const n = rows.length;
  const slot = plotW / n;
  const bw = Math.min(56, slot * 0.6);
  const y = (v: number) => padT + (1 - v / vmax) * plotH;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={FONT_PROPS} role="img" aria-label={label}>
      <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke={BORDER} strokeWidth={1} />
      {rows.map((r, i) => {
        const cx = padL + slot * i + slot / 2;
        const isHl = r.ticker.toUpperCase() === highlight.toUpperCase();
        return (
          <g key={i}>
            <rect
              x={cx - bw / 2}
              y={y(r.v)}
              width={bw}
              height={padT + plotH - y(r.v)}
              rx={2}
              fill={isHl ? NAVY : NAVY3}
              opacity={isHl ? 1 : 0.7}
            >
              <title>{`${r.ticker}: ${r.v.toFixed(1)}×`}</title>
            </rect>
            <text x={cx} y={y(r.v) - 4} fontSize={9} fill={NAVY} fontWeight={isHl ? 700 : 400} textAnchor="middle" style={{ fontFamily: NUM_FONT }}>
              {r.v.toFixed(1)}
            </text>
            <text x={cx} y={padT + plotH + 13} fontSize={8.5} fill={isHl ? NAVY : MUTED} fontWeight={isHl ? 700 : 400} textAnchor="middle">
              {r.ticker}
            </text>
          </g>
        );
      })}
      {isNum(median) ? (
        <>
          <line x1={padL} y1={y(median)} x2={padL + plotW} y2={y(median)} stroke={CHART_AMBER} strokeWidth={1.2} strokeDasharray="5 3" />
          <text x={padL + plotW} y={y(median) - 4} fontSize={9} fill={CHART_AMBER} fontWeight={700} textAnchor="end">
            median {median.toFixed(1)}×
          </text>
        </>
      ) : null}
      <text x={padL} y={12} fontSize={9.5} fill={MUTED} fontWeight={600}>
        {label}
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------- 13F ownership

/** Top institutional holders — bar = % of shares; green adding, red reducing. */
export function OwnershipBars({ ownership }: { ownership: OwnershipSummary | null | undefined }) {
  const holders = ((ownership?.top_holders || []) as NonNullable<OwnershipSummary["top_holders"]>)
    .filter((h) => isNum(h.weight_pct))
    .slice(0, 8);
  if (holders.length === 0) return null;
  const vmax = Math.max(...holders.map((h) => h.weight_pct as number)) * 1.1 || 1;
  return (
    <div className="flex flex-col gap-1.5">
      {holders.map((h, i) => {
        const chg = h.shares_changed;
        const color = isNum(chg) && chg > 0 ? CHART_GREEN : isNum(chg) && chg < 0 ? CHART_RED : NAVY3;
        const dir = isNum(chg) && chg > 0 ? "▲ added" : isNum(chg) && chg < 0 ? "▼ reduced" : "held";
        return (
          <div key={i} className="flex items-center gap-3">
            <div className="w-44 shrink-0 sm:w-56">
              <div className="truncate text-[11.5px] font-medium text-navy">{h.manager || "—"}</div>
              <div className="text-[9.5px] text-muted">
                {h.is_passive ? "passive" : "active"} · <span style={{ color }}>{dir}</span>
              </div>
            </div>
            <div className="relative h-3.5 flex-1 overflow-hidden rounded-sm bg-border-soft">
              <div
                className="h-full rounded-sm"
                style={{ width: `${Math.max(2, ((h.weight_pct as number) / vmax) * 100)}%`, background: color, opacity: 0.85 }}
                title={`${h.manager}: ${(h.weight_pct as number).toFixed(1)}% of shares`}
              />
            </div>
            <span className="num w-12 shrink-0 text-right text-[11px] font-bold text-navy">
              {(h.weight_pct as number).toFixed(1)}%
            </span>
          </div>
        );
      })}
    </div>
  );
}
