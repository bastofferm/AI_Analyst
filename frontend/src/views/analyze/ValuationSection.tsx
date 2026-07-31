"use client";

// The valuation deep-dive: football field, DCF bridge + projected cash flows,
// the growth×WACC stress grid, sum-of-the-parts and the reverse-DCF expectation
// gauge. Every block renders only when its data block arrived.

import { useMemo } from "react";
import type { CommitteeResponse, Scenario } from "@/lib/api";
import { useMoney, type MoneyKit } from "@/lib/currency";
import { pctPoint } from "@/lib/fmt";
import {
  FcfBars,
  FootballField,
  SegmentBars,
  SensitivityHeatmap,
  WaterfallChart,
} from "@/components/charts/valuation";
import { GaugeBar } from "@/components/charts/primitives";
import { CHART_RED, NAVY, isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { HelpTip } from "@/components/ui/HelpTip";

function baseScenario(result: CommitteeResponse): Scenario | null {
  const rows = result.scenarios?.scenarios || [];
  return rows.find((s) => (s.label || "").toLowerCase() === "base") || rows[0] || null;
}

/** Convert every absolute-money field the valuation charts consume into the
 *  display currency, in one place. Percentages, weights and share counts pass
 *  through untouched; a linear FX factor keeps all per-share arithmetic
 *  (value ÷ shares) consistent. */
function useConvertedValuation(result: CommitteeResponse, cv: MoneyKit) {
  const jur = result.jurisdiction;
  return useMemo(() => {
    const c = (v: number | null | undefined): number | null => (isNum(v) ? cv.convert(v, jur) : null);
    const tri = result.triangulation;
    const base = baseScenario(result);
    const dcf = base?.dcf_full || null;
    const analytics = result.analytics || null;
    const sotp = result.sotp;

    const price = c(tri?.current_price ?? result.scenarios?.current_price);
    const methods = (tri?.methods || [])
      .filter((m) => isNum(m.low) && isNum(m.high))
      .map((m) => ({ ...m, low: c(m.low), high: c(m.high), mid: c(m.mid) }));
    const waterfall = (dcf?.waterfall || [])
      .filter((w) => isNum(w.value))
      .map((w) => ({ ...w, value: c(w.value) as number }));
    const fcfs = (dcf?.fcfs || []).map((v) => (isNum(v) ? (c(v) as number) : v));
    const discounted = (dcf?.discounted_fcfs || []).map((v) => (isNum(v) ? (c(v) as number) : v));
    const shares =
      analytics?.shares ??
      (isNum(dcf?.equity_value) && isNum(dcf?.per_share_value) && dcf!.per_share_value !== 0
        ? dcf!.equity_value! / dcf!.per_share_value!
        : null);
    const rawGrid = analytics?.sensitivity_grid || null;
    const grid = rawGrid?.per_share
      ? { ...rawGrid, per_share: rawGrid.per_share.map((row) => row.map((v) => c(v))) }
      : rawGrid;
    const segments = (sotp?.available ? sotp?.segments_base || [] : []).map((s) => ({
      ...s,
      enterprise_value: c(s.enterprise_value),
    }));
    return { price, methods, waterfall, fcfs, discounted, shares, grid, segments, dcf };
  }, [result, cv, jur]);
}

export function ValuationSection({ result }: { result: CommitteeResponse }) {
  const cv = useMoney();
  const jur = result.jurisdiction;
  const currency = cv.symbol(jur);
  const tri = result.triangulation;
  const sotp = result.sotp;
  const rev = result.reverse_dcf;

  const { price, methods, waterfall, fcfs, discounted, shares, grid, segments, dcf } =
    useConvertedValuation(result, cv);
  const showReverse = rev?.available && isNum(rev?.implied_growth_pct);

  const hasAnything =
    methods.length > 0 || waterfall.length > 1 || fcfs.length > 1 || grid?.per_share || segments.length > 0 || showReverse;
  if (!hasAnything) return null;

  return (
    <div className="flex flex-col gap-4">
      {methods.length > 0 ? (
        <SectionCard eyebrow="Step 4 · Valuation" title="Where the estimates land" copyKey="footballField">
          <FootballField methods={methods} currentPrice={price} />
          {tri?.primary_method ? (
            <div className="mt-1 text-[11px] text-muted">
              ★ The committee anchors its headline number on <b className="text-navy">{tri.primary_method}</b>.
            </div>
          ) : null}
        </SectionCard>
      ) : null}

      {waterfall.length > 1 || fcfs.length > 1 ? (
        <SectionCard eyebrow="Valuation" title="From business value to your share" copyKey="waterfall">
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            {waterfall.length > 1 ? (
              <div>
                <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                  The bridge (base case)
                </div>
                <WaterfallChart items={waterfall} shares={shares} currency={currency} />
              </div>
            ) : null}
            {fcfs.length > 1 ? (
              <div>
                <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                  The <HelpTip term="free cash flow">cash</HelpTip> behind it · 7-year forecast
                </div>
                <FcfBars fcfs={fcfs} discounted={discounted} currency={currency} />
                {isNum(dcf?.terminal_pv) && isNum(dcf?.enterprise_value) && dcf!.enterprise_value! > 0 ? (
                  <div className="mt-1 text-[10.5px] text-muted">
                    Everything beyond year 7 (the <HelpTip term="terminal growth">terminal value</HelpTip>) contributes{" "}
                    <b className="num text-navy">
                      {((dcf!.terminal_pv! / dcf!.enterprise_value!) * 100).toFixed(0)}%
                    </b>{" "}
                    of the business value.
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </SectionCard>
      ) : null}

      {grid?.per_share ? (
        <SectionCard eyebrow="Valuation" title="Stress-testing the assumptions" copyKey="sensitivity">
          <SensitivityHeatmap grid={grid} currentPrice={price} />
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-muted">
            <span>
              <span className="num font-bold text-white" style={{ background: "#1F7A52", padding: "0 5px", borderRadius: 3 }}>
                bold
              </span>{" "}
              cells = value at or above today&apos;s price
            </span>
            <span>
              <span className="inline-block h-2.5 w-2.5 rounded-sm border-2 border-amber align-middle" /> = the
              committee&apos;s base assumptions
            </span>
          </div>
        </SectionCard>
      ) : null}

      {segments.length > 0 ? (
        <SectionCard eyebrow="Valuation" title="Valuing the parts separately" copyKey="sotp">
          <SegmentBars segments={segments} currency={currency} />
          {sotp?.per_share && (isNum(sotp.per_share.upside) || isNum(sotp.per_share.base) || isNum(sotp.per_share.downside)) ? (
            <div className="mt-4 flex flex-wrap gap-2.5">
              {(["upside", "base", "downside"] as const).map((k) =>
                isNum(sotp.per_share?.[k]) ? (
                  <div key={k} className="rounded-md border border-border-soft bg-white/60 px-3 py-2">
                    <div className="text-[9px] uppercase tracking-[0.1em] text-muted">{k} case</div>
                    <div
                      className="num text-[15px] font-bold"
                      style={{ color: k === "upside" ? "#1F7A52" : k === "downside" ? "#8C2F39" : "#2F4D73" }}
                    >
                      {cv.perShare(sotp.per_share![k], jur)}
                    </div>
                  </div>
                ) : null
              )}
              {isNum(sotp.weighted_per_share) ? (
                <div className="rounded-md border border-navy/30 bg-navy/5 px-3 py-2">
                  <div className="text-[9px] uppercase tracking-[0.1em] text-muted">weighted</div>
                  <div className="num text-[15px] font-bold text-navy">{cv.perShare(sotp.weighted_per_share, jur)}</div>
                </div>
              ) : null}
            </div>
          ) : null}
        </SectionCard>
      ) : null}

      {showReverse ? (
        <SectionCard eyebrow="Valuation" title="What today's price already assumes" copyKey="reverseDcf">
          <ReverseDcfGauge implied={rev!.implied_growth_pct!} base={rev?.base_growth_pct} />
        </SectionCard>
      ) : null}
    </div>
  );
}

function ReverseDcfGauge({ implied, base }: { implied: number; base?: number | null }) {
  const vals = [implied, isNum(base) ? base : null].filter(isNum) as number[];
  const lo = Math.min(...vals, 0);
  const hi = Math.max(...vals, 1);
  const pad = (hi - lo || 1) * 0.35;
  const gap = isNum(base) ? implied - base : null;
  return (
    <div>
      <GaugeBar
        min={lo - pad}
        max={hi + pad}
        markers={[
          { value: implied, label: "Market's implied growth", color: CHART_RED, dashed: true },
          ...(isNum(base) ? [{ value: base, label: "Committee's base case", color: NAVY }] : []),
        ]}
        format={(v) => pctPoint(v)}
        height={100}
      />
      {isNum(gap) ? (
        <p className="mt-1 text-[12px] leading-relaxed text-navy">
          Buyers at today&apos;s price are implicitly betting on{" "}
          <b className="num">{pctPoint(implied)}</b> yearly <HelpTip term="implied growth">growth</HelpTip>. The
          committee&apos;s base case assumes <b className="num">{pctPoint(base)}</b> —{" "}
          {Math.abs(gap) < 1 ? (
            <>the market&apos;s expectations look about right.</>
          ) : gap > 0 ? (
            <>
              the market is <b className="text-red">more optimistic</b> than the committee by{" "}
              <b className="num">{pctPoint(Math.abs(gap))}</b> a year.
            </>
          ) : (
            <>
              the market is <b className="text-green">more pessimistic</b> than the committee by{" "}
              <b className="num">{pctPoint(Math.abs(gap))}</b> a year.
            </>
          )}
        </p>
      ) : null}
    </div>
  );
}
