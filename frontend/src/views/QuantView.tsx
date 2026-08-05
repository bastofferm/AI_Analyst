"use client";

// Quant — the qlib-powered desk: cross-sectional alpha (expected returns), a
// factor-structured risk model, portfolio optimization with a runtime-selectable
// optimizer backend (native SLSQP/MIP OR any qlib method), and a walk-forward
// out-of-sample backtest benchmarked against the Fama-French market + factor model.
// Talks to /api/quant/* (see backend/api/routers/quant.py).

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Jurisdiction,
  type QuantAlphaResponse,
  type QuantBackends,
  type QuantBacktestPoint,
  type QuantBacktestResponse,
  type QuantOptimizeResponse,
  type QuantPerName,
  type ScreenerRow,
} from "@/lib/api";
import { SectionCard } from "@/components/ui/SectionCard";
import { PortfolioTable } from "./quant/PortfolioTable";
import { ReturnDistribution } from "./quant/ReturnDistribution";

type Status = "idle" | "running" | "done" | "error";

// Per-market default universes (<=10 liquid names), repopulated when the market flips.
const DEFAULT_UNIVERSES: Record<Jurisdiction, string> = {
  US: "AAPL, MSFT, NVDA, GOOGL, META, AMZN, JPM, XOM, JNJ, PG",
  JP: "7203, 6758, 6861, 8306, 9984, 9432, 9433, 8035, 4063, 7974",
  INTL: "AAPL, MSFT, NVDA, GOOGL, META, AMZN, JPM, XOM, JNJ, PG",
};

const OPTIMIZER_LABELS: Record<string, string> = {
  native: "Native (SLSQP / MIP)",
  qlib_mvo: "qlib · Mean-Variance",
  qlib_gmv: "qlib · Min-Variance",
  qlib_rp: "qlib · Risk Parity",
  qlib_inv: "qlib · Inverse-Vol",
  qlib_enhanced_indexing: "qlib · Enhanced Indexing",
};
const RISK_LABELS: Record<string, string> = {
  qlib_structured: "qlib factor-structured",
  ledoit_wolf: "Ledoit-Wolf shrinkage",
  sample: "Sample covariance",
};
const BETA_LABELS: Record<string, string> = {
  mkt_rf: "Mkt", smb: "SMB", hml: "HML", rmw: "RMW", cma: "CMA", mom: "Mom",
};
// Forward-return horizons the alpha model is trained on. The value is the backend `label`.
const HORIZON_LABELS: Record<string, string> = {
  forward_1m: "1-month", forward_3m: "3-month", forward_6m: "6-month", forward_12m: "12-month",
};
const horizonShort = (label: string) => label.replace("forward_", "");

const pctFmt = (x: number | null | undefined, d = 1) =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(d)}%`;
const numFmt = (x: number | null | undefined, d = 2) =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : x.toFixed(d);

export function QuantView() {
  const [backends, setBackends] = useState<QuantBackends | null>(null);
  const [backendsLoaded, setBackendsLoaded] = useState(false);
  const [jurisdiction, setJurisdiction] = useState<Jurisdiction>("US");
  const [universe, setUniverse] = useState(DEFAULT_UNIVERSES.US);
  const [optimizer, setOptimizer] = useState("qlib_mvo");
  const [riskModel, setRiskModel] = useState("qlib_structured");
  const [alphaSource, setAlphaSource] = useState("model");
  const [horizon, setHorizon] = useState("forward_1m");

  const [optStatus, setOptStatus] = useState<Status>("idle");
  const [opt, setOpt] = useState<QuantOptimizeResponse | null>(null);
  const [optErr, setOptErr] = useState("");
  // Company metadata (logo, English name, valuation/growth metrics) + 2y price
  // sparklines for the holdings table — fetched after each optimize for the book.
  const [meta, setMeta] = useState<Map<string, ScreenerRow>>(new Map());
  const [priceSeries, setPriceSeries] = useState<Map<string, number[]>>(new Map());

  const [alpha, setAlpha] = useState<QuantAlphaResponse | null>(null);

  const [bt, setBt] = useState<QuantBacktestResponse | null>(null);
  const [btStatus, setBtStatus] = useState<Status>("idle");
  const [btErr, setBtErr] = useState("");
  const [topk, setTopk] = useState(30);
  const [longShort, setLongShort] = useState(false);

  const tickers = useMemo(
    () => universe.split(/[,\s]+/).map((t) => t.trim().toUpperCase()).filter(Boolean),
    [universe]
  );

  useEffect(() => {
    api.quantBackends()
      .then(setBackends)
      .catch(() => setBackends(null))
      .finally(() => setBackendsLoaded(true));
  }, []);
  useEffect(() => {
    api.quantAlpha({ jurisdiction, top: 12, label: horizon }).then(setAlpha).catch(() => setAlpha(null));
  }, [jurisdiction, horizon]);

  // Prefer the fetched alpha's model meta (reflects the selected horizon); fall back to /backends.
  const model = alpha?.model ?? backends?.alpha_models?.[jurisdiction];

  function changeMarket(j: Jurisdiction) {
    setJurisdiction(j);
    setUniverse(DEFAULT_UNIVERSES[j]);   // repopulate default tickers on market switch
    setOpt(null);
    setMeta(new Map());
    setPriceSeries(new Map());
    setBt(null);
    setBtStatus("idle");
  }

  async function runOptimize() {
    setOptStatus("running");
    setOptErr("");
    try {
      const res = await api.quantOptimize({
        jurisdiction, tickers, optimizer, risk_model: riskModel, alpha_source: alphaSource, label: horizon,
      });
      setOpt(res);
      setOptStatus("done");
      void loadHoldingsData(res);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setOptErr(/network|fetch|timeout|failed to fetch/i.test(msg)
        ? "Could not reach the optimizer — the alpha model may still be warming up on the server. Wait a few seconds and try again."
        : msg);
      setOptStatus("error");
    }
  }

  // Enrich the book with company metadata + 2y price history for the holdings table.
  // Best-effort: any miss leaves that cell as a graceful placeholder.
  async function loadHoldingsData(res: QuantOptimizeResponse) {
    const jur = jurisdiction === "JP" ? "JP" : "US";
    const held = (res.per_name ?? res.weights.map((w) => ({ ticker: w.ticker, weight: w.weight })))
      .filter((r) => r.weight > 1e-4)
      .map((r) => r.ticker);
    if (!held.length) return;
    setMeta(new Map());
    setPriceSeries(new Map());

    // JP names are keyed by their .T-suffixed primary_ticker in the company dimension,
    // but the optimizer returns the bare code — re-add the suffix for the metadata join.
    const dimTicker = (t: string) => (jur === "JP" && !/\.T$/i.test(t) ? `${t}.T` : t);
    api.screenerRun({
      universe: { jurisdiction, portfolio_tickers: held.map(dimTicker) },
      filters: {}, sort: { key: "market_cap_usd", dir: "desc" }, limit: Math.max(held.length, 1),
    })
      .then((r) => {
        const mp = new Map<string, ScreenerRow>();
        for (const row of r.rows) {
          mp.set(row.ticker.toUpperCase(), row);
          mp.set(row.ticker.replace(/\.T$/i, "").toUpperCase(), row);  // JP: match the .T-stripped code
        }
        setMeta(mp);
      })
      .catch(() => {});

    const from = new Date(Date.now() - 2.05 * 365.25 * 864e5).toISOString().slice(0, 10);
    Promise.all(
      held.map((t) =>
        api.prices(t, jur, from)
          .then((pr) => [t, downsample(pr.prices.map((p) => p.close), 64)] as const)
          .catch(() => [t, [] as number[]] as const)
      )
    ).then((pairs) => setPriceSeries(new Map(pairs)));
  }

  async function runBacktest() {
    setBtStatus("running");
    setBtErr("");
    try {
      const res = await api.quantBacktest({ jurisdiction, topk, long_short: longShort, label: horizon });
      setBt(res);
      setBtStatus(res.available ? "done" : "error");
      if (!res.available) setBtErr(res.reason || "backtest unavailable");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setBtErr(/network|fetch|timeout|failed to fetch/i.test(msg)
        ? "Could not reach the backtest — the first run trains a walk-forward model (~90s) and is cached after. Try again."
        : msg);
      setBtStatus("error");
    }
  }

  const sortedWeights = useMemo(
    () => (opt ? [...opt.weights].sort((a, b) => b.weight - a.weight) : []),
    [opt]
  );
  // Holdings for the enhanced table: the enriched per_name rows, or a metrics-less
  // fallback derived from raw weights if an older backend omitted them.
  const holdings = useMemo<QuantPerName[]>(() => {
    if (!opt) return [];
    if (opt.per_name?.length) return opt.per_name;
    return opt.weights.map((w) => ({
      ticker: w.ticker, weight: w.weight,
      expected_return_annual: NaN, expected_return_horizon: NaN,
      forward_vol_annual: NaN, forward_vol_horizon: NaN, alpha_source: "model" as const,
    }));
  }, [opt]);
  const perf = bt?.performance;
  const reg = bt?.factor_regression;

  return (
    <div className="space-y-5">
      {/* ---------------------------------------------- portfolio optimizer */}
      <SectionCard
        eyebrow="Quantitative desk"
        title="Return · Risk · Portfolio (qlib)"
        actions={
          !backendsLoaded ? (
            <span className="text-[11px] text-muted">loading model…</span>
          ) : model ? (
            <span className="text-[11px] text-muted">
              alpha model · rank-IC {numFmt(model.metrics?.rank_ic_mean, 3)} · forward {model.horizon_months}m
            </span>
          ) : (
            <span className="text-[11px] text-amber-600">no alpha model for {jurisdiction}</span>
          )
        }
      >
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Market">
            <select className={selCls} value={jurisdiction} onChange={(e) => changeMarket(e.target.value as Jurisdiction)}>
              <option value="US">US</option>
              <option value="JP">JP</option>
            </select>
          </Field>
          <Field label="Optimizer backend">
            <select className={selCls} value={optimizer} onChange={(e) => setOptimizer(e.target.value)}>
              {(backends?.optimizers || Object.keys(OPTIMIZER_LABELS)).map((o) => (
                <option key={o} value={o}>{OPTIMIZER_LABELS[o] || o}</option>
              ))}
            </select>
          </Field>
          <Field label="Risk model">
            <select className={selCls} value={riskModel} onChange={(e) => setRiskModel(e.target.value)}>
              {(backends?.risk_models || Object.keys(RISK_LABELS)).map((o) => (
                <option key={o} value={o}>{RISK_LABELS[o] || o}</option>
              ))}
            </select>
          </Field>
          <Field label="Expected returns (μ)">
            <select className={selCls} value={alphaSource} onChange={(e) => setAlphaSource(e.target.value)}>
              <option value="model">qlib alpha model</option>
              <option value="historical">historical mean</option>
            </select>
          </Field>
          <Field label="Horizon">
            <select className={selCls} value={horizon} onChange={(e) => setHorizon(e.target.value)}>
              {Object.entries(HORIZON_LABELS).map(([v, lbl]) => (
                <option key={v} value={v}>{lbl}</option>
              ))}
            </select>
          </Field>
          <button
            onClick={runOptimize}
            disabled={optStatus === "running" || tickers.length < 2}
            className="rounded-md bg-navy px-4 py-2 text-[13px] font-semibold text-white hover:bg-navy/90 disabled:opacity-50"
          >
            {optStatus === "running" ? "Optimizing…" : "Optimize portfolio"}
          </button>
        </div>

        <label className="mt-3 flex flex-col gap-1 text-[12px]">
          <span className="label">Universe ({tickers.length} tickers)</span>
          <textarea
            className="min-h-[48px] rounded-md border border-border bg-panel px-2 py-1.5 font-mono text-[12px]"
            value={universe}
            onChange={(e) => setUniverse(e.target.value)}
          />
        </label>

        {optErr ? <div className="mt-3 rounded-md bg-red-50 px-3 py-2 text-[12px] text-red-700">{optErr}</div> : null}

        {opt ? (
          <div className="mt-4 space-y-4">
            <div className="flex flex-wrap gap-5 text-[13px]">
              <Stat label="Backend" value={OPTIMIZER_LABELS[opt.backend] || opt.backend} />
              <Stat label="Exp. return (ann.)" value={pctFmt(opt.expected_return_annual)} />
              <Stat label="Volatility (ann.)" value={pctFmt(opt.vol_annual)} />
              <Stat label="Sharpe" value={numFmt(opt.sharpe)} />
              <Stat label="Positions" value={String(sortedWeights.filter((w) => w.weight > 1e-4).length)} />
            </div>
            {opt.warnings?.length ? <div className="text-[11px] text-amber-600">{opt.warnings.join(" · ")}</div> : null}

            <PortfolioTable
              perName={holdings}
              meta={meta}
              prices={priceSeries}
              jurisdiction={jurisdiction}
              horizonMonths={opt.horizon_months ?? 1}
            />

            {opt.distribution ? (
              <div className="rounded-lg border border-border bg-panel/40 p-4">
                <div className="flex items-baseline gap-2">
                  <span className="label">Return distribution</span>
                  <span className="text-[10px] text-muted">historical simulation · optimizer weights</span>
                </div>
                <div className="mt-2">
                  <ReturnDistribution dist={opt.distribution} horizonMonths={opt.horizon_months ?? 1} />
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </SectionCard>

      {/* ---------------------------------------------- backtest + FF benchmark */}
      <SectionCard
        eyebrow="Signal quality · out-of-sample"
        title="Walk-forward backtest vs Fama-French"
        actions={
          <div className="flex items-center gap-3 text-[12px]">
            <label className="flex items-center gap-1.5">
              <span className="text-muted">Top-k</span>
              <input type="number" min={5} max={200} value={topk}
                onChange={(e) => setTopk(Math.max(5, Math.min(200, Number(e.target.value) || 30)))}
                className="w-16 rounded-md border border-border bg-panel px-2 py-1 text-[12px]" />
            </label>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={longShort} onChange={(e) => setLongShort(e.target.checked)} />
              <span className="text-muted">long/short</span>
            </label>
            <button
              onClick={runBacktest}
              disabled={btStatus === "running"}
              className="rounded-md bg-navy px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-navy/90 disabled:opacity-50"
            >
              {btStatus === "running" ? "Running…" : "Run backtest"}
            </button>
          </div>
        }
      >
        {btStatus === "running" ? (
          <div className="text-[12px] text-muted">
            Training a walk-forward, out-of-sample model month by month… the first run takes ~90s (cached after).
          </div>
        ) : bt?.available && perf ? (
          <div className="space-y-4">
            <EquityChart curve={bt.curve || []} benchLabel={bt.benchmark?.label} />

            <div>
              <div className="label mb-1.5">Performance &amp; risk</div>
              <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 lg:grid-cols-6">
                <Stat label="Ann. return" value={pctFmt(perf.annualized_return)} />
                <Stat label="Volatility" value={pctFmt(perf.annualized_vol)} />
                <Stat label="Sharpe" value={numFmt(perf.sharpe)} />
                <Stat label="Sortino" value={numFmt(perf.sortino)} />
                <Stat label="Max drawdown" value={pctFmt(perf.max_drawdown)} />
                <Stat label="Hit rate" value={pctFmt(perf.hit_rate, 0)} />
              </div>
            </div>

            <div>
              <div className="label mb-1.5">Benchmark ({bt.benchmark?.label})</div>
              <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 lg:grid-cols-6">
                <Stat label="Benchmark ann." value={pctFmt(perf.benchmark_annualized_return)} />
                <Stat label="Excess (ann.)" value={pctFmt(perf.excess_annualized_return)} tone={perf.excess_annualized_return} />
                <Stat label="Info ratio" value={numFmt(perf.information_ratio)} tone={perf.information_ratio} />
                <Stat label="Tracking err." value={pctFmt(perf.tracking_error)} />
                <Stat label="Market beta" value={numFmt(perf.beta_vs_market)} />
                <Stat label="OOS rank-IC" value={numFmt(bt.ic?.rank_ic_mean, 3)} tone={bt.ic?.rank_ic_mean} />
              </div>
            </div>

            {reg?.available ? (
              <div>
                <div className="label mb-1.5">Fama-French 5-factor + momentum regression</div>
                <div className="flex flex-wrap items-start gap-6">
                  <div className="flex gap-6">
                    <Stat label="Alpha (ann.)" value={pctFmt(reg.alpha_annualized)} tone={reg.alpha_annualized} />
                    <Stat label="Alpha t-stat" value={numFmt(reg.alpha_tstat)}
                      sub={reg.alpha_tstat != null && Math.abs(reg.alpha_tstat) >= 2 ? "significant" : "not sig."} />
                    <Stat label="R²" value={numFmt(reg.r2)} />
                  </div>
                  <BetaBars betas={reg.betas || {}} />
                </div>
              </div>
            ) : null}

            <div className="text-[10.5px] text-muted">
              Out-of-sample walk-forward · ranked on the {bt.horizon_months ?? horizonShort(horizon)}-month alpha,
              {" "}rebalanced monthly · investable universe (≥ $2B) · equal-weight top-{bt.topk}
              {bt.long_short ? " long/short" : " long-only"} · gross of costs · {bt.n_periods} months.
            </div>
          </div>
        ) : (
          <div className="text-[12px] text-muted">
            {btErr || "Run a genuine out-of-sample walk-forward: each month the model is retrained on prior data only, then benchmarked against the Fama-French market and decomposed into factor alpha/betas."}
          </div>
        )}
      </SectionCard>

      {/* ---------------------------------------------- alpha table */}
      <SectionCard eyebrow="Return prediction" title="Top expected returns (alpha model)">
        {alpha?.available ? (
          <div className="overflow-x-auto">
            <table className="w-full max-w-xl text-[12px]">
              <thead>
                <tr className="border-b border-border text-left text-muted">
                  <th className="py-1.5 pr-3">Ticker</th>
                  <th className="py-1.5 pr-3 text-right">{horizonShort(horizon)} return</th>
                  <th className="py-1.5 text-right">Annualized</th>
                </tr>
              </thead>
              <tbody>
                {alpha.rows.map((r) => (
                  <tr key={r.ticker} className="border-b border-border/60">
                    <td className="py-1.5 pr-3 font-medium text-navy">{r.ticker}</td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">{pctFmt(r.expected_return_monthly, 2)}</td>
                    <td className="py-1.5 text-right tabular-nums">{pctFmt(r.expected_return_annual, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-[12px] text-muted">{alpha?.note || "No trained alpha model for this market yet."}</div>
        )}
      </SectionCard>
    </div>
  );
}

// Thin a dense daily price series down to ~`target` points for a crisp sparkline.
function downsample(values: number[], target: number): number[] {
  const clean = values.filter((v) => typeof v === "number" && isFinite(v));
  if (clean.length <= target) return clean;
  const step = clean.length / target;
  const out: number[] = [];
  for (let i = 0; i < target; i++) out.push(clean[Math.min(clean.length - 1, Math.floor(i * step))]);
  out.push(clean[clean.length - 1]);
  return out;
}

const selCls = "rounded-md border border-border bg-panel px-2 py-1.5 text-[13px]";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-[12px]">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: number | null }) {
  const color = tone == null ? "text-navy" : tone > 0 ? "text-green-700" : tone < 0 ? "text-red-700" : "text-navy";
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`mt-0.5 text-[15px] font-semibold tabular-nums ${color}`}>{value}</div>
      {sub ? <div className="text-[10px] text-muted">{sub}</div> : null}
    </div>
  );
}

// Diverging horizontal bars for the FF factor betas.
function BetaBars({ betas }: { betas: Record<string, number> }) {
  const keys = ["mkt_rf", "smb", "hml", "rmw", "cma", "mom"].filter((k) => k in betas);
  if (!keys.length) return null;
  const max = Math.max(1e-6, ...keys.map((k) => Math.abs(betas[k])));
  return (
    <div className="min-w-[220px] flex-1">
      <div className="space-y-1">
        {keys.map((k) => {
          const v = betas[k];
          const w = (Math.abs(v) / max) * 50;
          return (
            <div key={k} className="flex items-center gap-2 text-[11px]">
              <span className="w-8 shrink-0 text-muted">{BETA_LABELS[k] || k}</span>
              <div className="relative h-3 flex-1">
                <span className="absolute left-1/2 h-full w-px bg-border" />
                <span className="absolute h-[7px] rounded-sm"
                  style={{ width: `${w}%`, left: v >= 0 ? "50%" : `${50 - w}%`,
                           background: v >= 0 ? "#2F4D73" : "#DC2626", opacity: 0.8, top: 2 }} />
              </div>
              <span className="w-10 shrink-0 text-right tabular-nums text-navy">{v.toFixed(2)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Strategy vs benchmark cumulative-equity line chart (self-contained SVG).
function EquityChart({ curve, benchLabel }: { curve: QuantBacktestPoint[]; benchLabel?: string }) {
  if (curve.length < 2) return null;
  const W = 720, H = 240, pad = { l: 46, r: 14, t: 14, b: 26 };
  const strat = curve.map((p) => p.equity);
  const bench = curve.map((p) => p.bench_equity ?? null);
  const vals = [...strat, ...(bench.filter((v) => v != null) as number[]), 1];
  const ymin = Math.min(...vals), ymax = Math.max(...vals);
  const n = curve.length;
  const X = (i: number) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const Y = (v: number) => pad.t + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - pad.t - pad.b);
  const line = (series: (number | null)[]) =>
    series.map((v, i) => (v == null ? null : `${i === 0 ? "M" : "L"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`))
      .filter(Boolean).join(" ");
  const ticks = [ymin, (ymin + ymax) / 2, ymax];
  const firstDate = curve[0].date, lastDate = curve[n - 1].date;
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxWidth: W }}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={W - pad.r} y1={Y(t)} y2={Y(t)} stroke="#E3E6EA" strokeWidth={1} />
            <text x={pad.l - 6} y={Y(t) + 3} textAnchor="end" fontSize={9} fill="#6F7890">{t.toFixed(2)}×</text>
          </g>
        ))}
        <line x1={pad.l} x2={W - pad.r} y1={Y(1)} y2={Y(1)} stroke="#B9C0CA" strokeWidth={1} strokeDasharray="3 3" />
        {bench.some((v) => v != null) ? (
          <path d={line(bench)} fill="none" stroke="#94A3B8" strokeWidth={1.6} />
        ) : null}
        <path d={line(strat)} fill="none" stroke="#2F4D73" strokeWidth={2} />
        <text x={pad.l} y={H - 8} fontSize={9} fill="#6F7890">{firstDate}</text>
        <text x={W - pad.r} y={H - 8} textAnchor="end" fontSize={9} fill="#6F7890">{lastDate}</text>
      </svg>
      <div className="mt-1 flex gap-4 text-[11px] text-muted">
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4" style={{ background: "#2F4D73" }} />Strategy</span>
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4" style={{ background: "#94A3B8" }} />{benchLabel || "Benchmark"}</span>
      </div>
    </div>
  );
}
