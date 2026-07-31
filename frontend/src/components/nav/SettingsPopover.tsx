"use client";

// Nav settings popover hosting the LLM setup (provider / key / model) and the
// analyst roster.

import * as Popover from "@radix-ui/react-popover";
import { Settings2, X } from "lucide-react";
import type { CommitteeExtraAnalyst } from "@/lib/api";
import { hasKey, type LlmVault } from "@/lib/llm";
import { LlmSetupPanel } from "../committee/LlmSetupPanel";
import { AnalystRoster } from "../committee/AnalystRoster";

export function SettingsPopover({
  llm,
  onLlmChange,
  onForgetKeys,
  providerLabel,
  analysts,
  onAnalystsChange,
}: {
  llm: LlmVault;
  onLlmChange: (next: LlmVault) => void;
  onForgetKeys: () => void;
  providerLabel: string;
  analysts: CommitteeExtraAnalyst[];
  onAnalystsChange: (next: CommitteeExtraAnalyst[]) => void;
}) {
  const keySet = hasKey(llm);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          className="flex items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1 text-[10.5px] font-semibold text-navy transition-colors hover:border-navy/40"
          title="AI provider, API key & analyst roster"
        >
          <Settings2 className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">{providerLabel}</span>
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${keySet ? "bg-green" : "bg-amber"}`}
            title={keySet ? `${providerLabel} key set for this session` : "No key — server fallback"}
          />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className="z-50 max-h-[80vh] w-[min(94vw,460px)] overflow-y-auto rounded-lg border border-border bg-bg p-3 shadow-[0_18px_48px_rgba(26,39,68,0.18),0_2px_6px_rgba(26,39,68,0.08)]"
        >
          <div className="mb-2 flex items-center justify-between">
            <span className="label">Setup</span>
            <Popover.Close asChild>
              <button className="rounded p-1 text-muted hover:bg-border-soft" aria-label="Close">
                <X className="h-3.5 w-3.5" />
              </button>
            </Popover.Close>
          </div>
          <div className="flex flex-col gap-3">
            <LlmSetupPanel vault={llm} onChange={onLlmChange} onForget={onForgetKeys} />
            <AnalystRoster analysts={analysts} onChange={onAnalystsChange} />
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
