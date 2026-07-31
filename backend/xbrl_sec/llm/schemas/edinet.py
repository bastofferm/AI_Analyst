"""EDINET-Pipeline LLM-Output-Schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class GicsSuggestion(BaseModel):
    """LLM-Fallback für Japanese-Industry-Code → GICS-Mapping."""

    edinet_code: str
    suggested_gics_sector: str = Field(description="Sector-Code (z.B. '40' für Financials).")
    suggested_gics_industry_group: Optional[str] = Field(default=None)
    suggested_gics_industry: Optional[str] = Field(default=None)
    suggested_gics_sub_industry: Optional[str] = Field(default=None)
    rationale: str = Field(max_length=600)
    similar_filers: list[str] = Field(
        default_factory=list,
        description="EDINET-Codes ähnlicher Firmen, die als Vergleich dienten.",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class DriftClassification(BaseModel):
    """Drift-Explain-Agent-Output für Cross-Source-Recon (SEC vs EDINET)."""

    cik: Optional[str] = Field(default=None)
    edinet_code: Optional[str] = Field(default=None)
    period_end: str = Field(description="ISO-Datum.")
    concept: str = Field(description="Metric/Concept, in dem der Drift gefunden wurde.")
    reason: Literal[
        "fx_translation",
        "period_difference",
        "accounting_standard_difference",
        "scope_difference",
        "data_quality_issue",
        "unexplained",
    ]
    action: Literal["auto_accept", "auto_correct", "human_review", "halt_pipeline"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=600)
    fx_rate_used: Optional[float] = Field(default=None)
