"use client";

// The application shell — replaces the old CommitteeWorkbench with the consumer
// five-view layout (Home / Explore / Analyze / Compare / Ideas) under the MZQA
// terminal top nav. Same state contract as before, except the single DeepSeek
// key is now a multi-provider vault (lib/llm.ts): session-scoped keys for any of
// the five providers, persistent custom-analyst roster, per-view run activity,
// and the ticker hand-off into Analyze. All views stay mounted (hidden when
// inactive) so multi-minute committee runs survive view switches.

import { useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { type CommitteeExtraAnalyst } from "@/lib/api";
import {
  EMPTY_VAULT,
  FALLBACK_PROVIDERS,
  activeSelections,
  fetchProviders,
  forgetLlmKeys,
  isIdleExpired,
  loadVault,
  providerLabel,
  saveVault,
  selection,
  type LlmVault,
  type ProviderInfo,
} from "@/lib/llm";
import { CurrencyProvider } from "@/lib/currency";
import type { CommitteeActivity } from "./committee/activity";
import { TopNav, type ViewDef } from "./nav/TopNav";
import { Logomark } from "./nav/Logomark";
import type { NamedActivity } from "./nav/RunStatusChip";
import { HomeView } from "@/views/HomeView";
import { ExploreView } from "@/views/ExploreView";
import { AnalyzeView } from "@/views/AnalyzeView";
import { CompareView } from "@/views/CompareView";
import { IdeasView } from "@/views/IdeasView";
import { QuantView } from "@/views/QuantView";

const ROSTER_KEY = "aa_committee_analysts";

const VIEWS: ViewDef[] = [
  { value: "home", label: "Home" },
  { value: "explore", label: "Explore" },
  { value: "analyze", label: "Analyze" },
  { value: "compare", label: "Compare" },
  { value: "ideas", label: "Ideas" },
  { value: "quant", label: "Quant" },
];

const CONTENT_CLASS = "outline-none data-[state=inactive]:hidden";

export function AppShell() {
  const [llm, setLlm] = useState<LlmVault>(EMPTY_VAULT);
  const [providers, setProviders] = useState<ProviderInfo[]>(FALLBACK_PROVIDERS);
  const [analysts, setAnalysts] = useState<CommitteeExtraAnalyst[]>([]);
  const [analyzeActivity, setAnalyzeActivity] = useState<CommitteeActivity | null>(null);
  const [compareActivity, setCompareActivity] = useState<CommitteeActivity | null>(null);
  const [ideasActivity, setIdeasActivity] = useState<CommitteeActivity | null>(null);
  // Controlled view + the ticker handed to Analyze from Home/Explore/Ideas. The
  // nonce lets Analyze react to repeat picks of the same ticker.
  const [view, setView] = useState("home");
  const [presetTicker, setPresetTicker] = useState("");
  const [presetNonce, setPresetNonce] = useState(0);

  function analyzeInCommittee(ticker: string) {
    setPresetTicker(ticker);
    setPresetNonce((n) => n + 1);
    setView("analyze");
  }

  // Hydrate the LLM vault (session keys + persisted selection) and the roster.
  useEffect(() => {
    setLlm(loadVault());
    fetchProviders()
      .then(setProviders)
      .catch(() => {
        /* backend down — the static fallback list still drives the picker */
      });
    try {
      const raw = window.localStorage.getItem(ROSTER_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) setAnalysts(parsed);
      }
    } catch {
      /* ignore malformed roster */
    }
  }, []);

  // Idle wipe. The check is cheap and runs on a minute tick; every API call
  // stamps activity (api.ts -> touchLlmActivity), so an in-flight committee run
  // keeps the keys alive.
  useEffect(() => {
    if (!llm.idleMinutes) return;
    const t = setInterval(() => {
      if (isIdleExpired(llm.idleMinutes)) {
        forgetLlmKeys();
        setLlm((v) => ({ ...v, keys: {} }));
      }
    }, 60_000);
    return () => clearInterval(t);
  }, [llm.idleMinutes]);

  function saveLlm(next: LlmVault) {
    setLlm(saveVault(next));
  }

  function forgetKeys() {
    forgetLlmKeys();
    setLlm((v) => saveVault({ ...v, keys: {} }));
  }

  function saveRoster(next: CommitteeExtraAnalyst[]) {
    setAnalysts(next);
    try {
      window.localStorage.setItem(ROSTER_KEY, JSON.stringify(next));
    } catch {
      /* ignore quota errors */
    }
  }

  const activities: NamedActivity[] = [
    analyzeActivity ? { view: "analyze", viewLabel: "Analyze", activity: analyzeActivity } : null,
    compareActivity ? { view: "compare", viewLabel: "Compare", activity: compareActivity } : null,
    ideasActivity ? { view: "ideas", viewLabel: "Ideas", activity: ideasActivity } : null,
  ].filter(Boolean) as NamedActivity[];

  return (
    <CurrencyProvider>
    <Tabs.Root value={view} onValueChange={setView} className="flex min-h-screen flex-col">
      <TopNav
        views={VIEWS}
        activities={activities}
        onJump={setView}
        llm={llm}
        onLlmChange={saveLlm}
        onForgetKeys={forgetKeys}
        providerLabel={providerLabel(providers, llm.provider)}
        analysts={analysts}
        onAnalystsChange={saveRoster}
      />

      <main className="mx-auto w-full max-w-[1268px] flex-1 px-4 py-6 lg:px-6">
        <Tabs.Content forceMount value="home" className={CONTENT_CLASS}>
          <HomeView analysts={analysts} onAnalyze={analyzeInCommittee} onNavigate={setView} />
        </Tabs.Content>
        <Tabs.Content forceMount value="explore" className={CONTENT_CLASS}>
          <ExploreView onAnalyze={analyzeInCommittee} />
        </Tabs.Content>
        <Tabs.Content forceMount value="analyze" className={CONTENT_CLASS}>
          <AnalyzeView
            llm={selection(llm)}
            runs={activeSelections(llm, providers)}
            providers={providers}
            analysts={analysts}
            onActivityChange={setAnalyzeActivity}
            presetTicker={presetTicker}
            presetNonce={presetNonce}
          />
        </Tabs.Content>
        <Tabs.Content forceMount value="compare" className={CONTENT_CLASS}>
          <CompareView
            llm={selection(llm)}
            runs={activeSelections(llm, providers)}
            providers={providers}
            analysts={analysts}
            onActivityChange={setCompareActivity}
            onAnalyze={analyzeInCommittee}
          />
        </Tabs.Content>
        <Tabs.Content forceMount value="ideas" className={CONTENT_CLASS}>
          <IdeasView
            llm={selection(llm)}
            runs={activeSelections(llm, providers)}
            providers={providers}
            analysts={analysts}
            onActivityChange={setIdeasActivity}
            onAnalyze={analyzeInCommittee}
          />
        </Tabs.Content>
        <Tabs.Content forceMount value="quant" className={CONTENT_CLASS}>
          <QuantView />
        </Tabs.Content>
      </main>

      <footer className="mt-8 border-t border-border bg-panel/60">
        <div className="mx-auto flex max-w-[1268px] flex-col items-center justify-between gap-3 px-4 py-5 sm:flex-row lg:px-6">
          <div className="flex items-center gap-2.5">
            <Logomark size={16} stroke="#6B86A8" />
            <div className="leading-tight">
              <div className="text-[11px] font-semibold tracking-[0.06em] text-navy">MZQA</div>
              <div className="text-[8px] uppercase tracking-[0.1em] text-muted">Financial Technologies LLC</div>
            </div>
          </div>
          <div className="text-center text-[10.5px] text-muted sm:text-right">
            Independent research. Not investment advice.
            <span className="hidden sm:inline"> · </span>
            <br className="sm:hidden" />© {new Date().getFullYear()} MZQA Financial Technologies LLC
          </div>
        </div>
      </footer>
    </Tabs.Root>
    </CurrencyProvider>
  );
}
