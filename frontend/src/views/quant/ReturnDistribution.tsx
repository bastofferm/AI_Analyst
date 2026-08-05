"use client";

// Return-distribution card for the Quant desk — a historical-simulation view of the
// optimized book's return over the forecast horizon. A density histogram with a
// smoothed KDE curve laid over it, the downside (5% VaR) tail shaded, and the first
// four moments (mean · variance · skewness · kurtosis) in a small table embedded in
// the top-right of the plot. Single series, so it follows the app's navy house style
// (no legend needed — the title names it). Data comes from the /optimize response
// (backend api/quant/simulate.py).

import { useMemo } from "react";
import type { QuantDistribution } from "@/lib/api";
import { num, pct } from "@/lib/fmt";

const NAVY = "#2F4D73";
const NAVY_FILL = "rgba(47,77,115,0.16)";
const MUTED = "#6F7890";
const BORDER_SOFT = "#EEECE5";
const AMBER = "#B7791F";
const RED = "#8C2F39";

const signedPct = (v: number | null | undefined, d = 1) =>
  typeof v === "number" && isFinite(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%` : "—";

export function ReturnDistribution({ dist, horizonMonths }: { dist: QuantDistribution; horizonMonths: number }) {
  const view = useMemo(() => buildView(dist), [dist]);

  if (!dist.available || !view) {
    return (
      <div className="text-[12px] text-muted">
        {dist.reason || "A return distribution needs enough overlapping price history for this book."}
      </div>
    );
  }

  const m = dist.moments!;
  const p = dist.percentiles || {};
  const ann = dist.annualized;
  const { W, H, pad, bars, curvePath, X, Y, baseline, xticks, meanX, zeroX, varX } = view;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[11px] text-muted">
        <span>
          Distribution of the book’s <span className="font-medium text-navy">{horizonMonths}-month</span> return —
          historical simulation over {dist.n_samples?.toLocaleString()} overlapping windows
          {dist.history_from ? ` (${dist.history_from} → ${dist.history_to})` : ""}.
        </span>
        {ann ? (
          <span>
            ≈ <span className="font-medium text-navy tabular-nums">{signedPct(ann.mean)}</span> / yr ·
            σ <span className="font-medium text-navy tabular-nums">{pct(ann.vol)}</span>
          </span>
        ) : null}
      </div>

      <div className="relative overflow-x-auto">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxWidth: W }} role="img"
          aria-label={`Histogram of ${horizonMonths}-month portfolio returns with a smoothed density curve`}>
          {/* downside (< 5th percentile) tail shading */}
          {typeof p.p5 === "number" ? (
            <rect x={pad.l} y={pad.t} width={Math.max(0, X(p.p5) - pad.l)} height={baseline - pad.t}
              fill={RED} opacity={0.06} />
          ) : null}

          {/* histogram bars (density) */}
          {bars.map((b, i) => (
            <g key={i}>
              <rect x={b.x} y={b.y} width={b.w} height={Math.max(0, baseline - b.y)} rx={1.5}
                fill={NAVY_FILL} stroke={NAVY} strokeWidth={0.6}>
                <title>{`${signedPct(b.x0, 1)} … ${signedPct(b.x1, 1)}: ${b.count} windows`}</title>
              </rect>
            </g>
          ))}

          {/* smoothed KDE curve */}
          <path d={curvePath} fill="none" stroke={NAVY} strokeWidth={2} strokeLinejoin="round" />

          {/* zero + mean reference lines */}
          {zeroX !== null ? (
            <line x1={zeroX} x2={zeroX} y1={pad.t} y2={baseline} stroke={MUTED} strokeWidth={1} strokeDasharray="2 3" />
          ) : null}
          <line x1={meanX} x2={meanX} y1={pad.t} y2={baseline} stroke={AMBER} strokeWidth={1.4} strokeDasharray="4 3" />
          <text x={meanX} y={pad.t - 4} textAnchor="middle" fontSize={9} fill={AMBER}>mean {signedPct(m.mean)}</text>

          {/* 5% VaR marker */}
          {varX !== null && typeof p.p5 === "number" ? (
            <text x={varX} y={baseline + 22} textAnchor="middle" fontSize={9} fill={RED}>
              5% VaR {signedPct(p.p5)}
            </text>
          ) : null}

          {/* x-axis */}
          <line x1={pad.l} x2={W - pad.r} y1={baseline} y2={baseline} stroke={BORDER_SOFT} strokeWidth={1} />
          {xticks.map((t, i) => (
            <text key={i} x={X(t)} y={baseline + 12} textAnchor="middle" fontSize={9} fill={MUTED}>
              {signedPct(t, 0)}
            </text>
          ))}
        </svg>

        {/* embedded moments table */}
        <div className="pointer-events-none absolute right-2 top-2 rounded-md border border-border-soft bg-panel/90 px-3 py-2 text-[11px] shadow-sm backdrop-blur-sm">
          <div className="label mb-1 text-[9px]">Moments · {horizonMonths}-mo return</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 tabular-nums">
            <Moment label="Mean" value={signedPct(m.mean)} />
            <Moment label="Variance" value={num(m.variance, 4)} />
            <Moment label="Skewness" value={num(m.skewness, 2)} hint={m.skewness > 0.15 ? "right" : m.skewness < -0.15 ? "left" : undefined} />
            <Moment label="Kurtosis" value={num(m.kurtosis, 2)} hint={m.kurtosis > 0.5 ? "fat" : undefined} />
          </div>
          <div className="mt-1.5 grid grid-cols-3 gap-x-3 border-t border-border-soft pt-1 text-[10px] text-muted tabular-nums">
            <Ctx label="σ" value={pct(m.std)} />
            <Ctx label="Median" value={signedPct(p.p50)} />
            <Ctx label="95%" value={signedPct(p.p95)} />
          </div>
        </div>
      </div>

      {typeof dist.weight_covered === "number" && dist.weight_covered < 0.999 ? (
        <div className="text-[10px] text-amber-600">
          Covers {pct(dist.weight_covered, 0)} of book weight — names with too little price history are excluded.
        </div>
      ) : null}
    </div>
  );
}

function Moment({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-muted">{label}</span>
      <span className="font-semibold text-navy">
        {value}
        {hint ? <span className="ml-1 text-[9px] font-normal text-muted">{hint}</span> : null}
      </span>
    </div>
  );
}
function Ctx({ label, value }: { label: string; value: string }) {
  return <div className="flex items-baseline justify-between gap-1"><span>{label}</span><span className="font-medium text-navy">{value}</span></div>;
}

// Build the pixel-space geometry for the SVG from the distribution payload.
function buildView(dist: QuantDistribution) {
  const hist = dist.histogram || [];
  const curve = dist.curve || [];
  if (hist.length < 2 && curve.length < 2) return null;

  const W = 720, H = 260;
  const pad = { l: 10, r: 10, t: 18, b: 30 };
  const baseline = H - pad.b;

  const xs = [...hist.map((b) => b.x0), ...hist.map((b) => b.x1), ...curve.map((c) => c.x)];
  const xmin = Math.min(...xs);
  const xmax = Math.max(...xs);
  const ymax = Math.max(1e-9, ...hist.map((b) => b.density), ...curve.map((c) => c.y));

  const X = (v: number) => pad.l + ((v - xmin) / (xmax - xmin || 1)) * (W - pad.l - pad.r);
  const Y = (d: number) => pad.t + (1 - d / ymax) * (baseline - pad.t);

  const bars = hist.map((b) => {
    const x0 = X(b.x0), x1 = X(b.x1);
    const gap = Math.min(1, (x1 - x0) / 6);
    return { x: x0 + gap, w: Math.max(0.5, x1 - x0 - 2 * gap), y: Y(b.density), x0: b.x0, x1: b.x1, count: b.count };
  });

  const curvePath = curve
    .map((c, i) => `${i === 0 ? "M" : "L"}${X(c.x).toFixed(1)},${Y(c.y).toFixed(1)}`)
    .join(" ");

  // ~6 evenly spaced x ticks across the domain.
  const nTicks = 6;
  const xticks = Array.from({ length: nTicks + 1 }, (_, i) => xmin + (i / nTicks) * (xmax - xmin));

  const meanX = X(dist.moments?.mean ?? 0);
  const zeroX = xmin <= 0 && xmax >= 0 ? X(0) : null;
  const varX = typeof dist.percentiles?.p5 === "number" ? X(dist.percentiles.p5) : null;

  return { W, H, pad, bars, curvePath, X, Y, baseline, xticks, meanX, zeroX, varX };
}
