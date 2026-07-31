"use client";

// Three futures — upside / base / downside scenario cards with probability weights,
// the per-scenario value bars and the weighted outcome.

import { useMemo } from "react";
import type { CommitteeResponse, Scenario } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { pctPoint } from "@/lib/fmt";
import { ScenarioBars, WeightStrip } from "@/components/charts/valuation";
import { isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { HelpTip } from "@/components/ui/HelpTip";

function avgGrowth(g: Scenario["rev_growth_pct"]): number | null {
  if (isNum(g)) return g;
  if (Array.isArray(g)) {
    const vals = g.filter(isNum);
    if (vals.length === 0) return null;
    return vals.reduce((a, b) => a + b, 0) / vals.length;
  }
  return null;
}

const TONE: Record<string, { color: string; blurb: string }> = {
  upside: { color: "#1F7A52", blurb: "if things go well" },
  base: { color: "#2F4D73", blurb: "business as usual" },
  downside: { color: "#8C2F39", blurb: "if things go wrong" },
};

export function ScenariosSection({ result }: { result: CommitteeResponse }) {
  const cv = useMoney();
  const scen = result.scenarios;
  const jur = result.jurisdiction;
  const currency = cv.symbol(jur);
  const order = { upside: 0, base: 1, downside: 2 } as Record<string, number>;

  const convertOpt = (v: number | null | undefined) => (isNum(v) ? cv.convert(v, jur) : null);

  const rows = (scen?.scenarios || []).filter((s) => s && (isNum(s.per_share_value) || s.rationale));
  const sorted = [...rows].sort(
    (a, b) => (order[(a.label || "").toLowerCase()] ?? 9) - (order[(b.label || "").toLowerCase()] ?? 9)
  );
  // The bars get pre-converted values so lengths and labels agree with the
  // converted headline numbers; the cards format via cv.perShare directly.
  const chartScenarios = useMemo(
    () => sorted.map((s) => (isNum(s.per_share_value) ? { ...s, per_share_value: cv.convert(s.per_share_value, jur) } : s)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [scen, cv, jur]
  );
  if (sorted.length === 0) return null;

  return (
    <SectionCard eyebrow="Step 3" title="Three futures, one weighted answer" copyKey="scenarios">
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-5">
        <div className="lg:col-span-3">
          <ScenarioBars
            scenarios={chartScenarios}
            currentPrice={convertOpt(scen?.current_price ?? result.triangulation?.current_price)}
            weightedValue={convertOpt(scen?.probability_weighted_fair_value ?? result.probability_weighted_fair_value)}
            currency={currency}
          />
        </div>
        <div className="flex flex-col justify-center gap-3 lg:col-span-2">
          <div>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
              How likely is each future?
            </div>
            <WeightStrip scenarios={sorted} />
          </div>
          {isNum(scen?.probability_weighted_fair_value) ? (
            <div className="rounded-md border border-border-soft bg-white/60 px-3.5 py-3">
              <div className="text-[10px] uppercase tracking-[0.1em] text-muted">Probability-weighted value</div>
              <div className="num mt-1 text-[22px] font-bold leading-none text-navy">
                {cv.perShare(scen?.probability_weighted_fair_value, jur)}
              </div>
              <div className="mt-1 text-[10.5px] text-muted">the three futures blended by their weights</div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-3 lg:grid-cols-3">
        {sorted.map((s, i) => {
          const key = (s.label || "").toLowerCase();
          const tone = TONE[key] || TONE.base;
          const growth = avgGrowth(s.rev_growth_pct);
          return (
            <div
              key={i}
              className="rise-in hover-lift flex flex-col rounded-md border border-border-soft bg-white/70"
              style={{ animationDelay: `${i * 90}ms` }}
            >
              <div className="flex items-baseline justify-between border-b-2 px-3.5 py-2.5" style={{ borderColor: tone.color }}>
                <span className="text-[13px] font-semibold capitalize" style={{ color: tone.color }}>
                  {s.label} <span className="text-[10px] font-normal text-muted">· {tone.blurb}</span>
                </span>
                {isNum(s.weight) ? (
                  <span className="num text-[11px] font-bold text-navy">{(s.weight * 100).toFixed(0)}%</span>
                ) : null}
              </div>
              <div className="flex flex-col gap-2.5 px-3.5 py-3">
                {isNum(s.per_share_value) ? (
                  <div className="num text-[20px] font-bold leading-none" style={{ color: tone.color }}>
                    {cv.perShare(s.per_share_value, jur)}
                    <span className="ml-1 text-[10px] font-normal text-muted">per share</span>
                  </div>
                ) : null}
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10.5px] text-muted">
                  {isNum(growth) ? (
                    <span>
                      <HelpTip term="revenue CAGR">Growth</HelpTip> <b className="num text-navy">{pctPoint(growth)}</b>
                    </span>
                  ) : null}
                  {isNum(s.ebit_margin_pct) ? (
                    <span>
                      Margin <b className="num text-navy">{pctPoint(s.ebit_margin_pct)}</b>
                    </span>
                  ) : null}
                  {isNum(s.wacc_pct) ? (
                    <span>
                      <HelpTip term="WACC">WACC</HelpTip> <b className="num text-navy">{pctPoint(s.wacc_pct)}</b>
                    </span>
                  ) : null}
                  {isNum(s.terminal_growth_pct) ? (
                    <span>
                      <HelpTip term="terminal growth">Terminal</HelpTip>{" "}
                      <b className="num text-navy">{pctPoint(s.terminal_growth_pct)}</b>
                    </span>
                  ) : null}
                </div>
                {s.rationale ? <p className="text-[11px] leading-relaxed text-navy">{s.rationale}</p> : null}
              </div>
            </div>
          );
        })}
      </div>
    </SectionCard>
  );
}
