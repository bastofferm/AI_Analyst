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
    # INTL only: scopes fact_metrics_intl / fact_prices_intl to one country so the alpha
    # panel is built per country (US/JP leave this None). See `get_config("INTL:DE")`.
    country_code: str | None = None


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


def _intl_config(country_code: str | None) -> JurisdictionConfig:
    """Alpha-panel config for INTL, optionally scoped to one country ("INTL:DE").

    fact_metrics_intl / fact_prices_intl are Yahoo-backed and cover ~10.6k names across
    many countries; the alpha model trains one cross-section per country (the loaders scope
    on `country_code`). There is no INTL standardized-fundamentals table, so that field is
    empty — the alpha path uses the metrics/price tables only.
    """
    cc = (country_code or "").upper().strip() or None
    return JurisdictionConfig(
        code="INTL",
        label=f"International{(' · ' + cc) if cc else ''}",
        price_table="fact_prices_intl",
        metrics_table="fact_metrics_intl",
        fundamentals_table="",
        entity_id_column="intl_company_id",
        cycle_factor_id="intl_cycle",
        macro_categories=(),
        country_code=cc,
    )


def get_config(value: str) -> JurisdictionConfig:
    code = (value or "").upper().strip()
    if code in JURISDICTIONS:
        return JURISDICTIONS[code]
    # INTL is registry-supported for the alpha model only (not the US/JP cycle engine):
    # "INTL" (all countries) or "INTL:<ISO2>" (one country).
    if code == "INTL" or code.startswith("INTL:"):
        return _intl_config(code.split(":", 1)[1] if ":" in code else None)
    return JURISDICTIONS[normalize_jurisdiction(value)]
