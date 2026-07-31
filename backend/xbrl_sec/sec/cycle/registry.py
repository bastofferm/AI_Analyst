"""Jurisdiction registry for standalone cycle-model runs.

The registry intentionally supports only US and JP in v1. Future jurisdictions
should be added by registering a new config entry rather than branching model
code.
"""
from __future__ import annotations

from dataclasses import dataclass


SUPPORTED_JURISDICTIONS = ("US", "JP")


@dataclass(frozen=True)
class JurisdictionConfig:
    code: str
    label: str
    price_table: str
    metrics_table: str
    fundamentals_table: str
    entity_id_column: str
    cycle_factor_id: str
    macro_categories: tuple[str, ...]
    market_factor_models: tuple[str, ...] = ("FF3", "FF4", "FF5", "FF6")


JURISDICTIONS: dict[str, JurisdictionConfig] = {
    "US": JurisdictionConfig(
        code="US",
        label="United States",
        price_table="fact_prices_us",
        metrics_table="fact_metrics_us",
        fundamentals_table="fact_fundamentals_std_us",
        entity_id_column="cik",
        cycle_factor_id="us_cycle",
        macro_categories=(
            "rates",
            "inflation",
            "growth",
            "activity",
            "labor",
            "credit",
            "liquidity",
            "debt",
            "fx",
            "volatility",
            "sentiment",
            "nowcast",
            "state_probability",
            "state_proxy",
            "financial_stress",
        ),
    ),
    "JP": JurisdictionConfig(
        code="JP",
        label="Japan",
        price_table="fact_prices_jp",
        metrics_table="fact_metrics_jp",
        fundamentals_table="fact_fundamentals_std_jp",
        entity_id_column="edinet_code",
        cycle_factor_id="jp_cycle",
        macro_categories=(
            "rates",
            "inflation",
            "growth",
            "activity",
            "labor",
            "credit",
            "liquidity",
            "debt",
            "fx",
            "volatility",
            "sentiment",
            "nowcast",
            "state_probability",
            "state_proxy",
            "financial_stress",
            "money_supply",
        ),
    ),
}


def normalize_jurisdiction(value: str) -> str:
    code = (value or "").upper().strip()
    if code == "GLOBAL":
        raise ValueError("GLOBAL cycle models are intentionally unsupported in v1; use US or JP.")
    if code not in JURISDICTIONS:
        allowed = ", ".join(SUPPORTED_JURISDICTIONS)
        raise ValueError(f"Unsupported cycle-model jurisdiction {value!r}; supported: {allowed}.")
    return code


def get_config(value: str) -> JurisdictionConfig:
    return JURISDICTIONS[normalize_jurisdiction(value)]
