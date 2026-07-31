"use client";

// Compare — rank a whole sector in one pass (progress stepper, info box and the
// consumer GroupResultView with its score breakdown).

import { useEffect, useRef, useState } from "react";
import {
  api,
  type CommitteeExtraAnalyst,
  type GroupRequest,
  type GroupResponse,
  type IndustryOption,
  type SectorOption,
} from "@/lib/api";
import type { CommitteeActivityReporter } from "@/components/committee/activity";
import { InfoBox } from "@/components/ui/InfoBox";
import { ProgressStepper } from "@/components/ui/ProgressStepper";
import { WorkflowGraph } from "@/components/committee/WorkflowGraph";
import {
  ProviderStatusStrip,
  ProviderTabs,
  useActiveTab,
  useProviderRuns,
} from "@/components/committee/ProviderRuns";
import { ESTIMATED_RUN_MS, GROUP_STEPS, activeStepIndex } from "@/lib/pipeline";
import { GROUP_WORKFLOW } from "@/lib/workflow";
import { GroupResultView } from "./shared/GroupResultView";
import { MarketPicker, SELECT_CLASS, useMarkets, type MarketSelection } from "./shared/MarketPicker";

import { llmBody, providerLabel, type LlmSelection, type ProviderInfo } from "@/lib/llm";
type Status = "idle" | "loading" | "running" | "done" | "error";

// Kept next to the graph so the "the AI picked these" impression is corrected
// before it forms: the ordering is arithmetic, the AI only argues about it.
const UNDER_THE_HOOD = [
  {
    title: "The maths comes first",
    body:
      "Every company is scored before the AI sees anything — nine numbers each, compared against the rest of the group and added up. That ranking stands on its own, and it still works if no AI is connected.",
  },
  {
    title: "The AI argues, it doesn't calculate",
    body:
      "It receives the finished scores and does what an analyst does: explains what they mean, one sentence per company. It cannot move a company up the table just by liking it.",
  },
  {
    title: "Nothing is hidden",
    body:
      "Click any company and you see every number behind its score, how it compared with the group, and how much each one mattered. The parts add up to the total, exactly.",
  },
];

export function CompareView({
  llm,
  runs: selections,
  providers,
  analysts,
  onActivityChange,
  onAnalyze,
}: {
  llm: LlmSelection;
  /** One entry per provider to ask. Unlike Analyze there is no shared prepare
   *  phase here: a group run's deterministic part is a screener query plus
   *  z-score maths (seconds), not worth splitting a second graph for. */
  runs: LlmSelection[];
  providers: ProviderInfo[];
  analysts: CommitteeExtraAnalyst[];
  onActivityChange?: CommitteeActivityReporter;
  onAnalyze?: (ticker: string) => void;
}) {
  const markets = useMarkets();
  const [sel, setSel] = useState<MarketSelection>({ jur: "US", region: "", countryCode: "" });
  const [sectors, setSectors] = useState<SectorOption[]>([]);
  const [industries, setIndustries] = useState<IndustryOption[]>([]);
  const [sector, setSector] = useState("");
  const [industry, setIndustry] = useState("");
  const [limit, setLimit] = useState(12);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState("");
  const [startedAt, setStartedAt] = useState(0);
  // Ticks while a run is in flight so the workflow graph can highlight the step
  // the stepper believes we are on (it is a time estimate — the backend runs the
  // whole graph inside one blocking request and reports nothing until it lands).
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (status !== "running") return;
    const t = setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => clearInterval(t);
  }, [status, startedAt]);
  const liveStep = GROUP_STEPS[activeStepIndex(GROUP_STEPS, elapsed, ESTIMATED_RUN_MS.group)]?.key;

  // Each selected provider runs the same group request; the deterministic ranking
  // is identical across them, so what differs is the commentary.
  const pending = useRef<GroupRequest | null>(null);
  const providerRuns = useProviderRuns<void, GroupResponse>({
    selections,
    run: async (s) => api.committeeGroup({ ...(pending.current as GroupRequest), ...llmBody(s) }),
  });
  const [activeTab, setActiveTab] = useActiveTab(providerRuns.runs);
  const activeRun = providerRuns.runs.find((r) => r.provider === activeTab) || providerRuns.runs[0];
  const labelFor = (id: string) => providerLabel(providers, id);
  const result = activeRun?.result ?? null;

  useEffect(() => {
    if (providerRuns.busy || providerRuns.runs.length === 0) return;
    const ok = providerRuns.runs.filter((r) => r.status === "done");
    if (ok.length === 0) {
      setError(providerRuns.runs[0]?.error || "Group run failed.");
      setStatus("error");
    } else {
      setStatus("done");
      setError("");
    }
    onActivityChange?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerRuns.busy, providerRuns.runs]);

  const activeCountryCode = sel.jur === "INTL" && sel.countryCode ? sel.countryCode : null;

  useEffect(() => {
    let cancelled = false;
    setStatus((s) => (s === "running" ? s : "loading"));
    setSector("");
    setIndustry("");
    api
      .filters(sel.jur, activeCountryCode)
      .then((m) => {
        if (cancelled) return;
        setSectors(m.filters.sectors);
        setIndustries(m.filters.industries);
        setStatus((s) => (s === "running" ? s : "idle"));
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [sel.jur, activeCountryCode]);

  const industryOptions = sector ? industries.filter((i) => i.sector_code === sector) : industries;

  function run() {
    if (!sector && !industry) {
      setError("Pick a sector or industry group first.");
      setStatus("error");
      return;
    }
    const config: Record<string, unknown> = {};
    if (analysts.length) config.extra_analysts = analysts;
    setStatus("running");
    setStartedAt(Date.now());
    setElapsed(0);
    onActivityChange?.({
      status: "running",
      label: "Compare",
      detail: `${sel.jur}${activeCountryCode ? "/" + activeCountryCode : ""} ${industry || sector}: group verdict`,
    });
    setError("");
    pending.current = {
      mode: "industry",
      jurisdiction: sel.jur,
      country_code: activeCountryCode,
      region: sel.jur === "INTL" ? sel.region || null : null,
      sectors: !industry && sector ? [sector] : null,
      industries: industry ? [industry] : null,
      limit,
      config,
    };
    providerRuns.start();
  }

  return (
    <div className="flex flex-col gap-4">
      {/* ------------------------------------------------------------ hero */}
      <section
        className="relative overflow-hidden rounded-lg border border-navy/30 px-6 py-6"
        style={{ background: "linear-gradient(135deg, #1A2744 0%, #2F4D73 78%, #3A5B85 100%)" }}
      >
        <div className="relative z-10 flex flex-wrap items-end justify-between gap-4">
          <div className="max-w-xl">
            <div className="eyebrow-hero">Compare · relative value</div>
            <h1 className="hero-title" style={{ fontSize: 24, lineHeight: 1.15 }}>
              Which one is the <em>best value?</em>
            </h1>
            <p className="hero-subtext mt-2" style={{ fontSize: 12 }}>
              Pick a sector and the committee ranks its biggest names best → worst in a single debate —
              on the same numbers, at the same moment, with one reason per name.
            </p>
          </div>
          <div className="flex gap-5">
            {[
              { n: "9", l: "numbers each" },
              { n: "1", l: "AI debate" },
              { n: "~3", l: "minutes" },
            ].map((s) => (
              <div key={s.l} className="text-right">
                <div className="num text-[22px] font-bold leading-none text-amber">{s.n}</div>
                <div className="text-[8.5px] uppercase tracking-[0.12em] text-white/45">{s.l}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <InfoBox copyKey="compare" />

      <section className="card p-5">
        <div className="flex flex-wrap items-end gap-3">
          <MarketPicker value={sel} markets={markets} onChange={setSel} />
          <label className="flex flex-col gap-1">
            <span className="label">Sector</span>
            <select
              value={sector}
              onChange={(e) => {
                setSector(e.target.value);
                setIndustry("");
              }}
              className={`${SELECT_CLASS} w-60`}
              disabled={status === "loading"}
            >
              <option value="">{status === "loading" ? "Loading…" : "Pick a sector…"}</option>
              {sectors.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">Industry group (optional)</span>
            <select value={industry} onChange={(e) => setIndustry(e.target.value)} className={`${SELECT_CLASS} w-60`}>
              <option value="">All in sector</option>
              {industryOptions.map((i) => (
                <option key={i.code} value={i.code}>
                  {i.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="label"># companies</span>
            <input
              type="number"
              min={2}
              max={25}
              value={limit}
              onChange={(e) => setLimit(Math.max(2, Math.min(25, parseInt(e.target.value, 10) || 12)))}
              className={`${SELECT_CLASS} w-20`}
            />
          </label>
          <button
            onClick={run}
            disabled={status === "running" || status === "loading"}
            className="h-[32px] rounded-md bg-navy px-6 text-[13px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {status === "running" ? "Comparing…" : "Rank the sector"}
          </button>
        </div>
        <p className="mt-2.5 text-[11px] text-muted">
          The committee takes the largest companies in your pick and ranks them best → worst value in a single debate.
          Takes ~2–4 minutes.
        </p>
        {status === "error" && error && (
          <div className="mt-3 rounded border border-red/40 bg-red/5 p-2 text-[12px] text-red">{error}</div>
        )}
      </section>

      {/* --------------------------------------------- live run: stepper + graph */}
      {status === "running" && (
        <>
          <ProviderStatusStrip runs={providerRuns.runs} labelFor={labelFor} />
          <ProgressStepper
            steps={GROUP_STEPS}
            startedAt={startedAt}
            totalMs={ESTIMATED_RUN_MS.group}
            title={`Comparing ${limit} companies`}
          />
          <section className="card p-5">
            <div className="label">Where the run is right now</div>
            <p className="mt-1 text-[11.5px] text-muted">
              The lit-up step is roughly where things are. We time it rather than track it — the whole
              job runs in one go and only reports back at the end — but these are the real steps, in
              the real order.
            </p>
            <WorkflowGraph spec={GROUP_WORKFLOW} activeStep={liveStep} mode="live" className="mt-3" />
          </section>
        </>
      )}

      {/* ------------------------------------------------- idle: the explainer */}
      {status !== "running" && !result && (
        <section className="card p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <div className="label">{GROUP_WORKFLOW.title}</div>
              <p className="mt-0.5 text-[11.5px] text-muted">{GROUP_WORKFLOW.subtitle}</p>
            </div>
            <span className="text-[10px] text-muted">plays by itself · hover any step to hold it</span>
          </div>
          <WorkflowGraph spec={GROUP_WORKFLOW} mode="explainer" className="mt-3" />
          <div className="mt-4 grid grid-cols-1 gap-4 border-t border-border-soft pt-4 md:grid-cols-3">
            {UNDER_THE_HOOD.map((b) => (
              <div key={b.title}>
                <div className="text-[12px] font-semibold text-navy">{b.title}</div>
                <p className="mt-1 text-[11.5px] leading-relaxed text-muted">{b.body}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {status === "done" && result && (
        <>
          <ProviderTabs
            runs={providerRuns.runs}
            active={activeTab}
            onSelect={setActiveTab}
            labelFor={labelFor}
          />
          {activeRun?.status === "error" ? (
            <div className="rounded border border-red/40 bg-red/5 p-2.5 text-[12px] text-red">
              {labelFor(activeRun.provider)} could not finish: {activeRun.error}
            </div>
          ) : null}
          <GroupResultView result={result} onAnalyze={onAnalyze} />
          <details className="card p-5">
            <summary className="cursor-pointer list-none">
              <span className="label">How this ranking was produced</span>
              <span className="ml-2 text-[11px] text-muted">— the steps that just ran ▾</span>
            </summary>
            <WorkflowGraph spec={GROUP_WORKFLOW} mode="explainer" className="mt-3" />
          </details>
        </>
      )}
    </div>
  );
}
