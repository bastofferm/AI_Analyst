"use client";

// Alpha forecast search — restructures the old "Top expected returns" list into a
// searchable / filterable / sortable screen over the model's return predictions, with
// a confidence header up front: how good the model is (rank-IC / ICIR), how much data
// went into it (names scored, features, validation months, training window), what the
// forecast period is, and what the estimation window was. Company names + market caps
// come from the screener so the user can filter micro-cap noise out of the top ranks.

import { useEffect, useMemo, useState } from "react";
import { api, type Jurisdiction, type QuantAlphaResponse, type ScreenerRow } from "@/lib/api";
import { money, num, pct, signedTone } from "@/lib/fmt";

type SortKey = "return" | "cap";
type NameCap = { name: string; cap: number | null };

// USD market-cap floors (screener market_cap_usd). USD-scaled → US only; JP caps are ¥.
const SIZE_BANDS: { key: string; label: string; min: number | null }[] = [
  { key: "any", label: "Any size", min: null },
  { key: "micro", label: "≥ $300M", min: 3e8 },
  { key: "small", label: "≥ $2B", min: 2e9 },
  { key: "mid", label: "≥ $10B", min: 1e10 },
  { key: "large", label: "≥ $200B", min: 2e11 },
];

const HZ: Record<number, string> = { 1: "1-month", 3: "3-month", 6: "6-month", 12: "12-month" };

// Exported: the research panel grades the same quantities and must not drift from these
// thresholds — two places disagreeing about what "modest" skill means would be worse than
// either threshold being slightly wrong.
export function icQuality(ic: number | undefined): { label: string; tone: number } {
  if (typeof ic !== "number" || !isFinite(ic)) return { label: "—", tone: 0 };
  if (ic <= 0) return { label: "no usable skill", tone: -1 };
  if (ic < 0.01) return { label: "negligible", tone: -1 };
  if (ic < 0.03) return { label: "weak", tone: 0 };
  if (ic < 0.05) return { label: "modest", tone: 1 };
  if (ic < 0.08) return { label: "solid", tone: 1 };
  return { label: "strong", tone: 1 };
}
export function icirQuality(icir: number | undefined): string {
  if (typeof icir !== "number" || !isFinite(icir)) return "—";
  const a = Math.abs(icir);
  if (a < 0.2) return "noisy";
  if (a < 0.5) return "inconsistent";
  if (a < 1.0) return "fairly consistent";
  return "consistent";
}
export const toneCls = (t: number) => (t > 0 ? "text-green-700" : t < 0 ? "text-red-700" : "text-navy");

function yearsBetween(a?: string, b?: string): number | null {
  if (!a || !b) return null;
  const d = (new Date(b).getTime() - new Date(a).getTime()) / (365.25 * 864e5);
  return isFinite(d) && d > 0 ? d : null;
}

export function AlphaSearch({
  alpha,
  jurisdiction,
  horizon,
}: {
  alpha: QuantAlphaResponse | null;
  jurisdiction: Jurisdiction;
  horizon: string;
}) {
  const [nameCaps, setNameCaps] = useState<Map<string, NameCap>>(new Map());
  const [query, setQuery] = useState("");
  const [sizeBand, setSizeBand] = useState("any");
  const [sortKey, setSortKey] = useState<SortKey>("return");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Memoized: `alpha?.rows ?? []` produces a NEW array identity on every render whenever
  // `alpha` is null or has no rows. That array is a dependency of the effect below, whose
  // empty branch unconditionally calls setNameCaps(new Map()) — never Object.is-equal to the
  // previous Map — so the two would drive each other in a render loop for as long as the
  // market has no trained model. QuantView is force-mounted at app start, so that state is
  // reached on every load.
  const rows = useMemo(() => alpha?.rows ?? [], [alpha]);

  // Enrich the loaded predictions with company name + market cap (one screener call).
  useEffect(() => {
    if (!rows.length) { setNameCaps((prev) => (prev.size ? new Map() : prev)); return; }
    let cancelled = false;
    const jur = jurisdiction === "JP" ? "JP" : "US";
    const dimTicker = (t: string) => (jur === "JP" && !/\.T$/i.test(t) ? `${t}.T` : t);
    api.screenerRun({
      universe: { jurisdiction, portfolio_tickers: rows.map((r) => dimTicker(r.ticker)) },
      filters: {}, sort: { key: "market_cap_usd", dir: "desc" }, limit: rows.length,
    })
      .then((res) => {
        if (cancelled) return;
        const mp = new Map<string, NameCap>();
        for (const row of res.rows) {
          const nc: NameCap = { name: row.name, cap: (row.metrics?.market_cap_usd as number) ?? null };
          mp.set(row.ticker.toUpperCase(), nc);
          mp.set(row.ticker.replace(/\.T$/i, "").toUpperCase(), nc);
        }
        setNameCaps(mp);
      })
      .catch(() => { if (!cancelled) setNameCaps(new Map()); });
    return () => { cancelled = true; };
  }, [rows, jurisdiction]);

  const showSize = jurisdiction === "US";
  const band = SIZE_BANDS.find((b) => b.key === sizeBand) || SIZE_BANDS[0];

  const view = useMemo(() => {
    const q = query.trim().toLowerCase();
    const enriched = rows.map((r) => {
      const nc = nameCaps.get(r.ticker.toUpperCase());
      return { ...r, name: nc?.name ?? null, cap: nc?.cap ?? null };
    });
    let out = enriched.filter((r) => {
      if (q && !(r.ticker.toLowerCase().includes(q) || (r.name ?? "").toLowerCase().includes(q))) return false;
      if (showSize && band.min != null && !(typeof r.cap === "number" && r.cap >= band.min)) return false;
      return true;
    });
    out = out.sort((a, b) => {
      const av = sortKey === "return" ? (a.expected_return_monthly ?? -Infinity) : (a.cap ?? -Infinity);
      const bv = sortKey === "return" ? (b.expected_return_monthly ?? -Infinity) : (b.cap ?? -Infinity);
      return sortDir === "desc" ? bv - av : av - bv;
    });
    return out;
  }, [rows, nameCaps, query, showSize, band, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else { setSortKey(k); setSortDir("desc"); }
  }
  const arrow = (k: SortKey) => (sortKey === k ? (sortDir === "desc" ? " ▼" : " ▲") : "");

  if (!alpha?.available || !alpha.model) {
    return <div className="text-[12px] text-muted">{alpha?.note || "No trained alpha model for this market yet."}</div>;
  }

  const m = alpha.model;
  const rankIc = m.metrics?.rank_ic_mean;
  const icir = m.metrics?.rank_icir;
  const nDates = m.metrics?.n_dates;
  const trainYears = yearsBetween(m.train_range?.[0], m.train_range?.[1]);
  const icq = icQuality(rankIc);
  const hzLabel = HZ[m.horizon_months] || `${m.horizon_months}-month`;

  return (
    <div className="space-y-3">
      {/* confidence header — how trustworthy is this forecast, and on what basis */}
      <div className="grid grid-cols-2 gap-x-5 gap-y-3 rounded-lg border border-border-soft bg-paper/40 p-3 sm:grid-cols-3 lg:grid-cols-6">
        <Meta label="Forecast horizon" value={hzLabel} sub="prediction period" />
        <Meta label="Estimation window" value={m.train_range ? `${m.train_range[0]} → ${m.train_range[1]}` : "—"}
          sub={trainYears ? `${trainYears.toFixed(1)} years of history` : undefined} />
        <Meta label="Model skill" value={`rank-IC ${num(rankIc, 3)}`} sub={icq.label} tone={icq.tone} />
        <Meta label="Consistency" value={`ICIR ${num(icir, 2)}`} sub={icirQuality(icir)} />
        <Meta label="Breadth" value={`${(alpha.n_covered ?? rows.length).toLocaleString()} names`}
          sub={`${m.n_features} features`} />
        <Meta label="Validated on" value={nDates != null ? `${nDates} months` : "—"}
          sub={`trained ${(m.trained_at || "").slice(0, 10)}`} />
      </div>
      <p className="text-[10.5px] text-muted">
        Skill is the out-of-sample rank correlation between the model’s ranking and realized {hzLabel} returns
        (~0.03 is a genuine monthly-equity edge; near 0 is noise). Every prediction shares that skill — the very
        top ranks are usually thin micro-caps, so filter by size to find trustworthy high-return names.
      </p>

      {/* search + filters */}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-[12px]">
          <span className="label">Search</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ticker or company…"
            className="h-[32px] w-52 rounded-md border border-border bg-white px-2.5 text-[13px] text-navy outline-none focus:border-navy"
          />
        </label>
        {showSize ? (
          <label className="flex flex-col gap-1 text-[12px]">
            <span className="label">Min. market cap</span>
            <select value={sizeBand} onChange={(e) => setSizeBand(e.target.value)}
              className="h-[32px] rounded-md border border-border bg-panel px-2 text-[13px]">
              {SIZE_BANDS.map((b) => <option key={b.key} value={b.key}>{b.label}</option>)}
            </select>
          </label>
        ) : null}
        <div className="ml-auto text-[11px] text-muted">
          <span className="font-semibold text-navy">{view.length.toLocaleString()}</span> of {rows.length.toLocaleString()} shown
          {showSize && band.min != null ? " · size-filtered" : ""}
        </div>
      </div>

      {/* results */}
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="border-b border-border text-[10px] uppercase tracking-[0.07em] text-muted">
              <th className="py-2 pl-1 pr-3 text-left">#</th>
              <th className="px-2 py-2 text-left">Company</th>
              <th className="cursor-pointer px-2 py-2 text-right hover:text-navy" onClick={() => toggleSort("cap")}>Market cap{arrow("cap")}</th>
              <th className="cursor-pointer px-2 py-2 text-right hover:text-navy" onClick={() => toggleSort("return")}>{horizonShort(horizon)} return{arrow("return")}</th>
              <th className="px-2 py-2 text-right">Annualized</th>
            </tr>
          </thead>
          <tbody>
            {view.map((r, i) => (
              <tr key={r.ticker} className="border-b border-border-soft hover:bg-paper/60">
                <td className="num py-1.5 pl-1 pr-3 text-muted">{i + 1}</td>
                <td className="px-2 py-1.5">
                  <span className="font-semibold text-navy">{r.ticker}</span>
                  {r.name ? <span className="ml-2 text-[11px] text-muted">{r.name}</span> : null}
                </td>
                <td className="num px-2 py-1.5 text-right text-navy/80">{typeof r.cap === "number" ? money(r.cap, jurisdiction) : "—"}</td>
                <td className={`num px-2 py-1.5 text-right font-medium ${signedTone(r.expected_return_monthly)}`}>{pct(r.expected_return_monthly, 1)}</td>
                <td className="num px-2 py-1.5 text-right text-navy/80">{pct(r.expected_return_annual, 1)}</td>
              </tr>
            ))}
            {view.length === 0 ? (
              <tr><td colSpan={5} className="px-2 py-6 text-center text-[12px] text-muted">No predictions match — widen the search or size filter.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const horizonShort = (label: string) => label.replace("forward_", "");

export function Meta({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: number }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className={`mt-0.5 text-[13px] font-semibold tabular-nums ${tone == null ? "text-navy" : toneCls(tone)}`}>{value}</div>
      {sub ? <div className="text-[10px] text-muted">{sub}</div> : null}
    </div>
  );
}
