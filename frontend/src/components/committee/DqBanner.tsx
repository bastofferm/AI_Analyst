"use client";

import type { DqWarning } from "@/lib/api";

/** Advisory (amber) or blocking (red) data-governance banner. In advisory mode the
 *  committee ran anyway; in blocking mode the strict gate stopped it. */
export function DqBanner({ warning, blocked, ticker }: { warning: DqWarning; blocked?: boolean; ticker?: string }) {
  const errs = Array.from(new Set(warning.dq_errors || [])).slice(0, 15);
  return (
    <div className={`rounded border p-3 ${blocked ? "border-red/40 bg-red/5" : "border-amber/50 bg-amber/10"}`}>
      <div className="text-[12px] font-semibold" style={{ color: blocked ? "#DC2626" : "#B45309" }}>
        {blocked
          ? "Data-governance gate — committee did not run"
          : "Data-quality advisory — committee ran on data that failed an accounting identity"}
        {ticker ? ` · ${ticker.toUpperCase()}` : ""}
      </div>
      <div className="mt-1 flex flex-wrap gap-4 text-[11px] text-muted">
        <span>
          Data complete:{" "}
          <b style={{ color: warning.is_data_complete ? "#16A34A" : "#DC2626" }}>
            {warning.is_data_complete ? "yes" : "no"}
          </b>
        </span>
        <span>
          DQ passed:{" "}
          <b style={{ color: warning.is_dq_passed ? "#16A34A" : "#DC2626" }}>
            {warning.is_dq_passed ? "yes" : "no"}
          </b>
        </span>
        {!blocked && <span>Treat the valuation as lower-confidence.</span>}
      </div>
      {errs.length > 0 && (
        <ul className="mt-2 max-h-40 list-disc overflow-auto rounded border border-border-soft bg-white/60 p-2 pl-5 text-[11px] text-navy">
          {errs.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
