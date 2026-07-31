"use client";

// The "What is this?" explainer — the signature consumer element of the app.
// Every major section renders one, fed from the central copy deck (lib/copy.ts).

import { Info } from "lucide-react";
import { SECTION_COPY, type SectionCopyKey } from "@/lib/copy";
import { cn } from "@/lib/cn";

export function InfoBox({
  copyKey,
  title,
  children,
  className,
}: {
  copyKey?: SectionCopyKey;
  title?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  const copy = copyKey ? SECTION_COPY[copyKey] : null;
  const heading = title ?? copy?.title;
  const body = children ?? copy?.body;
  if (!heading && !body) return null;
  return (
    <div
      className={cn(
        "flex gap-2.5 rounded-md border border-navy/15 bg-navy/[0.04] px-3.5 py-3",
        className
      )}
    >
      <Info className="mt-0.5 h-4 w-4 shrink-0 text-navy-3" aria-hidden="true" />
      <div>
        {heading ? <div className="text-[12px] font-semibold text-navy">{heading}</div> : null}
        {body ? <div className="mt-0.5 text-[12px] leading-relaxed text-muted">{body}</div> : null}
      </div>
    </div>
  );
}
