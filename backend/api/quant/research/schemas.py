"""Structured outputs for the four research agents.

Mirrors the convention established in ``ai_analyst/committee/schemas.py``: fields that are
conceptually enums are typed ``str``, not ``Literal``. That is deliberate and load-bearing —
a model that answers "PASS" or "accept (with caveats)" would fail whole-response validation
under a ``Literal``, losing the entire analysis over a formatting quibble. The values are
normalized in code instead (:func:`normalize_status`, :func:`normalize_decision`), and the
authority for what a verdict *does* lives in the graph, not in the parser.

The one field that is strictly validated is :attr:`SpecPatch.patch`, and it is validated
downstream rather than here: ``spec.apply_patch`` whitelists every key and clamps every
value. Keeping the schema permissive and the applier strict means a malformed proposal
degrades to "no change, here is why" instead of crashing a round.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Canonical vocabularies. Anything outside them is mapped by the normalizers below.
VALIDATION_STATUSES = ("pass", "warn", "fail")
PM_DECISIONS = ("accept", "reject", "continue")


def normalize_status(value: str | None) -> str:
    """Map a free-form validation status onto pass|warn|fail (default: warn).

    Defaults to ``warn`` rather than ``pass`` so an unparseable verdict never silently
    clears a model for promotion.
    """
    v = (value or "").strip().lower()
    if not v:
        return "warn"
    if v.startswith("pass") or v in {"ok", "clean", "approved", "green"}:
        return "pass"
    if v.startswith("fail") or v in {"reject", "rejected", "blocked", "red", "critical"}:
        return "fail"
    return "warn"


def normalize_decision(value: str | None) -> str:
    """Map a free-form PM decision onto accept|reject|continue (default: continue)."""
    v = (value or "").strip().lower()
    if not v:
        return "continue"
    if v.startswith("accept") or v in {"approve", "approved", "promote", "yes"}:
        return "accept"
    if v.startswith("reject") or v in {"decline", "no", "abandon", "stop"}:
        return "reject"
    return "continue"


class SpecPatch(BaseModel):
    """The Quantitative Researcher's proposal for the next round."""

    patch: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields to change on the TrainingSpec, as a flat object. Only names from "
                    "the lecture's search space are honoured; everything else is discarded.",
    )
    rationale: str = Field(
        default="",
        description="Why this change, referencing the specific diagnostic that motivated it.",
    )
    hypothesis: str = Field(
        default="",
        description="A falsifiable prediction: which metric should move, in which direction, "
                    "and roughly by how much.",
    )
    confidence: float | None = Field(
        default=None, description="0..1 confidence that the hypothesis holds.")
    stop: bool = Field(
        default=False,
        description="True when the researcher believes the search has converged and further "
                    "rounds would only fit noise.")


class Finding(BaseModel):
    """One methodological defect the Model Validation unit is asserting."""

    category: str = Field(default="", description="e.g. overfitting, leakage, "
                                                  "sample_selection, thin_bucket, "
                                                  "metric_gaming, instability, factor_mimicry")
    severity: str = Field(default="warn", description="info | warn | critical")
    detail: str = Field(default="", description="What is wrong, with the numbers that show it.")
    evidence: str = Field(default="", description="Which metric or table this rests on.")


class ValidationVerdict(BaseModel):
    """The Model Validation unit's audit of one iteration.

    A ``fail`` is a veto: :func:`graph.select_champion` will not consider that iteration for
    promotion regardless of how good its headline metrics look. That asymmetry is the point
    of having a validation function separate from the researcher who proposed the spec.
    """

    status: str = Field(default="warn", description="pass | warn | fail")
    summary: str = Field(default="", description="Two sentences a PM can act on.")
    findings: list[Finding] = Field(default_factory=list)
    blocking: bool = Field(
        default=False,
        description="True when a finding is severe enough that this model must not be "
                    "promoted even if it beats the incumbent.")


class PMVerdict(BaseModel):
    """The Portfolio Manager's economic judgement and loop-control decision."""

    decision: str = Field(default="continue", description="accept | reject | continue")
    reasoning: str = Field(default="", description="Economic, not statistical: implementable "
                                                   "after turnover? diversified across "
                                                   "sectors? genuinely alpha, not factor beta?")
    preferred_iteration: int | None = Field(
        default=None, description="Which round's model the PM would actually run.")
    concerns: list[str] = Field(default_factory=list)


class AdvisorNote(BaseModel):
    """The External Advisor's outside view.

    Advisory only — it holds no veto and proposes no spec. Its value is being the one voice
    not invested in the search path taken so far, so it is asked for a direction that has NOT
    been tried rather than for an incremental improvement on what has.
    """

    contrarian_read: str = Field(
        default="", description="The least flattering defensible reading of these results.")
    orthogonal_direction: str = Field(
        default="", description="One direction the researcher has not tried, and why it "
                                "might work where the current path is stalling.")
    suggested_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional concrete spec fields expressing that direction.")
    reasoning: str = Field(default="")
