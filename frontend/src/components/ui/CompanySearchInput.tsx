"use client";

// Ticker box with name-search autocomplete, backed by /api/company/search
// (US + JP + INTL coverage universe). Typing a ticker still works exactly as
// before — Enter with the dropdown closed submits whatever was typed; picking
// a suggestion fills the resolved ticker into the box.

import { useEffect, useRef, useState } from "react";
import { api, type CompanySearchResult } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { CompanyLogo } from "./CompanyLogo";

const MIN_QUERY = 2;
const DEBOUNCE_MS = 250;

export function CompanySearchInput({
  value,
  onChange,
  onPick,
  onSubmit,
  placeholder = "MSFT or Microsoft",
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  /** A suggestion was chosen — receives the resolved ticker. */
  onPick: (r: CompanySearchResult) => void;
  /** Enter pressed with no suggestion highlighted (run whatever was typed). */
  onSubmit?: () => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const cv = useMoney();
  const [results, setResults] = useState<CompanySearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  // Suppresses the reopen that the value change from picking would trigger.
  const pickedRef = useRef<string | null>(null);
  const reqId = useRef(0);

  useEffect(() => {
    const q = value.trim();
    if (pickedRef.current === q) return;
    pickedRef.current = null;
    if (q.length < MIN_QUERY) {
      setResults([]);
      setOpen(false);
      setHighlight(-1);
      return;
    }
    const id = ++reqId.current;
    const t = setTimeout(() => {
      api
        .companySearch(q, 8)
        .then((res) => {
          if (id !== reqId.current) return;
          setResults(res.results);
          setOpen(res.results.length > 0);
          setHighlight(-1);
        })
        .catch(() => {
          if (id !== reqId.current) return;
          setResults([]);
          setOpen(false);
        });
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [value]);

  function pick(r: CompanySearchResult) {
    pickedRef.current = r.ticker;
    setOpen(false);
    setHighlight(-1);
    onChange(r.ticker);
    onPick(r);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (open && results.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((h) => (h + 1) % results.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((h) => (h <= 0 ? results.length - 1 : h - 1));
        return;
      }
      if (e.key === "Escape") {
        setOpen(false);
        setHighlight(-1);
        return;
      }
      if (e.key === "Enter" && highlight >= 0) {
        e.preventDefault();
        pick(results[highlight]);
        return;
      }
    }
    if (e.key === "Enter") {
      setOpen(false);
      onSubmit?.();
    }
  }

  return (
    <div className="relative">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => results.length > 0 && value.trim().length >= MIN_QUERY && setOpen(true)}
        onBlur={() => setOpen(false)}
        placeholder={placeholder}
        disabled={disabled}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-controls="company-search-listbox"
        className="h-[42px] w-72 rounded-md border border-border bg-white px-3 text-[15px] font-semibold text-navy outline-none focus:border-navy"
      />
      {open && results.length > 0 ? (
        <ul
          id="company-search-listbox"
          role="listbox"
          className="absolute left-0 top-full z-30 mt-1 w-[26rem] max-w-[80vw] overflow-hidden rounded-md border border-border bg-white shadow-lg"
        >
          {results.map((r, i) => (
            <li
              key={`${r.jurisdiction}:${r.ticker}`}
              role="option"
              aria-selected={i === highlight}
              // mousedown beats the input's blur so the pick registers
              onMouseDown={(e) => {
                e.preventDefault();
                pick(r);
              }}
              onMouseEnter={() => setHighlight(i)}
              className={`flex cursor-pointer items-center justify-between gap-3 px-3 py-2 text-[12px] ${
                i === highlight ? "bg-paper" : "bg-white"
              }`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <CompanyLogo logoId={r.logo_id} name={r.name} ticker={r.ticker} size="sm" />
                <div className="min-w-0">
                  <span className="num font-bold text-navy">{r.ticker}</span>
                  <span className="ml-2 truncate text-navy/80">{r.name}</span>
                  {r.sector ? <div className="truncate text-[10.5px] text-muted">{r.sector}</div> : null}
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-0.5">
                {/* Real listing country (FR, DE, NL…) rather than the INTL bucket —
                    "INTL" told the user nothing about where the name trades. The two
                    fields genuinely differ for cross-listed names (STMPA.PA lists in
                    FR, the company is Dutch), so the tooltip labels both. */}
                <span
                  className="rounded border border-border px-1 text-[9px] font-semibold uppercase tracking-[0.08em] text-muted"
                  title={
                    r.country_code
                      ? `Listed in ${r.country_code}${
                          r.country_name ? ` · company registered in ${r.country_name}` : ""
                        }`
                      : r.jurisdiction
                  }
                >
                  {r.country_code || r.jurisdiction}
                </span>
                {typeof r.market_cap === "number" ? (
                  <span className="num text-[10.5px] text-muted">{cv.money(r.market_cap, r.jurisdiction)}</span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
