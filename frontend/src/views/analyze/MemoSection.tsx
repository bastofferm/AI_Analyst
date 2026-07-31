"use client";

// The full committee memo, rendered as real markdown (finally) with an EN/DE toggle.

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CommitteeResponse } from "@/lib/api";
import { SectionCard } from "@/components/ui/SectionCard";

export function MemoSection({ result }: { result: CommitteeResponse }) {
  const en = result.memo?.en?.trim();
  const de = result.memo?.de?.trim();
  const [lang, setLang] = useState<"en" | "de">("en");
  if (!en && !de) return null;
  const active = lang === "de" && de ? de : en || de || "";

  return (
    <SectionCard
      eyebrow="Step 8"
      title="The full investment memo"
      copyKey="memo"
      actions={
        en && de ? (
          <div className="flex overflow-hidden rounded-md border border-border">
            {(["en", "de"] as const).map((l) => (
              <button
                key={l}
                onClick={() => setLang(l)}
                className={`px-3 py-1 text-[11px] font-semibold uppercase transition-colors ${
                  lang === l ? "bg-navy text-white" : "bg-white text-muted hover:text-navy"
                }`}
              >
                {l}
              </button>
            ))}
          </div>
        ) : undefined
      }
    >
      <div className="memo-prose max-w-3xl">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{active}</ReactMarkdown>
      </div>
    </SectionCard>
  );
}
