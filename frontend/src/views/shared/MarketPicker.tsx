"use client";

// Shared two-tier market picker (US / JP / INTL regions → countries) used by
// Explore, Compare and Ideas. State lives in the parent; this only renders.

import { useEffect, useMemo, useState } from "react";
import { api, type Jurisdiction, type ScreenerMarketsResponse } from "@/lib/api";

export const SELECT_CLASS =
  "h-[32px] rounded-md border border-border bg-white px-2 text-[12px] text-navy outline-none focus:border-navy";

export type MarketSelection = { jur: Jurisdiction; region: string; countryCode: string };

export function useMarkets(): ScreenerMarketsResponse | null {
  const [markets, setMarkets] = useState<ScreenerMarketsResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .screenerMarkets()
      .then((m) => {
        if (!cancelled) setMarkets(m);
      })
      .catch(() => {
        /* picker falls back to a flat US/JP list */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return markets;
}

export function MarketPicker({
  value,
  markets,
  onChange,
  showCountry = true,
}: {
  value: MarketSelection;
  markets: ScreenerMarketsResponse | null;
  onChange: (next: MarketSelection) => void;
  showCountry?: boolean;
}) {
  const regions = useMemo(() => markets?.intl_regions ?? [], [markets]);
  const activeRegion = useMemo(
    () => regions.find((r) => r.region === value.region) || null,
    [regions, value.region]
  );

  return (
    <>
      <label className="flex flex-col gap-1">
        <span className="label">Market</span>
        <select
          value={value.jur === "INTL" && value.region ? `INTL:${value.region}` : value.jur}
          onChange={(e) => {
            const v = e.target.value;
            if (v === "US" || v === "JP") onChange({ jur: v, region: "", countryCode: "" });
            else if (v.startsWith("INTL:")) onChange({ jur: "INTL", region: v.slice(5), countryCode: "" });
            else onChange({ jur: "INTL", region: "", countryCode: "" });
          }}
          className={SELECT_CLASS}
        >
          {markets?.primary?.map((p) => (
            <option key={p.jurisdiction} value={p.jurisdiction}>
              {p.label} {p.count ? `(${p.count.toLocaleString()})` : ""}
            </option>
          )) || (
            <>
              <option value="US">United States</option>
              <option value="JP">Japan</option>
            </>
          )}
          {regions.map((r) => (
            <option key={`INTL:${r.region}`} value={`INTL:${r.region}`}>
              {r.region} ({r.total.toLocaleString()})
            </option>
          ))}
        </select>
      </label>

      {showCountry && value.jur === "INTL" && activeRegion ? (
        <label className="flex flex-col gap-1">
          <span className="label">Country</span>
          <select
            value={value.countryCode}
            onChange={(e) => onChange({ ...value, countryCode: e.target.value })}
            className={SELECT_CLASS}
          >
            <option value="">
              All of {activeRegion.region} ({activeRegion.total.toLocaleString()})
            </option>
            {activeRegion.countries.map((c) => (
              <option key={c.code} value={c.code}>
                {c.name} ({c.count.toLocaleString()})
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </>
  );
}
