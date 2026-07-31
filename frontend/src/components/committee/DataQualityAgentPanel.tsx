"use client";

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  ArrowUpCircle,
  ChevronDown,
  ChevronRight,
  Database,
  Gauge,
  ListChecks,
  RefreshCw,
  Scale,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import type {
  DataQualityAgentOutput,
  DataQualityFinding,
  DataQualityReport,
  DqProposal,
  MetricReconciliation,
} from "@/lib/api";
import { api } from "@/lib/api";
import { num } from "@/lib/fmt";

type PromoteState = { state: "idle" | "loading" | "done" | "error"; action?: string; msg?: string };
const promoteKey = (p: DqProposal) => `${p.concept_id || ""}|${p.mapping_sector || ""}`;

const LAYER_LABELS: Record<string, string> = {
  raw: "Raw",
  standardized: "Std",
  metrics: "Metrics",
  recon: "Recon",
  yahoo_cross_check: "Yahoo",
};

const SEVERITY_RANK: Record<string, number> = {
  blocker: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

type SubTab = "dq" | "agent";

export function DataQualityAgentPanel({
  report,
  agent,
}: {
  report?: DataQualityReport | null;
  agent?: DataQualityAgentOutput | null;
}) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<SubTab>("dq");
  const [expandedFinding, setExpandedFinding] = useState<string | null>(null);
  const [promote, setPromote] = useState<Record<string, PromoteState>>({});
  const findings = report?.findings || [];
  const reconciliations = report?.metric_reconciliations || [];
  const severe = useMemo(
    () =>
      [...findings]
        .sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9))
        .slice(0, 10),
    [findings],
  );

  if (!report) return null;

  const triage = agent?.triage;
  const proposals = triage?.proposals || [];
  const wayForward = triage?.way_forward || [];
  const deltas = agent?.finding_deltas;
  const newCount = deltas?.new?.length || 0;
  const resolvedCount = deltas?.resolved?.length || 0;
  const queuedCount = agent?.queued_proposal_ids?.length || 0;

  const doPromote = async (proposal: DqProposal) => {
    if (!proposal.concept_id || !proposal.target_variable) return;
    const key = promoteKey(proposal);
    const label = `${proposal.concept_id} → ${proposal.target_variable}${proposal.mapping_sector ? ` (${proposal.mapping_sector})` : ""}`;
    if (
      typeof window !== "undefined" &&
      !window.confirm(
        `Promote to the PRODUCTION mapping table?\n\n${label}\n\nThis writes map_concept_to_taxonomy_versioned and changes how every future standardization run maps this concept.`,
      )
    )
      return;
    setPromote((s) => ({ ...s, [key]: { state: "loading" } }));
    try {
      const res = await api.promoteMapping({
        jurisdiction: report?.jurisdiction || "US",
        concept_id: proposal.concept_id,
        mapping_sector: proposal.mapping_sector,
        target_variable: proposal.target_variable,
      });
      setPromote((s) => ({ ...s, [key]: { state: "done", action: res.action || "promoted" } }));
    } catch (err) {
      setPromote((s) => ({ ...s, [key]: { state: "error", msg: err instanceof Error ? err.message : String(err) } }));
    }
  };

  const highCount = report.counts?.high_or_blocker || severe.filter((item) => ["high", "blocker"].includes(item.severity)).length;
  const overallScore = typeof report.overall_score === "number" && Number.isFinite(report.overall_score) ? report.overall_score : null;
  const scoreColor = overallScore === null ? "text-muted" : overallScore >= 85 ? "text-green" : overallScore >= 70 ? "text-amber" : "text-red";

  return (
    <div className="border-b border-border bg-white/35">
      <div className="flex flex-col gap-3 border-b border-border-soft p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <button type="button" onClick={() => setOpen((value) => !value)} className="flex min-w-0 items-start gap-2 text-left">
            <span className="mt-0.5 flex h-6 w-6 flex-shrink-0 items-center justify-center rounded border border-border bg-white text-muted">
              {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
            </span>
            <div className="min-w-0">
              <div className="label">Data quality</div>
              <div className="mt-1 text-[12px] text-muted">
                Raw, standardized, metrics, recon and Yahoo-relative audit as of {report.as_of}
              </div>
            </div>
          </button>
          <div className="flex flex-wrap items-center gap-2">
            <ScoreTile label="Score" value={overallScore === null ? "n/a" : overallScore.toFixed(1)} colorClass={scoreColor} icon={<Gauge className="h-3.5 w-3.5" />} />
            <ScoreTile label="High" value={String(highCount)} colorClass={highCount ? "text-red" : "text-green"} icon={<AlertTriangle className="h-3.5 w-3.5" />} />
            <ScoreTile label="Recon" value={String(reconciliations.length)} colorClass="text-navy" icon={<Scale className="h-3.5 w-3.5" />} />
          </div>
        </div>

        <div className="grid gap-2 sm:grid-cols-5">
          {Object.entries(report.layer_scores || {}).map(([layer, score]) => (
            <div key={layer} className="min-w-0 rounded border border-border bg-white px-2 py-2">
              <div className="truncate text-[9px] uppercase tracking-[0.14em] text-muted">
                {LAYER_LABELS[layer] || layer}
              </div>
              <div className={`mt-1 text-[14px] font-semibold ${score >= 85 ? "text-green" : score >= 70 ? "text-amber" : "text-red"}`}>
                {Number.isFinite(score) ? score.toFixed(0) : "n/a"}
              </div>
            </div>
          ))}
        </div>
      </div>

      {open && (
        <div>
          <div className="flex items-center gap-1 border-b border-border-soft bg-white/50 px-4 pt-3">
            <TabButton active={tab === "dq"} onClick={() => setTab("dq")} icon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}>
              Data quality
              <TabCount value={findings.length} />
            </TabButton>
            <TabButton active={tab === "agent"} onClick={() => setTab("agent")} icon={<Wrench className="h-3.5 w-3.5" aria-hidden />}>
              Committee triage
              <TabCount value={proposals.length} />
              {(newCount > 0 || queuedCount > 0) && (
                <span className="ml-1 rounded-full bg-red/10 px-1.5 py-0.5 text-[9px] font-semibold text-red">
                  {newCount + queuedCount}
                </span>
              )}
            </TabButton>
          </div>

          {tab === "dq" && (
            <div>
              {severe.length === 0 ? (
                <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-muted">
                  <ShieldCheck className="h-4 w-4" aria-hidden />
                  No material data-quality findings surfaced for this run.
                </div>
              ) : (
                <div>
                  {severe.map((finding) => (
                    <FindingRow
                      key={finding.finding_id}
                      finding={finding}
                      expanded={expandedFinding === finding.finding_id}
                      onToggle={() => setExpandedFinding(expandedFinding === finding.finding_id ? null : finding.finding_id)}
                    />
                  ))}
                </div>
              )}

              {reconciliations.length > 0 && (
                <div className="border-t border-border-soft p-4">
                  <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-muted">
                    <Scale className="h-3.5 w-3.5" aria-hidden />
                    Metric reconciliation
                  </div>
                  <div className="grid gap-2">
                    {reconciliations.slice(0, 6).map((item) => (
                      <ReconciliationRow key={item.reconciliation_id} item={item} />
                    ))}
                  </div>
                </div>
              )}

              {(report.repair_suggestions || []).length > 0 && (
                <div className="border-t border-border-soft p-4">
                  <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-muted">
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                    Suggested repair actions
                  </div>
                  <ul className="grid gap-1 text-[11px] text-navy">
                    {report.repair_suggestions.slice(0, 6).map((item) => (
                      <li key={item} className="flex gap-2">
                        <span className="mt-1 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-muted" />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(report.warnings || []).length > 0 && (
                <div className="border-t border-border-soft p-4 text-[11px] text-amber">
                  {(report.warnings || []).slice(0, 4).join(" | ")}
                </div>
              )}
            </div>
          )}

          {tab === "agent" && (
            <div>
              {!agent?.available && !triage ? (
                <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-muted">
                  <ShieldCheck className="h-4 w-4" aria-hidden />
                  {agent?.note === "disabled"
                    ? "Committee DQ agent is disabled for this run."
                    : "Committee DQ agent did not run for this ticker."}
                </div>
              ) : (
                <>
                  <div className="border-b border-border-soft bg-white/50 p-4">
                    {triage?.narrative && (
                      <div className="break-words text-[12px] leading-5 text-navy">{triage.narrative}</div>
                    )}
                    {(newCount > 0 || resolvedCount > 0 || queuedCount > 0 || agent?.triage_skipped_reason) && (
                      <div className={`flex flex-wrap items-center gap-1.5 ${triage?.narrative ? "mt-2" : ""}`}>
                        {newCount > 0 && <DeltaBadge label={`${newCount} new`} tone="red" />}
                        {resolvedCount > 0 && <DeltaBadge label={`${resolvedCount} resolved`} tone="green" />}
                        {queuedCount > 0 && <DeltaBadge label={`${queuedCount} queued`} tone="navy" />}
                        {agent?.triage_skipped_reason && <DeltaBadge label={agent.triage_skipped_reason} tone="muted" />}
                      </div>
                    )}
                  </div>

                  {proposals.length > 0 && (
                    <div className="border-t border-border-soft p-4">
                      <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-muted">
                        <Wrench className="h-3.5 w-3.5" aria-hidden />
                        Mapping & remediation proposals
                        <span className="font-normal normal-case tracking-normal text-[10px] text-muted">
                          · advisory review-queue entries, not live changes
                        </span>
                      </div>
                      <div className="grid gap-2">
                        {proposals.slice(0, 8).map((proposal, index) => (
                          <ProposalRow
                            key={`${proposal.kind}-${proposal.concept_id || index}`}
                            proposal={proposal}
                            queued={Boolean(
                              proposal.concept_id && agent?.queued_proposal_ids?.includes(proposal.concept_id),
                            )}
                            promoteState={promote[promoteKey(proposal)]}
                            onPromote={() => doPromote(proposal)}
                          />
                        ))}
                      </div>
                    </div>
                  )}

                  {wayForward.length > 0 && (
                    <div className="border-t border-border-soft p-4">
                      <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-muted">
                        <ListChecks className="h-3.5 w-3.5" aria-hidden />
                        Way forward
                      </div>
                      <ol className="grid gap-1 text-[11px] text-navy">
                        {wayForward.slice(0, 6).map((step, index) => (
                          <li key={step} className="flex gap-2">
                            <span className="mt-0.5 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full border border-border text-[9px] text-muted">
                              {index + 1}
                            </span>
                            <span>{step}</span>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}

                  {proposals.length === 0 && wayForward.length === 0 && !triage?.narrative && (
                    <div className="px-4 py-3 text-[12px] text-muted">
                      No triage proposals for this run.
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ScoreTile({
  label,
  value,
  colorClass,
  icon,
}: {
  label: string;
  value: string;
  colorClass: string;
  icon: ReactNode;
}) {
  return (
    <div className="min-w-[76px] rounded border border-border bg-white px-2 py-2 text-right">
      <div className="flex items-center justify-end gap-1 text-[9px] uppercase tracking-[0.14em] text-muted">
        {icon}
        {label}
      </div>
      <div className={`mt-1 text-[15px] font-semibold ${colorClass}`}>{value}</div>
    </div>
  );
}

function FindingRow({
  finding,
  expanded,
  onToggle,
}: {
  finding: DataQualityFinding;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="border-t border-border-soft px-4 py-3 first:border-t-0">
      <div className="flex min-w-0 items-start gap-3">
        <button
          type="button"
          title={expanded ? "Collapse finding details" : "Expand finding details"}
          onClick={onToggle}
          className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-border bg-white text-muted hover:text-navy"
        >
          {expanded ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
        </button>
        <Database className="mt-1 h-4 w-4 flex-shrink-0 text-muted" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[12px] font-semibold text-navy">{finding.title}</span>
            <Severity severity={finding.severity} />
            <span className="rounded border border-border bg-white px-1.5 py-0.5 text-[10px] text-muted">
              {LAYER_LABELS[finding.layer] || finding.layer}
            </span>
            {finding.fiscal_year && <span className="text-[10px] text-muted">FY{finding.fiscal_year}</span>}
            {typeof finding.pct_delta === "number" && <span className="text-[10px] text-muted">{finding.pct_delta.toFixed(1)}%</span>}
          </div>
          <div className="mt-1 break-words text-[12px] leading-5 text-navy">{finding.message}</div>
          <div className="mt-1 break-all font-mono text-[10px] text-muted">{finding.finding_id}</div>
        </div>
      </div>
      {expanded && (
        <div className="mt-3 ml-10 grid gap-2 border-t border-border-soft pt-3 text-[11px] text-muted">
          <Detail label="Metric" value={finding.metric_id || finding.line_item_id || "n/a"} />
          <Detail label="Entity" value={finding.entity_id || "n/a"} />
          {finding.suggested_action && <Detail label="Action" value={finding.suggested_action} />}
        </div>
      )}
    </div>
  );
}

function ReconciliationRow({ item }: { item: MetricReconciliation }) {
  return (
    <div className="min-w-0 border-t border-border-soft pt-2 first:border-t-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[12px] font-semibold text-navy">{item.label || item.metric_id}</div>
          <div className="mt-1 break-words text-[11px] text-muted">
            {item.likely_driver.replace(/_/g, " ")}
            {typeof item.pct_delta === "number" ? ` (${item.pct_delta.toFixed(1)}%)` : ""}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 text-right text-[11px]">
          <Mini label="Filing" value={`${num(item.standardized_value)} ${item.standardized_currency || ""}`} />
          <Mini label="Yahoo" value={`${num(item.yahoo_value)} ${item.yahoo_currency || ""}`} />
        </div>
      </div>
      {item.source_relation && <div className="mt-2 break-words text-[11px] leading-5 text-muted">{item.source_relation}</div>}
      <div className="mt-1 break-all font-mono text-[10px] text-muted">{item.reconciliation_id}</div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`-mb-px flex items-center gap-1.5 rounded-t border-b-2 px-3 py-2 text-[11px] font-semibold ${
        active
          ? "border-navy bg-white text-navy"
          : "border-transparent text-muted hover:text-navy"
      }`}
    >
      {icon}
      {children}
    </button>
  );
}

function TabCount({ value }: { value: number }) {
  if (!value) return null;
  return (
    <span className="ml-1 rounded-full border border-border bg-white px-1.5 py-0.5 text-[9px] font-semibold text-muted">
      {value}
    </span>
  );
}

function DeltaBadge({ label, tone }: { label: string; tone: "red" | "green" | "navy" | "muted" }) {
  const color =
    tone === "red" ? "text-red" : tone === "green" ? "text-green" : tone === "navy" ? "text-navy" : "text-muted";
  return (
    <span className={`rounded border border-border bg-white px-1.5 py-0.5 text-[10px] font-semibold ${color}`}>
      {label}
    </span>
  );
}

function ProposalRow({
  proposal,
  queued,
  promoteState,
  onPromote,
}: {
  proposal: DqProposal;
  queued: boolean;
  promoteState?: PromoteState;
  onPromote: () => void;
}) {
  const target = proposal.target_variable || "?";
  const concept = proposal.concept_id || "—";
  const isMapping = (proposal.kind || "").startsWith("mapping") && Boolean(proposal.concept_id && proposal.target_variable);
  const state = promoteState?.state || "idle";
  return (
    <div className="min-w-0 rounded border border-border bg-white px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-border bg-white px-1.5 py-0.5 text-[10px] font-semibold text-navy">
          {proposal.kind.replace(/_/g, " ")}
        </span>
        {proposal.proposed_action && (
          <span className="text-[10px] text-muted">{proposal.proposed_action.replace(/_/g, " ")}</span>
        )}
        {proposal.mapping_sector && <span className="text-[10px] text-muted">· {proposal.mapping_sector}</span>}
        {typeof proposal.confidence === "number" && (
          <span className="text-[10px] text-muted">· conf {(proposal.confidence * 100).toFixed(0)}%</span>
        )}
        {queued && state !== "done" && <DeltaBadge label="queued" tone="green" />}
        {isMapping && (
          <div className="ml-auto">
            {state === "done" ? (
              <span className="rounded border border-green/40 bg-green/10 px-2 py-0.5 text-[10px] font-semibold text-green">
                {promoteState?.action === "updated" ? "remapped ✓" : "promoted ✓"}
              </span>
            ) : (
              <button
                type="button"
                onClick={onPromote}
                disabled={state === "loading"}
                title={
                  state === "error"
                    ? promoteState?.msg
                    : "Promote this mapping into the production table (affects future standardization runs)"
                }
                className={`flex items-center gap-1 rounded border px-2 py-0.5 text-[10px] font-semibold ${
                  state === "error"
                    ? "border-red/40 text-red hover:bg-red/10"
                    : "border-border text-navy hover:bg-navy hover:text-white"
                } disabled:opacity-50`}
              >
                <ArrowUpCircle className="h-3 w-3" aria-hidden />
                {state === "loading" ? "Promoting…" : state === "error" ? "Retry promote" : "Promote"}
              </button>
            )}
          </div>
        )}
      </div>
      <div className="mt-1 break-words font-mono text-[11px] text-navy">
        {concept} <span className="text-muted">→</span> {target}
      </div>
      {proposal.reasoning && <div className="mt-1 break-words text-[11px] leading-5 text-muted">{proposal.reasoning}</div>}
      {proposal.next_step && (
        <div className="mt-1 break-words font-mono text-[10px] text-muted">{proposal.next_step}</div>
      )}
      {state === "error" && promoteState?.msg && (
        <div className="mt-1 break-words text-[10px] leading-4 text-red">{promoteState.msg}</div>
      )}
    </div>
  );
}

function Severity({ severity }: { severity: DataQualityFinding["severity"] }) {
  const color = severity === "blocker" || severity === "high" ? "text-red" : severity === "medium" ? "text-amber" : "text-muted";
  return <span className={`text-[10px] font-semibold uppercase ${color}`}>{severity}</span>;
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-[0.14em] text-muted">{label}</div>
      <div className="mt-1 break-words text-navy">{value}</div>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-[92px] rounded border border-border bg-white px-2 py-1">
      <div className="text-[9px] uppercase tracking-[0.14em] text-muted">{label}</div>
      <div className="mt-0.5 truncate text-[12px] font-semibold text-navy">{value}</div>
    </div>
  );
}
