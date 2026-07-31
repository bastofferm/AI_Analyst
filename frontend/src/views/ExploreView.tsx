"use client";

// Explore — the coverage-universe browser (port of the old BrowseTab logic with
// consumer chrome: info box, chips, skeleton rows, friendlier copy).

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Range, type IndustryOption, type ScreenerRow, type SectorOption } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { num, pct, signedTone } from "@/lib/fmt";
import { InfoBox } from "@/components/ui/InfoBox";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { EmptyState } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Skeleton";
import { HelpTip } from "@/components/ui/HelpTip";
import { MarketPicker, SELECT_CLASS, useMarkets, type MarketSelection } from "./shared/MarketPicker";

type Status = "idle" | "loading" | "error";

// How many names to pull per browse query. Server-capped at 500; 300 keeps the
// biggest constituents of any drill-down in view while the text box filters locally.
const BROWSE_LIMIT = 300;
// Collapses rapid market/sector switching into one query. Short enough that a
// single deliberate change still feels instant.
const BROWSE_DEBOUNCE_MS = 250;

// Size tiers on the USD scale the warehouse stores for US / INTL market cap.
// JP market cap is JPY-scaled, so the band filter is suppressed there.
type SizeBand = { key: string; label: string; min: number | null; max: number | null };
const SIZE_BANDS: SizeBand[] = [
  { key: "any", label: "Any size", min: null, max: null },
  { key: "mega", label: "Mega · ≥ $200B", min: 2e11, max: null },
  { key: "large", label: "Large · $10–200B", min: 1e10, max: 2e11 },
  { key: "mid", label: "Mid · $2–10B", min: 2e9, max: 1e10 },
  { key: "small", label: "Small · $300M–2B", min: 3e8, max: 2e9 },
  { key: "micro", label: "Micro · < $300M", min: null, max: 3e8 },
];

// Metric columns beyond name/sector. Every key except "name" reads straight
// from ScreenerRow.metrics — the screener always returns this display set.
type SortKey = "name" | "cap" | "pe" | "pb" | "ev_ebitda" | "fcf_yield" | "rev_yoy";
type SortDir = "asc" | "desc";
const METRIC_FOR_SORT: Record<Exclude<SortKey, "name">, string> = {
  cap: "market_cap_usd",
  pe: "pe",
  pb: "pb",
  ev_ebitda: "ev_ebitda",
  fcf_yield: "fcf_yield",
  rev_yoy: "rev_yoy",
};

export function ExploreView({ onAnalyze }: { onAnalyze?: (ticker: string) => void }) {
  const cv = useMoney();
  const markets = useMarkets();
  const [sel, setSel] = useState<MarketSelection>({ jur: "US", region: "", countryCode: "" });
  const [sectors, setSectors] = useState<SectorOption[]>([]);
  const [industries, setIndustries] = useState<IndustryOption[]>([]);
  const [sector, setSector] = useState("");
  const [industry, setIndustry] = useState("");
  const [sizeBand, setSizeBand] = useState("any");

  const [rows, setRows] = useState<ScreenerRow[]>([]);
  const [totalMatched, setTotalMatched] = useState(0);
  const [status, setStatus] = useState<Status>("idle");
  const [filterStatus, setFilterStatus] = useState<Status>("idle");
  const [error, setError] = useState("");

  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("cap");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Guards against out-of-order responses when filters change quickly.
  const reqId = useRef(0);

  const activeCountryCode = sel.jur === "INTL" && sel.countryCode ? sel.countryCode : null;
  const showSizeBands = sel.jur !== "JP";

  // Load sector / industry options whenever the market or country changes.
  useEffect(() => {
    let cancelled = false;
    setFilterStatus("loading");
    api
      .filters(sel.jur, activeCountryCode)
      .then((m) => {
        if (cancelled) return;
        setSectors(m.filters.sectors);
        setIndustries(m.filters.industries);
        setFilterStatus("idle");
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setFilterStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [sel.jur, activeCountryCode]);

  // Auto-browse: re-run the deterministic screener whenever the universe or the
  // size band changes; the list always lands ordered largest → smallest.
  //
  // Debounced because this query is expensive — a 300-row JP browse costs the
  // backend ~9s — and clicking through markets fires one per click. `reqId`
  // already discards superseded RESPONSES, but without the delay the server
  // still executes every abandoned query. The wait is short enough to be
  // invisible on a single deliberate change.
  useEffect(() => {
    const band = SIZE_BANDS.find((b) => b.key === sizeBand) || SIZE_BANDS[0];
    const capRange: Range | null =
      showSizeBands && (band.min !== null || band.max !== null) ? { min: band.min, max: band.max } : null;

    setStatus("loading");
    setError("");
    const timer = setTimeout(() => {
      const id = ++reqId.current;
      api
        .screenerRun({
          universe: {
            jurisdiction: sel.jur,
            country_code: activeCountryCode,
            region: sel.jur === "INTL" ? sel.region || null : null,
            sectors: !industry && sector ? [sector] : null,
            industries: industry ? [industry] : null,
          },
          filters: capRange ? { market_cap_usd: capRange } : {},
          sort: { key: "market_cap_usd", dir: "desc" },
          limit: BROWSE_LIMIT,
        })
        .then((res) => {
          if (id !== reqId.current) return; // superseded by a newer request
          setRows(res.rows);
          setTotalMatched(res.total_matched);
          setStatus("idle");
        })
        .catch((e) => {
          if (id !== reqId.current) return;
          setError(e instanceof Error ? e.message : "Browse query failed.");
          setStatus("error");
        });
    }, BROWSE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [sel.jur, sel.region, activeCountryCode, sector, industry, sizeBand, showSizeBands]);

  const sectorName = useMemo(() => {
    const m = new Map(sectors.map((s) => [s.code, s.name]));
    return (code: string | null) => (code ? m.get(code) || code : "—");
  }, [sectors]);

  const industryOptions = sector ? industries.filter((i) => i.sector_code === sector) : industries;

  const displayRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = q
      ? rows.filter((r) => r.ticker.toLowerCase().includes(q) || r.name.toLowerCase().includes(q))
      : rows;
    const sorted = [...filtered].sort((a, b) => {
      if (sortKey === "name") {
        return sortDir === "asc" ? a.name.localeCompare(b.name) : b.name.localeCompare(a.name);
      }
      const metric = METRIC_FOR_SORT[sortKey];
      const av = a.metrics[metric];
      const bv = b.metrics[metric];
      const an = typeof av !== "number" || !isFinite(av);
      const bn = typeof bv !== "number" || !isFinite(bv);
      if (an && bn) return 0;
      if (an) return 1; // nulls sink
      if (bn) return -1;
      return sortDir === "asc" ? (av as number) - (bv as number) : (bv as number) - (av as number);
    });
    return sorted;
  }, [rows, search, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
  }
  const sortArrow = (key: SortKey) => (sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "");

  const regions = markets?.intl_regions ?? [];
  const activeRegion = regions.find((r) => r.region === sel.region) || null;
  const marketLabel =
    sel.jur === "US" ? "United States" : sel.jur === "JP" ? "Japan" : activeRegion?.region || sel.region || "International";
  const countryLabel =
    sel.jur === "INTL" && sel.countryCode
      ? activeRegion?.countries.find((c) => c.code === sel.countryCode)?.name || sel.countryCode
      : null;
  const industryName = industry ? industryOptions.find((i) => i.code === industry)?.name || industry : null;
  const crumbs = [
    marketLabel,
    countryLabel,
    sector ? sectorName(sector) : "All sectors",
    industryName,
    showSizeBands ? SIZE_BANDS.find((b) => b.key === sizeBand)?.label : null,
  ].filter(Boolean) as string[];

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="label">Explore</div>
        <h1 className="mt-1 text-[22px] font-semibold text-navy">Find a company worth analyzing</h1>
      </div>
      <InfoBox copyKey="explore" />

      <section className="card p-5">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="label">Search</span>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Ticker or company name…"
              className="h-[32px] w-56 rounded-md border border-border bg-white px-2.5 text-[13px] text-navy outline-none focus:border-navy"
            />
          </label>
          <MarketPicker value={sel} markets={markets} onChange={(next) => { setSel(next); setSector(""); setIndustry(""); }} />
          <label className="flex flex-col gap-1">
            <span className="label">
              <HelpTip term="GICS">Sector</HelpTip>
            </span>
            <select
              value={sector}
              onChange={(e) => {
                setSector(e.target.value);
                setIndustry("");
              }}
              className={`${SELECT_CLASS} w-56`}
              disabled={filterStatus === "loading"}
            >
              <option value="">{filterStatus === "loading" ? "Loading…" : "All sectors"}</option>
              {sectors.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">Industry group</span>
            <select value={industry} onChange={(e) => setIndustry(e.target.value)} className={`${SELECT_CLASS} w-56`} disabled={filterStatus === "loading"}>
              <option value="">{sector ? "All in sector" : "All industries"}</option>
              {industryOptions.map((i) => (
                <option key={i.code} value={i.code}>
                  {i.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {showSizeBands ? (
          <div className="mt-3.5 flex flex-wrap items-center gap-1.5">
            <span className="label mr-1">
              <HelpTip term="market cap">Company size</HelpTip>
            </span>
            {SIZE_BANDS.map((b) => (
              <button
                key={b.key}
                onClick={() => setSizeBand(b.key)}
                className={
                  "rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors " +
                  (sizeBand === b.key
                    ? "border-navy bg-navy text-white"
                    : "border-border bg-white text-muted hover:border-navy hover:text-navy")
                }
              >
                {b.label}
              </button>
            ))}
          </div>
        ) : (
          <div className="mt-3 text-[11px] text-muted">
            Japanese market cap is stored in yen, so size tiers are hidden — the list is still ordered largest → smallest.
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-1 text-[11px] text-navy">
          {crumbs.map((c, i) => (
            <span key={`${c}-${i}`} className="flex items-center gap-1">
              {i > 0 && <span className="text-muted">›</span>}
              <span className={i === crumbs.length - 1 ? "font-semibold" : ""}>{c}</span>
            </span>
          ))}
        </div>

        {status === "error" && error && (
          <div className="mt-3 rounded border border-red/40 bg-red/5 p-2 text-[12px] text-red">{error}</div>
        )}
      </section>

      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border p-3.5">
          <div className="text-[12px] text-navy">
            {status === "loading" ? (
              <span className="flex items-center gap-2 text-muted">
                <span className="h-2 w-2 animate-pulse rounded-full bg-navy" /> Loading companies…
              </span>
            ) : (
              <>
                <span className="num font-semibold">{displayRows.length.toLocaleString()}</span>
                <span className="text-muted">
                  {" "}
                  {search.trim() ? "matching" : "shown"}
                  {totalMatched > rows.length && !search.trim()
                    ? ` · top ${rows.length.toLocaleString()} of ${totalMatched.toLocaleString()} by size`
                    : totalMatched
                      ? ` of ${totalMatched.toLocaleString()} in this universe`
                      : ""}
                </span>
              </>
            )}
          </div>
          {totalMatched > rows.length && !search.trim() && (
            <div className="text-[11px] text-muted">Narrow by sector, industry or size to see the full set.</div>
          )}
        </div>

        {status === "loading" && rows.length === 0 ? (
          <div className="p-4">
            <SkeletonRows rows={8} />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.08em] text-muted">
                  <th className="px-3 py-2.5 text-left">#</th>
                  <th className="cursor-pointer px-3 py-2.5 text-left hover:text-navy" onClick={() => toggleSort("name")}>
                    Company{sortArrow("name")}
                  </th>
                  <th className="px-3 py-2.5 text-left">Sector</th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("cap")}>
                    Market cap{sortArrow("cap")}
                  </th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("pe")} title="Price ÷ one year's earnings — how many years of profit you pay">
                    P/E{sortArrow("pe")}
                  </th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("pb")} title="Price relative to accounting net worth per share">
                    P/B{sortArrow("pb")}
                  </th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("ev_ebitda")} title="Whole-business price tag vs raw operating profit — lower is cheaper">
                    EV/EBITDA{sortArrow("ev_ebitda")}
                  </th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("fcf_yield")} title="Spare cash generated per year as a % of the company's price">
                    FCF yld{sortArrow("fcf_yield")}
                  </th>
                  <th className="cursor-pointer px-3 py-2.5 text-right hover:text-navy" onClick={() => toggleSort("rev_yoy")} title="Revenue growth vs a year earlier">
                    Rev YoY{sortArrow("rev_yoy")}
                  </th>
                  <th className="px-3 py-2.5 text-right" />
                </tr>
              </thead>
              <tbody>
                {displayRows.map((r, i) => (
                  <tr key={r.ticker} className="border-t border-border-soft align-middle hover:bg-paper/60">
                    <td className="num px-3 py-2.5 text-muted">{i + 1}</td>
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-2.5">
                        <BrandLogo ticker={r.ticker} name={r.name} logoId={r.logo_id} />
                        <div className="min-w-0">
                          <div className="font-semibold text-navy">{r.ticker}</div>
                          <div className="truncate text-[11px] text-muted">{r.name}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-[11px] text-navy">{sectorName(r.sector)}</td>
                    <td className="num px-3 py-2.5 text-right font-medium text-navy">
                      {/* keyed to the ROW's jurisdiction, not the picker — rows from the
                          previous market linger while a new universe loads */}
                      {cv.money(r.metrics.market_cap_usd, r.jurisdiction)}
                    </td>
                    <td className="num px-3 py-2.5 text-right">{num(r.metrics.pe, 1)}</td>
                    <td className="num px-3 py-2.5 text-right">{num(r.metrics.pb, 1)}</td>
                    <td className="num px-3 py-2.5 text-right">{num(r.metrics.ev_ebitda, 1)}</td>
                    <td className={`num px-3 py-2.5 text-right ${signedTone(r.metrics.fcf_yield)}`}>
                      {pct(r.metrics.fcf_yield)}
                    </td>
                    <td className={`num px-3 py-2.5 text-right ${signedTone(r.metrics.rev_yoy)}`}>
                      {pct(r.metrics.rev_yoy)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <button
                        onClick={() => onAnalyze?.(r.ticker)}
                        className="rounded border border-navy px-2.5 py-1 text-[11px] font-semibold text-navy transition-colors hover:bg-navy hover:text-white"
                        title="Send to the committee for a full analysis"
                      >
                        Analyze →
                      </button>
                    </td>
                  </tr>
                ))}
                {status !== "loading" && displayRows.length === 0 && (
                  <tr>
                    <td colSpan={10} className="px-3 py-6">
                      <EmptyState
                        title={search.trim() ? "No companies in the loaded set match that search." : "No companies found for this universe."}
                        hint={search.trim() ? "Try a shorter search or widen the filters." : "Try another sector or size tier."}
                      />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
