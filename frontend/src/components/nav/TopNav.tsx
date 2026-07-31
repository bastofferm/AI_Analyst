"use client";

// The MZQA terminal-style top navigation: wordmark + underline nav tabs + run
// status + settings + backend-health LED + live clock. Must be rendered inside
// the shell's <Tabs.Root> so the triggers participate in view switching.

import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { api, type CommitteeExtraAnalyst } from "@/lib/api";
import type { LlmVault } from "@/lib/llm";
import { useCurrency, type CurrencyPref } from "@/lib/currency";
import { LiveClock } from "./LiveClock";
import { Logomark } from "./Logomark";
import { RunStatusChip, type NamedActivity } from "./RunStatusChip";
import { SettingsPopover } from "./SettingsPopover";

export type ViewDef = { value: string; label: string };

const TRIGGER_CLASS =
  "relative px-3.5 py-2.5 text-[12px] font-medium tracking-[0.02em] text-muted border-b-2 border-transparent " +
  "transition-colors hover:text-navy data-[state=active]:text-navy data-[state=active]:border-navy " +
  "data-[state=active]:font-semibold outline-none whitespace-nowrap";

const CURRENCY_OPTIONS: { key: CurrencyPref; label: string; title: string }[] = [
  { key: "HOME", label: "Home", title: "Each figure in its native currency ($ for US, ¥ for Japan)" },
  { key: "USD", label: "USD", title: "Show all money in US dollars" },
  { key: "EUR", label: "EUR", title: "Show all money in euros" },
];

/** Global display-currency toggle. Hidden until FX rates have loaded. */
function CurrencySwitcher() {
  const { pref, setPref, rates, asOf } = useCurrency();
  if (!rates) return null;
  return (
    <div
      className="flex items-center overflow-hidden rounded-full border border-border bg-white"
      role="group"
      aria-label="Display currency"
      title={`Display currency. Historical figures convert at today's rate${asOf ? ` (${asOf})` : ""}.`}
    >
      {CURRENCY_OPTIONS.filter((o) => o.key === "HOME" || typeof rates[o.key] === "number").map((o) => (
        <button
          key={o.key}
          onClick={() => setPref(o.key)}
          title={o.title}
          aria-pressed={pref === o.key}
          className={`px-2.5 py-1 text-[10px] font-semibold tracking-[0.04em] transition-colors ${
            pref === o.key ? "bg-navy text-white" : "text-muted hover:text-navy"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function TopNav({
  views,
  activities,
  onJump,
  llm,
  onLlmChange,
  onForgetKeys,
  providerLabel,
  analysts,
  onAnalystsChange,
}: {
  views: ViewDef[];
  activities: NamedActivity[];
  onJump: (view: string) => void;
  llm: LlmVault;
  onLlmChange: (next: LlmVault) => void;
  onForgetKeys: () => void;
  providerLabel: string;
  analysts: CommitteeExtraAnalyst[];
  onAnalystsChange: (next: CommitteeExtraAnalyst[]) => void;
}) {
  const [db, setDb] = useState<"ok" | "down" | "unknown">("unknown");
  useEffect(() => {
    let alive = true;
    const check = () =>
      api
        .health()
        .then((h) => alive && setDb(h.db === "connected" ? "ok" : "down"))
        .catch(() => alive && setDb("down"));
    check();
    const t = setInterval(check, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const activeByView = new Map(activities.map((a) => [a.view, a]));

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg/95 backdrop-blur-sm">
      <div className="mx-auto flex max-w-[1268px] items-center justify-between gap-4 px-4 pt-2.5 lg:px-6">
        <div className="flex items-center gap-2.5">
          <Logomark size={22} />
          <div className="leading-tight">
            <div className="text-[15px] font-semibold tracking-[0.06em] text-navy">MZQA</div>
            <div className="text-[8.5px] uppercase tracking-[0.1em] text-muted">
              AI Investment Committee
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <CurrencySwitcher />
          <RunStatusChip items={activities} onJump={onJump} />
          <SettingsPopover
            llm={llm}
            onLlmChange={onLlmChange}
            onForgetKeys={onForgetKeys}
            providerLabel={providerLabel}
            analysts={analysts}
            onAnalystsChange={onAnalystsChange}
          />
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              db === "ok" ? "bg-green" : db === "down" ? "bg-red" : "bg-border"
            }`}
            title={db === "ok" ? "Data warehouse connected" : db === "down" ? "Backend unreachable" : "Checking backend…"}
          />
          <LiveClock />
        </div>
      </div>
      <div className="mx-auto max-w-[1268px] px-4 lg:px-6">
        <Tabs.List className="no-scrollbar -mb-px flex gap-1 overflow-x-auto" aria-label="Main navigation">
          {views.map((v) => (
            <Tabs.Trigger key={v.value} value={v.value} className={TRIGGER_CLASS}>
              {v.label}
              {activeByView.has(v.value) ? (
                <span className="ml-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-navy align-middle" />
              ) : null}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
      </div>
    </header>
  );
}
