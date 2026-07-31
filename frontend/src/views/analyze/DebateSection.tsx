"use client";

// The committee's debate — Advocate vs Challenger vs Auditor cards (from the chat history),
// specialist verdict cards, the merged what-moves-the-value tornado and the
// specialists' peer-comparison table.

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CommitteeResponse, SpecialistVerdict } from "@/lib/api";
import { num, signedPctPoint } from "@/lib/fmt";
import { isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { HelpTip } from "@/components/ui/HelpTip";
import { cn } from "@/lib/cn";

const CORE_VOICES = [
  { key: "advocate", name: "The Advocate", tone: "#1F7A52", tag: "builds the case", match: ["advocate"] },
  { key: "challenger", name: "The Challenger", tone: "#8C2F39", tag: "stress-tests it", match: ["challenger"] },
  { key: "auditor", name: "The Auditor", tone: "#2F4D73", tag: "checks the books", match: ["auditor"] },
];

function lastMessageFor(history: { role: string; content: string }[], match: string[]): string | null {
  for (let i = history.length - 1; i >= 0; i--) {
    const role = (history[i].role || "").toLowerCase();
    if (match.some((m) => role.includes(m))) return history[i].content;
  }
  return null;
}

// Plain-English names + display units for the model-driver keys the LLM emits
// in stress tests and peer comparisons. `pct` drivers carry percent-point
// values (9.0 → 10.0 means 9% → 10%), `x` drivers are multiples. Unknown keys
// fall back to a de-snake-cased guess with raw numbers.
type DriverUnit = "pct" | "x" | "raw";
const DRIVER_META: Record<string, { label: string; unit: DriverUnit }> = {
  wacc_pct: { label: "Cost of capital (WACC)", unit: "pct" },
  wacc: { label: "Cost of capital (WACC)", unit: "pct" },
  operating_margin_pct: { label: "Operating margin", unit: "pct" },
  operating_margin: { label: "Operating margin", unit: "pct" },
  ebit_margin_pct: { label: "Operating margin", unit: "pct" },
  ebitda_margin_pct: { label: "EBITDA margin", unit: "pct" },
  revenue_growth_pct: { label: "Revenue growth", unit: "pct" },
  rev_growth_pct: { label: "Revenue growth", unit: "pct" },
  revenue_growth: { label: "Revenue growth", unit: "pct" },
  terminal_growth_rate: { label: "Terminal growth", unit: "pct" },
  terminal_growth_pct: { label: "Terminal growth", unit: "pct" },
  capex_pct_of_rev: { label: "Capex as % of revenue", unit: "pct" },
  capex_pct_revenue: { label: "Capex as % of revenue", unit: "pct" },
  ev_ebitda_multiple: { label: "EV/EBITDA multiple", unit: "x" },
  ev_ebitda: { label: "EV/EBITDA", unit: "x" },
  pe: { label: "P/E", unit: "x" },
  pe_ratio: { label: "P/E", unit: "x" },
  pb: { label: "P/B", unit: "x" },
  fcf_yield: { label: "FCF yield", unit: "pct" },
  fcf_conversion: { label: "FCF conversion", unit: "raw" },
  tax_rate_pct: { label: "Tax rate", unit: "pct" },
  tax_rate: { label: "Tax rate", unit: "pct" },
};

function driverMeta(raw: string): { label: string; unit: DriverUnit } {
  const key = raw.trim().toLowerCase();
  if (DRIVER_META[key]) return DRIVER_META[key];
  const words = key.replace(/_pct$/, "").replace(/_/g, " ").trim();
  return {
    label: words ? words.charAt(0).toUpperCase() + words.slice(1) : raw,
    unit: /_pct$|_rate$|margin|growth|yield/.test(key) ? "pct" : "raw",
  };
}

function prettyDriver(raw: string): string {
  return driverMeta(raw).label;
}

function fmtDriverValue(v: number | null | undefined, unit: DriverUnit): string {
  if (!isNum(v)) return "—";
  if (unit === "pct") return `${num(v, 1)}%`;
  if (unit === "x") return `${num(v, 1)}×`;
  return num(v, 1);
}

export function DebateSection({ result }: { result: CommitteeResponse }) {
  const history = result.committee_chat_history || [];
  const verdicts = (result.specialist_verdicts || []).filter((v) => v && (v.thesis || v.analyst));

  const coreCards = CORE_VOICES.map((v) => ({ ...v, text: lastMessageFor(history, v.match) })).filter(
    (v) => v.text
  );

  // Merge every specialist's stress tests into ONE row per driver: several
  // specialists usually stress the same assumption (three WACC rows say less
  // than one). The harshest proposal leads the row; the rest are listed under
  // it so every transmission mechanism stays visible.
  type StressProposal = {
    analyst: string;
    base: number | null;
    stressed: number | null;
    impact: number;
    rationale: string;
  };
  const byDriver = new Map<string, { label: string; unit: DriverUnit; proposals: StressProposal[] }>();
  for (const v of verdicts) {
    for (const a of v.sensitivity_adjustments || []) {
      if (!isNum(a.fair_value_impact_pct) || !a.driver) continue;
      const meta = driverMeta(a.driver);
      const proposal: StressProposal = {
        analyst: v.analyst || v.analyst_key || "Specialist",
        base: isNum(a.base_value) ? a.base_value : null,
        stressed: isNum(a.stressed_value) ? a.stressed_value : null,
        impact: a.fair_value_impact_pct,
        rationale: (a.rationale || "").trim(),
      };
      const cur = byDriver.get(meta.label);
      if (!cur) byDriver.set(meta.label, { label: meta.label, unit: meta.unit, proposals: [proposal] });
      else cur.proposals.push(proposal);
    }
  }
  const stressRows = Array.from(byDriver.values())
    .map((d) => {
      const sorted = [...d.proposals].sort((a, b) => Math.abs(b.impact) - Math.abs(a.impact));
      return { ...d, primary: sorted[0], others: sorted.slice(1) };
    })
    .sort((a, b) => Math.abs(b.primary.impact) - Math.abs(a.primary.impact))
    .slice(0, 8);
  const maxImpact = Math.max(...stressRows.map((r) => Math.abs(r.primary.impact)), 0.01);

  const peerRows = verdicts
    .flatMap((v) =>
      (v.peer_comparison_metrics || [])
        .filter((p) => p.metric)
        .map((p) => ({ ...p, analyst: v.analyst || v.analyst_key || "" }))
    )
    .slice(0, 10);

  if (coreCards.length === 0 && verdicts.length === 0) return null;

  return (
    <SectionCard eyebrow="Step 2" title="The committee's debate" copyKey="debate">
      {coreCards.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          {coreCards.map((c) => (
            <DebateCard key={c.key} name={c.name} tag={c.tag} tone={c.tone} text={c.text as string} />
          ))}
        </div>
      ) : null}

      {verdicts.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2 text-[12px] font-semibold text-navy">The specialists weigh in</div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {verdicts.map((v, i) => (
              <SpecialistCard key={v.analyst_key || i} v={v} />
            ))}
          </div>
        </div>
      ) : null}

      {stressRows.length > 0 ? (
        <div className="mt-6">
          <div className="text-[12px] font-semibold text-navy">What moves the value most</div>
          <p className="mb-2 mt-0.5 text-[11px] text-muted">
            One row per assumption. Each shows the exact shock the specialists applied, the mechanism through
            which it changes the fair value, and the resulting impact. Where several specialists stressed the
            same driver, the harshest shock leads the row and the others are listed beneath it.
          </p>
          <div className="overflow-x-auto rounded-md border border-border-soft">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="bg-paper/70 text-left text-[10px] uppercase tracking-[0.08em] text-muted">
                  <th className="px-3 py-2">Assumption</th>
                  <th className="whitespace-nowrap px-3 py-2">The shock</th>
                  <th className="px-3 py-2">How it hits the value</th>
                  <th className="px-3 py-2 text-right">Fair-value impact</th>
                </tr>
              </thead>
              <tbody>
                {stressRows.map((r) => {
                  const p = r.primary;
                  const neg = p.impact < 0;
                  return (
                    <tr key={r.label} className="border-t border-border-soft align-top hover:bg-paper/40">
                      <td className="px-3 py-2.5 font-medium text-navy">
                        {r.label}
                        {r.others.length > 0 ? (
                          <div className="mt-0.5 text-[10px] font-normal text-muted">
                            stressed by {r.others.length + 1} specialists
                          </div>
                        ) : null}
                      </td>
                      <td className="num whitespace-nowrap px-3 py-2.5 text-navy">
                        {fmtDriverValue(p.base, r.unit)} <span className="text-muted">→</span>{" "}
                        <b>{fmtDriverValue(p.stressed, r.unit)}</b>
                      </td>
                      <td className="max-w-md px-3 py-2.5 text-[11.5px] leading-relaxed text-navy">
                        {p.rationale || "No mechanism stated — treat this stress with caution."}
                        <span className="ml-1 text-muted">— {p.analyst}</span>
                        {r.others.map((o, i) => (
                          <div key={i} className="mt-1.5 border-l-2 border-border-soft pl-2 text-[10.5px] leading-relaxed text-muted">
                            <span className="num">
                              {fmtDriverValue(o.base, r.unit)} → {fmtDriverValue(o.stressed, r.unit)} ⇒{" "}
                              {signedPctPoint(o.impact)}
                            </span>
                            {o.rationale ? <> · {o.rationale}</> : null} — {o.analyst}
                          </div>
                        ))}
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        <span
                          className="num text-[12.5px] font-bold"
                          style={{ color: neg ? "#8C2F39" : "#1F7A52" }}
                        >
                          {signedPctPoint(p.impact)}
                        </span>
                        <div className="mt-1 flex h-1.5 w-28 justify-end overflow-hidden rounded-full bg-border-soft">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${Math.max(4, (Math.abs(p.impact) / maxImpact) * 100)}%`,
                              background: neg ? "#8C2F39" : "#1F7A52",
                              opacity: 0.85,
                            }}
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {peerRows.length > 0 ? (
        <div className="mt-6">
          <div className="text-[12px] font-semibold text-navy">Where it stands vs. the neighbours</div>
          <p className="mb-2 mt-0.5 text-[11px] text-muted">Relative-value spreads the specialists flagged.</p>
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.08em] text-muted">
                  <th className="px-2.5 py-2 text-left">Metric</th>
                  <th className="px-2.5 py-2 text-right">This company</th>
                  <th className="px-2.5 py-2 text-right">Peer median</th>
                  <th className="px-2.5 py-2 text-right">
                    <HelpTip term="P/E">Premium / discount</HelpTip>
                  </th>
                  <th className="px-2.5 py-2 text-left">Reading</th>
                </tr>
              </thead>
              <tbody>
                {peerRows.map((p, i) => (
                  <tr key={i} className="border-t border-border-soft align-top">
                    <td className="px-2.5 py-2 font-medium text-navy">{prettyDriver(p.metric || "")}</td>
                    <td className="num px-2.5 py-2 text-right">{num(p.target_value, 1)}</td>
                    <td className="num px-2.5 py-2 text-right">{num(p.peer_median, 1)}</td>
                    <td
                      className={`num px-2.5 py-2 text-right font-semibold ${
                        isNum(p.premium_discount_pct) ? (p.premium_discount_pct >= 0 ? "text-red" : "text-green") : ""
                      }`}
                    >
                      {signedPctPoint(p.premium_discount_pct)}
                    </td>
                    <td className="max-w-sm px-2.5 py-2 text-[11px] leading-relaxed text-navy">
                      {p.interpretation}
                      <span className="ml-1 text-muted">— {p.analyst}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </SectionCard>
  );
}

function DebateCard({ name, tag, tone, text }: { name: string; tag: string; tone: string; text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col rounded-md border border-border-soft bg-white/70">
      <div className="flex items-center justify-between gap-2 border-b-2 px-3.5 py-2.5" style={{ borderColor: tone }}>
        <span className="text-[12.5px] font-semibold" style={{ color: tone }}>
          {name}
        </span>
        <span className="text-[9px] font-semibold uppercase tracking-[0.1em] text-muted">{tag}</span>
      </div>
      <div className={cn("memo-prose px-3.5 py-3 !text-[12px]", !open && "max-h-56 overflow-hidden")}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
      </div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="border-t border-border-soft px-3.5 py-2 text-left text-[10.5px] font-semibold text-navy-2 hover:text-navy"
      >
        {open ? "Show less ↑" : "Read the full argument ↓"}
      </button>
    </div>
  );
}

function SpecialistCard({ v }: { v: SpecialistVerdict }) {
  const conf = isNum(v.confidence) ? Math.round(v.confidence * 5) : null;
  return (
    <div className="rounded-md border border-border-soft bg-white/60 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] font-semibold text-navy">{v.analyst || v.analyst_key}</span>
        {conf !== null ? (
          <span className="flex items-center gap-0.5" title={`Confidence ${(v.confidence! * 100).toFixed(0)}%`}>
            {Array.from({ length: 5 }).map((_, i) => (
              <span
                key={i}
                className={`inline-block h-1.5 w-1.5 rounded-full ${i < conf ? "bg-navy" : "bg-border"}`}
              />
            ))}
          </span>
        ) : null}
      </div>
      {v.thesis ? <p className="mt-1.5 text-[11.5px] leading-relaxed text-navy">{v.thesis}</p> : null}
      {(v.risk_flags || []).length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {(v.risk_flags || []).slice(0, 4).map((f, i) => (
            <span key={i} className="badge-neg rounded px-1.5 py-0.5 text-[9.5px] font-medium">
              {f}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
