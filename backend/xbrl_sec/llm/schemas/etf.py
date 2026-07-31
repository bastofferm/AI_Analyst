"""ETF-Pipeline LLM-Output-Schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ProviderClassification(BaseModel):
    """Result einer Provider-Klassifizierung pro ETF."""

    isin: str = Field(description="ISIN of the classified ETF, uppercase 12 chars.")
    provider_id: str = Field(
        description="Lowercase snake_case provider id (existing or new), or 'unknown_provider'."
    )
    provider_label: str = Field(description="Display name, e.g. 'iShares' or 'Unknown Provider'.")
    domain: Optional[str] = Field(default=None, description="Provider domain (xetra.com etc.)")
    aliases: list[str] = Field(default_factory=list, description="Brand aliases seen in the fund name.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence 0..1.")
    rationale: str = Field(default="", max_length=400)

    @field_validator("isin")
    @classmethod
    def _isin_upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("provider_id")
    @classmethod
    def _provider_id_format(cls, v: str) -> str:
        return v.strip().lower()


class ProviderClassificationBatch(BaseModel):
    """Wrapper für eine Liste von Klassifizierungen — passt für DeepSeeks JSON-Modus."""

    results: list[ProviderClassification]


class HoldingsAnomaly(BaseModel):
    """Befund einer Holdings-Anomalie-Erkennung."""

    isin: str
    severity: Literal["low", "medium", "high"]
    hypothesis: str = Field(description="Plausible explanation: corporate action, data error, market drift.")
    suggested_action: Literal["ignore", "rerun_fetch", "human_review", "halt_pipeline"]
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Beobachtete Kennzahlen (z.B. {'pct_change_30d': -0.42, 'holdings_count_drop': 5}).",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class BondRatingResolution(BaseModel):
    """Ergebnis der Bond-Rating-Multi-Source-Chain."""

    isin: str
    rating: Optional[str] = Field(default=None, description="z.B. 'AA-', 'Baa1', 'BBB'.")
    rating_scale: Literal["moodys", "sp", "fitch", "approximation"]
    source: str = Field(description="Konkrete Quelle: 'moodys.com', 'spglobal.com', 'fitch.com', 'deepseek_llm'.")
    source_url: Optional[str] = Field(default=None)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_warn: bool = Field(
        default=False,
        description="True wenn LLM-Approximation als letzter Ausweg verwendet wurde.",
    )
    rationale: str = Field(default="", max_length=400)
