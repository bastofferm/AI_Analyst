"use client";

// Inline glossary tooltip: renders its children with a dotted underline and shows
// the plain-English GLOSSARY definition on hover / keyboard focus.

import { GLOSSARY } from "@/lib/copy";

function lookup(term: string): string | null {
  if (GLOSSARY[term]) return GLOSSARY[term];
  const lower = term.toLowerCase();
  for (const [k, v] of Object.entries(GLOSSARY)) {
    if (k.toLowerCase() === lower) return v;
  }
  return null;
}

export function HelpTip({ term, children }: { term: string; children?: React.ReactNode }) {
  const def = lookup(term);
  const label = children ?? term;
  if (!def) return <>{label}</>;
  return (
    <span className="group relative inline-block" tabIndex={0}>
      <span className="cursor-help border-b border-dotted border-navy-3/70">{label}</span>
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute bottom-full left-1/2 z-50 mb-2 w-72 -translate-x-1/2 rounded-md border border-border bg-white px-3 py-2.5 text-left text-[11px] font-normal normal-case leading-relaxed tracking-normal text-navy opacity-0 shadow-[0_6px_18px_rgba(47,77,115,0.14)] transition-opacity duration-100 group-hover:visible group-hover:opacity-100 group-focus:visible group-focus:opacity-100"
      >
        <span className="mb-0.5 block text-[10px] font-semibold uppercase tracking-[0.08em] text-navy-3">
          {term}
        </span>
        {def}
      </span>
    </span>
  );
}
