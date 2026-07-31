"use client";

// Company logo with an initials fallback — ported from the MZQA terminal's
// logo-with-fallback.tsx.
//
// Images come from the shared MZQA logo library, served by the backend at
// /logos/{id} (US files are named by zero-padded CIK, JP by EDINET code).
// Coverage is uneven — ~94% of US names have one, but only ~14% of JP and none
// of INTL — so the fallback is the common path for anything outside the US, not
// an edge case. It renders a navy initials tile that looks deliberate rather
// than broken.

import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/api";

const SIZES = {
  sm: { box: "h-6 w-6 rounded", text: "text-[9px]" },
  md: { box: "h-9 w-9 rounded-md", text: "text-[12px]" },
  lg: { box: "h-14 w-14 rounded-lg", text: "text-[17px]" },
} as const;

function initials(name: string, ticker: string): string {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (ticker || "?").slice(0, 2).toUpperCase();
}

export function CompanyLogo({
  logoId,
  name,
  ticker,
  size = "md",
  className = "",
}: {
  /** CIK (zero-padded) or EDINET code; null when we have no image. */
  logoId?: string | null;
  name: string;
  ticker: string;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  // A new company must get a fresh attempt — otherwise one 404 would poison
  // every subsequent logo rendered by this component instance.
  useEffect(() => setFailed(false), [logoId]);

  const s = SIZES[size];

  if (!logoId || failed) {
    return (
      <div
        className={`flex shrink-0 items-center justify-center bg-navy font-semibold text-bg ${s.box} ${s.text} ${className}`}
        aria-hidden="true"
        title={name}
      >
        {initials(name, ticker)}
      </div>
    );
  }

  return (
    <img
      src={`${API_BASE}/logos/${encodeURIComponent(logoId)}`}
      alt=""
      aria-hidden="true"
      // Deliberately NOT loading="lazy": all five views stay mounted and the
      // inactive ones are display:none, where the browser defers a lazy image
      // forever. onError would then never fire, so a company with no logo file
      // (most JP names) rendered an empty box instead of its initials tile.
      // These are a few KB each — there is nothing to defer.
      onError={() => setFailed(true)}
      className={`shrink-0 border border-border-soft bg-white object-contain p-0.5 ${s.box} ${className}`}
    />
  );
}
