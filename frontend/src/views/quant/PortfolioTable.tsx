"use client";

// Enhanced portfolio holdings table for the Quant desk — the optimizer's weights
// dressed up as an "infotainment" book: brand logo + real company name, a 2-year
// price sparkline, the headline valuation & growth metrics, the model's predicted
// return / risk over the forecast horizon, and a Details drawer that expands the
// full fundamentals card. Purely presentational — QuantView fetches and passes
// the screener metadata + price series in.

import { Fragment, useMemo, useState } from "react";
import type { Jurisdiction, QuantPerName, ScreenerRow } from "@/lib/api";
import { num, pct, signedTone } from "@/lib/fmt";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { Sparkline } from "@/components/charts/primitives";
import { FundamentalsCard } from "./FundamentalsCard";

const HORIZON_SHORT: Record<number, string> = { 1: "1-mo", 3: "3-mo", 6: "6-mo", 12: "12-mo" };

export function PortfolioTable({
  perName,
  meta,
  prices,
  jurisdiction,
  horizonMonths,
}: {
  perName: QuantPerName[];
  meta: Map<string, ScreenerRow>;
  prices: Map<string, number[]>;
  jurisdiction: Jurisdiction;
  horizonMonths: number;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const rows = useMemo(
    () => [...perName].filter((r) => r.weight > 1e-4).sort((a, b) => b.weight - a.weight),
    [perName]
  );
  const maxWeight = Math.max(1e-6, ...rows.map((r) => r.weight));
  const hz = HORIZON_SHORT[horizonMonths] || `${horizonMonths}-mo`;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[880px] text-[12px]">
        <thead>
          <tr className="border-b border-border text-[10px] uppercase tracking-[0.07em] text-muted">
            <th className="py-2 pl-1 pr-3 text-left">Company</th>
            <th className="px-3 py-2 text-left">Weight</th>
            <th className="px-2 py-2 text-center">2-year</th>
            <th className="px-2 py-2 text-right" title="Price ÷ trailing earnings">P/E</th>
            <th className="px-2 py-2 text-right" title="Price ÷ book value">P/B</th>
            <th className="px-2 py-2 text-right" title="Enterprise value ÷ EBITDA">EV/EBITDA</th>
            <th className="px-2 py-2 text-right" title="Free-cash-flow yield">FCF yld</th>
            <th className="px-2 py-2 text-right" title="Revenue growth vs a year earlier">Rev YoY</th>
            <th className="px-3 py-2 text-right" title={`Model-predicted return and risk over the ${horizonMonths}-month horizon`}>
              {hz} forecast
            </th>
            <th className="py-2 pl-2 pr-1 text-right" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const m = meta.get(r.ticker);
            const met = m?.metrics || {};
            const series = prices.get(r.ticker);
            const isOpen = open === r.ticker;
            return (
              <Fragment key={r.ticker}>
                <tr className={`border-b border-border-soft align-middle hover:bg-paper/60 ${isOpen ? "bg-paper/60" : ""}`}>
                  {/* company */}
                  <td className="py-2.5 pl-1 pr-3">
                    <div className="flex items-center gap-2.5">
                      <BrandLogo ticker={r.ticker} name={m?.name || r.ticker} logoId={m?.logo_id} className="h-9 w-9" />
                      <div className="min-w-0">
                        <div className="truncate font-semibold text-navy" style={{ maxWidth: 190 }} title={m?.name || r.ticker}>
                          {m?.name || r.ticker}
                        </div>
                        <div className="text-[10.5px] text-muted">{r.ticker}</div>
                      </div>
                    </div>
                  </td>
                  {/* weight */}
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-panel">
                        <div className="h-full rounded-full bg-navy" style={{ width: `${(r.weight / maxWeight) * 100}%` }} />
                      </div>
                      <span className="w-11 shrink-0 tabular-nums font-semibold text-navy">{pct(r.weight)}</span>
                    </div>
                  </td>
                  {/* 2y sparkline */}
                  <td className="px-2 py-2.5">
                    <div className="flex justify-center">
                      {series && series.length > 1 ? <Sparkline points={series} width={72} height={24} smooth /> : <span className="text-muted">—</span>}
                    </div>
                  </td>
                  {/* valuation + growth */}
                  <td className="px-2 py-2.5 text-right tabular-nums">{ratioFmt(met.pe)}</td>
                  <td className="px-2 py-2.5 text-right tabular-nums">{ratioFmt(met.pb)}</td>
                  <td className="px-2 py-2.5 text-right tabular-nums">{ratioFmt(met.ev_ebitda)}</td>
                  <td className={`px-2 py-2.5 text-right tabular-nums ${signedTone(met.fcf_yield)}`}>{pct(met.fcf_yield)}</td>
                  <td className={`px-2 py-2.5 text-right tabular-nums ${signedTone(met.rev_yoy)}`}>{pct(met.rev_yoy)}</td>
                  {/* forecast: return over horizon + risk */}
                  <td className="px-3 py-2.5 text-right">
                    <div
                      className={`text-[13px] font-semibold tabular-nums ${signedTone(r.expected_return_horizon)}`}
                      title={`${pct(r.expected_return_annual)} annualized · source: ${r.alpha_source}`}
                    >
                      {signed(pct(r.expected_return_horizon))}
                    </div>
                    <div className="text-[10px] text-muted tabular-nums" title={`${pct(r.forward_vol_annual)} annualized volatility`}>
                      ± {pct(r.forward_vol_horizon)} risk
                    </div>
                  </td>
                  {/* details toggle */}
                  <td className="py-2.5 pl-2 pr-1 text-right">
                    <button
                      onClick={() => setOpen(isOpen ? null : r.ticker)}
                      className={`rounded border px-2 py-1 text-[11px] font-medium transition-colors ${
                        isOpen ? "border-navy bg-navy text-white" : "border-border text-navy hover:border-navy"
                      }`}
                      aria-expanded={isOpen}
                      title="Show the full fundamentals summary"
                    >
                      {isOpen ? "Hide" : "Details"}
                    </button>
                  </td>
                </tr>
                {isOpen ? (
                  <tr>
                    <td colSpan={10} className="px-1 pb-3 pt-1">
                      <FundamentalsCard ticker={r.ticker} jurisdiction={jurisdiction} />
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

function ratioFmt(v: number | null | undefined): string {
  return typeof v === "number" && isFinite(v) ? `${num(v, 1)}×` : "—";
}
function signed(s: string): string {
  return s === "—" || s.startsWith("-") || s.startsWith("+") ? s : `+${s}`;
}
