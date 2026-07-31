"use client";

// Brand logo for the Explore universe browser — shows a company's real logo so
// the list reads as recognizable names, not just tickers ("infotainment"),
// falling back to a navy initials tile (styled like CompanyLogo) when none is
// available.
//
// Unlike CompanyLogo — which serves the internal /logos/{CIK} asset library that
// isn't bundled with this standalone repo — this keys off the plain ticker that
// every screener row already carries, via Parqet's public symbol-logo CDN (no
// API key). It's the only external asset the app loads. Unknown symbols (most JP
// numeric tickers, some INTL names) return 404 -> onError -> the initials tile,
// so a miss looks deliberate rather than broken.

import { useEffect, useState } from "react";

function initials(name: string, ticker: string): string {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (ticker || "?").slice(0, 2).toUpperCase();
}

function logoUrl(ticker: string): string {
  return `https://assets.parqet.com/logos/symbol/${encodeURIComponent(ticker.trim())}`;
}

export function BrandLogo({
  ticker,
  name,
  className = "",
}: {
  ticker: string;
  name: string;
  className?: string;
}) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  // A new ticker gets a fresh attempt — otherwise one 404 would poison every
  // subsequent logo rendered by this component instance (rows reuse instances).
  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [ticker]);

  return (
    <div className={`relative h-8 w-8 shrink-0 overflow-hidden rounded-md ${className}`} title={name}>
      {/* Initials tile — always underneath; the only thing visible until/unless the logo paints. */}
      <div
        className="absolute inset-0 flex items-center justify-center bg-navy text-[10px] font-semibold text-bg"
        aria-hidden="true"
      >
        {initials(name, ticker)}
      </div>
      {!failed && (
        <img
          src={logoUrl(ticker)}
          alt=""
          aria-hidden="true"
          // Not lazy: all views stay mounted (display:none when inactive), where a
          // lazy image never loads and onError never fires — same reasoning as
          // CompanyLogo. These are a few KB each.
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
          className={`absolute inset-0 h-full w-full border border-border-soft bg-white object-contain p-0.5 transition-opacity ${
            loaded ? "opacity-100" : "opacity-0"
          }`}
        />
      )}
    </div>
  );
}
