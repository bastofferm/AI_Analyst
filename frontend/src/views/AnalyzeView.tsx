"use client";

// Analyze — the deep-dive view. Form → estimated progress stepper (+ instant
// data snapshot while waiting) → the full story: verdict, debate, scenarios,
// valuation charts, health, tone, ownership, memo, toolkit, follow-up.
// Ports SingleStockTab's run/gate/preset logic unchanged.

import { useEffect, useRef, useState } from "react";
import { api, type CommitteeExtraAnalyst, type CommitteeResponse, type DqWarning } from "@/lib/api";
import type { CommitteeActivityReporter } from "@/components/committee/activity";
import { DqBanner } from "@/components/committee/DqBanner";
import { CompanySearchInput } from "@/components/ui/CompanySearchInput";
import { ProgressStepper } from "@/components/ui/ProgressStepper";
import { HelpTip } from "@/components/ui/HelpTip";
import { ESTIMATED_RUN_MS, SINGLE_STOCK_STEPS, activeStepIndex } from "@/lib/pipeline";
import { WorkflowGraph } from "@/components/committee/WorkflowGraph";
import { KeySourceNote } from "@/components/committee/KeySourceNote";
import { CompanyIdentityHeader } from "./analyze/CompanyIdentityHeader";
import { COMMITTEE_WORKFLOW } from "@/lib/workflow";
import { SnapshotStrip } from "./analyze/Snapshot";
import { CompanyDataSection } from "./analyze/CompanyDataSection";
import { VerdictHero } from "./analyze/VerdictHero";
import { DebateSection } from "./analyze/DebateSection";
import { ScenariosSection } from "./analyze/ScenariosSection";
import { ValuationSection } from "./analyze/ValuationSection";
import { HealthSection } from "./analyze/HealthSection";
import { ToneSection } from "./analyze/ToneSection";
import { OwnershipSection } from "./analyze/OwnershipSection";
import { MemoSection } from "./analyze/MemoSection";
import { FollowUpSection } from "./analyze/FollowUpSection";
import { AnalystToolkit } from "./analyze/AnalystToolkit";

import {
  ProviderStatusStrip,
  ProviderTabs,
  useActiveTab,
  useProviderRuns,
} from "@/components/committee/ProviderRuns";
import { llmBody, providerLabel, type LlmSelection, type ProviderInfo } from "@/lib/llm";
type Status = "idle" | "running" | "done" | "error";

/** Pull the structured gate detail out of a 422 error thrown by fetchJSON
 *  (message shape: "422 …: {\"detail\": {…}}"). Returns null for other errors. */
function parseGateError(err: unknown): DqWarning | null {
  if (!(err instanceof Error)) return null;
  const i = err.message.indexOf("{");
  if (i < 0) return null;
  try {
    const body = JSON.parse(err.message.slice(i));
    const detail = (body?.detail ?? body) as DqWarning;
    if (detail && (Array.isArray(detail.dq_errors) || "is_dq_passed" in detail || "is_data_complete" in detail)) {
      return detail;
    }
  } catch {
    /* not JSON */
  }
  return null;
}

export function AnalyzeView({
  llm,
  runs: selections,
  providers,
  analysts,
  onActivityChange,
  presetTicker,
  presetNonce,
}: {
  llm: LlmSelection;
  /** One entry per provider this run should ask. The first is the primary and runs
   *  the shared preparation phase; extras debate the same prepared evidence. */
  runs: LlmSelection[];
  providers: ProviderInfo[];
  analysts: CommitteeExtraAnalyst[];
  onActivityChange?: CommitteeActivityReporter;
  /** Ticker handed over from Home/Explore/Ideas; applied whenever presetNonce ticks. */
  presetTicker?: string;
  presetNonce?: number;
}) {
  const [ticker, setTicker] = useState("");
  const [years, setYears] = useState("");
  const [strict, setStrict] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState("");
  const [gate, setGate] = useState<DqWarning | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  // Drives the workflow graph's highlight while a run is in flight. Time-based,
  // like the stepper — the backend streams nothing until the graph finishes.
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (status !== "running") return;
    setElapsed(Date.now() - startedAt);
    const t = setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => clearInterval(t);
  }, [status, startedAt]);
  const liveStep = SINGLE_STOCK_STEPS[activeStepIndex(SINGLE_STOCK_STEPS, elapsed, ESTIMATED_RUN_MS.single)]?.key;
  const [runNonce, setRunNonce] = useState(0);
  const [runningTicker, setRunningTicker] = useState("");

  // Phase 1 runs once and its evidence is shared; phase 2 runs per provider, in
  // parallel, so each tab's status is the real state of its own request.
  const pending = useRef<{ ticker: string; years: number[]; config: Record<string, unknown> } | null>(null);
  const providerRuns = useProviderRuns<string, CommitteeResponse>({
    selections,
    prepare: async (primary) => {
      const p = pending.current!;
      const res = await api.committeePrepare({
        ticker: p.ticker,
        target_years: p.years,
        config: p.config,
        ...llmBody(primary),
      });
      return res.prepared_id;
    },
    run: async (sel, preparedId) =>
      api.committeeDebate({ prepared_id: preparedId, ticker: pending.current!.ticker, ...llmBody(sel) }),
  });
  const [activeTab, setActiveTab] = useActiveTab(providerRuns.runs);
  const activeRun = providerRuns.runs.find((r) => r.provider === activeTab) || providerRuns.runs[0];
  const labelFor = (id: string) => providerLabel(providers, id);

  // Mirror the fan-out into this view's single status/result model, so everything
  // downstream (the story sections, the activity chip) is unchanged.
  useEffect(() => {
    if (providerRuns.prepareError) {
      const detail = parseGateError(new Error(providerRuns.prepareError));
      if (detail) setGate(detail);
      else setError(providerRuns.prepareError);
      setStatus("error");
      onActivityChange?.(null);
      return;
    }
    if (providerRuns.busy) return;
    if (providerRuns.runs.length === 0) return;
    const ok = providerRuns.runs.filter((r) => r.status === "done");
    if (ok.length === 0) {
      setError(providerRuns.runs[0]?.error || "Committee run failed.");
      setStatus("error");
    } else {
      setStatus("done");
      setError("");
    }
    onActivityChange?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerRuns.busy, providerRuns.runs, providerRuns.prepareError]);

  const result = activeRun?.result ?? null;

  // A pick from Home/Explore/Ideas prefills the ticker (nonce guards repeat picks
  // of the same name). We stop short of auto-running — runs take minutes, so the
  // user presses the button when ready.
  useEffect(() => {
    if (!presetNonce) return;
    const tk = (presetTicker || "").trim().toUpperCase();
    if (tk) {
      setTicker(tk);
      setError("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presetNonce]);

  async function run() {
    const tk = ticker.trim().toUpperCase();
    if (!tk) {
      setError("Enter a ticker first — try MSFT.");
      setGate(null);
      setStatus("error");
      return;
    }
    const target_years = years
      .split(/[\s,]+/)
      .map((s) => parseInt(s, 10))
      .filter((n) => Number.isFinite(n));

    const config: Record<string, unknown> = {};
    if (analysts.length) config.extra_analysts = analysts;
    if (strict) config.dq_enforce = true;

    setStatus("running");
    setStartedAt(Date.now());
    setRunningTicker(tk);
    setRunNonce((n) => n + 1);
    onActivityChange?.({
      status: "running",
      label: "Analyze",
      detail:
        selections.length > 1
          ? `${tk}: evidence → ${selections.length} models debating`
          : `${tk}: gate → analysis → debate → memo`,
    });
    setError("");
    setGate(null);
    pending.current = { ticker: tk, years: target_years, config };
    providerRuns.start();
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="label">Analyze</div>
        <h1 className="mt-1 text-[22px] font-semibold text-navy">Put a stock in front of the committee</h1>
      </div>

      {/* ------------------------------------------------ Form */}
      <section className="card p-5">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="label">Ticker or company name</span>
            <CompanySearchInput
              value={ticker}
              onChange={setTicker}
              onPick={(r) => setTicker(r.ticker)}
              onSubmit={() => status !== "running" && run()}
              disabled={status === "running"}
            />
          </label>
          <button
            onClick={run}
            disabled={status === "running"}
            className="h-[42px] rounded-md bg-navy px-8 text-[14px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {status === "running" ? "Committee in session…" : "Run the committee"}
          </button>
          <button
            onClick={() => setShowAdvanced((v) => !v)}
            className="h-[42px] rounded-md px-2 text-[11px] font-medium text-muted transition-colors hover:text-navy"
          >
            {showAdvanced ? "Hide options" : "Options"}
          </button>
        </div>
        <p className="mt-2 text-[11px] text-muted">
          Nine AI analysts study the company&apos;s official filings and debate it — a full run takes about 3–6 minutes.
          You can browse other views while it works.
        </p>

        {/* Which key this run will use: the browser vault, else a Windows user
            environment variable resolved server-side. */}
        <KeySourceNote llm={llm} className="mt-2.5" />

        {showAdvanced ? (
          <div className="mt-3 flex flex-wrap items-end gap-4 border-t border-border-soft pt-3">
            <label className="flex flex-col gap-1">
              <span className="label">Fiscal years (optional)</span>
              <input
                value={years}
                onChange={(e) => setYears(e.target.value)}
                placeholder="2022 2023 2024 2025"
                className="h-[32px] w-56 rounded-md border border-border bg-white px-2 text-[12.5px] text-navy outline-none focus:border-navy"
              />
            </label>
            <label className="flex items-center gap-2 pb-1.5 text-[12px] text-navy" title="If the accounting data fails an identity check, refuse to run instead of proceeding with a warning.">
              <input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} />
              Strict <HelpTip term="data confidence">data-governance</HelpTip> gate
            </label>
          </div>
        ) : null}

        {status === "error" && error && (
          <div className="mt-3 rounded border border-red/40 bg-red/5 p-2.5 text-[12px] text-red">{error}</div>
        )}
      </section>

      {/* ------------------------------------------------ Running */}
      {status === "running" && (
        <>
          {/* Real per-provider status — each debate is its own request, so this is
              the actual lifecycle, not the stepper's time estimate. */}
          <ProviderStatusStrip
            runs={providerRuns.runs}
            preparing={providerRuns.preparing}
            labelFor={labelFor}
          />
          <ProgressStepper
            steps={SINGLE_STOCK_STEPS}
            startedAt={startedAt}
            totalMs={ESTIMATED_RUN_MS.single}
            title={`The committee is deliberating on ${runningTicker}`}
          />
          <section className="card p-5">
            <div className="label">Where the committee is right now</div>
            <p className="mt-1 text-[11.5px] text-muted">
              The lit-up step is roughly where things are. We time it rather than track it — the whole job
              runs in one go and only reports back at the end — but these are the real steps, in the real
              order.
            </p>
            <WorkflowGraph spec={COMMITTEE_WORKFLOW} activeStep={liveStep} mode="live" className="mt-3" />
          </section>
          <SnapshotStrip ticker={runningTicker} runNonce={runNonce} />
          <CompanyDataSection ticker={runningTicker} eyebrow="Read while you wait" />
        </>
      )}

      {/* ------------------------------------------------ Who are we looking at?
          Renders as soon as the typed/picked ticker resolves, so the page shows a
          company rather than a code. During a run it follows the running ticker. */}
      <CompanyIdentityHeader ticker={status === "running" ? runningTicker : ticker} />

      {/* ------------------------------------------------ Idle: what a run involves */}
      {status === "idle" && (
        <section className="card p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <div className="label">{COMMITTEE_WORKFLOW.title}</div>
              <p className="mt-0.5 text-[11.5px] text-muted">{COMMITTEE_WORKFLOW.subtitle}</p>
            </div>
            <span className="text-[10px] text-muted">plays by itself · hover any step to hold it</span>
          </div>
          <WorkflowGraph spec={COMMITTEE_WORKFLOW} mode="explainer" className="mt-3" />
        </section>
      )}

      {/* ------------------------------------------------ Data basis preview (before any run) */}
      {(status === "idle" || (status === "error" && !gate)) && (
        <CompanyDataSection ticker={ticker} eyebrow="Before the committee sits" />
      )}

      {/* ------------------------------------------------ Blocked by the strict gate */}
      {status === "error" && gate && (
        <section className="card p-5">
          <DqBanner warning={gate} blocked ticker={ticker} />
          <div className="mt-3 text-[11.5px] text-muted">
            The strict gate stopped the run before any opinions were formed. Untick{" "}
            <b>Strict data-governance</b> under Options to let the committee run anyway — it will flag the data
            issues as a warning instead.
          </div>
        </section>
      )}

      {/* ------------------------------------------------ The story */}
      {status === "done" && result && (
        <div className="flex flex-col gap-4">
          {/* Only appears when more than one model answered. */}
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
          <VerdictHero result={result} />
          <DebateSection result={result} />
          <ScenariosSection result={result} />
          <ValuationSection result={result} />
          <HealthSection result={result} />
          <ToneSection result={result} />
          <OwnershipSection result={result} />
          <MemoSection result={result} />
          <CompanyDataSection ticker={result.ticker} eyebrow="Appendix · the data basis" />
          <AnalystToolkit result={result} />
          <FollowUpSection result={result} llm={llm} />
          <p className="px-1 text-center text-[10.5px] text-muted">
            Model-generated research, not investment advice. Estimates can be wrong — always do your own research.
          </p>
        </div>
      )}
    </div>
  );
}
