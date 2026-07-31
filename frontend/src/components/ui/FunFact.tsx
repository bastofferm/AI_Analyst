"use client";

// Rotating "market wisdom" card for the long committee waits. Cycles through
// the facts deck on a slow timer with a soft crossfade; the deck position is
// seeded from the run start so back-to-back runs open on different facts.

import { useEffect, useState } from "react";
import { Lightbulb } from "lucide-react";
import { MARKET_FACTS, factStartIndex } from "@/lib/facts";

const ROTATE_MS = 12_000;

export function FunFactCard({ seed = 0 }: { seed?: number }) {
  const [idx, setIdx] = useState(() => factStartIndex(seed));

  useEffect(() => {
    const t = setInterval(() => setIdx((i) => (i + 1) % MARKET_FACTS.length), ROTATE_MS);
    return () => clearInterval(t);
  }, []);

  const fact = MARKET_FACTS[idx];

  return (
    <div className="mt-4 rounded-md border border-border-soft bg-paper/60 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[9.5px] font-semibold uppercase tracking-[0.12em] text-navy-3">
          <Lightbulb className="h-3 w-3" aria-hidden="true" />
          While you wait · {fact.kind}
        </div>
        <button
          onClick={() => setIdx((i) => (i + 1) % MARKET_FACTS.length)}
          className="text-[10px] font-medium text-muted transition-colors hover:text-navy"
          aria-label="Show the next fact"
        >
          next →
        </button>
      </div>
      <p key={idx} className="fact-swap mt-1.5 text-[12px] leading-relaxed text-navy">
        {fact.text}
      </p>
    </div>
  );
}
