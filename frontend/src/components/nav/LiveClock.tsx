"use client";

// Terminal-signature live clock: HH:MM:SS · WED 17 JUL 2026 (mono, tabular).

import { useEffect, useState } from "react";

const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function fmt(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} · ${DAYS[d.getDay()]} ${p(
    d.getDate()
  )} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export function LiveClock() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span
      suppressHydrationWarning
      className="num hidden text-[11px] tracking-[0.1em] text-muted md:inline"
    >
      {now ? fmt(now) : ""}
    </span>
  );
}
