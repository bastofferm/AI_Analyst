"use client";

// The standard section wrapper: card + uppercase eyebrow + title + a toggleable
// "What is this?" InfoBox from the copy deck + optional right-side actions.

import { useState } from "react";
import { HelpCircle } from "lucide-react";
import type { SectionCopyKey } from "@/lib/copy";
import { cn } from "@/lib/cn";
import { InfoBox } from "./InfoBox";

export function SectionCard({
  eyebrow,
  title,
  copyKey,
  defaultInfoOpen = false,
  actions,
  children,
  className,
  id,
}: {
  eyebrow?: string;
  title: string;
  copyKey?: SectionCopyKey;
  defaultInfoOpen?: boolean;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  id?: string;
}) {
  const [infoOpen, setInfoOpen] = useState(defaultInfoOpen);
  return (
    <section id={id} className={cn("card p-5", className)}>
      <div className="flex items-start justify-between gap-3">
        <div>
          {eyebrow ? <div className="label">{eyebrow}</div> : null}
          <h2 className="mt-0.5 flex items-center gap-2 text-[16px] font-semibold text-navy">
            {title}
            {copyKey ? (
              <button
                onClick={() => setInfoOpen((v) => !v)}
                className={cn(
                  "rounded-full p-0.5 transition-colors",
                  infoOpen ? "text-navy" : "text-navy-3 hover:text-navy"
                )}
                aria-label="What is this section?"
                aria-expanded={infoOpen}
                title="What is this?"
              >
                <HelpCircle className="h-4 w-4" />
              </button>
            ) : null}
          </h2>
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      {copyKey && infoOpen ? <InfoBox copyKey={copyKey} className="mt-3" /> : null}
      <div className="mt-4">{children}</div>
    </section>
  );
}
