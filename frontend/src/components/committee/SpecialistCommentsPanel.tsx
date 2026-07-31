"use client";

import { MessagesSquare } from "lucide-react";
import type { SpecialistComment } from "@/lib/api";

export function SpecialistCommentsPanel({ comments }: { comments?: SpecialistComment[] | null }) {
  const items = comments || [];
  if (!items.length) return null;

  return (
    <div className="border-b border-border bg-white/35 p-4">
      <div className="mb-3 flex items-center gap-2">
        <MessagesSquare className="h-4 w-4 text-muted" aria-hidden />
        <div>
          <div className="label">Specialist comments</div>
          <div className="mt-1 text-[12px] text-muted">Auto specialists are comments on the case, not the headline verdict.</div>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {items.map((item) => (
          <div key={item.analyst_key} className="border-t border-border-soft pt-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-semibold text-navy">{item.analyst}</span>
              {item.focus && <span className="rounded border border-border bg-white px-1.5 py-0.5 text-[10px] text-muted">{item.focus}</span>}
              {typeof item.confidence === "number" && <span className="text-[10px] font-semibold text-amber">{Math.round(item.confidence * 100)}%</span>}
            </div>
            <ul className="mt-2 list-disc space-y-1 pl-4 text-[12px] leading-5 text-navy">
              {item.bullets.map((bullet) => (
                <li key={bullet}>{bullet}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
