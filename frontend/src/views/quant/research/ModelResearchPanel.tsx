"use client";

// The agentic training panel: start a run, watch it, read what the committee concluded.
//
// Unlike every other long action in this app, a research run is NOT a blocking request. The
// server returns a run_id immediately and the work continues on a worker thread, so this
// component polls. That buys three things the blocking shape cannot: no timeout ceiling on
// a run that takes 20+ minutes, rounds that are readable the moment they land rather than
// only at the end, and — because the run lives in the ledger, not in a request — the panel
// re-attaches to a run in flight after a page reload.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api, type Jurisdiction, type ResearchRun,
} from "@/lib/api";
import { loadVault, selection } from "@/lib/llm";
import { IterationTimeline } from "./IterationTimeline";

const POLL_MS = 2500;
const TERMINAL = ["complete", "failed", "cancelled"];

const RATING_TONE: Record<number, string> = {
  1: "text-green-700", 2: "text-amber-600", 3: "text-red-700",
};
const RATING_WORD: Record<number, string> = { 1: "robust", 2: "moderate", 3: "fragile" };

const n = (v: unknown, d = 4) => (typeof v === "number" && isFinite(v) ? v.toFixed(d) : "—");

function Stat({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`mt-0.5 text-[15px] font-semibold tabular-nums ${tone ?? "text-navy"}`}>{value}</div>
      {sub ? <div className="text-[10px] text-muted">{sub}</div> : null}
    </div>
  );
}

export function ModelResearchPanel({
  jurisdiction, horizon,
}: { jurisdiction: Jurisdiction; horizon: string }) {
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [iterations, setIterations] = useState(4);
  const [advisorProvider, setAdvisorProvider] = useState<string>("");
  const [err, setErr] = useState("");
  const [starting, setStarting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Providers the browser vault actually holds a key for. The advisor is offered a
  // DIFFERENT one on purpose — an outside view carries more information when it does not
  // share the researcher's priors.
  const [vaultProviders, setVaultProviders] = useState<string[]>([]);
  useEffect(() => {
    try {
      const v = loadVault();
      setVaultProviders(Object.keys(v?.keys ?? {}).filter(Boolean));
    } catch { setVaultProviders([]); }
  }, []);

  const isLive = !!run && !TERMINAL.includes(run.status);

  const refresh = useCallback(async (id: string) => {
    try {
      const r = await api.quantResearchGet(id);
      setRun(r);
      if (TERMINAL.includes(r.status) && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    } catch { /* a dropped poll is not fatal; the next tick retries */ }
  }, []);

  // Re-attach to whatever ran last for this market/horizon, including a run still in flight.
  useEffect(() => {
    let cancelled = false;
    setRun(null); setRunId(null); setErr("");
    api.quantResearchLatest(jurisdiction, horizon)
      .then((r) => {
        if (cancelled || !r || (r as { available?: false }).available === false) return;
        setRun(r); setRunId(r.run_id);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [jurisdiction, horizon]);

  // Poll only while a run is live.
  useEffect(() => {
    if (!runId || !isLive) return;
    pollRef.current = setInterval(() => void refresh(runId), POLL_MS);
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [runId, isLive, refresh]);

  async function start() {
    setStarting(true); setErr("");
    try {
      const sel = selection(loadVault());
      const res = await api.quantResearchStart({
        jurisdiction, label: horizon, max_iterations: iterations,
        advisor_provider: advisorProvider || null,
        // No key for any provider → run the deterministic ladder rather than failing.
        offline: vaultProviders.length === 0 && !sel.apiKey,
      });
      setRunId(res.run_id);
      await refresh(res.run_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally { setStarting(false); }
  }

  async function cancel() {
    if (!runId) return;
    try { await api.quantResearchCancel(runId); await refresh(runId); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  }

  const champRating = (() => {
    const it = (run?.iterations ?? []).find((x) => x.iteration === run?.champion_iteration)
      ?? (run?.iterations ?? []).slice(-1)[0];
    return it?.rating_json?.rating;
  })();

  return (
    <div className="space-y-4">
      {/* controls */}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-[12px]">
          <span className="label">Rounds</span>
          <select value={iterations} onChange={(e) => setIterations(Number(e.target.value))}
            disabled={isLive}
            className="h-[32px] rounded-md border border-border bg-panel px-2 text-[13px] disabled:opacity-50">
            {[2, 3, 4, 6, 8].map((v) => <option key={v} value={v}>{v} rounds</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[12px]">
          <span className="label">External advisor</span>
          <select value={advisorProvider} onChange={(e) => setAdvisorProvider(e.target.value)}
            disabled={isLive}
            title="Running the advisor on a different provider gives the outside view different priors"
            className="h-[32px] rounded-md border border-border bg-panel px-2 text-[13px] disabled:opacity-50">
            <option value="">same provider as the desk</option>
            {vaultProviders.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <button onClick={start} disabled={isLive || starting}
          className="rounded-md bg-navy px-4 py-2 text-[13px] font-semibold text-white hover:bg-navy/90 disabled:opacity-50">
          {starting ? "Starting…" : isLive ? "Research running…" : "Run research committee"}
        </button>
        {isLive ? (
          <button onClick={cancel}
            className="rounded-md border border-amber-500/70 px-3 py-2 text-[12px] font-semibold text-amber-700 hover:bg-amber-50">
            Cancel
          </button>
        ) : null}
        {run && !isLive ? (
          <a href={api.quantResearchReportUrl(run.run_id)} target="_blank" rel="noreferrer"
            className="rounded-md border border-border px-3 py-2 text-[12px] font-semibold text-navy hover:border-navy">
            Download dossier (PDF)
          </a>
        ) : null}
        {vaultProviders.length === 0 ? (
          <span className="text-[11px] text-muted">
            No LLM key in this session — the committee will run its deterministic path.
          </span>
        ) : null}
      </div>

      {err ? (
        <div className="rounded-md bg-red-50 px-3 py-2 text-[12px] text-red-700">{err}</div>
      ) : null}

      {!run ? (
        <p className="text-[12px] text-muted">
          A researcher, a model validation unit, a portfolio manager and an external advisor
          iterate over the training specification — sample selection, outlier treatment,
          normalization, feature set, model family — and each round produces a full validation
          report. The champion is promoted only if it beats the model in production
          out-of-sample and the committee signs off.
        </p>
      ) : (
        <>
          {/* run header */}
          <div className="flex flex-wrap items-start gap-x-7 gap-y-3 rounded-lg border border-border-soft bg-paper/40 p-3">
            <Stat label="Status"
              value={isLive ? "running" : run.status}
              sub={isLive ? (run.current_stage ?? "working…") : (run.stop_reason ?? "")} />
            <Stat label="Rounds"
              value={`${run.iterations_done} of ${run.max_iterations}`}
              sub={run.elapsed_seconds ? `${Math.round(run.elapsed_seconds)}s elapsed` : undefined} />
            <Stat label="Champion"
              value={run.champion_iteration != null ? `round ${run.champion_iteration}` : "—"}
              sub={run.champion_kind ?? undefined} />
            <Stat label="Robustness"
              value={champRating ? `${champRating} — ${RATING_WORD[champRating]}` : "—"}
              sub="1 robust · 3 fragile"
              tone={RATING_TONE[champRating ?? 0]} />
            <Stat label="Best rank-IC" value={n(run.champion_score)} sub="purged walk-forward OOS" />
            <Stat label="Promoted"
              value={run.promoted ? "Yes" : "No"}
              tone={run.promoted ? "text-green-700" : "text-navy"} />
          </div>

          {run.promotion_reason ? (
            <p className="text-[11px] text-muted">{run.promotion_reason}</p>
          ) : null}
          {run.error ? (
            <div className="rounded-md bg-red-50 px-3 py-2 text-[11.5px] text-red-700">{run.error}</div>
          ) : null}

          <IterationTimeline run={run} />

          <p className="text-[10.5px] text-muted">
            Every figure is purged, expanding-window, out-of-sample: at each prediction month
            the model saw only labels already realized by that month minus the horizon
            embargo. These numbers are therefore not comparable with the production model&rsquo;s
            headline, which is measured on a single held-out block with no embargo and is
            correspondingly optimistic.
          </p>
        </>
      )}
    </div>
  );
}
