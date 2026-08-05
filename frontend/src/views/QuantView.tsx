"use client";

// Quant — the qlib-powered desk: cross-sectional alpha (expected returns), a
// factor-structured risk model, portfolio optimization with a runtime-selectable
// optimizer backend (native SLSQP/MIP OR any qlib method), a historical-simulation
// return distribution, and a fixed-weight portfolio backtest of the optimized book
// (equity vs Fama-French + rolling factor exposures). Talks to /api/quant/*.

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Jurisdiction,
  type QuantAlphaResponse,
  type QuantBackends,
  type QuantOptimizeResponse,
  type QuantPerName,
  type ScreenerRow,
} from "@/lib/api";
import { SectionCard } from "@/components/ui/SectionCard";
import { PortfolioTable } from "./quant/PortfolioTable";
import { ReturnDistribution } from "./quant/ReturnDistribution";
import { PortfolioBacktest } from "./quant/PortfolioBacktest";

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

  // Retrain: (re)train the alpha model server-side to cover names it doesn't forecast yet.
  const [retrainStatus, setRetrainStatus] = useState<Status>("idle");
  const [retrainMsg, setRetrainMsg] = useState("");

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
    setRetrainMsg("");
    setRetrainStatus("idle");
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

  // Retrain the (jurisdiction, horizon) alpha model, then re-run the optimize so newly
  // covered names pick up a model forecast. Slow (~1–3 min) — the button shows progress.
  async function runRetrain() {
    setRetrainStatus("running");
    setRetrainMsg("Retraining the alpha model on the full panel — this takes 1–3 minutes…");
    try {
      const res = await api.quantRetrain({ jurisdiction, label: horizon });
      if (res.ok) {
        setRetrainStatus("done");
        setRetrainMsg(
          `Retrained ${res.jurisdiction} ${horizonShort(horizon)} model` +
          (res.rank_ic != null ? ` · rank-IC ${numFmt(res.rank_ic, 3)}` : "") +
          (res.coverage != null ? ` · now covers ${res.coverage} names` : "") +
          ". Re-optimizing…"
        );
        await runOptimize();
        api.quantAlpha({ jurisdiction, top: 12, label: horizon }).then(setAlpha).catch(() => {});
      } else {
        setRetrainStatus("error");
        setRetrainMsg(res.error || "Retraining failed.");
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setRetrainStatus("error");
      setRetrainMsg(/network|fetch|timeout|failed to fetch/i.test(msg)
        ? "Lost the connection while retraining — it may still be running on the server. Try optimizing again in a minute."
        : msg);
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
  // Any universe name the model doesn't forecast (fell back to a historical mean) → offer
  // retrain. per_name covers every present name (incl. 0-weight ones the warning counts).
  const hasUncovered = useMemo(
    () => !!opt?.per_name?.some((p) => p.alpha_source === "historical"),
    [opt]
  );

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
            {opt.warnings?.length || hasUncovered ? (
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {opt.warnings?.length ? <span className="text-[11px] text-amber-600">{opt.warnings.join(" · ")}</span> : null}
                {hasUncovered ? (
                  <button
                    onClick={runRetrain}
                    disabled={retrainStatus === "running"}
                    className="rounded border border-amber-500/70 px-2 py-0.5 text-[11px] font-semibold text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                    title="Train the alpha model so these names get a model forecast instead of a historical mean"
                  >
                    {retrainStatus === "running" ? "Retraining…" : "Retrain model"}
                  </button>
                ) : null}
              </div>
            ) : null}
            {retrainMsg ? (
              <div className={`text-[11px] ${retrainStatus === "error" ? "text-red-700" : "text-muted"}`}>{retrainMsg}</div>
            ) : null}

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

      {/* ---------------------------------------------- portfolio backtest (current weights on past data) */}
      <SectionCard
        eyebrow="Backtest · current book on past data"
        title="Portfolio backtest"
        actions={
          opt?.portfolio_backtest?.available ? (
            <span className="text-[11px] text-muted">fixed weights · {opt.portfolio_backtest.n_months} months</span>
          ) : null
        }
      >
        {opt?.portfolio_backtest ? (
          <PortfolioBacktest bt={opt.portfolio_backtest} />
        ) : (
          <div className="text-[12px] text-muted">
            Optimize a portfolio above — its exact weights are then held constant over past return data and
            benchmarked against the Fama-French market, with the book’s factor exposures traced over time.
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

