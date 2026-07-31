"use client";

// Compact nav chip summarizing running committee work; click jumps to that view.

import type { CommitteeActivity } from "../committee/activity";

export type NamedActivity = { view: string; viewLabel: string; activity: CommitteeActivity };

export function RunStatusChip({
  items,
  onJump,
}: {
  items: NamedActivity[];
  onJump: (view: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="flex items-center gap-1.5">
      {items.map((it) => (
        <button
          key={it.view}
          onClick={() => onJump(it.view)}
          className="flex items-center gap-1.5 rounded-full border border-navy/25 bg-white px-2.5 py-1 text-[10.5px] font-semibold text-navy transition-colors hover:bg-navy/5"
          title={`${it.activity.label}${it.activity.detail ? ` — ${it.activity.detail}` : ""} (click to view)`}
        >
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-navy opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-navy" />
          </span>
          {it.viewLabel}
        </button>
      ))}
    </div>
  );
}
