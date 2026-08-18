"use client";

// The four personas' output for one round, colour-coded like the committee tribunal:
// navy for the objective functions (validation, researcher), amber for the outside view.
//
// The Model Validation block is deliberately first and carries the loudest treatment: its
// verdict is the only one with a veto, so a `fail` needs to be the thing you see.

import { type ResearchIteration } from "@/lib/api";

const STATUS_STYLE: Record<string, string> = {
  pass: "border-green-700/60 text-green-700",
  warn: "border-amber-500/70 text-amber-700",
  fail: "border-red-700/70 text-red-700",
};
const DECISION_STYLE: Record<string, string> = {
  accept: "border-green-700/60 text-green-700",
  reject: "border-red-700/70 text-red-700",
  continue: "border-border text-navy/70",
};
const SEVERITY_STYLE: Record<string, string> = {
  critical: "text-red-700",
  warn: "text-amber-700",
  info: "text-muted",
};

function Chip({ text, cls }: { text: string; cls: string }) {
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.06em] ${cls}`}>
      {text}
    </span>
  );
}

function Voice({ who, accent, chip, children }: {
  who: string; accent: string; chip?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className={`border-l-2 pl-3 ${accent}`}>
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[11px] font-semibold text-navy">{who}</span>
        {chip}
      </div>
      <div className="space-y-1.5 text-[11.5px] leading-relaxed text-navy/80">{children}</div>
    </div>
  );
}

export function AgentTranscript({ it }: { it: ResearchIteration }) {
  const val = it.validation_json ?? {};
  const pm = it.pm_json ?? {};
  const adv = it.advisor_json ?? {};
  const res = it.researcher_json ?? {};
  const status = val.status ?? "warn";
  const decision = pm.decision ?? "continue";
  const offline = res.source === "deterministic";

  return (
    <div className="space-y-3">
      {offline ? (
        <div className="rounded border border-border-soft bg-paper/60 px-2.5 py-1.5 text-[10.5px] text-muted">
          No LLM provider was configured for this run, so the committee ran its deterministic
          path: a pre-registered ladder of spec changes and rule-based versions of the same
          checklists. The findings below are real; the prose is not generated.
        </div>
      ) : null}

      <Voice who="Model Validation" accent="border-navy"
        chip={<>
          <Chip text={status} cls={STATUS_STYLE[status] ?? STATUS_STYLE.warn} />
          {val.blocking ? <Chip text="blocking" cls="border-red-700/70 text-red-700" /> : null}
        </>}>
        {val.summary ? <p>{val.summary}</p> : null}
        {(val.findings ?? []).length ? (
          <ul className="space-y-1">
            {(val.findings ?? []).map((f, i) => (
              <li key={i} className="flex gap-1.5">
                <span className={`font-semibold ${SEVERITY_STYLE[f.severity] ?? "text-muted"}`}>
                  {f.category}
                </span>
                <span className="text-navy/75">— {f.detail}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">No findings raised.</p>
        )}
      </Voice>

      <Voice who="Portfolio Manager" accent="border-navy/50"
        chip={<Chip text={decision} cls={DECISION_STYLE[decision] ?? DECISION_STYLE.continue} />}>
        {pm.reasoning ? <p>{pm.reasoning}</p> : null}
        {(pm.concerns ?? []).length ? (
          <ul className="list-disc space-y-0.5 pl-4">
            {(pm.concerns ?? []).map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        ) : null}
      </Voice>

      <Voice who="External Advisor" accent="border-amber-500/70"
        chip={adv.provider ? <Chip text={adv.provider} cls="border-border text-muted" /> : undefined}>
        {adv.contrarian_read ? (
          <p><span className="font-semibold text-navy">Contrarian read. </span>{adv.contrarian_read}</p>
        ) : null}
        {adv.orthogonal_direction ? (
          <p><span className="font-semibold text-navy">Orthogonal direction. </span>{adv.orthogonal_direction}</p>
        ) : null}
      </Voice>

      <Voice who="Quantitative Researcher — proposal for the next round" accent="border-navy/30">
        {res.rationale ? <p>{res.rationale}</p> : null}
        {res.hypothesis ? <p className="italic text-navy/70">{res.hypothesis}</p> : null}
        {(res.applied_changes ?? []).length ? (
          <p className="font-mono text-[10.5px] text-navy/70">
            {(res.applied_changes ?? []).join(" · ")}
          </p>
        ) : null}
        {(res.rejected ?? []).length ? (
          <p className="text-[10.5px] text-amber-700">
            Rejected by the spec validator: {(res.rejected ?? []).join("; ")}
          </p>
        ) : null}
      </Voice>
    </div>
  );
}
