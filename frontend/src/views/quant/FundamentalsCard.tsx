"use client";

// The Quant holdings "Details" drawer — the same deterministic warehouse panel the
// Analyze view shows ("The data behind {ticker}"): profile, highlight chips, 52-week
// range, and the full statement + ratio history. Reuses CompanyDataBody in compact
// mode. No logo here on purpose — the table row already shows one (avoid a second).

import { useEffect, useState } from "react";
import { api, type CompanyDataResponse, type Jurisdiction } from "@/lib/api";
import { CompanyDataBody } from "@/views/analyze/CompanyDataSection";

export function FundamentalsCard({ ticker, jurisdiction }: { ticker: string; jurisdiction: Jurisdiction }) {
  const [data, setData] = useState<CompanyDataResponse | null>(null);
  const [status, setStatus] = useState<"loading" | "done" | "error">("loading");
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    setErr("");
    // companyData is US/JP only (the desk never selects INTL). JP names are keyed by
    // their .T-suffixed primary_ticker, but the table carries the bare code.
    const jur = jurisdiction === "JP" ? "JP" : "US";
    const wt = jur === "JP" && !/\.T$/i.test(ticker) ? `${ticker}.T` : ticker;
    api
      .companyData(wt, jur)
      .then((d) => { if (!cancelled) { setData(d); setStatus("done"); } })
      .catch((e) => { if (!cancelled) { setErr(e instanceof Error ? e.message : String(e)); setStatus("error"); } });
    return () => { cancelled = true; };
  }, [ticker, jurisdiction]);

  if (status === "loading") {
    return <div className="animate-pulse py-6 text-center text-[12px] text-muted">Loading the data behind {ticker}…</div>;
  }
  if (status === "error" || !data) {
    return <div className="py-4 text-[12px] text-red-700">Couldn’t load the data behind {ticker}. {err}</div>;
  }

  return (
    <div className="rounded-lg border border-border-soft bg-paper/40 p-4">
      <div className="mb-2 text-[12px] font-semibold text-navy">
        The data behind {data.ticker} — {data.profile.name}
      </div>
      <CompanyDataBody data={data} compact />
    </div>
  );
}
