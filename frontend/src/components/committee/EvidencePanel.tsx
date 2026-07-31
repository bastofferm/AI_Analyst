"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  Database,
  FileText,
  Filter,
  Landmark,
  Newspaper,
  ShieldCheck,
  Table2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { EvidenceBundle, EvidenceCard, EvidenceKind } from "@/lib/api";

type FilterKey = EvidenceKind | "all";

const KIND_META: Record<EvidenceKind, { label: string; Icon: LucideIcon }> = {
  mda: { label: "MD&A", Icon: FileText },
  filing_section: { label: "Filings", Icon: FileText },
  rich_filing_section: { label: "Rich filings", Icon: Table2 },
  news: { label: "News", Icon: Newspaper },
  ownership: { label: "Ownership", Icon: Landmark },
  macro: { label: "Macro", Icon: BarChart3 },
  statement: { label: "Statements", Icon: Table2 },
  recon: { label: "Recon", Icon: ShieldCheck },
  yahoo: { label: "Yahoo", Icon: Database },
  data_quality: { label: "Data quality", Icon: ShieldCheck },
};

export function EvidencePanel({ bundle }: { bundle?: EvidenceBundle | null }) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const cards = bundle?.cards || [];
  const warnings = bundle?.warnings || [];
  const kinds = useMemo(() => {
    const seen = new Set(cards.map((card) => card.kind));
    return (Object.keys(KIND_META) as EvidenceKind[]).filter((kind) => seen.has(kind) || (bundle?.counts?.[kind] || 0) > 0);
  }, [bundle?.counts, cards]);
  const filteredCards = filter === "all" ? cards : cards.filter((card) => card.kind === filter);
  const selectedCount = filter === "all" ? cards.length : filteredCards.length;

  if (!bundle) return null;

  return (
    <div className="border-b border-border bg-white/35">
      <div className="flex flex-col gap-3 border-b border-border-soft p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="flex min-w-0 items-start gap-2 text-left"
            aria-expanded={open}
          >
            <span className="mt-0.5 flex h-6 w-6 flex-shrink-0 items-center justify-center rounded border border-border bg-white text-muted">
              {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
            </span>
            <div className="min-w-0">
            <div className="label">Evidence bundle</div>
            <div className="mt-1 text-[12px] text-muted">
              {cards.length} cards, {bundle.trees?.length || 0} trees
              {bundle.truncated ? " · response truncated" : ""}
            </div>
            </div>
          </button>
          {open && <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted">
            <Filter className="h-3.5 w-3.5" aria-hidden />
            <button
              type="button"
              title="Show all evidence"
              onClick={() => setFilter("all")}
              className={filterButtonClass(filter === "all")}
            >
              All <span className="font-semibold">{cards.length}</span>
            </button>
            {kinds.map((kind) => {
              const { Icon, label } = KIND_META[kind];
              const count = bundle.counts?.[kind] || cards.filter((card) => card.kind === kind).length;
              return (
                <button
                  key={kind}
                  type="button"
                  title={`Filter ${label} evidence`}
                  onClick={() => setFilter(kind)}
                  className={filterButtonClass(filter === kind)}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden />
                  <span>{label}</span>
                  <span className="font-semibold">{count}</span>
                </button>
              );
            })}
          </div>}
        </div>

        {warnings.length > 0 && (
          <div className="flex gap-2 rounded border border-amber/40 bg-amber/5 p-2 text-[11px] text-amber">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden />
            <div className="min-w-0 break-words">{warnings.slice(0, 4).join(" · ")}</div>
          </div>
        )}
      </div>

      {!open ? null : selectedCount === 0 ? (
        <div className="p-4 text-[12px] text-muted">No evidence cards available for this source filter.</div>
      ) : (
        <div>
          {filteredCards.map((card) => (
            <EvidenceRow
              key={card.card_id}
              card={card}
              expanded={expanded === card.card_id}
              onToggle={() => setExpanded(expanded === card.card_id ? null : card.card_id)}
            />
          ))}
        </div>
      )}

      {open && (bundle.trees?.length || 0) > 0 && (
        <div className="border-t border-border-soft p-4">
          <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-muted">
            <Database className="h-3.5 w-3.5" aria-hidden />
            Section trees
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {bundle.trees.map((tree) => {
              const nodeCount = Object.keys(tree.nodes || {}).length;
              return (
                <div key={tree.tree_id} className="min-w-0 border-t border-border-soft pt-2 text-[11px] text-navy">
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate font-semibold">{tree.title}</span>
                    <span className="flex-shrink-0 text-muted">{nodeCount} nodes</span>
                  </div>
                  <div className="mt-1 break-all font-mono text-[10px] text-muted">{tree.tree_id}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function EvidenceRow({
  card,
  expanded,
  onToggle,
}: {
  card: EvidenceCard;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { Icon, label } = KIND_META[card.kind];
  const citations = card.citations || [];
  return (
    <div className="border-t border-border-soft px-4 py-3 first:border-t-0">
      <div className="flex min-w-0 items-start gap-3">
        <button
          type="button"
          title={expanded ? "Collapse evidence details" : "Expand evidence details"}
          onClick={onToggle}
          className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-border bg-white text-muted hover:text-navy"
        >
          {expanded ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
        </button>
        <Icon className="mt-1 h-4 w-4 flex-shrink-0 text-muted" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[12px] font-semibold text-navy">{card.title}</span>
            <span className="rounded border border-border bg-white px-1.5 py-0.5 text-[10px] text-muted">{label}</span>
            <Confidence confidence={card.confidence} />
            {card.as_of && (
              <span className="inline-flex items-center gap-1 text-[10px] text-muted">
                <CalendarDays className="h-3 w-3" aria-hidden />
                {card.as_of}
              </span>
            )}
          </div>
          <div className="mt-1 break-words text-[12px] leading-5 text-navy">{card.summary}</div>
          <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted">
            <span className="break-all font-mono">{card.card_id}</span>
            {citations.slice(0, 2).map((citation) => (
              <span key={citation.citation_id} className="break-all font-mono">
                {citation.citation_id}
              </span>
            ))}
          </div>
        </div>
      </div>

      {expanded && (
        <div className="mt-3 ml-10 grid gap-3 border-t border-border-soft pt-3 text-[11px] text-muted">
          {card.excerpt && <div className="break-words leading-5 text-navy">{card.excerpt}</div>}
          <div className="grid gap-2 md:grid-cols-2">
            <Detail label="Source" value={`${card.source.label} (${card.source.source_id})`} />
            <Detail label="Path" value={card.source.source_path || card.source.uri || "runtime"} />
          </div>
          {citations.length > 0 && (
            <div className="grid gap-2">
              {citations.map((citation) => (
                <div key={citation.citation_id} className="border-t border-border-soft pt-2">
                  <div className="break-all font-mono text-[10px] text-muted">{citation.citation_id}</div>
                  {citation.label && <div className="mt-1 text-[11px] font-semibold text-navy">{citation.label}</div>}
                  {citation.quote && <div className="mt-1 break-words leading-5 text-muted">{citation.quote}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Confidence({ confidence }: { confidence?: EvidenceCard["confidence"] }) {
  const label = confidence || "medium";
  const color = label === "high" ? "text-green" : label === "low" ? "text-red" : "text-amber";
  return <span className={`text-[10px] font-semibold uppercase ${color}`}>{label}</span>;
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-[0.14em] text-muted">{label}</div>
      <div className="mt-1 break-words text-navy">{value}</div>
    </div>
  );
}

function filterButtonClass(active: boolean) {
  return [
    "inline-flex h-7 items-center gap-1.5 rounded border px-2 text-[11px] transition-colors",
    active ? "border-navy bg-navy text-white" : "border-border bg-white text-muted hover:text-navy",
  ].join(" ");
}
