"use client";

// The data basis — the deterministic warehouse numbers behind any committee
// opinion: standardized filing line items, ratio history, profile and the
// 52-week price range, straight from /api/company/{ticker}. US/JP only (the
// router 404/422s otherwise); the section renders nothing when unavailable.

import { useEffect, useMemo, useState } from "react";
import { api, type CompanyDataResponse, type CompanyDataRow } from "@/lib/api";
import { useMoney, type MoneyKit } from "@/lib/currency";
import { num, pct, signedPct } from "@/lib/fmt";
import { Sparkline } from "@/components/charts/primitives";
import { CHART_GREEN, CHART_RED, isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { snapshotJurisdiction } from "./Snapshot";

// Session cache so the section survives the idle → running → done remounts of
// AnalyzeView without refetching (the underlying data never changes mid-session).
const companyDataCache = new Map<string, CompanyDataResponse>();

const STATEMENT_GROUPS: Record<string, string> = {
  income: "Income statement",
  cashflow: "Cash flow",
  balance: "Balance sheet",
};
const RATIO_GROUPS: Record<string, string> = {
  growth: "Growth",
  profitability: "Margins",
  returns: "Returns on capital",
  valuation: "Valuation & payout",
};

export function useCompanyData(ticker: string): {
  data: CompanyDataResponse | null;
  loading: boolean;
} {
  const [data, setData] = useState<CompanyDataResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const tk = ticker.trim().toUpperCase();
  const jur = snapshotJurisdiction(tk);

  useEffect(() => {
    if (!tk || !jur) {
      setData(null);
      return;
    }
    const cacheKey = `${jur}:${tk}`;
    const cached = companyDataCache.get(cacheKey);
    if (cached) {
      setData(cached);
      return;
    }
    // Debounce hand-typed tickers so "MS" doesn't flash Morgan Stanley en
    // route to MSFT; picks from Explore/Ideas arrive complete and only pay
    // the delay once.
    let cancelled = false;
    setLoading(true);
    const t = setTimeout(() => {
      api
        .companyData(tk, jur)
        .then((res) => {
          companyDataCache.set(cacheKey, res);
          if (!cancelled) {
            setData(res);
            setLoading(false);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setData(null);
            setLoading(false);
          }
        });
    }, 450);
    return () => {
      cancelled = true;
      clearTimeout(t);
      setLoading(false);
    };
  }, [tk, jur]);

  return { data, loading };
}

/** Deterministic "what jumps out" chips computed from the raw rows. */
function highlights(d: CompanyDataResponse, cv: MoneyKit): { text: string; tone: "pos" | "neg" | "neu" }[] {
  const out: { text: string; tone: "pos" | "neg" | "neu" }[] = [];
  const row = (rows: CompanyDataRow[], key: string) => rows.find((r) => r.key === key);
  const lastVal = (r?: CompanyDataRow) => {
    const vals = (r?.values || []).filter(isNum);
    return vals.length ? vals[vals.length - 1] : null;
  };

  const revenue = row(d.statement_rows, "revenue");
  if (revenue) {
    const idx = revenue.values.map((v, i) => (isNum(v) ? i : -1)).filter((i) => i >= 0);
    if (idx.length >= 2) {
      const first = revenue.values[idx[0]] as number;
      const last = revenue.values[idx[idx.length - 1]] as number;
      const span = d.years[idx[idx.length - 1]] - d.years[idx[0]];
      if (first > 0 && span > 0) {
        const cagr = Math.pow(last / first, 1 / span) - 1;
        out.push({
          text: `Revenue ${cagr >= 0 ? "grew" : "shrank"} ~${pct(Math.abs(cagr))}/yr since FY${d.years[idx[0]]}`,
          tone: cagr > 0.02 ? "pos" : cagr < 0 ? "neg" : "neu",
        });
      }
    }
  }

  const fcf = lastVal(row(d.statement_rows, "free_cash_flow"));
  const rev = lastVal(revenue);
  if (isNum(fcf) && isNum(rev) && rev > 0) {
    const m = fcf / rev;
    out.push({
      text: `Turns ${pct(m)} of sales into free cash`,
      tone: m >= 0.1 ? "pos" : m < 0 ? "neg" : "neu",
    });
  }

  const netDebt = lastVal(row(d.statement_rows, "net_debt"));
  const ebitda = lastVal(row(d.statement_rows, "earnings_before_interest_taxes_depreciation_amortization"));
  if (isNum(netDebt)) {
    if (netDebt < 0) {
      out.push({ text: `Sits on net cash of ${cv.money(-netDebt, d.profile.currency)}`, tone: "pos" });
    } else if (isNum(ebitda) && ebitda > 0) {
      const lev = netDebt / ebitda;
      out.push({
        text: `Net debt ${num(lev, 1)}× EBITDA`,
        tone: lev > 3 ? "neg" : lev > 2 ? "neu" : "pos",
      });
    }
  }

  const p = d.price;
  if (isNum(p.last) && isNum(p.high_52w) && isNum(p.low_52w) && p.high_52w > p.low_52w) {
    const posFrac = (p.last - p.low_52w) / (p.high_52w - p.low_52w);
    if (posFrac >= 0.93) out.push({ text: "Trading near its 52-week high", tone: "neu" });
    else if (posFrac <= 0.07) out.push({ text: "Trading near its 52-week low", tone: "neg" });
  }

  return out.slice(0, 4);
}

export function CompanyDataSection({
  ticker,
  eyebrow = "The raw numbers",
}: {
  ticker: string;
  eyebrow?: string;
}) {
  const { data, loading } = useCompanyData(ticker);
  const tk = ticker.trim().toUpperCase();

  if (!tk || !snapshotJurisdiction(tk)) return null;
  if (!data && !loading) return null;

  return (
    <SectionCard
      eyebrow={eyebrow}
      title={data ? `The data behind ${data.ticker} — ${data.profile.name}` : `Pulling the data behind ${tk}…`}
      copyKey="companyData"
    >
      {!data ? (
        <SkeletonRows rows={6} />
      ) : (
        <CompanyDataBody data={data} />
      )}
    </SectionCard>
  );
}

function CompanyDataBody({ data }: { data: CompanyDataResponse }) {
  const d = data;
  const cv = useMoney();
  const chips = useMemo(() => highlights(d, cv), [d, cv]);
  const profileBits = [
    d.profile.name_local,
    d.profile.sector,
    d.profile.industry_group,
    d.profile.exchange,
    d.profile.fy_min && d.profile.fy_max ? `Filings FY${d.profile.fy_min}–FY${d.profile.fy_max}` : null,
  ].filter(Boolean) as string[];

  return (
    <div className="flex flex-col gap-5">
      {/* Profile line + market cap */}
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-navy">
          {profileBits.map((b, i) => (
            // Index-keyed on purpose: a company can repeat a value across the
            // bits (ENEOS is sector "Energy" AND industry group "Energy"), and
            // the list is static for a given company, so position is stable.
            <span key={`${i}-${b}`} className="flex items-center gap-1.5">
              {i > 0 && <span className="text-muted">·</span>}
              <span>{b}</span>
            </span>
          ))}
        </div>
        {isNum(d.profile.market_cap) ? (
          <div className="text-right">
            <span className="text-[9.5px] uppercase tracking-[0.12em] text-muted">Market cap </span>
            <span className="num text-[15px] font-bold text-navy">{cv.money(d.profile.market_cap, d.profile.currency)}</span>
          </div>
        ) : null}
      </div>

      {/* Highlights */}
      {chips.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {chips.map((c, i) => (
            <span
              key={c.text}
              className={`rise-in hover-lift rounded-full border px-2.5 py-1 text-[11px] font-medium ${
                c.tone === "pos"
                  ? "border-green/30 bg-green/5 text-green"
                  : c.tone === "neg"
                    ? "border-red/30 bg-red/5 text-red"
                    : "border-border bg-white text-navy"
              }`}
              style={{ animationDelay: `${i * 80}ms` }}
            >
              {c.text}
            </span>
          ))}
        </div>
      ) : null}

      {/* 52-week range */}
      <FiftyTwoWeekBar data={d} />

      {d.statement_rows.length > 0 ? (
        <DataTable
          title="The financial statements"
          note={`All figures in ${cv.displayCode(d.profile.currency)}, per fiscal year`}
          rows={d.statement_rows}
          groups={STATEMENT_GROUPS}
          years={d.years}
          homeCcy={d.profile.currency}
        />
      ) : null}

      {d.ratio_rows.length > 0 ? (
        <DataTable
          title="Ratios & returns"
          note="Computed from the statements above and market prices"
          rows={d.ratio_rows}
          groups={RATIO_GROUPS}
          years={d.years}
          homeCcy={d.profile.currency}
        />
      ) : null}

      <p className="text-[10.5px] text-muted">
        {d.source_note}
        {d.price.last_date ? ` · prices as of ${d.price.last_date}` : ""}
        {cv.note ? ` · ${cv.note}` : ""} · This is the same data the committee argues from — no AI involved on
        this card.
      </p>
    </div>
  );
}

function FiftyTwoWeekBar({ data }: { data: CompanyDataResponse }) {
  const cv = useMoney();
  const p = data.price;
  if (!isNum(p.last) || !isNum(p.high_52w) || !isNum(p.low_52w) || p.high_52w <= p.low_52w) return null;
  const frac = Math.max(0, Math.min(1, (p.last - p.low_52w) / (p.high_52w - p.low_52w)));
  const fmtPx = (v: number) => cv.perShare(v, data.profile.currency);
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[10px] uppercase tracking-[0.1em] text-muted">52-week range</span>
        {isNum(p.change_1y) ? (
          <span className={`num rounded px-1.5 py-px text-[10px] font-bold ${p.change_1y >= 0 ? "badge-pos" : "badge-neg"}`}>
            {p.change_1y >= 0 ? "▲" : "▼"} {signedPct(p.change_1y * 100)} 1Y
          </span>
        ) : null}
      </div>
      <div className="relative h-2 rounded-full bg-border-soft">
        <div
          className="absolute top-1/2 h-3.5 w-[3px] -translate-y-1/2 rounded-full"
          style={{ left: `calc(${(frac * 100).toFixed(1)}% - 1px)`, background: "#2F4D73" }}
          title={`Today: ${fmtPx(p.last)}`}
        />
      </div>
      <div className="mt-1 flex items-center justify-between text-[10.5px] text-muted">
        <span className="num">{fmtPx(p.low_52w)}</span>
        <span className="num font-semibold text-navy">today {fmtPx(p.last)}</span>
        <span className="num">{fmtPx(p.high_52w)}</span>
      </div>
    </div>
  );
}

/** Latest year-over-year change of a row, formatted per unit. Null when the
 *  change would be meaningless: missing years, or a currency row that isn't
 *  strictly positive (a "% change" of capex or net debt flips sign the wrong
 *  way round — the sparkline still shows the direction). `neutral` marks rows
 *  where more/less isn't better/worse (valuation multiples). */
function lastDelta(row: CompanyDataRow): { label: string; positive: boolean; neutral: boolean } | null {
  const vals = row.values;
  let lastIdx = -1;
  for (let i = vals.length - 1; i >= 0; i--) {
    if (isNum(vals[i])) {
      lastIdx = i;
      break;
    }
  }
  if (lastIdx <= 0 || !isNum(vals[lastIdx - 1])) return null;
  const cur = vals[lastIdx] as number;
  const prev = vals[lastIdx - 1] as number;
  if (row.unit === "currency") {
    if (prev <= 0 || cur <= 0) return null;
    const chg = (cur - prev) / prev;
    return { label: signedPct(chg * 100), positive: chg >= 0, neutral: false };
  }
  if (row.unit === "pct") {
    const pp = (cur - prev) * 100;
    return { label: `${pp >= 0 ? "+" : ""}${pp.toFixed(1)}pp`, positive: pp >= 0, neutral: false };
  }
  // Ratio rows are valuation multiples — cheaper isn't "worse", so no color.
  const diff = cur - prev;
  return { label: `${diff >= 0 ? "+" : ""}${num(diff, 1)}×`, positive: diff >= 0, neutral: true };
}

function fmtCell(v: number | null, unit: CompanyDataRow["unit"], homeCcy: string, cv: MoneyKit): string {
  if (!isNum(v)) return "—";
  if (unit === "currency") return cv.money(v, homeCcy);
  if (unit === "pct") return pct(v);
  return `${num(v, 1)}×`;
}

function DataTable({
  title,
  note,
  rows,
  groups,
  years,
  homeCcy,
}: {
  title: string;
  note: string;
  rows: CompanyDataRow[];
  groups: Record<string, string>;
  years: number[];
  homeCcy: string;
}) {
  const latestYear = years[years.length - 1];
  // Preserve backend row order, but emit a group header whenever it changes.
  const sections: { group: string; rows: CompanyDataRow[] }[] = [];
  for (const r of rows) {
    const last = sections[sections.length - 1];
    if (last && last.group === r.group) last.rows.push(r);
    else sections.push({ group: r.group, rows: [r] });
  }

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">{title}</span>
        <span className="text-[10px] text-muted">{note}</span>
      </div>
      <div className="overflow-x-auto rounded-md border border-border-soft">
        <table className="w-full text-[11.5px]">
          <thead>
            <tr className="bg-paper/70 text-[9.5px] uppercase tracking-[0.08em] text-muted">
              <th className="sticky left-0 z-10 bg-paper px-3 py-2 text-left">Metric</th>
              <th className="px-2 py-2 text-center">Trend</th>
              {years.map((y) => (
                <th
                  key={y}
                  className={`num px-2.5 py-2 text-right ${y === latestYear ? "font-bold text-navy" : ""}`}
                >
                  FY{String(y).slice(2)}
                </th>
              ))}
              <th className="px-2.5 py-2 text-right">Δ YoY</th>
            </tr>
          </thead>
          <tbody>
            {sections.map((sec) => (
              <SectionRows
                key={sec.group}
                label={groups[sec.group] || sec.group}
                rows={sec.rows}
                years={years}
                latestYear={latestYear}
                homeCcy={homeCcy}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SectionRows({
  label,
  rows,
  years,
  latestYear,
  homeCcy,
}: {
  label: string;
  rows: CompanyDataRow[];
  years: number[];
  latestYear: number;
  homeCcy: string;
}) {
  const cv = useMoney();
  return (
    <>
      <tr>
        <td
          colSpan={years.length + 3}
          className="border-t border-border-soft bg-paper/40 px-3 py-1 text-[9.5px] font-semibold uppercase tracking-[0.1em] text-navy-3"
        >
          {label}
        </td>
      </tr>
      {rows.map((r) => {
        const delta = lastDelta(r);
        return (
          <tr key={r.key} className="border-t border-border-soft align-middle hover:bg-paper/60">
            <td className="sticky left-0 z-10 whitespace-nowrap bg-white px-3 py-1.5 font-medium text-navy">
              {r.label}
            </td>
            <td className="px-2 py-1.5 text-center">
              <Sparkline points={r.values} width={56} height={16} strokeWidth={1.3} />
            </td>
            {years.map((y, i) => (
              <td
                key={y}
                className={`num whitespace-nowrap px-2.5 py-1.5 text-right ${
                  y === latestYear ? "font-semibold text-navy" : "text-navy/80"
                }`}
              >
                {fmtCell(r.values[i], r.unit, homeCcy, cv)}
              </td>
            ))}
            <td
              className="num whitespace-nowrap px-2.5 py-1.5 text-right text-[10.5px] font-semibold"
              style={{
                color: delta && !delta.neutral ? (delta.positive ? CHART_GREEN : CHART_RED) : undefined,
              }}
            >
              {delta ? delta.label : ""}
            </td>
          </tr>
        );
      })}
    </>
  );
}
