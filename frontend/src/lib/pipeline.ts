// Estimated progress-stepper definitions for long-running committee calls.
//
// The backend runs the whole LangGraph inside ONE blocking HTTP request — there is no
// streaming and no polling — so the UI cannot know the true stage. These steps mirror
// the real graph topology (backend/ai_analyst/committee/graph.py: completeness →
// dq_validation → financial_analysis → {news/macro, institutional, dq_agent} →
// tribunal → lead_analyst → memo) with rough share-of-runtime weights, and the stepper
// is explicitly labelled as an estimate. It advances on elapsed time and parks on the
// final step until the fetch actually resolves.

export type PipelineStep = {
  key: string;
  label: string;
  detail: string;
  /** Rough share of a typical run, used to scale elapsed time into a step index. */
  weight: number;
};

export const SINGLE_STOCK_STEPS: PipelineStep[] = [
  {
    key: "gate",
    label: "Checking the data",
    detail: "Making sure the filings are complete and the accounts add up",
    weight: 0.08,
  },
  {
    key: "engine",
    label: "Crunching the numbers",
    detail: "Building the financial model, scenarios and valuation ranges",
    weight: 0.3,
  },
  {
    key: "context",
    label: "Scanning news, macro & owners",
    detail: "News flow, the macro regime and 13F institutional positioning",
    weight: 0.15,
  },
  {
    key: "tribunal",
    label: "The committee debates",
    detail: "Advocate vs. Challenger vs. Auditor, plus the sector specialists",
    weight: 0.27,
  },
  {
    key: "lead",
    label: "Lead analyst weighs in",
    detail: "Synthesizing the debate and settling the scenario weights",
    weight: 0.12,
  },
  {
    key: "memo",
    label: "Writing your memo",
    detail: "Drafting the plain-English investment memo",
    weight: 0.08,
  },
];

export const GROUP_STEPS: PipelineStep[] = [
  {
    key: "universe",
    label: "Resolving the universe",
    detail: "Finding the companies that match your criteria",
    weight: 0.12,
  },
  {
    key: "scoring",
    label: "Scoring the names",
    detail: "Valuation, growth and sentiment for every candidate",
    weight: 0.28,
  },
  {
    key: "deliberate",
    label: "The committee deliberates",
    detail: "One relative-value debate across the whole group",
    weight: 0.45,
  },
  {
    key: "memo",
    label: "Writing the group memo",
    detail: "Ranking every name with a reason",
    weight: 0.15,
  },
];

export const SCAN_STEPS: PipelineStep[] = [
  {
    key: "universe",
    label: "Screening the market",
    detail: "Filtering for cheap, growing companies",
    weight: 0.35,
  },
  {
    key: "tone",
    label: "Reading management tone",
    detail: "Scoring MD&A language and news sentiment",
    weight: 0.45,
  },
  {
    key: "rank",
    label: "Ranking the ideas",
    detail: "Blending the signals into interest scores",
    weight: 0.2,
  },
];

/** Typical wall-clock estimate per run kind (ms) used to pace the stepper. */
export const ESTIMATED_RUN_MS = {
  single: 4.5 * 60_000,
  group: 3 * 60_000,
  scan: 75_000,
} as const;

export type RunKind = keyof typeof ESTIMATED_RUN_MS;

/**
 * Map elapsed ms onto the index of the currently "active" step.
 * Caps at the last step — the run finishes only when the response lands.
 */
export function activeStepIndex(steps: PipelineStep[], elapsedMs: number, totalMs: number): number {
  const frac = Math.max(0, Math.min(1, elapsedMs / Math.max(1, totalMs)));
  let acc = 0;
  for (let i = 0; i < steps.length; i++) {
    acc += steps[i].weight;
    if (frac < acc) return i;
  }
  return steps.length - 1;
}

export function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return `${mm}:${String(ss).padStart(2, "0")}`;
}
