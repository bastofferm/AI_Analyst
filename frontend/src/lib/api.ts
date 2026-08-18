// Trimmed API client for the standalone AI_Analyst committee app.
// Talks to the standalone FastAPI backend (default http://127.0.0.1:8027).
//
// LLM-backed endpoints take {provider, model, api_key}; anything a caller omits
// is filled from the browser-side vault in lib/llm.ts (see withSessionLlm).

import { loadVault, selection, touchLlmActivity } from "./llm";

/** Fields every provider-aware request shares. */
export type LlmRequestFields = {
  provider?: string | null;
  model?: string | null;
  api_key?: string | null;
};

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8027";

export type Jurisdiction = "US" | "JP" | "INTL";

async function fetchJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

// Session cache + in-flight dedup for static reference lookups (market list,
// sector/industry filters). All five views stay mounted (forceMount in
// app-shell), so three of them call useMarkets() and two call filters() at the
// same instant — a plain result cache would still let the simultaneous calls
// through, hence sharing the pending promise rather than only the result.
// These lists do not change during a session; a reload refetches them.
const refCache = new Map<string, Promise<unknown>>();

function cached<T>(key: string, load: () => Promise<T>): Promise<T> {
  const hit = refCache.get(key) as Promise<T> | undefined;
  if (hit) return hit;
  const p = load().catch((err) => {
    // Never cache a failure — the next caller should be able to retry.
    refCache.delete(key);
    throw err;
  });
  refCache.set(key, p);
  return p;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Fill in provider/model/key from the session vault for any request that did not
 *  carry them explicitly. Keeps callers that already spread `llmBody(...)` intact,
 *  and lets a request with no key at all fall back to the server's env key. */
function withSessionLlm<T extends LlmRequestFields>(body: T): T {
  if (typeof window === "undefined") return body;
  const vault = loadVault();
  const sel = selection(vault);
  const next: T = { ...body };
  if (!next.provider) next.provider = sel.provider;
  if (!next.model && next.provider === sel.provider && sel.model) next.model = sel.model;
  if (!(next.api_key || "").trim() && next.provider === sel.provider && sel.apiKey) {
    next.api_key = sel.apiKey;
  }
  // Anything that reaches for a key counts as activity, so the idle wipe never
  // fires in the middle of a multi-minute committee run.
  touchLlmActivity();
  return next;
}

// ---------------------------------------------------------------- GICS meta
export type SectorOption = { code: string; name: string };
export type IndustryOption = { code: string; name: string; sector_code: string };
export type ExchangeOption = { value: string; label: string; count: number };
export type MetaResponse = {
  jurisdiction: Jurisdiction;
  filters: { exchanges: ExchangeOption[]; sectors: SectorOption[]; industries: IndustryOption[] };
};

// ---------------------------------------------------------------- screener
export type Range = { min?: number | null; max?: number | null };
export type FilterDef = {
  key: string; label: string; group: string; unit: string;
  decimals: number; suggested_min: number | null; suggested_max: number | null;
};
export type ScreenerMetaResponse = { filters: FilterDef[]; groups: string[]; sort_keys: string[] };
export type ScreenerUniverse = {
  jurisdiction: Jurisdiction;
  country_code?: string | null;   // ISO-2, only meaningful when jurisdiction=INTL
  region?: string | null;         // INTL region bucket (e.g. "Europe"), only meaningful when jurisdiction=INTL
  exchanges?: string[] | null;
  sectors?: string[] | null;
  industries?: string[] | null;
  portfolio_tickers?: string[] | null;
};
export type ScreenerSort = { key: string; dir: "asc" | "desc" };
export type ScreenerRow = {
  ticker: string; name: string; jurisdiction: Jurisdiction;
  sector: string | null; metrics: Record<string, number | null>;
  /** CIK (US, zero-padded) or EDINET code (JP); null for INTL — see GET /logos/{id}. */
  logo_id?: string | null;
};
export type ScreenerRunRequest = {
  universe: ScreenerUniverse;
  filters: Record<string, Range>;
  sort: ScreenerSort;
  limit: number;
};
export type ScreenerRunResponse = {
  rows: ScreenerRow[]; total_matched: number;
  applied_filters: Record<string, Range>; applied_universe: ScreenerUniverse; applied_sort: ScreenerSort;
};
export type ScreenerAiRequest = {
  prompt: string; jurisdiction: Jurisdiction;
  provider?: string | null; model?: string | null; api_key?: string | null;
};
export type ScreenerAiResponse = {
  filters: Record<string, Range>; universe: ScreenerUniverse; sort: ScreenerSort;
  rationale: string | null; warnings: string[];
};

// ---------------------------------------------------------------- valuation & analytics blocks
// Shapes verified against backend/ai_analyst/committee/{valuation,charts,schemas}.py
// and the marketdata/comps analytics builders. Everything is optional/nullable and
// components must degrade gracefully (INTL names ship most blocks empty).
export type TriMethod = {
  label: string;
  low?: number | null;
  high?: number | null;
  mid?: number | null;
  primary?: boolean;
};
export type Triangulation = {
  methods?: TriMethod[] | null;
  primary_fair_value?: number | null;
  primary_method?: string | null;
  implied_upside_pct?: number | null;
  blended_fair_value?: number | null;
  current_price?: number | null;
};
export type WaterfallItem = { name: string; value: number; is_total?: boolean };
export type DcfFull = {
  fcfs?: number[] | null;
  discounted_fcfs?: number[] | null;
  terminal_value?: number | null;
  terminal_pv?: number | null;
  waterfall?: WaterfallItem[] | null;
  enterprise_value?: number | null;
  net_debt?: number | null;
  equity_value?: number | null;
  per_share_value?: number | null;
  current_price?: number | null;
  assumptions?: Record<string, unknown> | null;
  projected_income?: Record<string, unknown>[] | null;
  projected_cashflow?: Record<string, unknown>[] | null;
};
export type Scenario = {
  label: string;
  weight?: number | null;
  per_share_value?: number | null;
  wacc_pct?: number | null;
  terminal_growth_pct?: number | null;
  ebit_margin_pct?: number | null;
  rev_growth_pct?: number[] | number | null;
  enterprise_value?: number | null;
  equity_value?: number | null;
  rationale?: string | null;
  implemented?: boolean;
  message?: string | null;
  dcf_full?: DcfFull | null;
};
export type ScenarioSet = {
  scenarios?: Scenario[] | null;
  probability_weighted_fair_value?: number | null;
  current_price?: number | null;
  implied_upside_pct?: number | null;
  implemented?: boolean;
};
export type SotpSegment = {
  segment?: string | null;
  revenue?: number | null;
  growth_pct?: number | null;
  operating_margin_pct?: number | null;
  wacc_pct?: number | null;
  fcf_conversion?: number | null;
  enterprise_value?: number | null;
};
export type Sotp = {
  available?: boolean;
  per_share?: { upside?: number | null; base?: number | null; downside?: number | null } | null;
  weights?: Record<string, number | null> | null;
  weighted_per_share?: number | null;
  primary_per_share?: number | null;
  segments_base?: SotpSegment[] | null;
  note?: string | null;
};
export type ReverseDcf = {
  available?: boolean;
  implied_growth_pct?: number | null;
  base_growth_pct?: number | null;
  bounded?: boolean;
  note?: string | null;
};
export type SensitivityGrid = {
  growth_axis?: number[] | null;
  wacc_axis?: number[] | null;
  per_share?: (number | null)[][] | null; // rows = growth axis, cols = WACC axis
  base_growth?: number | null;
  base_wacc?: number | null;
};
export type PricePoint = { date: string; close: number };
export type CashflowYear = {
  fiscal_year?: number | null;
  revenue?: number | null;
  free_cash_flow?: number | null;
  capex?: number | null;
  capex_pct_revenue?: number | null;
  dividends?: number | null;
  buybacks?: number | null;
  roic_pct?: number | null;
  [key: string]: unknown;
};
export type QuarterRow = {
  fiscal_year?: number | null;
  fiscal_period?: string | null;
  revenue?: number | null;
  yoy_rev_growth_pct?: number | null;
  [key: string]: unknown;
};
export type QuarterlyData = {
  available?: boolean;
  quarters?: QuarterRow[] | null;
  ttm?: Record<string, unknown> | null;
  note?: string | null;
  [key: string]: unknown;
};
export type CompsRow = { ticker?: string | null; name?: string | null; [metric: string]: unknown };
export type CompsData = {
  available?: boolean;
  target?: CompsRow | null;
  sector_peers?: CompsRow[] | null;
  peer_median?: Record<string, number | null> | null;
  implied?: Record<string, unknown> | null;
  [key: string]: unknown;
};
export type WaccData = {
  wacc_pct?: number | null;
  source?: string | null;
  sector_scope?: string | null;
  note?: string | null;
  [key: string]: unknown;
};
export type IncrementalRoic = {
  available?: boolean;
  incremental_roic_pct?: number | null;
  spread_vs_wacc_pct?: number | null;
  value_accretive?: boolean | null;
  [key: string]: unknown;
};
export type SegmentTrendPoint = { fiscal_year: number; revenue?: number | null; [key: string]: unknown };
export type CommitteeAnalytics = {
  wacc?: WaccData | null;
  comps?: CompsData | null;
  cashflow_history?: CashflowYear[] | null;
  incremental_roic?: IncrementalRoic | null;
  segment_trend?: Record<string, SegmentTrendPoint[]> | null;
  price_history?: Record<string, PricePoint[]> | null;
  price_peers?: string[] | null;
  quarterly?: QuarterlyData | null;
  base_assumptions?: Record<string, number | null> | null;
  current_price?: number | null;
  shares?: number | null;
  net_debt?: number | null;
  sensitivity_grid?: SensitivityGrid | null;
  [key: string]: unknown;
};
export type SensitivityAdjustment = {
  driver?: string | null;
  base_value?: number | null;
  stressed_value?: number | null;
  fair_value_impact_pct?: number | null;
  rationale?: string | null;
};
export type PeerComparisonMetric = {
  metric?: string | null;
  target_value?: number | null;
  peer_median?: number | null;
  premium_discount_pct?: number | null;
  interpretation?: string | null;
};
export type SpecialistVerdict = {
  analyst_key?: string | null;
  analyst?: string | null;
  thesis?: string | null;
  sensitivity_adjustments?: SensitivityAdjustment[] | null;
  peer_comparison_metrics?: PeerComparisonMetric[] | null;
  dcf_tilt?: Record<string, number> | null;
  risk_flags?: string[] | null;
  confidence?: number | null;
};
export type TopHolder = {
  manager?: string | null;
  classification?: string | null;
  weight_pct?: number | null;
  shares_changed?: number | null;
  is_passive?: boolean | null;
  market_value_usd?: number | null;
  shares_held?: number | null;
  [key: string]: unknown;
};
export type OwnershipSummary = {
  available?: boolean;
  quarter?: string | null;
  holder_count?: number | null;
  top_holders?: TopHolder[] | null;
  net_direction?: string | null;
  passive_share_of_reported_pct?: number | null;
  notable_adds?: unknown[] | null;
  notable_reduces?: unknown[] | null;
  [key: string]: unknown;
};

// ---------------------------------------------------------------- sector pulse / prices / KPIs
export type SectorReturnRow = {
  jurisdiction: string;
  grouping_level: string;
  gics_code: string;
  gics_name: string;
  total_market_cap?: number | null;
  market_cap_currency?: string | null;
  pe_ratio?: number | null;
  ret_1d?: number | null;
  ret_1w?: number | null;
  ret_1m?: number | null;
  ret_ytd?: number | null;
  level_series?: number[] | null;
  as_of?: string | null;
};

// Sector hover popout: top-N constituents by market cap + "Other"/"Total" rollups.
export type SectorConstituentRow = {
  ticker?: string | null;          // null on the rollup rows
  name: string;
  market_cap?: number | null;
  weight_pct?: number | null;      // fraction of sector market cap
  ret_1d?: number | null;
  ret_1w?: number | null;
  ret_1m?: number | null;
  pe_ratio?: number | null;
};
export type SectorConstituentsResponse = {
  jurisdiction: string;
  gics_code: string;
  gics_name: string;
  total_market_cap: number;
  n_tickers: number;
  top: SectorConstituentRow[];
  other?: SectorConstituentRow | null;
  total: SectorConstituentRow;
  /** Newest close behind the 1D/1W/1M columns — the feed is not always current. */
  prices_as_of?: string | null;
};
export type PricesResponse = {
  ticker: string;
  date_from: string;
  date_to: string;
  prices: PricePoint[];
};
export type CompanySearchResult = {
  ticker: string;
  name: string;
  jurisdiction: Jurisdiction;
  /** ISO-2 listing country — "US"/"JP", or the real country for INTL names (FR, DE, NL…). */
  country_code?: string | null;
  country_name?: string | null;
  sector?: string | null;
  market_cap?: number | null; // home currency (JPY for JP, USD otherwise)
  /** CIK (US, zero-padded) or EDINET code (JP); null for INTL — see GET /logos/{id}. */
  logo_id?: string | null;
};
export type CompanySearchResponse = { query: string; results: CompanySearchResult[] };

export type CompanyProfile = {
  name: string;
  name_local?: string | null;
  sector?: string | null;
  industry_group?: string | null;
  exchange?: string | null;
  currency: string;
  fy_min?: number | null;
  fy_max?: number | null;
  shares_outstanding?: number | null;
  market_cap?: number | null;
  /** Filing-authority id: CIK (US) or EDINET code (JP). */
  entity_id?: string | null;
  entity_id_label?: string | null;
  logo_id?: string | null;
};
export type CompanyPriceStats = {
  last?: number | null;
  last_date?: string | null;
  high_52w?: number | null;
  low_52w?: number | null;
  change_1y?: number | null; // fraction
};
export type CompanyDataRow = {
  key: string;
  label: string;
  unit: "currency" | "pct" | "ratio";
  group: string;
  values: (number | null)[]; // aligned to `years`
};
export type CompanyDataResponse = {
  ticker: string;
  jurisdiction: "US" | "JP";
  profile: CompanyProfile;
  price: CompanyPriceStats;
  years: number[];
  statement_rows: CompanyDataRow[];
  ratio_rows: CompanyDataRow[];
  source_note: string;
};

// Spot FX from the macro warehouse: units of currency per 1 USD.
export type FxResponse = { as_of?: string | null; rates: Record<string, number> };

export type KpiPoint = { period: string; value: number | null };
export type KpiChip = {
  label: string;
  value?: number | null;
  formatted: string;
  delta_pct?: number | null;
  delta_label?: string | null;
  delta_direction?: "up" | "down" | "neu";
  series?: KpiPoint[] | null;
};
export type KpiResponse = { ticker: string; period?: string | null; chips: Record<string, KpiChip> };

// ---------------------------------------------------------------- committee
export type CommitteeExtraAnalyst = { name: string; mandate: string };
export type EvidenceKind =
  | "mda"
  | "filing_section"
  | "rich_filing_section"
  | "news"
  | "ownership"
  | "macro"
  | "statement"
  | "recon"
  | "yahoo"
  | "data_quality";
export type EvidenceSource = {
  kind: EvidenceKind;
  source_id: string;
  label: string;
  as_of?: string | null;
  uri?: string | null;
  source_path?: string | null;
};
export type EvidenceCitation = {
  citation_id: string;
  source_id: string;
  label?: string | null;
  quote?: string | null;
  section_id?: string | null;
  filing_id?: string | null;
  url?: string | null;
  char_offset?: number | null;
};
export type EvidenceCard = {
  card_id: string;
  kind: EvidenceKind;
  title: string;
  summary: string;
  excerpt?: string | null;
  as_of?: string | null;
  confidence?: "low" | "medium" | "high";
  source: EvidenceSource;
  citations?: EvidenceCitation[];
  tags?: string[];
};
export type EvidenceTreeNode = {
  node_id: string;
  parent_id?: string | null;
  position?: number;
  title: string;
  summary: string;
  content?: string | null;
  citations?: EvidenceCitation[];
  children_ids?: string[];
};
export type EvidenceTree = {
  tree_id: string;
  kind: EvidenceKind;
  title: string;
  root_node_id: string;
  nodes: Record<string, EvidenceTreeNode>;
  as_of?: string | null;
  source: EvidenceSource;
};
export type EvidenceBundle = {
  ticker: string;
  jurisdiction: string;
  cards: EvidenceCard[];
  trees: EvidenceTree[];
  counts: Record<string, number>;
  warnings: string[];
  truncated: boolean;
};
export type MdaAnalysis = {
  ticker?: string;
  jurisdiction?: string;
  tone_score?: number | null;
  guidance?: "positive" | "neutral" | "negative" | string | null;
  peer_percentile?: number | null;
  peer_rank?: number | null;
  peer_count?: number | null;
  summary?: string | null;
  buzzword_headlines?: string[];
  risk_flags?: string[];
  raw_excerpt?: string | null;
  warnings?: string[];
};
export type SpecialistComment = {
  analyst_key: string;
  analyst: string;
  origin?: string | null;
  focus?: string | null;
  confidence?: number | null;
  bullets: string[];
};
export type DataQualityFinding = {
  finding_id: string;
  layer: "raw" | "standardized" | "metrics" | "recon" | "yahoo_cross_check";
  severity: "info" | "low" | "medium" | "high" | "blocker";
  status?: "open" | "explained" | "resolved";
  title: string;
  message: string;
  jurisdiction?: string | null;
  ticker?: string | null;
  entity_id?: string | null;
  fiscal_year?: number | null;
  period_end?: string | null;
  metric_id?: string | null;
  line_item_id?: string | null;
  absolute_delta?: number | null;
  pct_delta?: number | null;
  evidence_ids?: string[];
  suggested_action?: string | null;
};
export type MetricReconciliation = {
  reconciliation_id: string;
  metric_id: string;
  label?: string | null;
  fiscal_year?: number | null;
  period_end?: string | null;
  standardized_value?: number | null;
  standardized_currency?: string | null;
  yahoo_value?: number | null;
  yahoo_currency?: string | null;
  absolute_delta?: number | null;
  pct_delta?: number | null;
  severity?: string | null;
  likely_driver: string;
  source_relation?: string | null;
  source_line_items?: string[];
  source_concept_ids?: string[];
  source_filing_ids?: string[];
  raw_trace?: Record<string, unknown>[];
  formula_with_values?: string | null;
};
export type DataQualityReport = {
  ticker: string;
  jurisdiction: string;
  entity_id?: string | null;
  as_of: string;
  overall_score: number;
  layer_scores: Record<string, number>;
  counts: Record<string, number>;
  findings: DataQualityFinding[];
  metric_reconciliations: MetricReconciliation[];
  coverage_gaps: Record<string, unknown>;
  repair_suggestions: string[];
  warnings: string[];
};
export type DqProposal = {
  kind: string;
  concept_id?: string | null;
  target_variable?: string | null;
  mapping_sector?: string | null;
  proposed_action?: string | null;
  confidence?: number | null;
  reasoning?: string;
  evidence_finding_ids?: string[];
  next_step?: string;
};
export type DqTriageItem = {
  finding_id: string;
  root_cause: string;
  explanation?: string;
  priority?: number;
};
export type DqFindingDeltas = {
  new?: string[];
  resolved?: string[];
  explained?: string[];
  still_open?: number;
  note?: string;
};
export type DataQualityAgentTriage = {
  triage?: DqTriageItem[];
  proposals?: DqProposal[];
  way_forward?: string[];
  narrative?: string;
};
export type DataQualityAgentOutput = {
  available: boolean;
  note?: string;
  sector_scope?: string;
  mapping_pack?: Record<string, unknown>;
  triage?: DataQualityAgentTriage;
  queued_proposal_ids?: string[];
  queue_error?: string;
  triage_skipped_reason?: string;
  finding_deltas?: DqFindingDeltas;
};
export type PromoteMappingRequest = {
  jurisdiction: string;
  concept_id: string;
  mapping_sector?: string | null;
  target_variable?: string | null;
};
export type PromoteMappingResponse = {
  status: string;
  action?: "inserted" | "updated" | null;
  mapping_id?: number | null;
  concept_id?: string | null;
  target_variable?: string | null;
  mapping_sector?: string | null;
  jurisdiction?: string | null;
};
export type CommitteeRequest = {
  ticker: string;
  target_years?: number[];
  provider?: string | null;
  api_key?: string | null;
  model?: string | null;
  config?: Record<string, unknown>;
};
export type DqWarning = {
  is_data_complete?: boolean;
  is_dq_passed?: boolean;
  completeness_report?: Record<string, unknown>;
  dq_errors?: string[];
};
/** Phase 1 of a split run: the shared, provider-independent evidence base. Its
 *  state stays on the server; `prepared_id` references it. */
export type CommitteePrepareRequest = CommitteeRequest;
export type CommitteePrepareResponse = {
  prepared_id: string;
  ticker: string;
  jurisdiction?: string | null;
  expires_at: number;
  dq_warning?: DqWarning | null;
};
/** Phase 2: one provider's debate over a prepared state. Several of these run
 *  concurrently against the same `prepared_id`. */
export type CommitteeDebateRequest = {
  prepared_id: string;
  ticker: string;
  provider?: string | null;
  api_key?: string | null;
  model?: string | null;
};
export type CommitteeResponse = {
  ticker: string;
  jurisdiction?: string | null;
  primary_fair_value?: number | null;
  triangulation?: Triangulation | null;
  reverse_dcf?: ReverseDcf | null;
  sotp?: Sotp | null;
  probability_weighted_fair_value?: number | null;
  scenarios?: ScenarioSet | null;
  analytics?: CommitteeAnalytics | null;
  segment_data?: Record<string, unknown> | null;
  rich_filing_sections?: Record<string, unknown> | null;
  ownership?: OwnershipSummary | null;
  macro?: Record<string, unknown> | null;
  evidence_bundle?: EvidenceBundle | null;
  data_quality_report?: DataQualityReport | null;
  data_quality_agent?: DataQualityAgentOutput | null;
  mda_analysis?: MdaAnalysis | null;
  specialist_comments?: SpecialistComment[];
  yahoo_fundamentals?: Record<string, unknown> | null;
  yahoo_cross_check?: Record<string, unknown> | null;
  memo?: { en?: string; de?: string } | null;
  committee_chat_history?: { role: string; content: string }[] | null;
  specialist_verdicts?: SpecialistVerdict[] | null;
  report_html?: string | null;
  completeness_report?: Record<string, unknown> | null;
  dq_errors?: string[];
  dq_warning?: DqWarning | null;
  iteration_count?: number | null;
};
export type CommitteeIterationItem = {
  iteration_number: number;
  iteration_status?: "completed" | "fallback" | string;
  user_comment?: string;
  received_user_comment?: string | null;
  prompt_template_id?: string | null;
  prompt_template_label?: string | null;
  response_markdown: string;
  revised_memo_en?: string | null;
  change_summary: string;
  cited_evidence_ids: string[];
  warnings: string[];
};
export type CommitteeIterateRequest = {
  ticker: string;
  user_comment: string;
  current_result: Record<string, unknown>;
  iteration_history?: CommitteeIterationItem[];
  provider?: string | null;
  api_key?: string | null;
  model?: string | null;
  prompt_template_id?: string | null;
  prompt_template_label?: string | null;
};
export type CommitteeIterateResponse = CommitteeIterationItem;

// ---------------------------------------------------------------- group committee
export type GroupRequest = {
  mode: "industry" | "screen";
  jurisdiction: Jurisdiction;
  country_code?: string | null;
  region?: string | null;
  sectors?: string[] | null;
  industries?: string[] | null;
  filters?: Record<string, Range> | null;
  prompt?: string | null;
  tickers?: string[] | null;
  limit?: number;
  provider?: string | null;
  api_key?: string | null;
  model?: string | null;
  config?: Record<string, unknown>;
};
/** One metric's audit trail inside the composite score. The composite is exactly
 *  the sum of every `contribution`, so the UI can show why a name ranked where it did. */
export type ScoreInput = {
  key: string;
  label: string;
  group: string;                 // "Valuation" | "Growth" | "Quality"
  metric_id?: string | null;     // warehouse metric the screener read
  unit: "pct" | "ratio";
  value?: number | null;         // raw warehouse value
  z?: number | null;             // z-score within this peer group
  weight: number;                // signed weight (negative = cheaper is better)
  contribution?: number | null;  // weight x z
  /** Why it did not score, when contribution is null. */
  note?: "missing" | "negative" | "no_spread" | null;
};
export type GroupRankItem = {
  ticker: string; name: string; sector?: string | null; stance: string;
  composite_score?: number; rationale: string; metrics?: Record<string, number | null>;
  score_inputs?: ScoreInput[];
};
export type GroupResponse = {
  universe: Record<string, unknown>;
  resolved_tickers: string[];
  ranking: GroupRankItem[];
  group_memo?: string | null;
  evidence: ScreenerRow[];
  report_html?: string | null;
  warnings: string[];
};

// ---------------------------------------------------------------- value+sentiment agent
export type AgentRequest = {
  jurisdiction: Jurisdiction;
  country_code?: string | null;
  region?: string | null;
  limit?: number;
  max_pe?: number | null;
  min_fcf_yield?: number | null;
  min_rev_yoy?: number | null;
  min_market_cap_usd?: number | null;
  include_news?: boolean;
  provider?: string | null;
  model?: string | null;
  api_key?: string | null;
};
export type AgentRow = {
  ticker: string; name: string; sector?: string | null;
  key_metrics: Record<string, number | null>;
  mda_tone?: number | null; mda_note?: string | null; mda_risk_flags: string[];
  news_sentiment?: number | null;
  // qlib alpha enrichment (null when no model is trained for the jurisdiction).
  alpha?: number | null;              // monthly expected return
  alpha_percentile?: number | null;   // rank within the shortlist (0-100)
  score_components?: Record<string, number>;
  interest_score: number; rationale: string;
};
export type AgentResponse = {
  rows: AgentRow[]; universe: Record<string, unknown>; scored_count: number;
  scoring?: Record<string, unknown>;  // weights / alpha-model actually used
  warnings: string[];
};

// ---------------------------------------------------------------- quant (qlib)
export type QuantAlphaModelMeta = {
  jurisdiction: string; label: string; horizon_months: number; annualization: number;
  trained_at: string; train_range: [string, string]; metrics: Record<string, number>; n_features: number;
};
export type QuantBackends = {
  optimizers: string[]; risk_models: string[]; alpha_sources: string[];
  alpha_models: Record<string, QuantAlphaModelMeta>;
};
export type QuantAlphaRow = {
  ticker: string; expected_return_monthly: number | null; expected_return_annual: number | null;
};
export type QuantAlphaResponse = {
  available: boolean; model: QuantAlphaModelMeta | null; rows: QuantAlphaRow[]; note?: string;
  /** Number of names the model actually scored this month (the full cross-section). */
  n_covered?: number;
};
export type QuantRiskRow = {
  ticker: string; forward_vol_annual: number; factor_exposures: Record<string, number>;
};
export type QuantRiskResponse = {
  available: boolean; n_obs?: number; factor_names?: string[];
  tickers_dropped?: string[]; rows: QuantRiskRow[]; warnings?: string[]; note?: string;
};
export type PortfolioWeight = { ticker: string; weight: number };
// Per-name predicted return & risk, both annualized and scaled to the forecast horizon.
export type QuantPerName = {
  ticker: string; weight: number;
  expected_return_annual: number; expected_return_horizon: number;
  forward_vol_annual: number; forward_vol_horizon: number;
  alpha_source: "model" | "historical";
};
// Historical-simulation distribution of the book's horizon return (see backend simulate.py).
export type QuantMoments = { mean: number; variance: number; std: number; skewness: number; kurtosis: number };
export type QuantHistBin = { x0: number; x1: number; mid: number; density: number; count: number };
export type QuantDistribution = {
  available: boolean; reason?: string; method?: string;
  horizon_months?: number; n_obs?: number; n_samples?: number; window_days?: number;
  moments?: QuantMoments;
  annualized?: { mean: number; vol: number };
  percentiles?: Record<string, number>;
  histogram?: QuantHistBin[];
  curve?: { x: number; y: number }[];
  weight_covered?: number; names_used?: number; history_from?: string | null; history_to?: string | null;
};
// Fixed-weight portfolio backtest of the optimized book on past returns (backend
// qlib_backtest.weighted_portfolio_backtest): equity vs FF market, performance, a
// full-period FF regression, and rolling factor exposures over time.
export type QuantExposurePoint = { date: string; betas: Record<string, number> };
export type QuantPortfolioBacktest = {
  available: boolean; reason?: string; benchmarked?: boolean;
  n_months?: number; history_from?: string; history_to?: string; roll_window?: number;
  performance?: QuantPerformance;
  factor_regression?: QuantFactorRegression;
  benchmark?: { available: boolean; label: string };
  curve?: QuantBacktestPoint[];
  exposures?: QuantExposurePoint[];
  weight_covered?: number;
};
export type QuantOptimizeResponse = {
  backend: string; weights: PortfolioWeight[];
  expected_return_annual: number; vol_annual: number; sharpe: number | null;
  factor_exposures: Record<string, number>; warnings: string[]; diagnostics: Record<string, unknown>;
  risk_model: string; alpha_source: string; tickers_dropped: string[]; n_obs: number;
  horizon_months?: number;
  per_name?: QuantPerName[];
  distribution?: QuantDistribution;
  portfolio_backtest?: QuantPortfolioBacktest;
};
export type QuantRetrainResponse = {
  ok: boolean; jurisdiction: string; label: string;
  rank_ic?: number | null; coverage?: number | null; error?: string | null;
  model?: QuantAlphaModelMeta | null;
};

// ---------------------------------------------------------- agentic model research
// The iterative replacement for one-shot retraining. A run is a background job on the
// server; the client starts it, then polls GET /research/{run_id} until a terminal status.
export type ResearchStatus = "queued" | "running" | "complete" | "failed" | "cancelled";

export type ResearchFinding = {
  category: string; severity: string; detail: string; evidence?: string;
};
export type ResearchValidation = {
  status?: "pass" | "warn" | "fail"; summary?: string;
  findings?: ResearchFinding[]; blocking?: boolean; source?: string;
};
export type ResearchPM = {
  decision?: "accept" | "reject" | "continue"; reasoning?: string;
  preferred_iteration?: number | null; concerns?: string[]; source?: string;
};
export type ResearchAdvisor = {
  contrarian_read?: string; orthogonal_direction?: string; reasoning?: string;
  source?: string; provider?: string | null;
};
export type ResearchProposal = {
  patch?: Record<string, unknown>; rationale?: string; hypothesis?: string;
  applied_changes?: string[]; rejected?: string[]; stop?: boolean; source?: string;
};
export type ResearchPerturbation = {
  id: string; label: string; stands_for: string; available?: boolean;
  rank_ic_degradation?: number | null; confounding_share_pct?: number | null;
};
export type ResearchRating = {
  available?: boolean; rating?: number; rating_label?: string; scale?: string;
  worst_case?: { id?: string; label?: string; rank_ic_degradation?: number | null };
  mean_degradation?: number | null; deconfounding?: string; confounder?: string | null;
  perturbations?: ResearchPerturbation[]; reason?: string;
};
export type ResearchBucket = {
  cut: string; bucket: string; n_names: number; n_months: number;
  rank_ic: number | null; rank_icir: number | null; rank_ic_t_stat: number | null;
  r2_oos: number | null; top_decile_spread: number | null; coverage: number | null;
  thin: boolean;
};
export type ResearchBreakdowns = {
  available?: boolean; cuts?: Record<string, ResearchBucket[]>;
};
export type ResearchHeadline = {
  robustness_rating?: number | null; robustness_label?: string | null;
  rank_ic?: number | null; rank_ic_ci95?: (number | null)[];
  rank_icir_annualized?: number | null; r2_oos?: number | null;
  long_short_sharpe?: number | null; turnover?: number | null; n_months?: number | null;
};
/** The full per-round validation report — the object both the drawer and the PDF render. */
export type ResearchReport = {
  iteration: number; market: string; horizon: string; horizon_months?: number;
  spec?: Record<string, unknown>; spec_hash?: string;
  spec_changes?: string[]; spec_rejected?: string[];
  headline?: ResearchHeadline;
  sections?: Record<string, Record<string, unknown>>;
  elapsed_seconds?: number | null;
};
export type ResearchIteration = {
  iteration: number; spec_hash?: string | null;
  patch_json?: { changes?: string[]; rejected?: string[] };
  /** The battery and the sub-population tables, also reachable via report_json.sections. */
  metrics_json?: Record<string, Record<string, unknown>>;
  breakdown_json?: ResearchBreakdowns;
  rating_json?: ResearchRating;
  validation_json?: ResearchValidation;
  pm_json?: ResearchPM;
  advisor_json?: ResearchAdvisor;
  researcher_json?: ResearchProposal;
  report_json?: ResearchReport;
  elapsed_seconds?: number | null;
};
export type ResearchRun = {
  run_id: string; model_key: string; jurisdiction: string; label: string;
  status: ResearchStatus; provider?: string | null; advisor_provider?: string | null;
  max_iterations: number; iterations_done: number; current_stage?: string | null;
  baseline_json?: Record<string, unknown>;
  champion_iteration?: number | null; champion_kind?: string | null;
  champion_score?: number | null;
  promoted: boolean; promotion_reason?: string | null; stop_reason?: string | null;
  started_at?: string | null; completed_at?: string | null;
  elapsed_seconds?: number | null; error?: string | null;
  summary_json?: Record<string, unknown>;
  iterations?: ResearchIteration[];
  /** Present only on the "no run yet" placeholder from /research/latest. */
  available?: false; note?: string;
};
export type ResearchStartRequest = LlmRequestFields & {
  jurisdiction?: Jurisdiction; label?: string; max_iterations?: number;
  advisor_provider?: string | null; advisor_model?: string | null;
  advisor_api_key?: string | null;
  spec_overrides?: Record<string, unknown>; offline?: boolean;
};
export type ResearchStartResponse = {
  ok: boolean; run_id: string; status?: string; model_key?: string; label?: string;
  error?: string;
};
export type ResearchRunSummary = {
  run_id: string; model_key: string; label: string; status: ResearchStatus;
  iterations_done: number; max_iterations: number;
  champion_iteration?: number | null; champion_kind?: string | null;
  champion_score?: number | null; promoted: boolean; stop_reason?: string | null;
  started_at?: string | null; completed_at?: string | null; elapsed_seconds?: number | null;
};
export type QuantPerformance = {
  annualized_return: number; annualized_vol: number; sharpe: number | null;
  sortino: number | null; max_drawdown: number; hit_rate: number; cumulative_return: number;
  benchmark_annualized_return: number; excess_annualized_return: number;
  tracking_error: number; information_ratio: number | null; beta_vs_market: number | null;
  n_months: number;
};
export type QuantFactorRegression = {
  available: boolean; reason?: string;
  alpha_monthly?: number; alpha_annualized?: number; alpha_tstat?: number | null;
  r2?: number; betas?: Record<string, number>; n_months?: number;
};
export type QuantBacktestPoint = { date: string; ret: number; equity: number; bench_equity?: number };
export type QuantBacktestResponse = {
  available: boolean; reason?: string; out_of_sample?: boolean;
  jurisdiction?: string; label?: string; horizon_months?: number; rebalance?: string;
  topk?: number; long_short?: boolean; n_periods?: number;
  metrics?: Record<string, number>; ic?: Record<string, number | null>;
  performance?: QuantPerformance;
  factor_regression?: QuantFactorRegression;
  benchmark?: { available: boolean; label: string };
  curve?: QuantBacktestPoint[];
};
export type QuantAlphaRequest = { jurisdiction?: Jurisdiction; tickers?: string[] | null; top?: number; label?: string };
export type QuantRiskRequest = { jurisdiction?: Jurisdiction; tickers: string[]; lookback_months?: number; num_factors?: number };
export type QuantOptimizeRequest = {
  jurisdiction?: Jurisdiction; tickers: string[]; optimizer?: string; risk_model?: string;
  alpha_source?: string; label?: string; lookback_months?: number; num_factors?: number;
  lamb?: number | null; delta?: number; b_dev?: number; risk_free_annual?: number;
};
export type QuantBacktestRequest = {
  jurisdiction?: Jurisdiction; start?: string | null; end?: string | null; topk?: number; long_short?: boolean; label?: string;
};

export type ScreenerMarketsPrimary = { jurisdiction: "US" | "JP"; label: string; count: number };
export type ScreenerMarketsCountry = { code: string; name: string; count: number };
export type ScreenerMarketsRegion = { region: string; total: number; countries: ScreenerMarketsCountry[] };
export type ScreenerMarketsResponse = {
  primary: ScreenerMarketsPrimary[];
  intl_regions: ScreenerMarketsRegion[];
};

export const api = {
  health: () => fetchJSON<{ status: string; db: string; schema: string }>(`/api/healthz`),

  filters: (jurisdiction: Jurisdiction, country_code?: string | null) => {
    const path = `/api/meta/filters${qs({ jurisdiction, country_code })}`;
    return cached(path, () => fetchJSON<MetaResponse>(path));
  },

  // US/JP only — the backend routers 422 on other jurisdictions; never call for INTL.
  sectorReturns: (jurisdiction: "US" | "JP", level: "sector" | "industry_group" = "sector") =>
    fetchJSON<SectorReturnRow[]>(`/api/sector/returns${qs({ jurisdiction, level })}`),

  sectorConstituents: (gics_code: string, jurisdiction: "US" | "JP" = "US", top_n = 10) =>
    fetchJSON<SectorConstituentsResponse>(`/api/sector/constituents${qs({ gics_code, jurisdiction, top_n })}`),

  prices: (ticker: string, jurisdiction: "US" | "JP", date_from?: string, date_to?: string) =>
    fetchJSON<PricesResponse>(
      `/api/prices/${encodeURIComponent(ticker)}${qs({ jurisdiction, date_from, date_to })}`
    ),

  kpis: (ticker: string, jurisdiction: "US" | "JP", year_min?: number, year_max?: number) =>
    fetchJSON<KpiResponse>(
      `/api/kpis/${encodeURIComponent(ticker)}${qs({ jurisdiction, year_min, year_max })}`
    ),

  fx: () => cached(`/api/fx`, () => fetchJSON<FxResponse>(`/api/fx`)),

  companySearch: (q: string, limit = 8) =>
    fetchJSON<CompanySearchResponse>(`/api/company/search${qs({ q, limit })}`),

  // US/JP only — the warehouse has no standardized statement layer for INTL.
  companyData: (ticker: string, jurisdiction: "US" | "JP") =>
    fetchJSON<CompanyDataResponse>(
      `/api/company/${encodeURIComponent(ticker)}${qs({ jurisdiction })}`
    ),

  screenerMeta: () =>
    cached(`/api/screener/meta`, () => fetchJSON<ScreenerMetaResponse>(`/api/screener/meta`)),

  screenerMarkets: () =>
    cached(`/api/screener/markets`, () => fetchJSON<ScreenerMarketsResponse>(`/api/screener/markets`)),

  screenerRun: (body: ScreenerRunRequest) =>
    fetchJSON<ScreenerRunResponse>(`/api/screener/run`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  screenerAi: (body: ScreenerAiRequest) =>
    fetchJSON<ScreenerAiResponse>(`/api/screener/ai`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  valueSentimentAgent: (body: AgentRequest) =>
    fetchJSON<AgentResponse>(`/api/screener/agent/value-sentiment`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  committee: (body: CommitteeRequest) =>
    fetchJSON<CommitteeResponse>(`/api/ai/committee`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  committeePrepare: (body: CommitteePrepareRequest) =>
    fetchJSON<CommitteePrepareResponse>(`/api/ai/committee/prepare`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  // Each parallel debate carries its own provider/model/key; withSessionLlm only
  // fills gaps, so it leaves those alone — but it also arms the idle-wipe timer,
  // which a multi-minute debate must have or its keys get erased mid-flight.
  committeeDebate: (body: CommitteeDebateRequest) =>
    fetchJSON<CommitteeResponse>(`/api/ai/committee/debate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  committeeIterate: (body: CommitteeIterateRequest) =>
    fetchJSON<CommitteeIterateResponse>(`/api/ai/committee/iterate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  committeeGroup: (body: GroupRequest) =>
    fetchJSON<GroupResponse>(`/api/ai/committee/group`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(withSessionLlm(body)),
    }),

  promoteMapping: (body: PromoteMappingRequest) =>
    fetchJSON<PromoteMappingResponse>(`/api/ai/committee/promote_mapping`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  // ------------------------------------------------------------ quant (qlib)
  quantBackends: () => cached(`/api/quant/backends`, () => fetchJSON<QuantBackends>(`/api/quant/backends`)),

  quantAlpha: (body: QuantAlphaRequest) =>
    fetchJSON<QuantAlphaResponse>(`/api/quant/alpha`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  quantRisk: (body: QuantRiskRequest) =>
    fetchJSON<QuantRiskResponse>(`/api/quant/risk`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  quantOptimize: (body: QuantOptimizeRequest) =>
    fetchJSON<QuantOptimizeResponse>(`/api/quant/optimize`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  quantBacktest: (body: QuantBacktestRequest) =>
    fetchJSON<QuantBacktestResponse>(`/api/quant/backtest`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  // Slow (~1–3 min): trains + persists the (jurisdiction, horizon) alpha model server-side.
  quantRetrain: (body: { jurisdiction?: Jurisdiction; label?: string }) =>
    fetchJSON<QuantRetrainResponse>(`/api/quant/retrain`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  // --- agentic model research -------------------------------------------------------
  // Returns a run_id immediately; the work continues server-side. This is the first quant
  // endpoint that needs LLM credentials, hence withSessionLlm.
  quantResearchStart: (body: ResearchStartRequest) =>
    fetchJSON<ResearchStartResponse>(`/api/quant/research/start`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withSessionLlm(body)),
    }),
  /** Poll target while a run is in flight. */
  quantResearchGet: (runId: string) =>
    fetchJSON<ResearchRun>(`/api/quant/research/${encodeURIComponent(runId)}`),
  /** Most recent run for a market/horizon, so the panel opens populated after a reload. */
  quantResearchLatest: (jurisdiction: Jurisdiction, label: string) =>
    fetchJSON<ResearchRun>(`/api/quant/research/latest${qs({ jurisdiction, label })}`),
  quantResearchRuns: (jurisdiction?: Jurisdiction, label?: string, limit = 20) =>
    fetchJSON<{ runs: ResearchRunSummary[] }>(
      `/api/quant/research/runs${qs({ jurisdiction, label, limit })}`),
  quantResearchCancel: (runId: string) =>
    fetchJSON<{ ok: boolean; run_id: string; note?: string }>(
      `/api/quant/research/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  /** Direct link — the browser fetches the PDF itself, so no fetch wrapper. */
  quantResearchReportUrl: (runId: string) =>
    `${API_BASE}/api/quant/research/${encodeURIComponent(runId)}/report.pdf`,
};
