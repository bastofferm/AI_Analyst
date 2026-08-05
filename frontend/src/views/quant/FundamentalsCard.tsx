"use client";

// Fundamentals summary card — the expandable "Details" panel behind each holding
// in the Quant desk portfolio table. Lazily pulls the full company data block
// (/api/company/{ticker}) on first open and lays out a recognizable snapshot:
// identity, price stats, multi-year revenue / free-cash-flow trend, and the key
// growth / profitability / returns / valuation ratios. US + JP only (the
// standardized statement layer the endpoint reads exists for those two).

import { useEffect, useState } from "react";
import { api, type CompanyDataResponse, type CompanyDataRow, type Jurisdiction } from "@/lib/api";
import { money, num, pct, perShareC, homeCurrency, signedTone } from "@/lib/fmt";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { Sparkline } from "@/components/charts/primitives";

const RATIO_GROUPS: { title: string; keys: string[] }[] = [
  { title: "Growth", keys: ["revenue_growth_year_over_year", "revenue_compound_annual_growth_rate_3_year"] },
  { title: "Profitability", keys: ["gross_margin", "operating_margin", "net_profit_margin"] },
  { title: "Returns", keys: ["return_on_equity", "return_on_invested_capital"] },
  { title: "Valuation", keys: ["price_to_earnings_trailing", "price_to_book", "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization", "free_cash_flow_yield", "dividend_yield"] },
];

function latest(row?: CompanyDataRow): number | null {
  if (!row) return null;
  for (let i = row.values.length - 1; i >= 0; i--) {
    const v = row.values[i];
    if (typeof v === "number" && isFinite(v)) return v;
  }
  return null;
}

function fmtRow(row: CompanyDataRow | undefined, jur: Jurisdiction): string {
  const v = latest(row);
  if (v === null || !row) return "—";
  if (row.unit === "pct") return pct(v);
  if (row.unit === "ratio") return `${num(v, 1)}×`;
  return money(v, jur);
}

// Compound annual growth from the first to last non-null point of a series.
function cagr(values: (number | null)[]): number | null {
  const pts = values.map((v, i) => ({ v, i })).filter((p) => typeof p.v === "number" && isFinite(p.v!) && p.v! > 0);
  if (pts.length < 2) return null;
  const first = pts[0], last = pts[pts.length - 1];
  const span = last.i - first.i;
  if (span <= 0) return null;
  return Math.pow((last.v as number) / (first.v as number), 1 / span) - 1;
}

export function FundamentalsCard({ ticker, jurisdiction }: { ticker: string; jurisdiction: Jurisdiction }) {
  const [data, setData] = useState<CompanyDataResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "done" | "error">("loading");
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    setErr("");
    // companyData is US/JP only; the Quant desk never selects INTL. JP names are keyed
    // by their .T-suffixed primary_ticker, but the table carries the bare code.
    const wt = jurisdiction === "JP" && !/\.T$/i.test(ticker) ? `${ticker}.T` : ticker;
    api
      .companyData(wt, jurisdiction === "JP" ? "JP" : "US")
      .then((d) => { if (!cancelled) { setData(d); setStatus("done"); } })
      .catch((e) => { if (!cancelled) { setErr(e instanceof Error ? e.message : String(e)); setStatus("error"); } });
    return () => { cancelled = true; };
  }, [ticker, jurisdiction]);

  if (status === "loading") {
    return <div className="animate-pulse py-6 text-center text-[12px] text-muted">Loading fundamentals for {ticker}…</div>;
  }
  if (status === "error" || !data) {
    return <div className="py-4 text-[12px] text-red-700">Couldn’t load fundamentals for {ticker}. {err}</div>;
  }

  const { profile, price } = data;
  const jur = data.jurisdiction as Jurisdiction;
  const curr = homeCurrency(jur);
  const stmt = new Map(data.statement_rows.map((r) => [r.key, r]));
  const ratio = new Map(data.ratio_rows.map((r) => [r.key, r]));
  const revenue = stmt.get("revenue");
  const fcf = stmt.get("free_cash_flow");
  const netInc = stmt.get("net_income");

  return (
    <div className="rounded-lg border border-border-soft bg-paper/50 p-4">
      {/* identity */}
      <div className="flex flex-wrap items-start gap-3">
        <BrandLogo ticker={ticker} name={profile.name} logoId={profile.logo_id} className="h-11 w-11" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-[15px] font-semibold text-navy">{profile.name}</span>
            <span className="text-[12px] font-medium text-muted">{ticker}</span>
            {profile.name_local && profile.name_local !== profile.name ? (
              <span className="text-[11px] text-muted">· {profile.name_local}</span>
            ) : null}
          </div>
          <div className="mt-0.5 text-[11px] text-muted">
            {[profile.sector, profile.industry_group, profile.exchange].filter(Boolean).join(" · ") || "—"}
          </div>
        </div>
        <div className="text-right">
          <div className="label">Market cap</div>
          <div className="text-[14px] font-semibold text-navy">{money(profile.market_cap, jur)}</div>
        </div>
      </div>

      {/* price stats */}
      <div className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2 border-t border-border-soft pt-3 text-[12px] sm:grid-cols-4">
        <Cell label="Last" value={perShareC(price.last ?? null, curr)} sub={price.last_date || undefined} />
        <Cell label="52-week range"
          value={price.low_52w != null && price.high_52w != null
            ? `${perShareC(price.low_52w, curr)} – ${perShareC(price.high_52w, curr)}` : "—"} />
        <Cell label="1-year change" value={price.change_1y != null ? pct(price.change_1y) : "—"} tone={price.change_1y} />
        <Cell label="Shares out." value={profile.shares_outstanding != null ? num(profile.shares_outstanding / 1e6, 0) + "M" : "—"} />
      </div>

      {/* multi-year trend */}
      {(revenue || fcf || netInc) ? (
        <div className="mt-3 grid grid-cols-1 gap-3 border-t border-border-soft pt-3 sm:grid-cols-3">
          <TrendMini title="Revenue" row={revenue} years={data.years} jur={jur} />
          <TrendMini title="Free cash flow" row={fcf} years={data.years} jur={jur} />
          <TrendMini title="Net income" row={netInc} years={data.years} jur={jur} />
        </div>
      ) : null}

      {/* ratio grid */}
      <div className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3 border-t border-border-soft pt-3 sm:grid-cols-4">
        {RATIO_GROUPS.map((g) => (
          <div key={g.title}>
            <div className="label mb-1">{g.title}</div>
            <div className="space-y-1">
              {g.keys.map((k) => {
                const row = ratio.get(k);
                if (!row) return null;
                return (
                  <div key={k} className="flex items-baseline justify-between gap-2 text-[11.5px]">
                    <span className="truncate text-muted">{shortLabel(row.label)}</span>
                    <span className="tabular-nums font-medium text-navy">{fmtRow(row, jur)}</span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3 text-[10px] text-muted">{data.source_note}</div>
    </div>
  );
}

function Cell({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: number | null }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`text-[13px] font-semibold tabular-nums ${tone == null ? "text-navy" : signedTone(tone)}`}>{value}</div>
      {sub ? <div className="text-[10px] text-muted">{sub}</div> : null}
    </div>
  );
}

function TrendMini({ title, row, years, jur }: { title: string; row?: CompanyDataRow; years: number[]; jur: Jurisdiction }) {
  if (!row) return null;
  const g = cagr(row.values);
  const last = latest(row);
  return (
    <div className="rounded-md border border-border-soft bg-panel px-3 py-2">
      <div className="flex items-center justify-between">
        <span className="label">{title}</span>
        {g != null ? <span className={`text-[10px] font-medium ${signedTone(g)}`}>{pct(g)} CAGR</span> : null}
      </div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <span className="text-[14px] font-semibold tabular-nums text-navy">{money(last, jur)}</span>
        <Sparkline points={row.values} width={72} height={26} />
      </div>
      <div className="mt-0.5 text-[9.5px] text-muted">{years[0]}–{years[years.length - 1]}</div>
    </div>
  );
}

// Trim the long warehouse labels for the compact ratio grid.
function shortLabel(label: string): string {
  return label
    .replace("Return on invested capital", "ROIC")
    .replace("Return on equity", "ROE")
    .replace(" (trailing)", "")
    .replace("Revenue growth YoY", "Rev growth")
    .replace("Revenue CAGR 3Y", "Rev CAGR 3Y");
}
