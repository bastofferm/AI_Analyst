"""Pydantic output schemas for the committee LLM nodes.

Fed to ``ChatDeepSeek.with_structured_output(...)`` exactly like the pipeline
schemas in ``xbrl_sec/llm/schemas/*.py``. ``ScenarioAssumptions`` deliberately
reuses the ``dcf_engine`` assumption keys so a scenario can be handed straight to
``services.corporate_dcf(ticker, assumptions=...)``.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class AgentThesis(BaseModel):
    """One tribunal agent's argued view over the deterministic data packet."""

    stance: Literal["advocate", "challenger", "auditor"]
    thesis: str = Field(max_length=1600, description="The core argument in 3-6 sentences.")
    key_claims: list[str] = Field(
        default_factory=list, description="3-6 falsifiable claims backing the thesis."
    )
    segment_read: str = Field(
        default="",
        max_length=800,
        description="What the reportable-segment / product-geo breakdown implies for this stance.",
    )
    falsification_kpis: list[str] = Field(
        default_factory=list,
        description="Concrete KPI thresholds that, if breached, would falsify this thesis.",
    )
    # Proposed tilt to the DCF assumptions (percent inputs, same keys as dcf_engine).
    rev_growth_tilt_pct: Optional[float] = Field(
        default=None, description="Suggested steady-state revenue growth (percent) for this stance."
    )
    ebit_margin_pct: Optional[float] = Field(default=None)
    wacc_pct: Optional[float] = Field(default=None)
    terminal_growth_pct: Optional[float] = Field(default=None)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class ScenarioAssumptions(BaseModel):
    """DCF assumption set for one scenario (Upside / Base / Downside)."""

    label: Literal["upside", "base", "downside"]
    rev_growth_pct: list[float] = Field(
        default_factory=list, description="Year-1..year-5 revenue growth, percent."
    )
    terminal_growth_pct: float = 2.5
    ebit_margin_pct: float = 15.0
    tax_rate_pct: float = 21.0
    capex_pct_of_rev: float = 4.0
    nwc_pct_of_rev: float = 2.0
    wacc_pct: float = 9.0
    weight: float = Field(ge=0.0, le=1.0, description="Probability weight for this scenario.")
    rationale: str = Field(default="", max_length=800)


class CommitteeVerdict(BaseModel):
    """Lead-analyst synthesis + routing decision."""

    synthesis: str = Field(max_length=2400, description="Balanced synthesis of the three agents.")
    scenarios: list[ScenarioAssumptions] = Field(
        description="Exactly three scenarios: upside, base, downside (weights should sum to ~1.0)."
    )
    unresolved_contradictions: list[str] = Field(
        default_factory=list,
        description="Points where the Advocate/Challenger/Auditor conflict and the data cannot yet resolve them.",
    )
    decision_ready: bool = Field(
        description="False if another debate round is warranted (subject to the iteration cap)."
    )


class SensitivityAdjustment(BaseModel):
    """One stress-test or forecast adjustment proposed by a specialist analyst."""

    driver: str = Field(description="Input being adjusted, e.g. revenue growth, WACC, EBIT margin.")
    base_value: Optional[float] = Field(default=None, description="Base value in percent or multiple units.")
    stressed_value: Optional[float] = Field(default=None, description="Specialist's proposed stressed value.")
    fair_value_impact_pct: Optional[float] = Field(
        default=None, description="Approximate fair-value impact, percent, if inferable from supplied evidence."
    )
    rationale: str = Field(default="", max_length=500)


class PeerComparisonMetric(BaseModel):
    """One relative-value spread highlighted by a specialist analyst."""

    metric: str = Field(description="Metric name, e.g. P/E, EV/EBITDA, EV/FCF, FCF yield.")
    target_value: Optional[float] = None
    peer_median: Optional[float] = None
    premium_discount_pct: Optional[float] = Field(
        default=None, description="Target premium/(discount) to peer median, percent."
    )
    interpretation: str = Field(default="", max_length=500)


class SpecialistVerdict(BaseModel):
    """Structured signal extracted from a specialist analyst's prose."""

    analyst_key: str = Field(description="Stable specialist key.")
    analyst: str = Field(description="Human-readable analyst name.")
    thesis: str = Field(max_length=1000)
    sensitivity_adjustments: list[SensitivityAdjustment] = Field(default_factory=list)
    peer_comparison_metrics: list[PeerComparisonMetric] = Field(default_factory=list)
    dcf_tilt: dict[str, float] = Field(
        default_factory=dict,
        description="Suggested DCF input overrides, percent units where applicable.",
    )
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class CommitteeMemo(BaseModel):
    """Bilingual institutional memo (final output)."""

    en: str = Field(description="English institutional memo (Markdown).")
    de: str = Field(description="German institutional memo (Markdown), register-matched not literal.")


class DqTriageItem(BaseModel):
    """One reasoned root-cause classification for a deterministic DQ finding.

    ``root_cause`` is advisory free text (typical values: mapping_gap,
    wrong_target_variable, sector_scope_mismatch, sign_or_multiplier, parse_gap,
    period_mismatch, currency_or_unit, formula_gap, source_data_gap,
    benign_definition_difference). Kept as a plain str because DeepSeek JSON mode
    reasonably returns synonyms; nothing downstream branches on the exact token
    except the benign_definition_difference "explained" hint.
    """

    finding_id: str = Field(description="The dq-... id of the finding being triaged.")
    root_cause: str = Field(default="", description="Most likely root cause given the evidence.")
    explanation: str = Field(default="", max_length=600, description="Why this root cause, citing the evidence.")
    priority: int = Field(ge=1, le=5, default=3, description="1 = fix first, 5 = defer.")


class DqProposal(BaseModel):
    """One concrete, typed remediation proposal for a data-quality issue.

    ``kind`` and ``proposed_action`` are plain str (not Literal): governance is
    enforced at the queue writer (``dq_triage._proposal_rows`` only queues
    ``kind`` in the mapping set and validates ``proposed_action`` against the DB
    CHECK), so schema-level strictness would only cause whole-response validation
    failures on minor enum drift. Typical ``kind`` values: mapping_add,
    mapping_retarget, mapping_sector_override, reparse_filing, restandardize_entity,
    recompute_metrics, refresh_yahoo, no_action. Typical ``proposed_action`` values:
    global_mapping, sector_scope, sign_fix, unmap.
    """

    kind: str = Field(description="Remediation type. mapping_* kinds are written to the review queue.")
    concept_id: Optional[str] = Field(default=None, description="Raw XBRL concept (mapping kinds only).")
    target_variable: Optional[str] = Field(default=None, description="Standardized line item the concept should map to.")
    mapping_sector: Optional[str] = Field(default=None, description="Sector scope for the mapping, e.g. insurance.")
    proposed_action: Optional[str] = Field(default=None, description="Governed review-queue action for a mapping proposal.")
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reasoning: str = Field(default="", max_length=600)
    evidence_finding_ids: list[str] = Field(default_factory=list, description="Finding ids that justify this proposal.")
    next_step: str = Field(default="", max_length=300, description="Exact CLI command or queue action to take next.")


class DqTriage(BaseModel):
    """DeepSeek triage over the deterministic report + per-ticker mapping evidence pack."""

    triage: list[DqTriageItem] = Field(default_factory=list)
    proposals: list[DqProposal] = Field(default_factory=list)
    way_forward: list[str] = Field(
        default_factory=list, description="Ordered remediation steps, most impactful first."
    )
    narrative: str = Field(default="", max_length=800, description="2-3 sentence summary for the committee.")
