"use client";

import { useState } from "react";
import { RefreshCw, Send } from "lucide-react";
import { api, type CommitteeIterationItem, type CommitteeResponse } from "@/lib/api";

import { llmBody, type LlmSelection } from "@/lib/llm";
type Status = "idle" | "running" | "error";
type PromptTemplate = { id: string; label: string; text: string };

const PROMPT_TEMPLATES: PromptTemplate[] = [
  {
    id: "conservative_revision",
    label: "Conservative revision",
    text: "Revise the interpretation conservatively, but do not turn the downside case into a doom scenario. Keep the fact base frozen.",
  },
  {
    id: "mda_guidance",
    label: "MD&A guidance",
    text: "Expand the management-guidance read. Summarize the MD&A tone, forward-looking language, risk flags and buzzword headlines with evidence IDs.",
  },
  {
    id: "data_quality_challenge",
    label: "Data quality challenge",
    text: "Challenge the conclusion using only the data-quality report, recon traces and Yahoo cross-checks. Explain whether any issues lower confidence.",
  },
  {
    id: "exec_bullets",
    label: "Executive bullets",
    text: "Rewrite the top interpretation as concise executive-summary bullets. Do not add new facts or recompute valuation.",
  },
  {
    id: "valuation_sensitivity",
    label: "Valuation sensitivity",
    text: "Explain what would need to change in DCF/SOTP/multiples interpretation for the rating to improve or worsen, without rerunning facts.",
  },
];

export function CommitteeIterationPanel({
  result,
  llm,
}: {
  result: CommitteeResponse;
  llm: LlmSelection;
}) {
  const [comment, setComment] = useState("");
  const [history, setHistory] = useState<CommitteeIterationItem[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState("");
  const [selectedPrompt, setSelectedPrompt] = useState<PromptTemplate | null>(null);

  function applyPrompt(template: PromptTemplate) {
    setSelectedPrompt(template);
    setComment((current) => {
      const trimmed = current.trim();
      return trimmed ? `${trimmed}\n\n${template.text}` : template.text;
    });
  }

  async function runIteration() {
    const text = comment.trim();
    if (!text || status === "running") return;
    setStatus("running");
    setError("");
    try {
      const { report_html: _reportHtml, ...frozenResult } = result;
      const response = await api.committeeIterate({
        ticker: result.ticker,
        user_comment: text,
        current_result: frozenResult as Record<string, unknown>,
        iteration_history: history,
        ...llmBody(llm),
        prompt_template_id: selectedPrompt?.id || null,
        prompt_template_label: selectedPrompt?.label || null,
      });
      setHistory((items) => [...items, { ...response, user_comment: response.received_user_comment || text }]);
      setComment("");
      setSelectedPrompt(null);
      setStatus("idle");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Revision iteration failed.");
      setStatus("error");
    }
  }

  return (
    <div className="border-b border-border bg-white/35 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="label">Revision iteration</div>
          <div className="mt-1 text-[12px] text-muted">Comment on the current output; facts and valuation stay frozen.</div>
        </div>
        <RefreshCw className={`h-4 w-4 text-muted ${status === "running" ? "animate-spin" : ""}`} aria-hidden />
      </div>
      <textarea
        value={comment}
        onChange={(event) => setComment(event.target.value)}
        placeholder="Example: Revisit the conclusion assuming Azure growth deserves more weight, but do not rerun facts."
        className="min-h-[88px] w-full resize-y rounded border border-border bg-white px-3 py-2 text-[12px] leading-5 text-navy outline-none focus:border-navy"
      />
      <div className="mt-2 flex flex-wrap gap-2">
        {PROMPT_TEMPLATES.map((template) => (
          <button
            key={template.id}
            type="button"
            title={template.text}
            onClick={() => applyPrompt(template)}
            className={[
              "rounded border px-2.5 py-1.5 text-[11px] font-semibold transition-colors",
              selectedPrompt?.id === template.id
                ? "border-navy bg-navy text-white"
                : "border-border bg-white text-muted hover:text-navy",
            ].join(" ")}
          >
            {template.label}
          </button>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
        <button
          type="button"
          onClick={runIteration}
          disabled={!comment.trim() || status === "running"}
          className="inline-flex items-center gap-2 rounded bg-navy px-4 py-2 text-[12px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <Send className="h-3.5 w-3.5" aria-hidden />
          {status === "running" ? "Running revision" : "Run revision"}
        </button>
        {error && <div className="text-[11px] text-red">{error}</div>}
      </div>

      {history.length > 0 && (
        <div className="mt-4 grid gap-3">
          {history.map((item) => (
            <div key={`${item.iteration_number}-${item.received_user_comment || item.user_comment || ""}`} className="border-t border-border-soft pt-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
                  Iteration {item.iteration_number}
                  {item.iteration_status === "fallback" ? " | fallback" : ""}
                </div>
                <div className="text-[11px] text-muted">
                  {item.prompt_template_label ? `${item.prompt_template_label} | ` : ""}
                  {item.change_summary}
                </div>
              </div>
              {(item.received_user_comment || item.user_comment) && (
                <div className="mt-2 rounded border border-border bg-white px-3 py-2 text-[11px] text-muted">
                  <span className="font-semibold text-navy">Sent prompt: </span>
                  {item.received_user_comment || item.user_comment}
                </div>
              )}
              <pre className="mt-2 whitespace-pre-wrap rounded border border-border-soft bg-white px-3 py-2 text-[12px] leading-5 text-navy">
                {item.response_markdown}
              </pre>
              {item.warnings.length > 0 && <div className="mt-2 text-[11px] text-amber">{item.warnings.join(" | ")}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
