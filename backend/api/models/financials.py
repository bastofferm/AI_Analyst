from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


Direction = Literal["up", "down", "neu"]
Statement = Literal["BS", "IS", "CF"]
FilingCoverageState = Literal["filled", "partial", "miss", "empty"]
Period = Literal["FY", "Q", "H1", "Q1", "Q2", "Q3", "Q4"]
FinancialDisplaySource = Literal["metric", "line_item", "derived"]
FinancialDisplayVisibility = Literal["default", "supplemental", "audit_only"]
FinancialDisplayProvenance = Literal[
    "reported",
    "computed_metric",
    "derived",
    "residual",
    "mixed",
    "mapping_gap",
]


class KpiPoint(BaseModel):
    period: str           # e.g. "FY2022" or "2022"
    value: Optional[float] = None


class KpiChip(BaseModel):
    label: str
    value: Optional[float] = None
    formatted: str = "—"
    delta_pct: Optional[float] = None
    delta_label: str = ""
    delta_direction: Direction = "neu"
    series: Optional[List[KpiPoint]] = None  # historical series, oldest→newest


class KpiResponse(BaseModel):
    ticker: str
    period: Period
    chips: Dict[str, KpiChip] = Field(
        ...,
        description=(
            "Keys: market_cap, revenue_cagr_5y, eps_growth, ev_ebitda, "
            "return_1y, dividend_yield"
        ),
    )


class CoverageMatrix(BaseModel):
    FY: Dict[int, FilingCoverageState] = Field(default_factory=dict)
    H1: Dict[int, FilingCoverageState] = Field(default_factory=dict)
    Q1: Dict[int, FilingCoverageState] = Field(default_factory=dict)
    Q2: Dict[int, FilingCoverageState] = Field(default_factory=dict)
    Q3: Dict[int, FilingCoverageState] = Field(default_factory=dict)
    Q4: Dict[int, FilingCoverageState] = Field(default_factory=dict)


class CoverageResponse(BaseModel):
    ticker: str
    years: List[int]
    matrix: CoverageMatrix


class StatementRow(BaseModel):
    line_item_id: str
    label: str
    category: Optional[str] = None
    unit_type: Optional[str] = None
    display_role: Optional[str] = None
    parent_id: Optional[str] = None
    depth: int = 0
    values: Dict[str, Optional[float]] = Field(default_factory=dict)
    cagr: Optional[float] = None
    source_concept_id: Optional[str] = None
    source_parent_concept_id: Optional[str] = None
    value_binding_concept_id: Optional[str] = None
    std_line_item_id: Optional[str] = None
    raw_label: Optional[str] = None
    standardized_label: Optional[str] = None
    default_visibility: Optional[str] = None
    presentation_depth: Optional[int] = None
    aggregation: Optional[str] = None
    source_node_keys: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    rationale: Optional[str] = None


class StatementColumn(BaseModel):
    key: str
    label: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None


class StatementFiling(BaseModel):
    filing_id: str
    filing_form: Optional[str] = None
    filed_date: Optional[str] = None
    statement_title: str
    role_uri: str


class StatementResponse(BaseModel):
    ticker: str
    statement: Statement
    period: Period
    currency: str = "USD"
    year_min: int
    year_max: int
    rows: List[StatementRow]
    display_mode: Literal["standardized", "filing_native", "llm_raw_filing"] = "standardized"
    columns: List[StatementColumn] = Field(default_factory=list)
    filing: Optional[StatementFiling] = None
    diagnostics: List[str] = Field(default_factory=list)


class StatementDisplayGenerationRequest(BaseModel):
    jurisdiction: Literal["US", "JP"]
    period: Period = "FY"
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    filing_id: Optional[str] = None
    statement: Optional[Statement] = None
    force: bool = False
    model: str = "deepseek-v4-flash"


class StatementDisplayGenerationResponse(BaseModel):
    status: str
    ticker: str
    jurisdiction: Literal["US", "JP"]
    run_id: Optional[int] = None
    statements: int = 0
    rows: int = 0
    values: int = 0
    diagnostics: int = 0
    message: Optional[str] = None


class AnalyticsRow(BaseModel):
    metric_id: str
    name: Optional[str] = None
    category: Optional[str] = None
    unit_type: Optional[str] = None
    values: Dict[int, Optional[float]] = Field(default_factory=dict)
    latest_value: Optional[float] = None
    latest_year: Optional[int] = None
    cagr: Optional[float] = None


class AnalyticsResponse(BaseModel):
    ticker: str
    year_min: int
    year_max: int
    rows: List[AnalyticsRow]


class AnalyticsMetricRow(BaseModel):
    metric_id: str
    name: str
    category: str
    unit_type: Optional[str] = None
    relevance: Literal["relevant", "missing"]
    tooltip: str = ""
    values: Dict[int, Optional[float]] = Field(default_factory=dict)
    formulas: Dict[int, str] = Field(default_factory=dict)


class AnalyticsMetricGroup(BaseModel):
    category: str
    computed_count: int
    defined_count: int
    rows: List[AnalyticsMetricRow]


class AnalyticsLineItemRow(BaseModel):
    line_item_id: str
    name: str
    category: str
    unit_type: Optional[str] = None
    tooltip: str = ""
    values: Dict[int, Optional[float]] = Field(default_factory=dict)


class AnalyticsLineItemSection(BaseModel):
    category: str
    rows: List[AnalyticsLineItemRow]


class AnalyticsCoverageResponse(BaseModel):
    ticker: str
    jurisdiction: Literal["US", "JP"]
    period: Period
    year_min: int
    year_max: int
    metric_table: str
    line_item_table: str
    metrics_defined: int
    metrics_computed: int
    metric_groups: List[AnalyticsMetricGroup]
    line_item_sections: List[AnalyticsLineItemSection]


class FinancialDisplayRow(BaseModel):
    row_id: str
    source_id: str
    source_type: FinancialDisplaySource
    label: str
    section: str
    unit_type: Optional[str] = None
    display_role: str = "metric"
    priority_rank: int = 9999
    default_visibility: FinancialDisplayVisibility = "default"
    values: Dict[int, Optional[float]] = Field(default_factory=dict)
    latest_value: Optional[float] = None
    latest_year: Optional[int] = None
    latest_change: Optional[float] = None
    growth: Optional[float] = None
    cagr: Optional[float] = None
    trend_direction: Direction = "neu"
    provenance: FinancialDisplayProvenance = "reported"
    quality_flags: List[str] = Field(default_factory=list)
    tooltip: str = ""


class FinancialDisplaySection(BaseModel):
    section_id: str
    title: str
    subtitle: str = ""
    max_default_rows: int = 12
    rows: List[FinancialDisplayRow] = Field(default_factory=list)


class FinancialDisplayResponse(BaseModel):
    ticker: str
    jurisdiction: Literal["US", "JP"]
    period: Period
    currency: str = "USD"
    accounting_standard: Literal["US_GAAP", "JP_GAAP"]
    sector_scope: str
    year_min: int
    year_max: int
    sections: List[FinancialDisplaySection]
    diagnostics: List[str] = Field(default_factory=list)
