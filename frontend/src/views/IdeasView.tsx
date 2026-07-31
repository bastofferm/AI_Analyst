"use client";

// Ideas — the AI screener: a one-click value+sentiment scan rendered as scored
// idea cards, plus a natural-language screen with example prompt chips.
// Port of AiScreenTab's logic (same endpoints, same INTL prompt behavior).

import { useState } from "react";
import {
  api,
  type AgentResponse,
  type AgentRow,
  type CommitteeExtraAnalyst,
  type GroupResponse,
} from "@/lib/api";
import type { CommitteeActivityReporter } from "@/components/committee/activity";
import { useMoney } from "@/lib/currency";
import { num, pct, signedTone } from "@/lib/fmt";
import { ScoreRing } from "@/components/charts/primitives";
import { InfoBox } from "@/components/ui/InfoBox";
import { HelpTip } from "@/components/ui/HelpTip";
import { ProgressStepper } from "@/components/ui/ProgressStepper";
import { TonePill } from "@/components/ui/StanceBadge";
import { ESTIMATED_RUN_MS, GROUP_STEPS, SCAN_STEPS } from "@/lib/pipeline";
import { GroupResultView } from "./shared/GroupResultView";
import { MarketPicker, useMarkets, type MarketSelection } from "./shared/MarketPicker";

import { llmBody, providerLabel, type LlmSelection, type ProviderInfo } from "@/lib/llm";
type Status = "idle" | "running" | "done" | "error";

const EXAMPLE_PROMPTS = [
  "cheap large-cap software with >15% revenue growth",
  "Japanese manufacturers with high dividend yields",
  "profitable US small-caps trading under 12× earnings",
  "European healthcare with strong free cash flow",
];

/** What one model's screen produced, so a model that found nothing is reported
 *  rather than silently dropped. */
type ScreenNote = { provider: string; count: number; rationale?: string | null; error?: string };

export function IdeasView({
  llm,
  runs: selections,
  providers,
  analysts,
  onActivityChange,
  onAnalyze,
}: {
  llm: LlmSelection;
  /** Every model to ask. Each translates the prompt itself, so they surface
   *  different companies — which is what the per-company badges show. */
  runs: LlmSelection[];
  providers: ProviderInfo[];
  analysts: CommitteeExtraAnalyst[];
  onActivityChange?: CommitteeActivityReporter;
  onAnalyze?: (ticker: string) => void;
}) {
  const markets = useMarkets();
  const [sel, setSel] = useState<MarketSelection>({ jur: "US", region: "", countryCode: "" });
  const activeCountryCode = sel.jur === "INTL" && sel.countryCode ? sel.countryCode : null;

  const [agentStatus, setAgentStatus] = useState<Status>("idle");
  const [agent, setAgent] = useState<AgentResponse | null>(null);
  const [agentErr, setAgentErr] = useState("");
  const [agentStartedAt, setAgentStartedAt] = useState(0);

  const [prompt, setPrompt] = useState("");
  const [groupStatus, setGroupStatus] = useState<Status>("idle");
  const [group, setGroup] = useState<GroupResponse | null>(null);
  const [groupErr, setGroupErr] = useState("");
  const [groupStartedAt, setGroupStartedAt] = useState(0);
  /** ticker → which models' screens surfaced it. */
  const [foundBy, setFoundBy] = useState<Record<string, string[]>>({});
  const [screenNotes, setScreenNotes] = useState<ScreenNote[]>([]);
  const labelFor = (id: string) => providerLabel(providers, id);

  function cfg(): Record<string, unknown> {
    return analysts.length ? { extra_analysts: analysts } : {};
  }

  async function runAgent() {
    setAgentStatus("running");
    setAgentStartedAt(Date.now());
    onActivityChange?.({ status: "running", label: "Ideas", detail: `${sel.jur} value + sentiment scan` });
    setAgentErr("");
    setAgent(null);
    try {
      const res = await api.valueSentimentAgent({
        jurisdiction: sel.jur,
        country_code: activeCountryCode,
        region: sel.jur === "INTL" ? sel.region || null : null,
        limit: 12,
        ...llmBody(llm),
      });
      setAgent(res);
      setAgentStatus("done");
      onActivityChange?.(null);
    } catch (e) {
      setAgentErr(e instanceof Error ? e.message : "Scan failed.");
      setAgentStatus("error");
      onActivityChange?.(null);
    }
  }

  async function runPrompt(text?: string) {
    const p = (text ?? prompt).trim();
    if (!p) {
      setGroupErr("Describe the stocks you want to screen for.");
      setGroupStatus("error");
      return;
    }
    if (text) setPrompt(text);
    setGroupStatus("running");
    setGroupStartedAt(Date.now());
    onActivityChange?.({ status: "running", label: "Ideas", detail: "prompt → screen → committee verdict" });
    setGroupErr("");
    setGroup(null);
    setFoundBy({});
    setScreenNotes([]);
    try {
      // Prompt-driven screens are NOT constrained by the Market dropdown — the LLM
      // infers jurisdiction/country from the prompt itself; "INTL" is the widest fallback.
      //
      // Each model reads the prompt differently and so translates it into different
      // filters, which is why they surface different companies. We run the
      // translate→screen chain per model, then rank the union once: the ranking maths
      // is deterministic, so re-running it per model would produce the same order.
      const screens = await Promise.allSettled(
        selections.map(async (sel) => {
          const ai = await api.screenerAi({ prompt: p, jurisdiction: "INTL", ...llmBody(sel) });
          // A model whose key is rejected still answers 200, just with nothing
          // usable in it. Sending that on would only earn a validation error, so
          // treat it as "this model could not translate the screen".
          if (!ai.universe || !ai.filters || !ai.sort) {
            throw new Error("could not turn that prompt into a screen (check its key under Setup)");
          }
          const rows = await api.screenerRun({
            universe: ai.universe,
            filters: ai.filters,
            sort: ai.sort,
            limit: 12,
          });
          return { provider: sel.provider, rows: rows.rows, rationale: ai.rationale };
        }),
      );

      const surfaced: Record<string, string[]> = {};
      const notes: ScreenNote[] = [];
      const order: string[] = [];
      screens.forEach((outcome, i) => {
        const provider = selections[i].provider;
        if (outcome.status === "rejected") {
          const err = outcome.reason;
          notes.push({
            provider,
            count: 0,
            error: err instanceof Error ? err.message : "Screen failed.",
          });
          return;
        }
        const { rows, rationale } = outcome.value;
        notes.push({ provider, count: rows.length, rationale: rationale || null });
        for (const row of rows) {
          if (!surfaced[row.ticker]) {
            surfaced[row.ticker] = [];
            order.push(row.ticker);
          }
          if (!surfaced[row.ticker].includes(provider)) surfaced[row.ticker].push(provider);
        }
      });

      setScreenNotes(notes);
      if (order.length === 0) {
        setGroupErr(
          notes.every((n) => n.error)
            ? "Every model failed to translate that screen. Check the keys under Setup."
            : "That screen matched no companies. Try describing it more loosely.",
        );
        setGroupStatus("error");
        onActivityChange?.(null);
        return;
      }
      setFoundBy(surfaced);

      const res = await api.committeeGroup({
        mode: "screen",
        jurisdiction: "INTL",
        country_code: null,
        tickers: order,
        limit: order.length,
        ...llmBody(selections[0] || llm),
        config: cfg(),
      });
      setGroup(res);
      setGroupStatus("done");
      onActivityChange?.(null);
    } catch (e) {
      setGroupErr(e instanceof Error ? e.message : "Screen committee failed.");
      setGroupStatus("error");
      onActivityChange?.(null);
    }
  }

  async function sendShortlist() {
    if (!agent?.rows.length) return;
    const tickers = agent.rows.slice(0, 12).map((r) => r.ticker);
    setGroupStatus("running");
    setGroupStartedAt(Date.now());
    onActivityChange?.({ status: "running", label: "Ideas", detail: "shortlist → committee verdict" });
    setGroupErr("");
    setGroup(null);
    try {
      const res = await api.committeeGroup({
        mode: "screen",
        jurisdiction: sel.jur,
        country_code: activeCountryCode,
        region: sel.jur === "INTL" ? sel.region || null : null,
        tickers,
        limit: tickers.length,
        ...llmBody(llm),
        config: cfg(),
      });
      setGroup(res);
      setGroupStatus("done");
      onActivityChange?.(null);
    } catch (e) {
      setGroupErr(e instanceof Error ? e.message : "Committee run failed.");
      setGroupStatus("error");
      onActivityChange?.(null);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="label">Ideas</div>
        <h1 className="mt-1 text-[22px] font-semibold text-navy">Let the AI hunt for opportunities</h1>
      </div>

      {/* --- Quick scan --- */}
      <section className="card p-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[15px] font-semibold text-navy">Quick scan · cheap, growing & confident</div>
            <p className="mt-1 max-w-2xl text-[12px] text-muted">
              One click. No settings needed.
            </p>
          </div>
          <button
            onClick={runAgent}
            disabled={agentStatus === "running"}
            className="h-[34px] rounded-md bg-navy px-6 text-[13px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {agentStatus === "running" ? "Scanning…" : "Run scan"}
          </button>
        </div>
        <div className="mt-3">
          <InfoBox copyKey="ideasScan" />
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <MarketPicker value={sel} markets={markets} onChange={setSel} />
        </div>

        {agentStatus === "running" && (
          <div className="mt-4">
            <ProgressStepper steps={SCAN_STEPS} startedAt={agentStartedAt} totalMs={ESTIMATED_RUN_MS.scan} title="Scanning the market" />
          </div>
        )}
        {agentStatus === "error" && agentErr && (
          <div className="mt-3 rounded border border-red/40 bg-red/5 p-2 text-[12px] text-red">{agentErr}</div>
        )}

        {agent && (
          <div className="mt-4">
            {agent.warnings?.length > 0 && (
              <div className="mb-3 rounded border border-amber/40 bg-amber/10 px-3 py-1.5 text-[11px] text-navy">
                {agent.warnings.join(" · ")}
              </div>
            )}
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {agent.rows.map((r, i) => (
                <IdeaCard key={r.ticker} r={r} rank={i + 1} jur={sel.jur} onAnalyze={onAnalyze} />
              ))}
            </div>
            {agent.rows.length > 0 && (
              <button
                onClick={sendShortlist}
                disabled={groupStatus === "running"}
                className="mt-4 rounded-md border border-navy px-4 py-1.5 text-[12px] font-semibold text-navy transition-colors hover:bg-navy hover:text-white disabled:opacity-50"
              >
                Have the committee rank this shortlist →
              </button>
            )}
          </div>
        )}
      </section>

      {/* --- Free-text screen --- */}
      <section className="card p-5">
        <div className="text-[15px] font-semibold text-navy">Describe your own screen</div>
        <div className="mt-3">
          <InfoBox copyKey="ideasPrompt" />
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {EXAMPLE_PROMPTS.map((p) => (
            <button
              key={p}
              onClick={() => setPrompt(p)}
              className="rounded-full border border-border bg-white px-3 py-1 text-[11px] text-muted transition-colors hover:border-navy hover:text-navy"
            >
              “{p}”
            </button>
          ))}
        </div>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={2}
          placeholder="e.g. cheap large-cap software with >15% revenue growth — name a country or region in the prompt itself"
          className="mt-3 w-full rounded-md border border-border bg-white px-3 py-2 text-[13px] text-navy outline-none focus:border-navy"
        />
        <button
          onClick={() => runPrompt()}
          disabled={groupStatus === "running"}
          className="mt-3 rounded-md bg-navy px-6 py-2 text-[13px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {groupStatus === "running" ? "Running committee…" : "Screen & run committee"}
        </button>
        {groupStatus === "error" && groupErr && (
          <div className="mt-3 rounded border border-red/40 bg-red/5 p-2 text-[12px] text-red">{groupErr}</div>
        )}
      </section>

      {groupStatus === "running" && (
        <ProgressStepper steps={GROUP_STEPS} startedAt={groupStartedAt} totalMs={ESTIMATED_RUN_MS.group} title="The committee is deliberating" />
      )}

      {screenNotes.length > 1 && groupStatus !== "running" ? (
        <section className="card p-4">
          <div className="label">What each model looked for</div>
          <p className="mt-1 text-[11.5px] text-muted">
            The same words mean slightly different things to each model, so they hand back different
            shortlists. Everything they found is ranked together below.
          </p>
          <div className="mt-2.5 flex flex-col gap-1.5">
            {screenNotes.map((n) => (
              <div key={n.provider} className="flex flex-wrap items-baseline gap-2 text-[11.5px]">
                <span className="font-semibold text-navy">{labelFor(n.provider)}</span>
                <span className={n.error ? "text-red" : "text-muted"}>
                  {n.error ? n.error : n.count === 0 ? "found nothing for this screen" : `found ${n.count}`}
                </span>
                {n.rationale ? <span className="text-muted">· {n.rationale}</span> : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {groupStatus === "done" && group && (
        <GroupResultView
          result={group}
          onAnalyze={onAnalyze}
          foundBy={foundBy}
          providerLabelFor={labelFor}
        />
      )}
    </div>
  );
}

function IdeaCard({
  r,
  rank,
  jur,
  onAnalyze,
}: {
  r: AgentRow;
  rank: number;
  jur: string;
  onAnalyze?: (t: string) => void;
}) {
  const cv = useMoney();
  const m = r.key_metrics || {};
  return (
    <div
      className="rise-in hover-lift card flex flex-col gap-3 border-border-soft bg-white/70 p-4"
      style={{ animationDelay: `${Math.min(rank - 1, 8) * 60}ms` }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold text-navy">
            <span className="num mr-1.5 text-[10px] text-muted">{String(rank).padStart(2, "0")}</span>
            {r.ticker}
          </div>
          <div className="truncate text-[11px] text-muted">{r.name}</div>
          {r.sector ? <div className="mt-0.5 truncate text-[10px] text-muted/80">{r.sector}</div> : null}
        </div>
        <ScoreRing score={r.interest_score} size={52} label="interest" />
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border-soft pt-2.5 text-[11px]">
        <span className="text-muted">
          <HelpTip term="market cap">Size</HelpTip> <b className="num text-navy">{cv.money(m.market_cap_usd, jur)}</b>
        </span>
        <span className="text-muted">
          <HelpTip term="P/E">P/E</HelpTip> <b className="num text-navy">{num(m.pe, 1)}</b>
        </span>
        <span className="text-muted">
          <HelpTip term="EV/EBITDA">EV/EBITDA</HelpTip> <b className="num text-navy">{num(m.ev_ebitda, 1)}</b>
        </span>
        <span className="text-muted">
          <HelpTip term="FCF yield">FCF yld</HelpTip>{" "}
          <b className={`num ${signedTone(m.fcf_yield) || "text-navy"}`}>{pct(m.fcf_yield)}</b>
        </span>
        <span className="text-muted">
          <HelpTip term="rev YoY">Growth</HelpTip>{" "}
          <b className={`num ${signedTone(m.rev_yoy) || "text-navy"}`}>{pct(m.rev_yoy)}</b>
        </span>
        <span className="text-muted">
          <HelpTip term="MD&A">Tone</HelpTip>{" "}
          <TonePill
            value={r.mda_tone}
            label={
              typeof r.mda_tone === "number" ? (r.mda_tone > 0.15 ? "positive" : r.mda_tone < -0.15 ? "negative" : "neutral") : undefined
            }
            title={r.mda_note || undefined}
          />
        </span>
      </div>
      {r.mda_risk_flags?.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {r.mda_risk_flags.slice(0, 3).map((f) => (
            <span key={f} className="badge-neg rounded px-1.5 py-0.5 text-[9.5px] font-medium">
              {f}
            </span>
          ))}
        </div>
      )}
      <p className="text-[11px] leading-relaxed text-navy">{r.rationale}</p>
      {onAnalyze ? (
        <button
          onClick={() => onAnalyze(r.ticker)}
          className="mt-auto rounded-md border border-navy px-3 py-1.5 text-[11px] font-semibold text-navy transition-colors hover:bg-navy hover:text-white"
        >
          Full committee analysis →
        </button>
      ) : null}
    </div>
  );
}
