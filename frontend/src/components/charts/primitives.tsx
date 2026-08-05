"use client";

// Hand-rolled SVG chart primitives in the MZQA print-chart style.
// All of these degrade to null when the data is missing.

import { useEffect, useState, type ReactNode } from "react";
import {
  BORDER,
  BORDER_SOFT,
  CHART_FONT,
  CHART_GREEN,
  CHART_RED,
  MUTED,
  NAVY,
  NUM_FONT,
  UI_AMBER,
  UI_GREEN,
  UI_RED,
  isNum,
  smoothPath,
} from "./theme";

// ---------------------------------------------------------------- Sparkline

export function Sparkline({
  points,
  width = 64,
  height = 22,
  color,
  strokeWidth = 1.5,
  smooth = false,
}: {
  points: (number | null | undefined)[];
  width?: number;
  height?: number;
  color?: string;
  strokeWidth?: number;
  smooth?: boolean;
}) {
  const vals = (points || []).filter(isNum);
  if (vals.length < 2) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const rng = hi - lo || 1;
  const pad = 2;
  const n = vals.length;
  const xy = vals.map((v, i): [number, number] => [
    pad + (i / (n - 1)) * (width - pad * 2),
    pad + (1 - (v - lo) / rng) * (height - pad * 2),
  ]);
  const d = smooth ? smoothPath(xy) : "M" + xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L");
  const trendColor = color || (vals[n - 1] >= vals[0] ? CHART_GREEN : CHART_RED);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} aria-hidden="true">
      <path
        d={d}
        pathLength={1}
        className="spark-draw"
        fill="none"
        stroke={trendColor}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------- GaugeBar

export type GaugeMarker = {
  value: number;
  label: string;
  sub?: string;
  color?: string;
  dashed?: boolean;
};
export type GaugeZone = { from: number; to: number; color: string; opacity?: number };

/**
 * A horizontal range gauge: a soft track between [min, max] with labelled tick
 * markers. Used for fair-value vs price, MD&A tone and reverse-DCF expectation gaps.
 * Marker labels alternate above/below the track to avoid collisions.
 */
export function GaugeBar({
  min,
  max,
  markers,
  zones,
  format = (v: number) => v.toFixed(0),
  height = 92,
  trackLabelLeft,
  trackLabelRight,
}: {
  min: number;
  max: number;
  markers: GaugeMarker[];
  zones?: GaugeZone[];
  format?: (v: number) => string;
  height?: number;
  trackLabelLeft?: string;
  trackLabelRight?: string;
}) {
  const ms = (markers || []).filter((m) => isNum(m.value));
  if (!isNum(min) || !isNum(max) || max <= min || ms.length === 0) return null;
  const W = 640;
  const padX = 16;
  const trackY = height / 2;
  const trackH = 10;
  const span = max - min;
  const x = (v: number) => padX + (Math.max(min, Math.min(max, v)) - min) / span * (W - padX * 2);

  // Alternate label placement above/below, ordered by x so near markers alternate.
  const ordered = [...ms].sort((a, b) => a.value - b.value);

  return (
    <svg viewBox={`0 0 ${W} ${height}`} width="100%" style={{ fontFamily: CHART_FONT }} role="img">
      <rect
        x={padX}
        y={trackY - trackH / 2}
        width={W - padX * 2}
        height={trackH}
        rx={trackH / 2}
        fill={BORDER_SOFT}
      />
      {(zones || []).map((z, i) => {
        const x1 = x(Math.max(min, z.from));
        const x2 = x(Math.min(max, z.to));
        if (x2 <= x1) return null;
        return (
          <rect
            key={i}
            x={x1}
            y={trackY - trackH / 2}
            width={x2 - x1}
            height={trackH}
            rx={trackH / 2}
            fill={z.color}
            opacity={z.opacity ?? 0.25}
          />
        );
      })}
      <text x={padX} y={trackY + 24} fontSize={9} fill={MUTED}>
        {trackLabelLeft ?? format(min)}
      </text>
      <text x={W - padX} y={trackY + 24} fontSize={9} fill={MUTED} textAnchor="end">
        {trackLabelRight ?? format(max)}
      </text>
      {ordered.map((m, i) => {
        const mx = x(m.value);
        const above = i % 2 === 0;
        const color = m.color || NAVY;
        const labelY = above ? trackY - trackH / 2 - 22 : trackY + trackH / 2 + 22;
        const valueY = above ? trackY - trackH / 2 - 10 : trackY + trackH / 2 + 34;
        // Clamp label anchor so edge markers stay readable.
        const anchor = mx < padX + 40 ? "start" : mx > W - padX - 40 ? "end" : "middle";
        return (
          <g key={i}>
            <line
              x1={mx}
              y1={trackY - trackH / 2 - 5}
              x2={mx}
              y2={trackY + trackH / 2 + 5}
              stroke={color}
              strokeWidth={2.5}
              strokeDasharray={m.dashed ? "4 3" : undefined}
            />
            <text x={mx} y={labelY} fontSize={9} fill={color} fontWeight={600} textAnchor={anchor}>
              {m.label}
            </text>
            <text
              x={mx}
              y={valueY}
              fontSize={11}
              fill={color}
              fontWeight={700}
              textAnchor={anchor}
              style={{ fontFamily: NUM_FONT }}
            >
              {format(m.value)}
            </text>
            {m.sub ? (
              <text
                x={mx}
                y={valueY + 11}
                fontSize={8.5}
                fill={MUTED}
                textAnchor={anchor}
              >
                {m.sub}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------- ScoreRing

export function scoreColor(score: number): string {
  if (score >= 70) return UI_GREEN;
  if (score >= 45) return UI_AMBER;
  return UI_RED;
}

/** Donut ring for a 0–100 score (data confidence, interest score …). The arc
 *  sweeps from zero to its value on mount (CSS transition; a no-op for
 *  reduced-motion users only in spirit — the sweep is subtle and brief). */
export function ScoreRing({
  score,
  size = 64,
  label,
  color,
}: {
  score: number | null | undefined;
  size?: number;
  label?: string;
  color?: string;
}) {
  // Flips one frame after mount so the dasharray transition has a 0 start.
  const [drawn, setDrawn] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setDrawn(true));
    return () => cancelAnimationFrame(id);
  }, []);

  if (!isNum(score)) return null;
  const s = Math.max(0, Math.min(100, score));
  const stroke = size >= 56 ? 6 : 4.5;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const shown = drawn ? s : 0;
  const ringColor = color || scoreColor(s);
  return (
    <div className="flex flex-col items-center gap-1">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${label || "score"} ${Math.round(s)} of 100`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={BORDER_SOFT} strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={ringColor}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${(shown / 100) * c} ${c}`}
          style={{ transition: "stroke-dasharray 0.9s ease-out" }}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
        <text
          x="50%"
          y="50%"
          dy="0.36em"
          textAnchor="middle"
          fontSize={size * 0.3}
          fontWeight={700}
          fill={NAVY}
          style={{ fontFamily: NUM_FONT }}
        >
          {Math.round(s)}
        </text>
      </svg>
      {label ? (
        <div className="label text-center" style={{ fontSize: 9 }}>
          {label}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- RankedScoreBars

export type RankedBarItem = {
  key: string;
  label: string;
  sublabel?: string;
  score: number | null | undefined; // 0–100
  right?: ReactNode;
  onClick?: () => void;
};

/** Horizontal 0–100 score bars for ranked lists (group verdicts, scans). */
export function RankedScoreBars({ items }: { items: RankedBarItem[] }) {
  const rows = (items || []).filter((it) => isNum(it.score));
  if (rows.length === 0) return null;
  const max = Math.max(...rows.map((r) => r.score as number), 1);
  return (
    <div className="flex flex-col gap-2">
      {rows.map((it, idx) => {
        const w = Math.max(2, ((it.score as number) / max) * 100);
        const inner = (
          <>
            <div className="flex w-40 shrink-0 flex-col sm:w-48">
              <span className="truncate text-[12px] font-semibold text-navy">
                <span className="num mr-1.5 text-[10px] text-muted">{String(idx + 1).padStart(2, "0")}</span>
                {it.label}
              </span>
              {it.sublabel ? <span className="truncate text-[10px] text-muted">{it.sublabel}</span> : null}
            </div>
            <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-border-soft">
              <div
                className="h-full rounded-sm"
                style={{ width: `${w}%`, background: idx === 0 ? NAVY : "#476D99", opacity: idx === 0 ? 1 : 0.75 }}
              />
            </div>
            <span className="num w-9 shrink-0 text-right text-[12px] font-bold text-navy">
              {Math.round(it.score as number)}
            </span>
            {it.right}
          </>
        );
        return it.onClick ? (
          <button
            key={it.key}
            onClick={it.onClick}
            className="flex w-full items-center gap-3 rounded px-1 py-0.5 text-left transition-colors hover:bg-white/70"
          >
            {inner}
          </button>
        ) : (
          <div key={it.key} className="flex items-center gap-3 px-1 py-0.5">
            {inner}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- DivergingBars

export type DivergingItem = {
  key: string;
  label: string;
  value: number; // signed percent impact
  note?: string;
};

/** Tornado-style diverging bars for fair-value impact of stressed assumptions. */
export function DivergingBars({ items, unit = "%" }: { items: DivergingItem[]; unit?: string }) {
  const rows = (items || []).filter((it) => isNum(it.value));
  if (rows.length === 0) return null;
  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.value)), 0.01);
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((it) => {
        const frac = Math.min(1, Math.abs(it.value) / maxAbs);
        const pos = it.value >= 0;
        return (
          <div key={it.key} className="flex items-center gap-2" title={it.note || undefined}>
            <div className="w-44 shrink-0 truncate text-right text-[11px] text-navy sm:w-56">{it.label}</div>
            <div className="relative h-3.5 flex-1">
              <div className="absolute inset-y-0 left-1/2 w-px bg-border" />
              <div
                className="absolute inset-y-0 rounded-sm"
                style={{
                  left: pos ? "50%" : `${50 - frac * 50}%`,
                  width: `${frac * 50}%`,
                  background: pos ? CHART_GREEN : CHART_RED,
                  opacity: 0.85,
                }}
              />
            </div>
            <span
              className="num w-14 shrink-0 text-right text-[11px] font-bold"
              style={{ color: pos ? CHART_GREEN : CHART_RED }}
            >
              {pos ? "+" : ""}
              {it.value.toFixed(1)}
              {unit}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- Trend arrow

export function TrendArrow({ direction }: { direction: "up" | "down" | "neu" | null | undefined }) {
  if (direction === "up") return <span style={{ color: "#166534" }}>▲</span>;
  if (direction === "down") return <span style={{ color: "#991B1B" }}>▼</span>;
  return <span style={{ color: MUTED }}>—</span>;
}

// Re-export axis border color for chart-adjacent layouts.
export { BORDER as CHART_BORDER };
