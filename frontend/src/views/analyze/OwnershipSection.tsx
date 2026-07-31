"use client";

// Who owns it — 13F institutional holders with add/reduce coloring.

import type { CommitteeResponse } from "@/lib/api";
import { OwnershipBars } from "@/components/charts/valuation";
import { isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";
import { HelpTip } from "@/components/ui/HelpTip";

export function OwnershipSection({ result }: { result: CommitteeResponse }) {
  const own = result.ownership;
  if (!own?.available || !(own.top_holders || []).length) return null;
  const dir = (own.net_direction || "").toLowerCase();

  return (
    <SectionCard eyebrow="Step 7" title="Who owns this stock?" copyKey="ownership">
      <OwnershipBars ownership={own} />
      <p className="mt-3 text-[11.5px] leading-relaxed text-muted">
        {isNum(own.holder_count) ? (
          <>
            <b className="num text-navy">{own.holder_count}</b> professional managers report a position
            {own.quarter ? <span> (as of {own.quarter})</span> : null}.{" "}
          </>
        ) : null}
        {dir ? (
          <>
            On balance they were{" "}
            <b className={dir.includes("add") || dir.includes("buy") ? "text-green" : dir.includes("reduc") || dir.includes("sell") ? "text-red" : "text-navy"}>
              {own.net_direction}
            </b>{" "}
            last quarter.{" "}
          </>
        ) : null}
        {isNum(own.passive_share_of_reported_pct) ? (
          <>
            <b className="num text-navy">{own.passive_share_of_reported_pct.toFixed(0)}%</b> of the reported value sits in
            passive index funds — money that holds regardless of the story. Data from{" "}
            <HelpTip term="13F">13F filings</HelpTip>, which arrive with a delay.
          </>
        ) : null}
      </p>
    </SectionCard>
  );
}
