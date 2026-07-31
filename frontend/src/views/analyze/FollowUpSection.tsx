"use client";

// "Ask the committee" — consumer wrapper around the existing revision-iteration
// panel (unchanged behavior: frozen fact base, template chips, revision history).

import type { CommitteeResponse } from "@/lib/api";
import { CommitteeIterationPanel } from "@/components/committee/CommitteeIterationPanel";
import { SectionCard } from "@/components/ui/SectionCard";

import { llmBody, type LlmSelection } from "@/lib/llm";
export function FollowUpSection({ result, llm }: { result: CommitteeResponse; llm: LlmSelection }) {
  return (
    <SectionCard eyebrow="Step 9" title="Not convinced? Push back" copyKey="followUp" defaultInfoOpen>
      <div className="card -mx-1 overflow-hidden bg-white/40">
        <CommitteeIterationPanel result={result} llm={llm} />
      </div>
    </SectionCard>
  );
}
