"""SEC-Pipeline LLM-Output-Schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ConceptMappingProposal(BaseModel):
    """Vorschlag des auto_map_unknown_concepts-Agents."""

    source_concept: str = Field(description="Unbekanntes XBRL-Concept, z.B. 'us-gaap:WeirdExtension'.")
    target_concept: str = Field(description="Standardisiertes Ziel-Concept.")
    target_taxonomy: Literal["us-gaap", "ifrs", "dei", "srt", "custom"]
    rationale: str = Field(max_length=600)
    similar_extensions: list[str] = Field(
        default_factory=list,
        description="Vergleichbare Extensions, die bereits gemappt sind.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    action: Literal["auto_promote", "human_review", "reject"]


class FilingSectionExtract(BaseModel):
    """Strukturierter Extrakt einer Filing-Sektion (Item 1A Risk, Item 7 MD&A, etc.)."""

    filing_id: str
    item: str = Field(description="Filing-Item-Heading, z.B. '1A', '7', '9A'.")
    text_excerpt: str = Field(max_length=8000, description="Wortwörtlicher Textauszug.")
    summary: str = Field(max_length=2000)
    key_risks: list[str] = Field(default_factory=list, description="3-7 zentrale Risiken/Themen.")
    sentiment: Literal["neutral", "cautionary", "optimistic", "alarming"]
    model_version: Optional[str] = Field(default=None)


class RawFilingDisplayRowSpec(BaseModel):
    """One executable display-row instruction over persisted filing-native rows."""

    row_key: str = Field(
        max_length=120,
        description="Stable ASCII row id unique within the statement, e.g. revenue or operating_expenses.",
    )
    parent_row_key: Optional[str] = Field(default=None, max_length=120)
    display_label: str = Field(max_length=120)
    row_kind: str = Field(
        max_length=32,
        description="Preferred values: detail, subtotal, total, section. Near misses are normalized by the validator.",
    )
    aggregation: str = Field(
        max_length=32,
        description="Preferred values: direct, sum, subtract, none. Near misses are normalized by the validator.",
    )
    visibility: str = Field(
        max_length=32,
        description="Preferred values: default or detail. Near misses are normalized by the validator.",
    )
    depth: int = Field(ge=0, le=2)
    source_node_keys: list[str] = Field(
        default_factory=list,
        description="Source filing row keys from the input packet. Empty only for section rows.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=400)


class RawFilingStatementDisplaySpec(BaseModel):
    """LLM display spec for one financial statement."""

    statement_type: Literal["income_statement", "balance_sheet", "cash_flow"]
    api_statement: Literal["BS", "IS", "CF"]
    display_title: str = Field(max_length=160)
    rows: list[RawFilingDisplayRowSpec]


class RawFilingDisplaySpec(BaseModel):
    """LLM display spec for the three primary financial statements."""

    statements: list[RawFilingStatementDisplaySpec]
    summary: str = Field(max_length=4000)
