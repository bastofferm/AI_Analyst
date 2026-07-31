"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

export function Collapsible({
  label,
  sublabel,
  defaultOpen = false,
  children,
  className,
}: {
  label: string;
  sublabel?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("card overflow-hidden", className)}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-5 py-3.5 text-left transition-colors hover:bg-white/60"
        aria-expanded={open}
      >
        <div>
          <div className="text-[13px] font-semibold text-navy">{label}</div>
          {sublabel ? <div className="mt-0.5 text-[11px] text-muted">{sublabel}</div> : null}
        </div>
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted transition-transform", open && "rotate-180")} />
      </button>
      {open ? <div className="border-t border-border-soft px-5 py-4">{children}</div> : null}
    </div>
  );
}
