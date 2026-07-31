"use client";

// Time-axis charts (hover tooltips, dual axes) built on Recharts, styled to the
// MZQA theme. Fixed-height containers — ResponsiveContainer needs a sized parent.

import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CashflowYear, PricePoint, QuarterRow } from "@/lib/api";
import {
  BORDER,
  BORDER_SOFT,
  CHART_GREEN,
  MUTED,
  NAVY,
  NAVY2,
  NAVY3,
  SERIES_COLORS,
  UI_AMBER,
  fmtShort,
  isNum,
} from "./theme";

const AXIS_TICK = { fontSize: 10, fill: MUTED } as const;

const TOOLTIP_STYLES = {
  contentStyle: {
    background: "#FFFFFF",
    border: `1px solid ${BORDER}`,
    borderRadius: 6,
    fontSize: 11,
    padding: "6px 10px",
    boxShadow: "0 6px 18px rgba(47,77,115,0.10)",
  },
  labelStyle: { color: MUTED, fontSize: 10, marginBottom: 2 },
  itemStyle: { padding: 0, fontFamily: "Consolas, monospace" },
} as const;

// ---------------------------------------------------------------- Relative price

/**
 * Multi-line price chart rebased to 100 at the first shared date — the company
 * (navy, bold) vs its peers. Falls back to whatever series exist.
 */
export function RelativePriceChart({
  priceHistory,
  highlight,
  height = 260,
}: {
  priceHistory: Record<string, PricePoint[]> | null | undefined;
  highlight: string;
  height?: number;
}) {
  const series = Object.entries(priceHistory || {}).filter(([, pts]) => (pts || []).length > 2);
  if (series.length === 0) return null;

  // Rebase each series to 100 at its own first close, then merge rows by date.
  const byDate = new Map<string, Record<string, number | string>>();
  for (const [tk, pts] of series) {
    const base = pts[0]?.close || 1;
    for (const p of pts) {
      if (!isNum(p.close)) continue;
      const row = byDate.get(p.date) || { date: p.date };
      row[tk.toUpperCase()] = (p.close / base) * 100;
      byDate.set(p.date, row);
    }
  }
  const data = Array.from(byDate.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const hl = highlight.toUpperCase();
  const tickers = series.map(([tk]) => tk.toUpperCase()).sort((a, b) => (a === hl ? -1 : b === hl ? 1 : 0));

  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 56, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={BORDER_SOFT} vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            minTickGap={70}
            tickFormatter={(d: string) => String(d).slice(0, 7)}
          />
          <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={40} domain={["auto", "auto"]} tickFormatter={(v: number) => v.toFixed(0)} />
          <Tooltip
            {...TOOLTIP_STYLES}
            formatter={(v: number, name: string) => [`${Number(v).toFixed(1)}`, name]}
          />
          {tickers.map((tk, idx) => {
            const isHl = tk === hl;
            return (
              <Line
                key={tk}
                type="monotone"
                dataKey={tk}
                stroke={isHl ? NAVY : SERIES_COLORS[idx % SERIES_COLORS.length]}
                strokeWidth={isHl ? 2.4 : 1.2}
                strokeOpacity={isHl ? 1 : 0.8}
                dot={false}
                connectNulls
                isAnimationActive={false}
              />
            );
          })}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------- Single price line

/** Simple single-series price line (snapshot card, JP fallback). Amber, MZQA-terminal style. */
export function PriceLineChart({
  points,
  height = 200,
  color = UI_AMBER,
}: {
  points: PricePoint[] | null | undefined;
  height?: number;
  color?: string;
}) {
  const data = (points || []).filter((p) => isNum(p.close));
  if (data.length < 2) return null;
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={BORDER_SOFT} vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: BORDER }}
            minTickGap={70}
            tickFormatter={(d: string) => String(d).slice(0, 7)}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={48}
            domain={["auto", "auto"]}
            tickFormatter={(v: number) => fmtShort(v)}
          />
          <Tooltip {...TOOLTIP_STYLES} formatter={(v: number) => [Number(v).toFixed(2), "close"]} />
          <Line type="monotone" dataKey="close" stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------- Quarterly trend

/** Quarterly revenue bars + YoY growth line (last 8 quarters), dual axis.
 *  `currency` is the display symbol for the revenue axis/tooltip. */
export function QuarterlyTrendChart({
  quarters,
  height = 240,
  currency = "$",
}: {
  quarters: QuarterRow[] | null | undefined;
  height?: number;
  currency?: string;
}) {
  const rows = (quarters || []).filter((q) => isNum(q.revenue)).slice(-8);
  if (rows.length < 2) return null;
  const revenueName = `Revenue (${currency}B)`;
  const data = rows.map((q) => ({
    label: `${q.fiscal_period || "Q?"}'${String(q.fiscal_year ?? "").slice(2)}`,
    revenue: (q.revenue as number) / 1e9,
    yoy: isNum(q.yoy_rev_growth_pct) ? q.yoy_rev_growth_pct : null,
  }));
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={BORDER_SOFT} vertical={false} />
          <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: BORDER }} />
          <YAxis yAxisId="rev" tick={AXIS_TICK} tickLine={false} axisLine={false} width={44} tickFormatter={(v: number) => `${fmtShort(v)}B`} />
          <YAxis
            yAxisId="yoy"
            orientation="right"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={40}
            tickFormatter={(v: number) => `${v.toFixed(0)}%`}
          />
          <Tooltip
            {...TOOLTIP_STYLES}
            formatter={(v: number, name: string) =>
              name === revenueName ? [`${currency}${Number(v).toFixed(1)}B`, name] : [`${Number(v).toFixed(1)}%`, name]
            }
          />
          <Bar yAxisId="rev" dataKey="revenue" name={revenueName} fill={NAVY2} radius={[2, 2, 0, 0]} maxBarSize={38} isAnimationActive={false} />
          <Line
            yAxisId="yoy"
            type="monotone"
            dataKey="yoy"
            name="YoY growth"
            stroke={CHART_GREEN}
            strokeWidth={2}
            dot={{ r: 2.4, fill: CHART_GREEN, strokeWidth: 0 }}
            connectNulls
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------- Capital returns

/** Stacked dividends + buybacks bars with the free-cash-flow line on top.
 *  `currency` is the display symbol for the tooltip/axis. */
export function CapitalReturnsChart({
  history,
  height = 240,
  currency = "$",
}: {
  history: CashflowYear[] | null | undefined;
  height?: number;
  currency?: string;
}) {
  const rows = (history || []).filter((r) => isNum(r.free_cash_flow));
  if (rows.length < 2) return null;
  const data = rows.map((r) => ({
    label: `FY${String(r.fiscal_year ?? "").slice(2)}`,
    dividends: ((r.dividends as number) || 0) / 1e9,
    buybacks: ((r.buybacks as number) || 0) / 1e9,
    fcf: ((r.free_cash_flow as number) || 0) / 1e9,
  }));
  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={BORDER_SOFT} vertical={false} />
          <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: BORDER }} />
          <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={44} tickFormatter={(v: number) => `${fmtShort(v)}B`} />
          <Tooltip {...TOOLTIP_STYLES} formatter={(v: number, name: string) => [`${currency}${Number(v).toFixed(1)}B`, name]} />
          <Bar dataKey="dividends" name="Dividends" stackId="ret" fill={NAVY2} maxBarSize={38} isAnimationActive={false} />
          <Bar dataKey="buybacks" name="Buybacks" stackId="ret" fill={NAVY3} radius={[2, 2, 0, 0]} maxBarSize={38} isAnimationActive={false} />
          <Line
            type="monotone"
            dataKey="fcf"
            name="Free cash flow"
            stroke={CHART_GREEN}
            strokeWidth={2}
            dot={{ r: 2.4, fill: CHART_GREEN, strokeWidth: 0 }}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Small shared legend chip row for charts whose legend lives outside the plot. */
export function LegendRow({ items }: { items: { label: string; color: string; line?: boolean }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1">
      {items.map((it) => (
        <span key={it.label} className="flex items-center gap-1.5 text-[10px] text-muted">
          {it.line ? (
            <span className="inline-block h-0.5 w-3 rounded" style={{ background: it.color }} />
          ) : (
            <span className="inline-block h-2 w-2 rounded-sm" style={{ background: it.color }} />
          )}
          {it.label}
        </span>
      ))}
    </div>
  );
}
