from __future__ import annotations

import re
from typing import Any


def compact_current_result(result: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "ticker",
        "jurisdiction",
        "primary_fair_value",
        "triangulation",
        "reverse_dcf",
        "sotp",
        "probability_weighted_fair_value",
        "scenarios",
        "segment_data",
        "rich_filing_sections",
        "ownership",
        "macro",
        "mda_analysis",
        "specialist_comments",
        "data_quality_report",
        "memo",
        "dq_warning",
        "evidence_bundle",
    }
    compact = {key: result.get(key) for key in keep if key in result}
    evidence = compact.get("evidence_bundle")
    if isinstance(evidence, dict):
        cards = evidence.get("cards") or []
        compact["evidence_bundle"] = {
            "counts": evidence.get("counts") or {},
            "warnings": evidence.get("warnings") or [],
            "cards": [
                {
                    "card_id": card.get("card_id"),
                    "kind": card.get("kind"),
                    "title": card.get("title"),
                    "summary": _truncate(card.get("summary"), 260),
                    "as_of": card.get("as_of"),
                }
                for card in cards[:12]
                if isinstance(card, dict)
            ],
        }
    rich = compact.get("rich_filing_sections")
    if isinstance(rich, dict):
        compact["rich_filing_sections"] = {
            "available": rich.get("available"),
            "warnings": (rich.get("warnings") or [])[:5],
            "sections": [
                {
                    "family": row.get("section_family"),
                    "sector_scope": row.get("sector_scope"),
                    "title": row.get("section_title"),
                    "form_type": row.get("form_type"),
                    "filing_date": row.get("filing_date"),
                    "summary": _truncate(row.get("summary") or row.get("excerpt"), 260),
                }
                for row in (rich.get("sections") or [])[:8]
                if isinstance(row, dict)
            ],
        }
    memo = compact.get("memo")
    if isinstance(memo, dict):
        compact["memo"] = {
            "en": _truncate(memo.get("en"), 5000),
            "de": _truncate(memo.get("de"), 1200),
        }
    dq = compact.get("data_quality_report")
    if isinstance(dq, dict):
        compact["data_quality_report"] = {
            "overall_score": dq.get("overall_score"),
            "layer_scores": dq.get("layer_scores") or {},
            "counts": dq.get("counts") or {},
            "findings": [
                {
                    "finding_id": row.get("finding_id"),
                    "layer": row.get("layer"),
                    "severity": row.get("severity"),
                    "title": row.get("title"),
                    "message": _truncate(row.get("message"), 220),
                    "metric_id": row.get("metric_id"),
                    "line_item_id": row.get("line_item_id"),
                    "pct_delta": row.get("pct_delta"),
                }
                for row in (dq.get("findings") or [])[:10]
                if isinstance(row, dict)
            ],
            "metric_reconciliations": [
                {
                    "reconciliation_id": row.get("reconciliation_id"),
                    "metric_id": row.get("metric_id"),
                    "severity": row.get("severity"),
                    "likely_driver": row.get("likely_driver"),
                    "pct_delta": row.get("pct_delta"),
                }
                for row in (dq.get("metric_reconciliations") or [])[:6]
                if isinstance(row, dict)
            ],
            "repair_suggestions": (dq.get("repair_suggestions") or [])[:5],
            "warnings": (dq.get("warnings") or [])[:5],
        }
    return compact


def normalize_iteration_response(
    data: dict[str, Any],
    *,
    iteration_number: int,
    fallback_markdown: str,
    user_comment: str | None = None,
    prompt_template_id: str | None = None,
    prompt_template_label: str | None = None,
) -> dict[str, Any]:
    response = str(data.get("response_markdown") or data.get("addendum") or fallback_markdown).strip()
    revised = data.get("revised_memo_en")
    summary = str(data.get("change_summary") or "Revision addendum generated from frozen committee output.").strip()
    cited = data.get("cited_evidence_ids")
    if not isinstance(cited, list):
        cited = re.findall(r"ev-[a-f0-9]{16}", response)
    cited = [str(item) for item in cited if str(item).startswith("ev-")][:16]
    return {
        "iteration_number": iteration_number,
        "iteration_status": "completed",
        "received_user_comment": user_comment,
        "prompt_template_id": prompt_template_id,
        "prompt_template_label": prompt_template_label,
        "response_markdown": response or fallback_markdown,
        "revised_memo_en": str(revised).strip() if revised else None,
        "change_summary": summary,
        "cited_evidence_ids": cited,
        "warnings": [str(w) for w in data.get("warnings") or [] if str(w).strip()],
    }


def no_key_iteration_response(
    iteration_number: int,
    user_comment: str,
    *,
    prompt_template_id: str | None = None,
    prompt_template_label: str | None = None,
) -> dict[str, Any]:
    return {
        "iteration_number": iteration_number,
        "iteration_status": "fallback",
        "received_user_comment": user_comment.strip(),
        "prompt_template_id": prompt_template_id,
        "prompt_template_label": prompt_template_label,
        "response_markdown": (
            "No revision was generated because no DeepSeek key is configured.\n\n"
            f"User comment preserved for the frozen committee output: {user_comment.strip()}"
        ),
        "revised_memo_en": None,
        "change_summary": "No model call was made.",
        "cited_evidence_ids": [],
        "warnings": ["No DeepSeek key - revision iteration was not generated."],
    }


def iteration_fallback_response(
    iteration_number: int,
    user_comment: str,
    warning: str,
    *,
    prompt_template_id: str | None = None,
    prompt_template_label: str | None = None,
) -> dict[str, Any]:
    return {
        "iteration_number": iteration_number,
        "iteration_status": "fallback",
        "received_user_comment": user_comment.strip(),
        "prompt_template_id": prompt_template_id,
        "prompt_template_label": prompt_template_label,
        "response_markdown": (
            "The revision model call failed, so the original committee output remains unchanged.\n\n"
            f"User comment preserved for retry: {user_comment.strip()}"
        ),
        "revised_memo_en": None,
        "change_summary": "Revision fallback returned without changing the frozen output.",
        "cited_evidence_ids": [],
        "warnings": [warning],
    }


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
