"use client";

// The company "letterhead" — the MZQA terminal's IdentityPanel idiom
// (apps/terminal/src/components/identity-panel.tsx) rebuilt for the consumer app:
// logo, name, what it is, and the handful of facts that orient you before the
// committee says anything.
//
// Appears as soon as a ticker resolves, so the search box is no longer just a
// code: you can see which company you actually picked. Reuses `useCompanyData`,
// which is already cached and debounced, so this costs no extra request.

import { useCompanyData } from "./CompanyDataSection";
import { snapshotJurisdiction } from "./Snapshot";
import { useMoney } from "@/lib/currency";
import { signedPct } from "@/lib/fmt";
import { CompanyLogo } from "@/components/ui/CompanyLogo";

export function CompanyIdentityHeader({ ticker }: { ticker: string }) {
  const tk = ticker.trim().toUpperCase();
  const jur = snapshotJurisdiction(tk);
  const { data } = useCompanyData(tk);
  const cv = useMoney();

  // US/JP only — the company router 422s elsewhere, and INTL has no logo either.
  if (!tk || !jur || !data?.profile) return null;

  const p = data.profile;
  const price = data.price;
  const subtitle = [tk, p.exchange, p.sector, p.industry_group].filter(Boolean).join(" · ");

  // Where today's price sits inside the 52-week range — the single most
  // orienting number on a terminal company page.
  const lo = price?.low_52w;
  const hi = price?.high_52w;
  const last = price?.last;
  const band =
    typeof lo === "number" && typeof hi === "number" && typeof last === "number" && hi > lo
      ? Math.max(0, Math.min(1, (last - lo) / (hi - lo)))
      : null;

  const facts: { k: string; v: string; tone?: string }[] = [
    {
      k: "Last price",
      v: typeof last === "number" ? cv.money(last, jur) : "—",
    },
    {
      k: "1-year move",
      v: typeof price?.change_1y === "number" ? signedPct(price.change_1y) : "—",
      tone:
        typeof price?.change_1y === "number"
          ? price.change_1y >= 0
            ? "text-green"
            : "text-red"
          : undefined,
    },
    { k: "Market value", v: typeof p.market_cap === "number" ? cv.money(p.market_cap, jur) : "—" },
    { k: p.entity_id_label || "Filer id", v: p.entity_id || "—" },
    {
      k: "Filings",
      v: p.fy_min && p.fy_max ? `FY${p.fy_min}–FY${p.fy_max}` : "—",
    },
  ];

  return (
    <section className="rise-in card overflow-hidden p-0">
      {/* Letterhead band */}
      <div className="flex items-start gap-3.5 border-b border-border-soft px-5 py-4">
        <CompanyLogo logoId={p.logo_id} name={p.name} ticker={tk} size="lg" className="hover-lift" />
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-[19px] font-semibold leading-tight text-navy">{p.name}</h2>
          {p.name_local ? (
            <div className="truncate text-[11.5px] text-muted">{p.name_local}</div>
          ) : null}
          <div className="mt-0.5 truncate text-[11px] tracking-[0.03em] text-muted">{subtitle}</div>
        </div>
        <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[9.5px] font-semibold uppercase tracking-[0.1em] text-muted">
          {jur === "JP" ? "Japan" : "United States"}
        </span>
      </div>

      {/* 52-week position */}
      {band !== null ? (
        <div className="border-b border-border-soft px-5 py-3">
          <div className="flex items-baseline justify-between text-[9.5px] uppercase tracking-[0.1em] text-muted">
            <span>52-week low {cv.money(lo, jur)}</span>
            <span className="font-semibold text-navy/70">where it trades today</span>
            <span>high {cv.money(hi, jur)}</span>
          </div>
          <div className="relative mt-1.5 h-1.5 rounded-full bg-border-soft">
            <div
              className="absolute inset-y-0 left-0 rounded-full bg-navy/25 transition-[width] duration-700 ease-out"
              style={{ width: `${band * 100}%` }}
            />
            <span
              className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-navy shadow-[0_1px_4px_rgba(47,77,115,0.4)] transition-[left] duration-700 ease-out"
              style={{ left: `${band * 100}%` }}
              title={`${Math.round(band * 100)}% of the way up its 52-week range`}
            />
          </div>
        </div>
      ) : null}

      {/* Fact strip */}
      <div className="grid grid-cols-2 divide-x divide-border-soft sm:grid-cols-5">
        {facts.map((f, i) => (
          <div
            key={f.k}
            className="rise-in px-4 py-2.5"
            style={{ animationDelay: `${80 + i * 55}ms` }}
          >
            <div className="text-[8.5px] font-medium uppercase tracking-[0.14em] text-muted">{f.k}</div>
            <div className={`num mt-0.5 truncate text-[13px] font-semibold ${f.tone || "text-navy"}`} title={f.v}>
              {f.v}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
