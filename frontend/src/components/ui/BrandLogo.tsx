"use client";

// Brand logo for the Explore universe browser — shows a company's real logo so
// the list reads as recognizable names, not just tickers ("infotainment"),
// falling back to a navy initials tile when none is available.
//
// Sources are tried in order, degrading cleanly on each 404:
//   1. Internal /logos/{CIK|EDINET} — the shared MZQA logo library (on-brand,
//      self-contained where the library is mounted; US ~94% / JP ~14% coverage,
//      no INTL). Only tried when the screener row carries a logo_id.
//   2. External Parqet symbol CDN, keyed by ticker — covers INTL names and any
//      checkout without the internal library mounted (no API key).
//   3. Navy initials tile.
// So a fresh clone with no logo library still shows logos via (2), and INTL
// rows (no logo_id) go straight to (2); a miss on both lands on the tile.

import { useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/api";

function initials(name: string, ticker: string): string {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (ticker || "?").slice(0, 2).toUpperCase();
}

export function BrandLogo({
  ticker,
  name,
  logoId,
  className = "",
}: {
  ticker: string;
  name: string;
  /** CIK (US) / EDINET (JP) stem for the internal /logos endpoint; omit for INTL. */
  logoId?: string | null;
  className?: string;
}) {
  const sources = useMemo(() => {
    const out: string[] = [];
    if (logoId) out.push(`${API_BASE}/logos/${encodeURIComponent(logoId)}`);
    if (ticker.trim()) out.push(`https://assets.parqet.com/logos/symbol/${encodeURIComponent(ticker.trim())}`);
    return out;
  }, [logoId, ticker]);

  const [idx, setIdx] = useState(0);
  const [loaded, setLoaded] = useState(false);
  // A new company gets a fresh attempt at the whole source cascade — otherwise a
  // prior 404 would poison the next row rendered by this component instance.
  useEffect(() => {
    setIdx(0);
    setLoaded(false);
  }, [logoId, ticker]);

  const src = idx < sources.length ? sources[idx] : null;

  return (
    <div className={`relative h-8 w-8 shrink-0 overflow-hidden rounded-md ${className}`} title={name}>
      {/* Initials tile — always underneath; the only thing visible until/unless a logo paints. */}
      <div
        className="absolute inset-0 flex items-center justify-center bg-navy text-[10px] font-semibold text-bg"
        aria-hidden="true"
      >
        {initials(name, ticker)}
      </div>
      {src && (
        <img
          key={src}
          src={src}
          alt=""
          aria-hidden="true"
          // Not lazy: all views stay mounted (display:none when inactive), where a
          // lazy image never loads and onError never fires — same reasoning as
          // CompanyLogo. These are a few KB each.
          onLoad={() => setLoaded(true)}
          onError={() => {
            setLoaded(false);
            setIdx((i) => i + 1); // fall through to the next source (or the tile)
          }}
          className={`absolute inset-0 h-full w-full border border-border-soft bg-white object-contain p-0.5 transition-opacity ${
            loaded ? "opacity-100" : "opacity-0"
          }`}
        />
      )}
    </div>
  );
}
