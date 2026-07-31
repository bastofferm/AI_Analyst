"use client";

// Hover popout for the Home sector cards — a port of the MZQA terminal's
// SectorConstituentsPopout (apps/terminal/src/components/sector-constituents-popout.tsx)
// into this app's tokens and plain-English labels.
//
// Shows the biggest companies inside the hovered sector with their weight, 1D/1W/1M
// move and P/E, then an "Everything else" rollup and the sector total. Fixed-position
// and pointer-events:none, exactly like the terminal — it follows the hovered card
// and never steals the mouse.

import type { SectorConstituentRow, SectorConstituentsResponse } from "@/lib/api";

const COLS = "20px 58px 1fr 76px 58px 52px 52px 52px 48px";

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const p = v * 100;
  return `${p > 0 ? "+" : ""}${p.toFixed(2)}%`;
}

function fmtMoney(v: number | null | undefined, ccy: string): string {
  if (v == null || v <= 0) return "—";
  if (v >= 1e12) return `${ccy}${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9) return `${ccy}${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${ccy}${(v / 1e6).toFixed(1)}M`;
  return `${ccy}${v.toFixed(0)}`;
}

function fmtPe(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "—" : `${v.toFixed(1)}×`;
}

function fmtWeight(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

function moveClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "text-muted/60";
  if (v > 0) return "text-green";
  if (v < 0) return "text-red";
  return "text-muted";
}

export function SectorConstituentsPopout({
  data,
  loading,
  x,
  y,
  jurisdiction = "US",
}: {
  data: SectorConstituentsResponse | null;
  loading: boolean;
  x: number;              // viewport coords, left edge
  y: number;              // viewport coords, top edge
  jurisdiction?: "US" | "JP";
}) {
  const ccy = jurisdiction === "JP" ? "¥" : "$";

  const row = (r: SectorConstituentRow, opts: { rank?: number; rollup?: boolean } = {}) => {
    const { rank, rollup = false } = opts;
    return (
      <div
        key={`${r.ticker ?? r.name}-${rank ?? "rollup"}`}
        className={`num grid items-center gap-1.5 py-[3px] text-[10.5px] ${
          rollup ? "mt-0.5 border-t border-border font-semibold text-navy" : "text-navy/85"
        }`}
        style={{ gridTemplateColumns: COLS }}
      >
        <span className="text-right text-muted/70">{rank ?? ""}</span>
        <span className="font-semibold text-navy">{r.ticker ?? ""}</span>
        <span className="truncate font-sans" title={r.name}>
          {r.name}
        </span>
        <span className="text-right">{fmtMoney(r.market_cap, ccy)}</span>
        <span className="text-right text-muted">{fmtWeight(r.weight_pct)}</span>
        <span className={`text-right ${moveClass(r.ret_1d)}`}>{fmtPct(r.ret_1d)}</span>
        <span className={`text-right ${moveClass(r.ret_1w)}`}>{fmtPct(r.ret_1w)}</span>
        <span className={`text-right ${moveClass(r.ret_1m)}`}>{fmtPct(r.ret_1m)}</span>
        <span className="text-right text-muted">{fmtPe(r.pe_ratio)}</span>
      </div>
    );
  };

  return (
    <div
      role="tooltip"
      className="pointer-events-none fixed z-[60] w-[660px] rounded-lg border border-navy/20 bg-panel p-3.5 shadow-[0_10px_30px_rgba(47,77,115,0.20)]"
      style={{ left: x, top: y, animation: "filing-popout-in 0.16s ease-out" }}
    >
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <span className="text-[12.5px] font-semibold text-navy">{data?.gics_name ?? "Sector"}</span>
        <span className="text-[10px] text-muted">
          {data
            ? `${data.n_tickers} companies · ${fmtMoney(data.total_market_cap, ccy)} combined value`
            : loading
              ? "Loading…"
              : ""}
        </span>
      </div>

      <div
        className="grid items-center gap-1.5 border-b border-border pb-1 text-[8.5px] font-medium uppercase tracking-[0.08em] text-muted"
        style={{ gridTemplateColumns: COLS }}
      >
        <span className="text-right">#</span>
        <span>Ticker</span>
        <span>Company</span>
        <span className="text-right">Size</span>
        <span className="text-right">Weight</span>
        <span className="text-right">1D</span>
        <span className="text-right">1W</span>
        <span className="text-right">1M</span>
        <span className="text-right">P/E</span>
      </div>

      {loading && !data ? (
        <div className="py-3 text-center text-[11px] italic text-muted">Loading the biggest names…</div>
      ) : data && data.top.length > 0 ? (
        <>
          {data.top.map((r, i) => row(r, { rank: i + 1 }))}
          {data.other ? row({ ...data.other, name: "Everything else" }, { rollup: true }) : null}
          {row({ ...data.total, name: "Sector total" }, { rollup: true })}
        </>
      ) : (
        <div className="py-3 text-center text-[11px] italic text-muted">No company data for this sector.</div>
      )}

      <div className="mt-1.5 flex items-baseline justify-between gap-3 text-[9px] text-muted/70">
        <span>
          {data?.prices_as_of ? (
            <>
              Moves to{" "}
              <b className="font-semibold text-navy/60">
                {new Date(data.prices_as_of).toLocaleDateString(undefined, {
                  day: "numeric",
                  month: "short",
                  year: "numeric",
                })}
              </b>{" "}
              — the price feed&apos;s latest close
            </>
          ) : null}
        </span>
        <span>Biggest companies by market value · weight is each company&apos;s share of the sector</span>
      </div>
    </div>
  );
}
