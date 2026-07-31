"use client";

// The analyst toolkit — the full institutional workbench, collapsed by default
// for the retail audience but fully functional: DQ triage (incl. the Promote
// mapping governance action), the evidence bundle, specialist bullet comments
// and the print-grade institutional report.

import type { CommitteeResponse } from "@/lib/api";
import { DataQualityAgentPanel } from "@/components/committee/DataQualityAgentPanel";
import { EvidencePanel } from "@/components/committee/EvidencePanel";
import { SpecialistCommentsPanel } from "@/components/committee/SpecialistCommentsPanel";
import { Collapsible } from "@/components/ui/Collapsible";
import { InfoBox } from "@/components/ui/InfoBox";

export function AnalystToolkit({ result }: { result: CommitteeResponse }) {
  const hasAny =
    result.data_quality_report || result.data_quality_agent || result.evidence_bundle ||
    (result.specialist_comments || []).length > 0 || result.report_html;
  if (!hasAny) return null;

  return (
    <Collapsible
      label="Analyst toolkit — for advanced users"
      sublabel="Raw evidence with citations, the full data-quality audit, and the print-grade institutional report"
    >
      <InfoBox copyKey="toolkit" className="mb-4" />
      <div className="card overflow-hidden">
        <DataQualityAgentPanel report={result.data_quality_report} agent={result.data_quality_agent} />
        <EvidencePanel bundle={result.evidence_bundle} />
        <SpecialistCommentsPanel comments={result.specialist_comments} />
      </div>
      {result.report_html ? (
        <div className="mt-4">
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="text-[12px] font-semibold text-navy">Institutional report · print view</span>
            <span className="text-[10px] text-muted">exactly what a fund&apos;s research desk would file</span>
          </div>
          <iframe
            title="Institutional committee report"
            srcDoc={result.report_html}
            className="h-[80vh] w-full rounded-md border border-border bg-white"
          />
        </div>
      ) : null}
    </Collapsible>
  );
}
