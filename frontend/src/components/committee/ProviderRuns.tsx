"use client";

// Runs one job per selected LLM provider and shows which one is working right now.
//
// The status here is REAL, not estimated. A committee run is a blocking request
// with no streaming, so the stepper inside a tab can only guess at progress — but
// because every provider gets its own request, the tab's queued/running/done state
// is the actual lifecycle of that request. DeepSeek's tab can go green while
// Claude's is still spinning.
//
// One provider selected collapses the chrome entirely, so the single-provider
// case looks exactly as it did before this existed.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LlmSelection } from "@/lib/llm";

export type RunStatus = "queued" | "running" | "done" | "error";

export type ProviderRun<T> = {
  provider: string;
  model: string | null;
  status: RunStatus;
  startedAt: number;
  result: T | null;
  error: string | null;
};

export type ProviderRunsState<T> = {
  runs: ProviderRun<T>[];
  /** True while any run is still queued or in flight. */
  busy: boolean;
  /** Shared setup step (e.g. the committee's evidence phase) still running. */
  preparing: boolean;
  prepareError: string | null;
  start: () => void;
  reset: () => void;
};

/** Drive N provider jobs, optionally behind one shared setup step whose result is
 *  passed to each job. `prepare` runs once; `run` runs per provider, in parallel. */
export function useProviderRuns<P, T>({
  selections,
  prepare,
  run,
  onSettled,
}: {
  selections: LlmSelection[];
  /** Shared, provider-independent setup. The first selection runs it. */
  prepare?: (primary: LlmSelection) => Promise<P>;
  run: (sel: LlmSelection, prepared: P) => Promise<T>;
  onSettled?: (runs: ProviderRun<T>[]) => void;
}): ProviderRunsState<T> {
  const [runs, setRuns] = useState<ProviderRun<T>[]>([]);
  const [preparing, setPreparing] = useState(false);
  const [prepareError, setPrepareError] = useState<string | null>(null);
  // Guards against a late response from a superseded run overwriting a newer one.
  const nonce = useRef(0);

  const latest = useRef({ selections, prepare, run, onSettled });
  useEffect(() => {
    latest.current = { selections, prepare, run, onSettled };
  });

  const reset = useCallback(() => {
    nonce.current += 1;
    setRuns([]);
    setPreparing(false);
    setPrepareError(null);
  }, []);

  const start = useCallback(() => {
    const mine = ++nonce.current;
    const { selections: sels, prepare: doPrepare, run: doRun, onSettled: settled } = latest.current;
    if (sels.length === 0) return;

    setPrepareError(null);
    setRuns(
      sels.map((s) => ({
        provider: s.provider,
        model: s.model,
        status: "queued" as RunStatus,
        startedAt: 0,
        result: null,
        error: null,
      })),
    );

    const patch = (provider: string, next: Partial<ProviderRun<T>>) => {
      if (nonce.current !== mine) return;
      setRuns((prev) => prev.map((r) => (r.provider === provider ? { ...r, ...next } : r)));
    };

    (async () => {
      let prepared: P;
      if (doPrepare) {
        setPreparing(true);
        try {
          prepared = await doPrepare(sels[0]);
        } catch (err) {
          if (nonce.current !== mine) return;
          setPreparing(false);
          setPrepareError(err instanceof Error ? err.message : "Preparation failed.");
          setRuns([]);
          return;
        }
        if (nonce.current !== mine) return;
        setPreparing(false);
      } else {
        prepared = undefined as unknown as P;
      }

      // allSettled, not all: one provider's 401 must not cancel the others.
      const results = await Promise.allSettled(
        sels.map(async (sel) => {
          patch(sel.provider, { status: "running", startedAt: Date.now() });
          try {
            const value = await doRun(sel, prepared);
            patch(sel.provider, { status: "done", result: value });
            return value;
          } catch (err) {
            patch(sel.provider, {
              status: "error",
              error: err instanceof Error ? err.message : "Run failed.",
            });
            throw err;
          }
        }),
      );

      if (nonce.current !== mine || !settled) return;
      setRuns((prev) => {
        settled(prev);
        return prev;
      });
      void results;
    })();
  }, []);

  const busy = preparing || runs.some((r) => r.status === "queued" || r.status === "running");
  return { runs, busy, preparing, prepareError, start, reset };
}

// ---------------------------------------------------------------- presentation

const DOT: Record<RunStatus, string> = {
  queued: "bg-muted/50",
  running: "bg-navy",
  done: "bg-[#1F7A52]",
  error: "bg-[#8C2F39]",
};

const WORD: Record<RunStatus, string> = {
  queued: "waiting",
  running: "working",
  done: "done",
  error: "failed",
};

function StatusDot({ status }: { status: RunStatus }) {
  return (
    <span className="relative flex h-2 w-2 shrink-0">
      {status === "running" ? (
        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${DOT[status]}`} />
      ) : null}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${DOT[status]}`} />
    </span>
  );
}

/** The "which API is running" strip: one chip per provider, live. */
export function ProviderStatusStrip<T>({
  runs,
  preparing,
  label,
  labelFor,
}: {
  runs: ProviderRun<T>[];
  preparing?: boolean;
  /** What the shared setup step is called, e.g. "Gathering the evidence". */
  label?: string;
  labelFor: (providerId: string) => string;
}) {
  if (runs.length === 0 && !preparing) return null;
  const done = runs.filter((r) => r.status === "done" || r.status === "error").length;

  return (
    <div className="card border-navy/25 p-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
          {preparing ? label || "Getting the evidence together" : "Running the analysis"}
        </div>
        {!preparing && runs.length > 1 ? (
          <div className="num text-[11px] text-muted">
            {done} of {runs.length} finished
          </div>
        ) : null}
      </div>

      {preparing ? (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-navy">
          <StatusDot status="running" />
          <span>Reading the filings and doing the maths — this part is shared by every model.</span>
        </div>
      ) : (
        <div className="mt-2.5 flex flex-wrap gap-2">
          {runs.map((r) => (
            <span
              key={r.provider}
              className="inline-flex items-center gap-2 rounded-full border border-border-soft bg-white/70 px-2.5 py-1"
              title={r.error || (r.model ? `${labelFor(r.provider)} · ${r.model}` : labelFor(r.provider))}
            >
              <StatusDot status={r.status} />
              <span className="text-[11.5px] font-semibold text-navy">{labelFor(r.provider)}</span>
              <span className="text-[10px] uppercase tracking-[0.1em] text-muted">{WORD[r.status]}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Tab bar across finished/running providers. Renders nothing for a single run —
 *  the one-provider case should look untouched. */
export function ProviderTabs<T>({
  runs,
  active,
  onSelect,
  labelFor,
}: {
  runs: ProviderRun<T>[];
  active: string;
  onSelect: (provider: string) => void;
  labelFor: (providerId: string) => string;
}) {
  if (runs.length <= 1) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b border-border-soft pb-1.5" role="tablist">
      {runs.map((r) => {
        const on = r.provider === active;
        return (
          <button
            key={r.provider}
            role="tab"
            aria-selected={on}
            onClick={() => onSelect(r.provider)}
            className={`inline-flex items-center gap-2 rounded-t-md px-3 py-1.5 text-[12px] transition-colors ${
              on ? "bg-navy/8 font-semibold text-navy" : "text-muted hover:text-navy"
            }`}
          >
            <StatusDot status={r.status} />
            {labelFor(r.provider)}
          </button>
        );
      })}
    </div>
  );
}

/** Pick which tab to show: the user's choice while it still exists, else the first
 *  provider that actually produced a result. */
export function useActiveTab<T>(runs: ProviderRun<T>[]): [string, (p: string) => void] {
  const [picked, setPicked] = useState<string>("");
  const active = useMemo(() => {
    if (picked && runs.some((r) => r.provider === picked)) return picked;
    return runs.find((r) => r.status === "done")?.provider || runs[0]?.provider || "";
  }, [picked, runs]);
  return [active, setPicked];
}
