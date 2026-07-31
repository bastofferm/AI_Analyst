"use client";

// Management tone — the MD&A language read: tone gauge (−1…+1), peer standing,
// buzzwords and risk flags. Replaces the old ManagementGuidancePanel.

import type { CommitteeResponse } from "@/lib/api";
import { GaugeBar } from "@/components/charts/primitives";
import { CHART_GREEN, CHART_RED, MUTED, NAVY, isNum } from "@/components/charts/theme";
import { SectionCard } from "@/components/ui/SectionCard";

export function ToneSection({ result }: { result: CommitteeResponse }) {
  const m = result.mda_analysis;
  if (!m) return null;
  const tone = m.tone_score;
  const hasAnything =
    isNum(tone) || m.summary || (m.buzzword_headlines || []).length > 0 || (m.risk_flags || []).length > 0;
  if (!hasAnything) return null;

  const guidance = (m.guidance || "").toLowerCase();
  const guidanceCls =
    guidance === "positive" ? "badge-pos" : guidance === "negative" ? "badge-neg" : "badge-neu";

  return (
    <SectionCard
      eyebrow="Step 6"
      title="How confident does management sound?"
      copyKey="tone"
      actions={
        m.guidance ? (
          <span className={`rounded px-2.5 py-1 text-[11px] font-semibold capitalize ${guidanceCls}`}>
            guidance: {m.guidance}
          </span>
        ) : undefined
      }
    >
      {isNum(tone) ? (
        <GaugeBar
          min={-1}
          max={1}
          markers={[{ value: tone, label: "This company's tone", color: tone > 0.15 ? CHART_GREEN : tone < -0.15 ? CHART_RED : NAVY }]}
          zones={[
            { from: -1, to: -0.15, color: CHART_RED, opacity: 0.15 },
            { from: 0.15, to: 1, color: CHART_GREEN, opacity: 0.15 },
          ]}
          format={(v) => v.toFixed(2)}
          trackLabelLeft="cautious −1"
          trackLabelRight="confident +1"
          height={88}
        />
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-2 text-[11.5px] text-muted">
        {isNum(m.peer_percentile) ? (
          <span>
            More confident than <b className="num text-navy">{Math.round(m.peer_percentile)}%</b> of peers
            {isNum(m.peer_rank) && isNum(m.peer_count) ? (
              <span className="num"> (#{m.peer_rank} of {m.peer_count})</span>
            ) : null}
          </span>
        ) : null}
      </div>

      {m.summary ? <p className="mt-3 max-w-3xl text-[12px] leading-relaxed text-navy">{m.summary}</p> : null}

      {(m.buzzword_headlines || []).length > 0 ? (
        <div className="mt-3.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-[0.1em]" style={{ color: MUTED }}>
            What they keep talking about
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(m.buzzword_headlines || []).slice(0, 8).map((b, i) => (
              <span key={i} className="rounded-full border border-border bg-white px-2.5 py-1 text-[10.5px] text-navy">
                {b}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {(m.risk_flags || []).length > 0 ? (
        <div className="mt-3.5">
          <div className="mb-1.5 text-[10px] uppercase tracking-[0.1em]" style={{ color: MUTED }}>
            Language that raised eyebrows
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(m.risk_flags || []).slice(0, 6).map((f, i) => (
              <span key={i} className="badge-neg rounded px-2 py-0.5 text-[10.5px] font-medium">
                {f}
              </span>
            ))}
          </div>
        </div>
      ) : null}
      {(m.warnings || []).length > 0 ? (
        <div className="mt-3 text-[10.5px] text-muted">{(m.warnings || []).join(" · ")}</div>
      ) : null}
    </SectionCard>
  );
}
