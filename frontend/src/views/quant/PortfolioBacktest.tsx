"use client";

// Portfolio backtest for the Quant desk — the CURRENT optimizer weights held constant
// over past return data (backend qlib_backtest.weighted_portfolio_backtest). Shows a
// smoothed cumulative-return curve vs the Fama-French market, performance & risk stats,
// a full-period FF 5-factor + momentum regression, and — the new bit — how the book's
// exposure to each factor drifts over time (rolling betas). All line charts are smoothed.

import type { QuantPortfolioBacktest, QuantBacktestPoint, QuantExposurePoint } from "@/lib/api";
import { num, pct } from "@/lib/fmt";
import { smoothPath } from "@/components/charts/theme";

const NAVY = "#2F4D73";
const GREY = "#94A3B8";
const MUTED = "#6F7890";
const BORDER = "#E3E6EA";
const BORDER_SOFT = "#EEECE5";
const GREEN = "#1F7A52";
const RED = "#8C2F39";

// Fixed factor order + colors for the exposure chart (categorical, assigned in order).
const FACTORS: { key: string; label: string; color: string }[] = [
  { key: "mkt_rf", label: "Mkt", color: "#2F4D73" },
  { key: "smb", label: "SMB", color: "#C2410C" },
  { key: "hml", label: "HML", color: "#0E7490" },
  { key: "rmw", label: "RMW", color: "#7C3AED" },
  { key: "cma", label: "CMA", color: "#B45309" },
  { key: "mom", label: "Mom", color: "#9333EA" },
];

const signedPct = (v: number | null | undefined, d = 1) =>
  typeof v === "number" && isFinite(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%` : "—";
const tone = (v: number | null | undefined) =>
  typeof v !== "number" || !isFinite(v) ? "text-navy" : v > 0 ? "text-green-700" : v < 0 ? "text-red-700" : "text-navy";

export function PortfolioBacktest({ bt }: { bt: QuantPortfolioBacktest }) {
  if (!bt.available) {
    return <div className="text-[12px] text-muted">{bt.reason || "Optimize a book to backtest it on past data."}</div>;
  }
  const p = bt.performance;
  const reg = bt.factor_regression;
  const curve = bt.curve || [];
  const exposures = bt.exposures || [];

  return (
    <div className="space-y-4">
      <div className="text-[11px] text-muted">
        The current optimized weights held fixed across {bt.n_months} months
        {bt.history_from ? ` (${bt.history_from} → ${bt.history_to})` : ""} of realized returns, vs the{" "}
        {bt.benchmark?.label || "market"}. In-sample by construction — this is “what this exact book would have
        done”, not an out-of-sample test.
      </div>

      <EquityChart curve={curve} benchLabel={bt.benchmark?.label} />

      {p && Object.keys(p).length ? (
        <>
          <div>
            <div className="label mb-1.5">Performance &amp; risk</div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 lg:grid-cols-6">
              <Stat label="Ann. return" value={pct(p.annualized_return)} />
              <Stat label="Volatility" value={pct(p.annualized_vol)} />
              <Stat label="Sharpe" value={num(p.sharpe, 2)} />
              <Stat label="Sortino" value={num(p.sortino, 2)} />
              <Stat label="Max drawdown" value={pct(p.max_drawdown)} />
              <Stat label="Hit rate" value={pct(p.hit_rate, 0)} />
            </div>
          </div>
          <div>
            <div className="label mb-1.5">Vs {bt.benchmark?.label}</div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 lg:grid-cols-6">
              <Stat label="Benchmark ann." value={pct(p.benchmark_annualized_return)} />
              <Stat label="Excess (ann.)" value={pct(p.excess_annualized_return)} tone={p.excess_annualized_return} />
              <Stat label="Info ratio" value={num(p.information_ratio, 2)} tone={p.information_ratio} />
              <Stat label="Tracking err." value={pct(p.tracking_error)} />
              <Stat label="Market beta" value={num(p.beta_vs_market, 2)} />
              {reg?.available ? <Stat label="FF alpha (ann.)" value={pct(reg.alpha_annualized)} tone={reg.alpha_annualized} /> : null}
            </div>
          </div>
        </>
      ) : null}

      {exposures.length > 1 ? (
        <div>
          <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
            <span className="label">Factor exposure over time</span>
            <span className="text-[10px] text-muted">trailing {bt.roll_window}-month rolling betas</span>
          </div>
          <ExposureChart exposures={exposures} />
        </div>
      ) : null}

      {reg?.available ? (
        <div className="text-[10.5px] text-muted">
          Full-period FF 5-factor + momentum regression: alpha {pct(reg.alpha_annualized)}/yr
          {reg.alpha_tstat != null ? ` (t=${num(reg.alpha_tstat, 2)}, ${Math.abs(reg.alpha_tstat) >= 2 ? "significant" : "not sig."})` : ""},
          R² {num(reg.r2, 2)}.
        </div>
      ) : null}
    </div>
  );
}

function Stat({ label, value, tone: t }: { label: string; value: string; tone?: number | null }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`mt-0.5 text-[15px] font-semibold tabular-nums ${t == null ? "text-navy" : tone(t)}`}>{value}</div>
    </div>
  );
}

// Smoothed cumulative-equity line: strategy vs benchmark.
function EquityChart({ curve, benchLabel }: { curve: QuantBacktestPoint[]; benchLabel?: string }) {
  if (curve.length < 2) return null;
  const W = 720, H = 240, pad = { l: 46, r: 14, t: 14, b: 26 };
  const strat = curve.map((p) => p.equity);
  const bench = curve.map((p) => (typeof p.bench_equity === "number" ? p.bench_equity : null));
  const vals = [...strat, ...(bench.filter((v) => v != null) as number[]), 1];
  const ymin = Math.min(...vals), ymax = Math.max(...vals);
  const n = curve.length;
  const X = (i: number) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const Y = (v: number) => pad.t + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - pad.t - pad.b);
  const stratPath = smoothPath(strat.map((v, i): [number, number] => [X(i), Y(v)]));
  const benchXY = bench
    .map((v, i): [number, number] | null => (v == null ? null : [X(i), Y(v)]))
    .filter(Boolean) as [number, number][];
  const benchPath = benchXY.length > 1 ? smoothPath(benchXY) : "";
  const ticks = [ymin, (ymin + ymax) / 2, ymax];
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxWidth: W }}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={W - pad.r} y1={Y(t)} y2={Y(t)} stroke={BORDER} strokeWidth={1} />
            <text x={pad.l - 6} y={Y(t) + 3} textAnchor="end" fontSize={9} fill={MUTED}>{t.toFixed(2)}×</text>
          </g>
        ))}
        <line x1={pad.l} x2={W - pad.r} y1={Y(1)} y2={Y(1)} stroke="#B9C0CA" strokeWidth={1} strokeDasharray="3 3" />
        {benchPath ? <path d={benchPath} fill="none" stroke={GREY} strokeWidth={1.8} /> : null}
        <path d={stratPath} fill="none" stroke={NAVY} strokeWidth={2.2} strokeLinejoin="round" />
        <text x={pad.l} y={H - 8} fontSize={9} fill={MUTED}>{curve[0].date}</text>
        <text x={W - pad.r} y={H - 8} textAnchor="end" fontSize={9} fill={MUTED}>{curve[n - 1].date}</text>
      </svg>
      <div className="mt-1 flex gap-4 text-[11px] text-muted">
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4" style={{ background: NAVY }} />Book</span>
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4" style={{ background: GREY }} />{benchLabel || "Benchmark"}</span>
      </div>
    </div>
  );
}

// Smoothed multi-line chart of the rolling factor betas over time.
function ExposureChart({ exposures }: { exposures: QuantExposurePoint[] }) {
  const W = 720, H = 220, pad = { l: 40, r: 44, t: 12, b: 24 };
  const n = exposures.length;
  const present = FACTORS.filter((f) => exposures.some((e) => typeof e.betas[f.key] === "number"));
  const all: number[] = [];
  for (const e of exposures) for (const f of present) if (typeof e.betas[f.key] === "number") all.push(e.betas[f.key]);
  const ymin = Math.min(0, ...all), ymax = Math.max(0, ...all);
  const X = (i: number) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const Y = (v: number) => pad.t + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - pad.t - pad.b);
  const ticks = niceTicks(ymin, ymax, 4);
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxWidth: W }}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={W - pad.r} y1={Y(t)} y2={Y(t)} stroke={BORDER} strokeWidth={t === 0 ? 1.2 : 1} strokeDasharray={t === 0 ? undefined : "2 3"} />
            <text x={pad.l - 5} y={Y(t) + 3} textAnchor="end" fontSize={9} fill={MUTED}>{t.toFixed(1)}</text>
          </g>
        ))}
        {present.map((f) => {
          const xy = exposures.map((e, i): [number, number] | null =>
            typeof e.betas[f.key] === "number" ? [X(i), Y(e.betas[f.key])] : null
          ).filter(Boolean) as [number, number][];
          if (xy.length < 2) return null;
          const last = exposures[n - 1].betas[f.key];
          return (
            <g key={f.key}>
              <path d={smoothPath(xy)} fill="none" stroke={f.color} strokeWidth={1.8} strokeLinejoin="round" opacity={0.9} />
              {typeof last === "number" ? (
                <text x={W - pad.r + 3} y={Y(last) + 3} fontSize={9} fill={f.color} fontWeight={600}>{f.label}</text>
              ) : null}
            </g>
          );
        })}
        <text x={pad.l} y={H - 6} fontSize={9} fill={MUTED}>{exposures[0].date}</text>
        <text x={W - pad.r} y={H - 6} textAnchor="end" fontSize={9} fill={MUTED}>{exposures[n - 1].date}</text>
      </svg>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted">
        {present.map((f) => (
          <span key={f.key} className="flex items-center gap-1">
            <span className="inline-block h-0.5 w-3.5" style={{ background: f.color }} />{f.label}
          </span>
        ))}
      </div>
    </div>
  );
}

// A few "nice" round tick values spanning [lo, hi] and always including 0 if in range.
function niceTicks(lo: number, hi: number, target: number): number[] {
  const span = hi - lo || 1;
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag;
  const start = Math.ceil(lo / step) * step;
  const out: number[] = [];
  for (let v = start; v <= hi + 1e-9; v += step) out.push(Math.abs(v) < 1e-9 ? 0 : Number(v.toFixed(6)));
  if (!out.includes(0) && lo <= 0 && hi >= 0) out.push(0);
  return out.sort((a, b) => a - b);
}
