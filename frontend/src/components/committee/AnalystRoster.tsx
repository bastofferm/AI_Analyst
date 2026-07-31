"use client";

import { useState } from "react";
import type { CommitteeExtraAnalyst } from "@/lib/api";

const BUILTINS = [
  { name: "The Advocate", mandate: "Growth optimist - proves upside with segment economics and incremental-ROIC math." },
  { name: "The Challenger", mandate: "Constructive skeptic - quantifies downside from FCF compression, de-rating and reverse-DCF implied growth." },
  { name: "The Auditor", mandate: "Narrative-blind - adjudicates capital allocation, earnings quality and WACC inputs." },
];

const SPECIALISTS = [
  { name: "Growth Extrapolator", mandate: "Forecasting lens for trend durability, market-share capture and operating leverage." },
  { name: "Quality-of-Earnings Auditor", mandate: "Fundamental lens for accruals, cash conversion, working capital and capitalization policy." },
  { name: "Relative-Value Arbitrageur", mandate: "Market-context lens for peer multiples and valuation spreads." },
  { name: "Macro-Regime Strategist", mandate: "Rates, inflation, FX and regime lens for WACC, terminal value and scenario weights." },
  { name: "Sensitivity Stress-Tester", mandate: "What-if lens for finding the assumptions that break the thesis." },
];

/** Shared analyst roster. Core analysts and automatic specialists run by default;
 *  deployed custom analysts are persisted (localStorage, owned by the parent) and
 *  passed as config.extra_analysts on every committee run. */
export function AnalystRoster({
  analysts,
  onChange,
}: {
  analysts: CommitteeExtraAnalyst[];
  onChange: (next: CommitteeExtraAnalyst[]) => void;
}) {
  const [name, setName] = useState("");
  const [mandate, setMandate] = useState("");
  const [err, setErr] = useState("");

  function deploy() {
    const n = name.trim();
    const m = mandate.trim();
    if (!n || !m) {
      setErr("Give the analyst a name and a mandate before deploying.");
      return;
    }
    if (analysts.some((a) => a.name.toLowerCase() === n.toLowerCase())) {
      setErr("An analyst with that name is already deployed.");
      return;
    }
    onChange([...analysts, { name: n, mandate: m }]);
    setName("");
    setMandate("");
    setErr("");
  }

  function remove(i: number) {
    onChange(analysts.filter((_, j) => j !== i));
  }

  return (
    <section className="card p-4">
      <div className="label">Analyst roster</div>
      <p className="mt-1 text-[11px] text-muted">
        The core tribunal and sector-aware specialists run automatically. Deploy your own to widen every
        debate (single stock, industry or screen). Deployed analysts persist on this device.
      </p>

      <div className="mt-3 flex flex-col gap-1.5">
        {BUILTINS.map((a) => (
          <div key={a.name} className="rounded border border-border-soft bg-white/60 px-3 py-1.5">
            <div className="text-[12px] font-semibold text-navy">
              {a.name} <span className="text-[10px] font-normal text-muted">- core</span>
            </div>
            <div className="text-[11px] text-muted">{a.mandate}</div>
          </div>
        ))}
        {SPECIALISTS.map((a) => (
          <div key={a.name} className="rounded border border-border-soft bg-white/50 px-3 py-1.5">
            <div className="text-[12px] font-semibold text-navy">
              {a.name} <span className="text-[10px] font-normal text-muted">- auto specialist</span>
            </div>
            <div className="text-[11px] text-muted">{a.mandate}</div>
          </div>
        ))}
        {analysts.map((a, i) => (
          <div key={i} className="flex items-start justify-between gap-2 rounded border border-navy/30 bg-white px-3 py-1.5">
            <div>
              <div className="text-[12px] font-semibold text-navy">
                {a.name} <span className="text-[10px] font-normal text-green">- deployed</span>
              </div>
              <div className="text-[11px] text-muted">{a.mandate}</div>
            </div>
            <button
              onClick={() => remove(i)}
              className="rounded px-1.5 text-[14px] leading-none text-red hover:bg-red/10"
              title="Remove analyst"
            >
              x
            </button>
          </div>
        ))}
      </div>

      <div className="mt-3 rounded border border-dashed border-border p-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New analyst name (e.g. ESG Skeptic)"
          className="w-full rounded border border-border bg-white px-2 py-1 text-[12px] font-semibold text-navy outline-none focus:border-navy"
        />
        <textarea
          value={mandate}
          onChange={(e) => setMandate(e.target.value)}
          rows={2}
          placeholder="Mandate / lens - what perspective should this analyst bring to the debate?"
          className="mt-2 w-full rounded border border-border bg-white px-2 py-1 text-[12px] text-navy outline-none focus:border-navy"
        />
        {err && <div className="mt-1 text-[11px] text-red">{err}</div>}
        <button
          onClick={deploy}
          className="mt-2 w-full rounded bg-navy px-3 py-1.5 text-[12px] font-semibold text-white transition-opacity hover:opacity-90"
        >
          Apply / Deploy analyst
        </button>
      </div>
    </section>
  );
}
