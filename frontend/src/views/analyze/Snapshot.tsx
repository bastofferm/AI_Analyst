"use client";

// "While the committee works" — an instant statistical snapshot from the
// prices/kpis routers. US/JP only (the routers 422 on other jurisdictions), so
// we guess jurisdiction from the ticker shape and skip anything international.

import { useEffect, useMemo, useState } from "react";
import { api, type KpiResponse, type PricesResponse } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { isNum } from "@/components/charts/theme";
import { Sparkline, TrendArrow } from "@/components/charts/primitives";
import { PriceLineChart } from "@/components/charts/market";
import { InfoBox } from "@/components/ui/InfoBox";
import { Skeleton } from "@/components/ui/Skeleton";

/** US/JP guess from the ticker shape; null = don't call the routers (INTL). */
export function snapshotJurisdiction(ticker: string): "US" | "JP" | null {
  const tk = ticker.trim().toUpperCase();
  if (!tk) return null;
  if (!tk.includes(".")) return "US";
  const suffix = tk.split(".").pop() || "";
  if (suffix === "T") return "JP";
  if (suffix.length === 1) return "US"; // share classes like BRK.B
  return null; // .DE / .HK / … → INTL, routers would 422
}

const CHIP_ORDER = ["market_cap", "revenue_cagr_5y", "eps_growth", "ev_ebitda", "return_1y", "dividend_yield"];

export function SnapshotStrip({ ticker, runNonce }: { ticker: string; runNonce: number }) {
  const cv = useMoney();
  const [kpis, setKpis] = useState<KpiResponse | null>(null);
  const [prices, setPrices] = useState<PricesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const jur = snapshotJurisdiction(ticker);

  // Share prices arrive in home currency; convert the series when a display
  // currency is active so the chart agrees with the converted KPI chips.
  const displayPrices = useMemo(() => {
    if (!prices || !jur || !cv.active) return prices;
    return { ...prices, prices: prices.prices.map((p) => ({ ...p, close: cv.convert(p.close, jur) })) };
  }, [prices, jur, cv]);

  useEffect(() => {
    if (!runNonce || !jur) {
      setKpis(null);
      setPrices(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setKpis(null);
    setPrices(null);
    const tk = ticker.trim().toUpperCase();
    const year = new Date().getFullYear();
    Promise.allSettled([api.kpis(tk, jur, year - 6, year), api.prices(tk, jur)]).then(([k, p]) => {
      if (cancelled) return;
      if (k.status === "fulfilled") setKpis(k.value);
      if (p.status === "fulfilled" && p.value.prices?.length > 1) setPrices(p.value);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runNonce]);

  if (!jur) return null;
  const chips = kpis
    ? CHIP_ORDER.map((k) => ({ key: k, chip: kpis.chips?.[k] })).filter((c) => c.chip)
    : [];
  if (!loading && chips.length === 0 && !prices) return null;

  return (
    <section className="card p-5">
      <div className="label">While the committee works · {ticker.trim().toUpperCase()} at a glance</div>
      <div className="mt-3">
        <InfoBox copyKey="snapshot" />
      </div>

      {loading && chips.length === 0 ? (
        <div className="mt-4 grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-[74px]" />
          ))}
        </div>
      ) : chips.length > 0 ? (
        <div className="mt-4 grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
          {chips.map(({ key, chip }, i) => (
            <div
              key={key}
              className="rise-in hover-lift rounded-md border border-border-soft bg-white/60 px-3 py-2.5"
              style={{ animationDelay: `${i * 60}ms` }}
            >
              <div className="truncate text-[9px] uppercase tracking-[0.1em] text-muted">{chip!.label}</div>
              <div className="num mt-1 text-[17px] font-bold leading-none text-navy">
                {/* Market cap is the only absolute-money chip; re-format it client-side
                    from the raw value so the display-currency toggle applies. */}
                {key === "market_cap" && cv.active && isNum(chip!.value) ? cv.money(chip!.value, jur) : chip!.formatted}
              </div>
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <span className="text-[10px] text-muted">
                  <TrendArrow direction={chip!.delta_direction} />{" "}
                  {(chip!.delta_label || "").replace(/^[▲▼—–\s]+/, "")}
                </span>
                {chip!.series && chip!.series.length > 1 ? (
                  <Sparkline points={chip!.series.map((p) => p.value)} width={52} height={18} />
                ) : null}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {displayPrices ? (
        <div className="mt-4">
          <div className="mb-1 text-[10px] uppercase tracking-[0.1em] text-muted">
            Share price · last 5 years{cv.active ? ` · in ${cv.displayCode(jur)}` : ""}
          </div>
          <PriceLineChart points={displayPrices.prices} height={190} />
        </div>
      ) : null}
    </section>
  );
}
