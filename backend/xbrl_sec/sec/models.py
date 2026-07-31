"""Small typed structures shared by the US and JP pipelines."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class RawFact:
    jurisdiction: str
    entity_id: str
    concept_id: str
    period_end: date
    value_type: str
    value: Optional[Decimal]
    unit: Optional[str]
    filing_id: Optional[str]
    filing_type: Optional[str]
    period_start: Optional[date] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    source_fp: Optional[str] = None
    taxonomy: Optional[str] = None
    context_tier: int = 0
    statement_type: Optional[str] = None
    parent_id: Optional[str] = None
    root_id: Optional[str] = None
    concept_path: Optional[str] = None
    concept_id_level: Optional[int] = None
    weight: Optional[Decimal] = None
    effective_weight: Optional[Decimal] = None
    pre_parent_id: Optional[str] = None
    pre_order: Optional[int] = None
    pre_level: Optional[int] = None
    pre_position: Optional[int] = None
