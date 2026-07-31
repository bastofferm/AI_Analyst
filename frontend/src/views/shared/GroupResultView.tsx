"use client";

// Consumer group-verdict view (Compare + Ideas).
//
// Split 50:50: the ranking on the left, and on the right the full decomposition
// of whichever name is selected — every warehouse metric that fed the composite,
// its z-score inside this peer group, the weight applied and the resulting
// contribution. The composite is exactly the sum of those contributions
// (committee/group.py::deterministic_ranking), so the panel is an audit trail
// rather than a restatement.

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { GroupRankItem, GroupResponse, ScoreInput } from "@/lib/api";
import { useMoney } from "@/lib/currency";
import { num, pct, signedTone } from "@/lib/fmt";
import { Collapsible } from "@/components/ui/Collapsible";
import { StanceBadge } from "@/components/ui/StanceBadge";
import { HelpTip } from "@/components/ui/HelpTip";

export function GroupResultView({
  result,
  onAnalyze,
  foundBy,
  providerLabelFor,
}: {
  result: GroupResponse;
  onAnalyze?: (ticker: string) => void;
  /** ticker → which models' screens surfaced it. Only set for prompt screens run
   *  against more than one model; absent everywhere else. */
  foundBy?: Record<string, string[]>;
  providerLabelFor?: (providerId: string) => string;
}) {
  const cv = useMoney();
  const ranked = useMemo(
    () => [...(result.ranking || [])].sort((a, b) => (b.composite_score ?? -1) - (a.composite_score ?? -1)),
    [result.ranking]
  );
  const hasScores = ranked.some((r) => typeof r.composite_score === "number");
  const [selected, setSelected] = useState<string>("");
  useEffect(() => {
    setSelected(ranked[0]?.ticker || "");
  }, [ranked]);

  const active = ranked.find((r) => r.ticker === selected) || ranked[0] || null;
  // Market caps are USD-scaled except for pure-JP universes (stored in yen).
  const universeJur = typeof result.universe?.jurisdiction === "string" ? (result.universe.jurisdiction as string) : undefined;

  const maxScore = Math.max(...ranked.map((r) => Math.abs(r.composite_score ?? 0)), 1);

  return (
    <div className="flex flex-col gap-4">
      <section className="card p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="label">The verdict · {result.resolved_tickers.length} companies compared</div>
          <span className="text-[10px] text-muted">pick a name to see how its score was built</span>
        </div>
        {result.warnings?.length > 0 && (
          <div className="mt-2 rounded border border-amber/40 bg-amber/10 px-3 py-1.5 text-[11px] text-navy">
            {result.warnings.join(" · ")}
          </div>
        )}

        {hasScores ? (
          <div className="mt-4 grid grid-cols-1 gap-5 lg:grid-cols-2">
            {/* ---------------------------------------------- left: the ranking */}
            <div className="min-w-0">
              <div className="mb-2 text-[12px] font-semibold text-navy">
                Ranked by <HelpTip term="composite score">composite score</HelpTip>
              </div>
              <div className="flex flex-col gap-1">
                {ranked.map((r, idx) => (
                  <RankRow
                    key={r.ticker}
                    item={r}
                    rank={idx + 1}
                    max={maxScore}
                    selected={r.ticker === selected}
                    onSelect={() => setSelected(r.ticker)}
                    foundBy={foundBy?.[r.ticker]}
                    providerLabelFor={providerLabelFor}
                  />
                ))}
              </div>
            </div>

            {/* ------------------------------------- right: the decomposition */}
            <div className="min-w-0">
              {active ? (
                <ScoreBreakdown item={active} onAnalyze={onAnalyze} peers={ranked.length} />
              ) : null}
            </div>
          </div>
        ) : null}

        {result.group_memo ? (
          <div className="memo-prose mt-5 max-w-3xl border-t border-border-soft pt-4">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.group_memo}</ReactMarkdown>
          </div>
        ) : null}
      </section>

      <Collapsible label="Every company in detail" sublabel="Valuation metrics, stance and the committee's one-line reasoning">
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-[0.08em] text-muted">
                <th className="px-3 py-2 text-right">#</th>
                <th className="px-3 py-2">Company</th>
                <th className="px-3 py-2">Stance</th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="market cap">Mkt cap</HelpTip>
                </th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="P/E">P/E</HelpTip>
                </th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="P/B">P/B</HelpTip>
                </th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="EV/EBITDA">EV/EBITDA</HelpTip>
                </th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="FCF yield">FCF yld</HelpTip>
                </th>
                <th className="px-3 py-2 text-right">
                  <HelpTip term="rev YoY">Rev YoY</HelpTip>
                </th>
                <th className="px-3 py-2">Why</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {ranked.map((r, i) => {
                const m = r.metrics || {};
                return (
                  <tr key={r.ticker} className="border-t border-border-soft align-top">
                    <td className="num px-3 py-2 text-right text-muted">{i + 1}</td>
                    <td className="px-3 py-2">
                      <div className="font-semibold text-navy">{r.ticker}</div>
                      <div className="text-[11px] text-muted">{r.name}</div>
                    </td>
                    <td className="px-3 py-2">
                      <StanceBadge stance={r.stance} />
                    </td>
                    <td className="num px-3 py-2 text-right">{cv.money(m.market_cap_usd, universeJur)}</td>
                    <td className="num px-3 py-2 text-right">{num(m.pe, 1)}</td>
                    <td className="num px-3 py-2 text-right">{num(m.pb, 1)}</td>
                    <td className="num px-3 py-2 text-right">{num(m.ev_ebitda, 1)}</td>
                    <td className={`num px-3 py-2 text-right ${signedTone(m.fcf_yield)}`}>{pct(m.fcf_yield)}</td>
                    <td className={`num px-3 py-2 text-right ${signedTone(m.rev_yoy)}`}>{pct(m.rev_yoy)}</td>
                    <td className="max-w-md px-3 py-2 text-[11px] leading-relaxed text-navy">{r.rationale}</td>
                    <td className="px-3 py-2 text-right">
                      {onAnalyze ? (
                        <button
                          onClick={() => onAnalyze(r.ticker)}
                          className="whitespace-nowrap rounded border border-navy px-2.5 py-1 text-[11px] font-semibold text-navy transition-colors hover:bg-navy hover:text-white"
                        >
                          Analyze →
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Collapsible>
    </div>
  );
}

/** One selectable bar in the ranking. Bars are scaled by |score| so the negative
 *  tail reads as a real distance from zero rather than a stub. */
function RankRow({
  item,
  rank,
  max,
  selected,
  onSelect,
  foundBy,
  providerLabelFor,
}: {
  item: GroupRankItem;
  rank: number;
  max: number;
  selected: boolean;
  onSelect: () => void;
  foundBy?: string[];
  providerLabelFor?: (providerId: string) => string;
}) {
  const score = item.composite_score ?? 0;
  const w = Math.max(2, (Math.abs(score) / max) * 100);
  const negative = score < 0;
  const label = providerLabelFor || ((id: string) => id);
  return (
    <button
      onClick={onSelect}
      aria-pressed={selected}
      className={`flex w-full items-center gap-2.5 rounded px-1.5 py-1 text-left transition-colors ${
        selected ? "bg-navy/[0.07] ring-1 ring-navy/25" : "hover:bg-white/70"
      }`}
    >
      <div className="flex w-32 shrink-0 flex-col sm:w-40">
        <span className="truncate text-[12px] font-semibold text-navy">
          <span className="num mr-1.5 text-[10px] text-muted">{String(rank).padStart(2, "0")}</span>
          {item.ticker}
        </span>
        <span className="truncate text-[10px] text-muted">{item.name}</span>
        {foundBy?.length ? (
          <span
            className="mt-0.5 flex flex-wrap gap-1"
            title={`Surfaced by ${foundBy.map(label).join(", ")}`}
          >
            {foundBy.map((p) => (
              <span
                key={p}
                className="rounded-sm bg-navy/10 px-1 py-px text-[8.5px] font-semibold uppercase tracking-[0.06em] text-navy"
              >
                {label(p)}
              </span>
            ))}
          </span>
        ) : null}
      </div>
      <div className="relative h-4 flex-1 overflow-hidden rounded-sm bg-border-soft">
        <div
          className="h-full rounded-sm transition-[width] duration-500 ease-out"
          style={{
            width: `${w}%`,
            background: negative ? "#DC2626" : selected ? "#2F4D73" : "#476D99",
            opacity: negative ? 0.55 : selected ? 1 : 0.75,
          }}
        />
      </div>
      <span className={`num w-10 shrink-0 text-right text-[12px] font-bold ${negative ? "text-red" : "text-navy"}`}>
        {score.toFixed(1)}
      </span>
      <StanceBadge stance={item.stance} />
    </button>
  );
}

const BREAKDOWN_COLS = "grid-cols-[1fr_54px_40px_34px_1fr_44px]";

/** Why a metric contributed nothing. A negative multiple is the interesting case:
 *  it is not "cheap", it means the denominator is negative (loss-making earnings,
 *  negative book equity), so it is deliberately left out of the ranking. */
const NOTE_ORDER = ["negative", "no_spread", "missing"] as const;
const NOTE_COPY: Record<(typeof NOTE_ORDER)[number], { label: string; why: string }> = {
  negative: {
    label: "Left out (negative)",
    why: "below zero, so there is no meaningful “cheap vs expensive” reading. Scoring it would rank the most distressed company in the group as the biggest bargain.",
  },
  no_spread: {
    label: "Left out (no comparison)",
    why: "too few companies here have a usable value to rank against.",
  },
  missing: {
    label: "Not available",
    why: "not in the warehouse for this company.",
  },
};

function fmtInput(si: ScoreInput): string {
  if (si.value == null) return "—";
  return si.unit === "pct" ? pct(si.value) : num(si.value, 1);
}

/** Plain-English read of what moved this name, before any arithmetic: the two
 *  strongest tailwinds and the two strongest drags, phrased relative to the peer
 *  group rather than as z-scores. */
function Highlights({ inputs, peers }: { inputs: ScoreInput[]; peers: number }) {
  const byStrength = [...inputs].sort(
    (a, b) => Math.abs(b.contribution as number) - Math.abs(a.contribution as number),
  );
  const helped = byStrength.filter((s) => (s.contribution as number) > 0).slice(0, 2);
  const hurt = byStrength.filter((s) => (s.contribution as number) < 0).slice(0, 2);
  if (helped.length === 0 && hurt.length === 0) return null;

  // Keep each label's own casing — lowercasing turns "FCF yield" into "fcf yield".
  const phrase = (list: ScoreInput[]) =>
    list.map((s) => `${s.label} (${fmtInput(s)})`).join(" and ");

  return (
    <div className="mt-3 space-y-2">
      {helped.length > 0 ? (
        <div className="flex gap-2">
          <span className="mt-[3px] h-2 w-2 shrink-0 rounded-full bg-[#16A34A]" />
          <p className="text-[11.5px] leading-relaxed text-navy">
            <b>Ranks well on</b> {phrase(helped)} — better than most of the {peers - 1} other companies here.
          </p>
        </div>
      ) : null}
      {hurt.length > 0 ? (
        <div className="flex gap-2">
          <span className="mt-[3px] h-2 w-2 shrink-0 rounded-full bg-[#DC2626]" />
          <p className="text-[11.5px] leading-relaxed text-navy">
            <b>Held back by</b> {phrase(hurt)} — weaker than the rest of the group.
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** The right-hand panel: how this one name's composite was assembled. */
function ScoreBreakdown({
  item,
  peers,
  onAnalyze,
}: {
  item: GroupRankItem;
  peers: number;
  onAnalyze?: (ticker: string) => void;
}) {
  const [showMaths, setShowMaths] = useState(false);
  const inputs = item.score_inputs || [];
  const scored = inputs.filter((s) => s.contribution != null);
  const missing = inputs.filter((s) => s.contribution == null);
  const maxAbs = Math.max(...scored.map((s) => Math.abs(s.contribution as number)), 0.001);
  const total = scored.reduce((sum, s) => sum + (s.contribution as number), 0);

  return (
    <div className="rounded-lg border border-border bg-paper p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="num text-[15px] font-bold text-navy">{item.ticker}</span>
            <StanceBadge stance={item.stance} />
          </div>
          <div className="truncate text-[11px] text-muted">{item.name}</div>
        </div>
        <div className="shrink-0 text-right">
          <div
            className={`num text-[26px] font-bold leading-none ${
              (item.composite_score ?? 0) < 0 ? "text-red" : "text-navy"
            }`}
          >
            {(item.composite_score ?? 0) > 0 ? "+" : ""}
            {(item.composite_score ?? 0).toFixed(2)}
          </div>
          <div className="text-[9px] uppercase tracking-[0.1em] text-muted">composite score</div>
        </div>
      </div>

      {inputs.length === 0 ? (
        <p className="mt-3 text-[11.5px] text-muted">
          No score breakdown was returned for this run.
        </p>
      ) : (
        <>
          {/* Lead with what actually moved this name, in words. The z-score /
              weight arithmetic is the audit trail underneath, not the headline. */}
          <Highlights inputs={scored} peers={peers} />

          <button
            onClick={() => setShowMaths((v) => !v)}
            aria-expanded={showMaths}
            className="mt-3 flex w-full items-center justify-between rounded border border-border-soft bg-white/60 px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:border-navy/40 hover:text-navy"
          >
            <span>{showMaths ? "Hide the maths" : "Show the maths"}</span>
            <span className="text-[13px] leading-none">{showMaths ? "−" : "+"}</span>
          </button>
        </>
      )}

      {inputs.length > 0 && showMaths ? (
        <>
          <p className="mt-2.5 text-[11px] leading-relaxed text-muted">
            Each metric below is read from the warehouse, turned into a{" "}
            <HelpTip term="z-score">z-score</HelpTip> against the other {peers - 1} companies in this
            group, then multiplied by its weight. The score above is just the sum.
          </p>

          <div className="mt-3 space-y-1.5">
            <div className={`grid ${BREAKDOWN_COLS} items-center gap-1.5 text-[8.5px] font-semibold uppercase tracking-[0.08em] text-muted`}>
              <span>Metric</span>
              <span className="text-right">Value</span>
              <span className="text-right">z</span>
              <span className="text-right">×w</span>
              <span className="text-center">Effect</span>
              <span className="text-right">Total</span>
            </div>
            {scored.map((si) => {
              const c = si.contribution as number;
              const w = (Math.abs(c) / maxAbs) * 50;
              return (
                <div
                  key={si.key}
                  className={`grid ${BREAKDOWN_COLS} items-center gap-1.5`}
                  title={si.metric_id ? `warehouse metric: ${si.metric_id}` : undefined}
                >
                  <span className="truncate text-[11px] text-navy">
                    {si.label}
                    <span className="ml-1 text-[9px] text-muted">{si.group}</span>
                  </span>
                  <span className="num text-right text-[11px] text-navy">{fmtInput(si)}</span>
                  <span className="num text-right text-[10.5px] text-muted">{si.z?.toFixed(2)}</span>
                  <span className="num text-right text-[10px] text-muted">
                    {si.weight > 0 ? "+" : ""}
                    {si.weight}
                  </span>
                  {/* Diverging bar from the centre: right = helped, left = hurt.
                      The number sits in its own column — overlaid on the bar, a long
                      bar swallowed its own label. */}
                  <span className="relative flex h-3 items-center">
                    <span className="absolute left-1/2 h-full w-px bg-border" />
                    <span
                      className="absolute h-[7px] rounded-sm"
                      style={{
                        width: `${w}%`,
                        left: c >= 0 ? "50%" : `${50 - w}%`,
                        background: c >= 0 ? "#16A34A" : "#DC2626",
                        opacity: 0.75,
                      }}
                    />
                  </span>
                  <span className={`num text-right text-[10px] font-semibold ${c >= 0 ? "text-green" : "text-red"}`}>
                    {c > 0 ? "+" : ""}
                    {c.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-2 flex items-center justify-between border-t border-border pt-1.5 text-[11px]">
            <span className="font-semibold text-navy">Sum of contributions</span>
            <span className={`num font-bold ${total < 0 ? "text-red" : "text-navy"}`}>
              {total > 0 ? "+" : ""}
              {total.toFixed(2)}
            </span>
          </div>

          {missing.length > 0 ? (
            <div className="mt-2 space-y-1">
              {NOTE_ORDER.map((note) => {
                const group = missing.filter((m) => (m.note ?? "missing") === note);
                if (group.length === 0) return null;
                return (
                  <p key={note} className="text-[10px] leading-relaxed text-muted">
                    <b className="text-navy/70">{NOTE_COPY[note].label}:</b>{" "}
                    {group.map((m) => m.label).join(", ")} — {NOTE_COPY[note].why}
                  </p>
                );
              })}
            </div>
          ) : null}
        </>
      ) : null}

      {item.rationale ? (
        <p className="mt-3 border-t border-border pt-2.5 text-[11.5px] leading-relaxed text-navy">
          {item.rationale}
        </p>
      ) : null}

      {onAnalyze ? (
        <button
          onClick={() => onAnalyze(item.ticker)}
          className="mt-3 w-full rounded-md bg-navy py-2 text-[12px] font-semibold text-white transition-opacity hover:opacity-90"
        >
          Run the full committee on {item.ticker} →
        </button>
      ) : null}
    </div>
  );
}
