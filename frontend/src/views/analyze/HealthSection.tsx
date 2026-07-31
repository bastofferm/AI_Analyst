"use client";

// Company health: quarterly momentum, capital returns vs FCF, the incremental
// ROIC-vs-WACC read and the peer-multiples comparison. Powered by the
// `analytics` block (needs the backend passthrough; absent for thin names).

import { useMemo } from "react";
import type { CommitteeResponse } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { pctPoint } from "@/lib/fmt";
import { CapitalReturnsChart, LegendRow, QuarterlyTrendChart, RelativePriceChart } from "@/components/charts/market";
import { PeerMultiplesBar } from "@/components/charts/valuation";
import { CHART_GREEN, NAVY2, NAVY3, isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { HelpTip } from "@/components/ui/HelpTip";

export function HealthSection({ result }: { result: CommitteeResponse }) {
  const cv = useMoney();
  const a = result.analytics;
  const jur = result.jurisdiction;
  const sym = cv.symbol(jur);

  // Absolute-money chart series (quarterly revenue, dividends/buybacks/FCF)
  // arrive in home currency; convert them so tooltips match the toggle.
  const { quarters, cashflow } = useMemo(() => {
    const rawQuarters = a?.quarterly?.available ? a.quarterly?.quarters || [] : [];
    const rawCashflow = a?.cashflow_history || [];
    const c = (v: unknown): unknown => (isNum(v) ? cv.convert(v, jur) : v);
    return {
      quarters: rawQuarters.map((q) => ({ ...q, revenue: c(q.revenue) as number | null })),
      cashflow: rawCashflow.map((r) => ({
        ...r,
        dividends: c(r.dividends) as number | null,
        buybacks: c(r.buybacks) as number | null,
        free_cash_flow: c(r.free_cash_flow) as number | null,
      })),
    };
  }, [a, cv, jur]);

  if (!a) return null;
  const inc = a.incremental_roic;
  const wacc = a.wacc?.wacc_pct;
  const comps = a.comps?.available ? a.comps : null;
  const priceHistory = a.price_history || {};
  const hasPrices = Object.keys(priceHistory).length > 0;

  const hasAnything = quarters.length > 1 || cashflow.length > 1 || comps || hasPrices;
  if (!hasAnything) return null;

  return (
    <SectionCard eyebrow="Step 5" title="How healthy is the business?" copyKey="health">
      <div className="grid grid-cols-1 gap-x-8 gap-y-6 xl:grid-cols-2">
        {quarters.length > 1 ? (
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                Quarterly momentum
              </span>
              <LegendRow
                items={[
                  { label: `Revenue (${sym}B)`, color: NAVY2 },
                  { label: "YoY growth", color: CHART_GREEN, line: true },
                ]}
              />
            </div>
            <QuarterlyTrendChart quarters={quarters} currency={sym} />
          </div>
        ) : null}

        {cashflow.length > 1 ? (
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
                Cash returned to shareholders
              </span>
              <LegendRow
                items={[
                  { label: "Dividends", color: NAVY2 },
                  { label: "Buybacks", color: NAVY3 },
                  { label: "Free cash flow", color: CHART_GREEN, line: true },
                ]}
              />
            </div>
            <CapitalReturnsChart history={cashflow} currency={sym} />
          </div>
        ) : null}
      </div>

      {inc?.available && isNum(inc.incremental_roic_pct) ? (
        <div
          className={`mt-5 rounded-md border px-4 py-3 ${
            inc.value_accretive ? "border-green/30 bg-green/5" : "border-red/30 bg-red/5"
          }`}
        >
          <span className="text-[12px] text-navy">
            New investment earns{" "}
            <b className="num" style={{ color: inc.value_accretive ? "#1F7A52" : "#8C2F39" }}>
              {pctPoint(inc.incremental_roic_pct)}
            </b>{" "}
            (<HelpTip term="ROIC">incremental ROIC</HelpTip>)
            {isNum(wacc) ? (
              <>
                {" "}
                against a <HelpTip term="WACC">cost of capital</HelpTip> of <b className="num">{pctPoint(wacc)}</b>
              </>
            ) : null}
            {" — "}
            {inc.value_accretive ? (
              <b style={{ color: "#1F7A52" }}>every dollar reinvested is creating value.</b>
            ) : (
              <b style={{ color: "#8C2F39" }}>growth spending is currently destroying value.</b>
            )}
          </span>
        </div>
      ) : null}

      {comps ? (
        <div className="mt-6">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
            <HelpTip term="EV/EBITDA">Valuation</HelpTip> vs. sector peers
          </div>
          <PeerMultiplesBar comps={comps} highlight={result.ticker} label="EV/EBITDA — lower bars are cheaper, all else equal" />
        </div>
      ) : null}

      {hasPrices ? (
        <div className="mt-6">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
            Share price vs. peers · rebased to 100
          </div>
          <RelativePriceChart priceHistory={priceHistory} highlight={result.ticker} />
          <div className="mt-0.5 text-[10.5px] text-muted">
            Everyone starts at 100 — whoever is higher today has performed better over the window.
          </div>
        </div>
      ) : null}
    </SectionCard>
  );
}
