from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


Jurisdiction = Literal["US", "JP"]
# Widened jurisdiction for surfaces that accept Yahoo-backed international
# companies (screener, screener_agent, ai_committee, ai_committee_group).
# Strict Jurisdiction stays US/JP for the ~40 US/JP-only endpoints that would
# otherwise silently degrade (statement, hero_stats, kpis, mda, filings, etc.).
JurisdictionIntl = Literal["US", "JP", "INTL"]


class Company(BaseModel):
    ticker: str
    name: str
    cik: Optional[str] = None
    edinet_code: Optional[str] = None
    exchange: Optional[str] = None
    gics_sector_code: Optional[str] = None
    gics_industry_group_code: Optional[str] = None


class EntityIdentity(BaseModel):
    ticker: str
    name: str
    jurisdiction: Jurisdiction
    cik: Optional[str] = None
    edinet_code: Optional[str] = None
    exchange: Optional[str] = None
    sic_code: Optional[str] = None
    fy_end: Optional[str] = Field(default=None, description="e.g. 'Sep-26'")
    gics_sector_name: Optional[str] = None
    gics_industry_group_name: Optional[str] = None
    filings: List[str] = Field(default_factory=list, description="e.g. ['10-K','20-F']")
    description_compact: Optional[str] = None
    description_source: Optional[str] = None
    description_path: Optional[str] = None


class ExchangeOption(BaseModel):
    value: str
    label: str
    count: int


class SectorOption(BaseModel):
    code: str
    name: str


class IndustryOption(BaseModel):
    code: str
    name: str
    sector_code: Optional[str] = None


class FilterOptions(BaseModel):
    exchanges: List[ExchangeOption]
    sectors: List[SectorOption]
    industries: List[IndustryOption]


class MetaResponse(BaseModel):
    jurisdiction: JurisdictionIntl
    filters: FilterOptions
