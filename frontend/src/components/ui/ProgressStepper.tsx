"use client";

// Estimated pipeline stepper for the multi-minute blocking committee runs.
// LED dots advance on elapsed time (there is no streaming from the backend), and
// the last step "parks" until the response actually lands — honestly labelled.

import { useEffect, useState } from "react";
import { activeStepIndex, formatElapsed, type PipelineStep } from "@/lib/pipeline";
import { FunFactCard } from "./FunFact";

export function ProgressStepper({
  steps,
  startedAt,
  totalMs,
  title,
  showFacts = true,
}: {
  steps: PipelineStep[];
  startedAt: number;
  totalMs: number;
  title?: string;
  /** Rotating market-wisdom card below the steps; on by default for long runs. */
  showFacts?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const elapsed = Math.max(0, now - startedAt);
  const active = activeStepIndex(steps, elapsed, totalMs);
  const overdue = elapsed > totalMs * 1.4;

  return (
    <div className="card border-navy/25 p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="relative flex h-2.5 w-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-navy opacity-60" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-navy" />
          </span>
          <span className="text-[13px] font-semibold text-navy">{title || "The committee is working"}</span>
        </div>
        <span className="num text-[13px] font-bold tabular-nums text-navy">{formatElapsed(elapsed)}</span>
      </div>

      <ol className="mt-4 flex flex-col gap-0">
        {steps.map((s, i) => {
          const state = i < active ? "done" : i === active ? "active" : "pending";
          return (
            <li key={s.key} className="flex gap-3">
              <div className="flex flex-col items-center">
                {state === "done" ? (
                  <span className="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-green text-[9px] font-bold text-white">
                    ✓
                  </span>
                ) : state === "active" ? (
                  <span className="mt-0.5 h-4 w-4 animate-pulse rounded-full border-[3px] border-navy bg-white" />
                ) : (
                  <span className="mt-0.5 h-4 w-4 rounded-full border-2 border-border bg-white" />
                )}
                {i < steps.length - 1 ? (
                  <span className={`w-px flex-1 ${state === "done" ? "bg-green/50" : "bg-border"}`} />
                ) : null}
              </div>
              <div className="pb-3.5">
                <div
                  className={`text-[12.5px] font-semibold ${
                    state === "pending" ? "text-muted/70" : "text-navy"
                  }`}
                >
                  {s.label}
                  {state === "active" ? <span className="ml-1.5 animate-pulse text-navy-3">…</span> : null}
                </div>
                {state !== "pending" ? <div className="text-[11px] text-muted">{s.detail}</div> : null}
              </div>
            </li>
          );
        })}
      </ol>

      {showFacts ? <FunFactCard seed={startedAt / 1000} /> : null}

      <p className="mt-1 text-[10.5px] italic text-muted">
        Progress shown is estimated — the committee reports back only when it has finished
        {overdue ? ". Thorough debates can run longer than usual; hang tight." : " (typically a few minutes)."}
        {" "}You can switch views freely; the run keeps going.
      </p>
    </div>
  );
}
