"use client";

// One row per research round: what changed, what it scored, and how the committee voted.
//
// Deltas are shown against the PREVIOUS round rather than against absolutes, because the
// question a reader has at this table is "did that change help?" — and a rank-IC of 0.031
// means nothing until you know the round before it scored 0.024.
//
// Clicking a row expands the full validation report inline, following the single-open
// accordion + soft-panel shell the holdings table already uses for its data-behind drawer.

import { Fragment, useState } from "react";
import { type ResearchIteration, type ResearchRun } from "@/lib/api";
import { ValidationReportBody } from "./ValidationReportBody";
import { AgentTranscript } from "./AgentTranscript";

const RATING_TONE: Record<number, string> = {
  1: "text-green-700", 2: "text-amber-600", 3: "text-red-700",
};
const STATUS_TONE: Record<string, string> = {
  pass: "text-green-700", warn: "text-amber-700", fail: "text-red-700",
};

const n = (v: unknown, d = 4) => (typeof v === "number" && isFinite(v) ? v.toFixed(d) : "—");
const p = (v: unknown, d = 0) =>
  typeof v === "number" && isFinite(v) ? `${(v * 100).toFixed(d)}%` : "—";

function Delta({ now, prev }: { now?: number | null; prev?: number | null }) {
  if (typeof now !== "number" || typeof prev !== "number" || !isFinite(now) || !isFinite(prev)) {
    return null;
  }
  const d = now - prev;
  if (Math.abs(d) < 1e-9) return <span className="ml-1 text-[10px] text-muted">=</span>;
  return (
    <span className={`ml-1 text-[10px] ${d > 0 ? "text-green-700" : "text-red-700"}`}>
      {d > 0 ? "▲" : "▼"}{Math.abs(d).toFixed(4)}
    </span>
  );
}

export function IterationTimeline({ run }: { run: ResearchRun }) {
  const [open, setOpen] = useState<number | null>(null);
  const rounds = run.iterations ?? [];
  if (!rounds.length) {
    return (
      <div className="text-[12px] text-muted">
        {run.status === "running"
          ? "The first round is still training — the panel will fill in as rounds complete."
          : "No rounds were recorded for this run."}
      </div>
    );
  }

  const champion = run.champion_iteration;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="border-b border-border text-[10px] uppercase tracking-[0.07em] text-muted">
            <th className="py-2 pl-1 pr-2 text-left">#</th>
            <th className="px-2 py-2 text-left">What changed</th>
            <th className="px-2 py-2 text-right">Rank-IC</th>
            <th className="px-2 py-2 text-right">R² OOS</th>
            <th className="px-2 py-2 text-right">Rating</th>
            <th className="px-2 py-2 text-right">Turnover</th>
            <th className="px-2 py-2 text-center">Validation</th>
            <th className="px-2 py-2 text-center">PM</th>
            <th className="px-2 py-2 text-right"></th>
          </tr>
        </thead>
        <tbody>
          {rounds.map((it: ResearchIteration, i: number) => {
            const head = it.report_json?.headline ?? {};
            const prev = i > 0 ? rounds[i - 1].report_json?.headline ?? {} : {};
            const changes = (it.patch_json?.changes ?? []).join(", ") || "baseline spec";
            const status = it.validation_json?.status ?? "—";
            const decision = it.pm_json?.decision ?? "—";
            const isOpen = open === it.iteration;
            const isChampion = champion === it.iteration;
            return (
              <Fragment key={it.iteration}>
                <tr
                  className={`border-b border-border-soft ${isOpen ? "bg-paper/60" : "hover:bg-paper/40"}`}>
                  <td className="num py-1.5 pl-1 pr-2 text-muted">
                    {it.iteration}
                    {isChampion ? (
                      <span className="ml-1 rounded border border-navy px-1 text-[9px] uppercase tracking-[0.05em] text-navy">
                        champ
                      </span>
                    ) : null}
                  </td>
                  <td className="max-w-[280px] truncate px-2 py-1.5 text-navy/80" title={changes}>
                    {changes}
                  </td>
                  <td className="num px-2 py-1.5 text-right font-medium text-navy">
                    {n(head.rank_ic)}<Delta now={head.rank_ic} prev={prev.rank_ic} />
                  </td>
                  <td className="num px-2 py-1.5 text-right text-navy/70">{n(head.r2_oos, 5)}</td>
                  <td className={`num px-2 py-1.5 text-right font-medium ${RATING_TONE[head.robustness_rating ?? 0] ?? "text-muted"}`}>
                    {head.robustness_rating ?? "—"}
                  </td>
                  <td className="num px-2 py-1.5 text-right text-navy/70">{p(head.turnover)}</td>
                  <td className={`px-2 py-1.5 text-center text-[11px] font-medium ${STATUS_TONE[status] ?? "text-muted"}`}>
                    {status}
                  </td>
                  <td className="px-2 py-1.5 text-center text-[11px] text-navy/70">{decision}</td>
                  <td className="px-2 py-1.5 text-right">
                    <button
                      onClick={() => setOpen(isOpen ? null : it.iteration)}
                      aria-expanded={isOpen}
                      title="Show the full model validation report for this round"
                      className={`rounded border px-2 py-0.5 text-[11px] font-medium transition-colors ${
                        isOpen ? "border-navy bg-navy text-white" : "border-border text-navy hover:border-navy"
                      }`}
                    >
                      {isOpen ? "Hide" : "Report"}
                    </button>
                  </td>
                </tr>
                {isOpen ? (
                  <tr>
                    <td colSpan={9} className="px-1 pb-3 pt-1">
                      <div className="space-y-4 rounded-lg border border-border-soft bg-paper/40 p-4">
                        <div className="text-[12px] font-semibold text-navy">
                          Round {it.iteration} — model validation report
                        </div>
                        <ValidationReportBody it={it} />
                        <div className="border-t border-border-soft pt-3">
                          <div className="label mb-2">The committee</div>
                          <AgentTranscript it={it} />
                        </div>
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
