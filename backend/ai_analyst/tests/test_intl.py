"""Tests for the INTL (Yahoo-backed) alignment work.

DB access is always mocked — these tests never touch Postgres. Coverage:
- INTL sector-scope normalization (Yahoo raw labels → canonical).
- compute_intl metric computation from mock Yahoo statement/profile data.
- committee node INTL branches (completeness, dq_validation, engine, institutional,
  data_quality_agent) skip US/JP-only code paths and emit the right stubs.
- report_data_packet dispatches to _report_data_packet_intl for INTL.
- screener SQL dispatch: fact_table and dim_table pick 'intl' variants.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from ai_analyst import services
from ai_analyst.committee import nodes
from xbrl_sec.sec.metrics import compute_intl


# ---------------------------------------------------------------- sector_scope

def test_intl_sector_scope_financial_services():
    assert services.sector_scope_from_company({"mapping_sector": "Financial Services"}) \
        == "asset_manager_other_financial"


def test_intl_sector_scope_industrials_maps_to_corp():
    assert services.sector_scope_from_company({"mapping_sector": "Industrials"}) == "corp"


def test_intl_sector_scope_none_maps_to_corp():
    assert services.sector_scope_from_company({}) == "corp"


def test_intl_sector_scope_preserves_canonical_values():
    # US-shaped input must still route through the existing GICS branch.
    assert services.sector_scope_from_company({
        "mapping_sector": "non_bank_financial",
        "gics_industry_group_code": "4030",
    }) == "insurance"


# ---------------------------------------------------------------- compute_intl

def test_compute_intl_alias_lookup_covers_screener_inputs():
    from xbrl_sec.sec.metrics.compute_intl import _YF_ALIAS_LOOKUP, _norm
    # These raw labels commonly appear in fact_yahoo_statement_item — they MUST map.
    assert _YF_ALIAS_LOOKUP[_norm("Total Revenue")] == "revenue"
    assert _YF_ALIAS_LOOKUP[_norm("Free Cash Flow")] == "free_cash_flow"
    assert _YF_ALIAS_LOOKUP[_norm("Stockholders Equity")] == "total_equity"
    assert _YF_ALIAS_LOOKUP[_norm("EBITDA")] == "earnings_before_interest_taxes_depreciation_amortization"


def test_compute_intl_prefers_yahoo_profile_over_formula():
    """When Yahoo profile provides a metric (trailingPE), use it; when it doesn't
    (ev_ebitda), compute from statement lines."""
    from xbrl_sec.sec.metrics.compute_intl import _compute_one
    company = {"ticker": "TEST", "intl_company_id": "1", "currency": "USD",
               "market_cap_local": 1_000_000_000.0, "shares_outstanding": 100_000_000}
    statements = {
        2024: {"revenue": 500e6, "gross_profit": 300e6, "earnings_before_interest_taxes": 100e6,
               "earnings_before_interest_taxes_depreciation_amortization": 150e6,
               "free_cash_flow": 80e6, "total_financial_debt": 200e6,
               "cash_and_cash_equivalents": 50e6, "net_income": 60e6},
        2023: {"revenue": 400e6}, 2021: {"revenue": 300e6},
    }
    # NB: Yahoo returns dividendYield as a PERCENT number (2.4 == 2.4%), unlike its
    # margins/growth which are decimals — so 2.4 here, normalized to 0.024 below.
    profile = {"trailingPE": 18.5, "priceToBook": 3.2, "dividendYield": 2.4,
               "grossMargins": 0.60, "revenueGrowth": 0.25, "marketCap": 1_000_000_000.0}
    rows, _period_end, mcap_usd = _compute_one(company, statements, profile, fx_map={"USD": 1.0})
    by_metric = {r[8]: r[14] for r in rows}   # metric_id -> value  (index 8, 14 in tuple)
    # Passthrough from profile:
    assert by_metric["price_to_earnings_trailing"] == pytest.approx(18.5)
    assert by_metric["price_to_book"] == pytest.approx(3.2)
    # percent -> decimal, matching the screener catalogue and the SEC/EDINET pipelines.
    assert by_metric["dividend_yield"] == pytest.approx(0.024)
    # Formula-derived:
    assert by_metric["enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization"] \
        == pytest.approx((1_000_000_000 + 200e6 - 50e6) / 150e6)
    assert by_metric["free_cash_flow_yield"] == pytest.approx(80e6 / 1_000_000_000)
    # 3y CAGR from 300→500 over 3 years:
    assert by_metric["revenue_compound_annual_growth_rate_3_year"] == \
        pytest.approx((500e6 / 300e6) ** (1 / 3) - 1)
    # USD ccy → USD mcap unchanged.
    assert mcap_usd == pytest.approx(1_000_000_000.0)


def test_compute_intl_converts_native_market_cap_to_usd():
    """A Korean-won ticker's marketCap must be FX-converted to USD before storage."""
    from xbrl_sec.sec.metrics.compute_intl import _compute_one
    company = {"ticker": "005930.KS", "intl_company_id": "42", "currency": "KRW",
               "market_cap_local": 500e12, "shares_outstanding": 5_969_782_550}
    statements = {2024: {"revenue": 300e12}, 2023: {"revenue": 250e12}}
    profile = {"marketCap": 500e12}   # native KRW
    fx = {"KRW": 0.00073}   # 1 KRW = 0.00073 USD (approximate)
    _rows, _period_end, mcap_usd = _compute_one(company, statements, profile, fx_map=fx)
    assert mcap_usd == pytest.approx(500e12 * 0.00073)


def test_compute_intl_gracefully_skips_when_no_statements():
    from xbrl_sec.sec.metrics.compute_intl import _compute_one
    company = {"ticker": "EMPTY", "intl_company_id": "2", "currency": "USD",
               "market_cap_local": None, "shares_outstanding": None}
    rows, period_end, mcap_usd = _compute_one(company, {}, {}, fx_map={"USD": 1.0})
    assert rows == []
    assert period_end is None
    assert mcap_usd is None


# ---------------------------------------------------------------- committee nodes: INTL branches

def _intl_state(**over):
    state = {"ticker": "005930.KS", "jurisdiction": "INTL", "config": {}}
    state.update(over)
    return state


def test_completeness_check_intl_uses_yahoo_snapshot(monkeypatch):
    monkeypatch.setattr(services, "company_overview",
                        lambda t: {"found": True, "jurisdiction": "INTL", "uid": "42"})
    monkeypatch.setattr(services, "modeled_statement_snapshot_intl",
                        lambda t, years=5: {"rows": [{"fiscal_year": 2024}, {"fiscal_year": 2023}]})
    out = nodes.completeness_check_node(_intl_state())
    assert out["jurisdiction"] == "INTL"
    assert out["cik"] is None and out["edinet_code"] is None
    assert out["completeness_report"]["fundamental_years_present"] == [2023, 2024]
    assert out["is_data_complete"] is True


def test_dq_validation_intl_passes_without_running_checks(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(services, "modeled_statement_snapshot",
                        lambda *a, **k: called.__setitem__("n", 1) or {"rows": []})
    out = nodes.dq_validation_node(_intl_state())
    assert out["is_dq_passed"] is True
    assert out["dq_errors"] == []
    assert called["n"] == 0   # US/JP-only snapshot was NOT called


def test_institutional_node_intl_skips_13f():
    out = nodes.institutional_node(_intl_state())
    assert out["ownership"]["available"] is False
    assert "INTL" in out["ownership"]["note"]


def test_data_quality_agent_node_intl_skips():
    out = nodes.data_quality_agent_node(_intl_state())
    assert out["data_quality_agent"]["available"] is False
    assert "INTL" in out["data_quality_agent"]["note"]


# ---------------------------------------------------------------- report_data_packet dispatch

def test_report_data_packet_dispatches_intl(monkeypatch):
    monkeypatch.setattr(services, "company_overview",
                        lambda t: {"found": True, "jurisdiction": "INTL", "uid": "42",
                                    "ticker": t, "name": "Test INTL Co"})
    monkeypatch.setattr(services, "modeled_statement_snapshot_intl",
                        lambda t, years=5: {"rows": [], "sector_scope": "corp", "accounting_standard": "IFRS"})
    monkeypatch.setattr(services, "metric_panel", lambda t, years=5: {"rows": []})
    monkeypatch.setattr(services, "market_metrics", lambda t: {"rows": []})
    monkeypatch.setattr(services, "peer_group_intl", lambda t, limit=10: {"peers": []})
    packet = services.report_data_packet("SAMPLE.INTL")
    # INTL-specific shape: factor_exposure and recon_flags empty; yahoo_cross_check summary hints Yahoo source.
    assert packet["factor_exposure"] == {"ticker": "SAMPLE.INTL", "rows": []}
    assert packet["recon_flags"] == {"ticker": "SAMPLE.INTL", "rows": []}
    assert "Yahoo" in packet["yahoo_cross_check"]["summary"]


# ---------------------------------------------------------------- screener dispatch tables

def test_screener_dispatch_tables_include_intl():
    import api.routers.screener as scr
    # Table dispatch happens inline; sanity-check the mapping literal exists in the SQL builder.
    import inspect
    source = inspect.getsource(scr.screener_run)
    assert "fact_metrics_intl" in source
    assert "dim_company_intl" in source
    assert "intl_company_id" in source


def test_screener_prefers_ttm_without_admitting_quarterly_rows():
    """The metric lateral is one SQL string shared by US/JP/INTL. Two properties keep
    that safe, and both are easy to break in a refactor:

    1. TTM preference is EXPLICIT. Without the sort key, FY-vs-TTM would be decided
       implicitly by `fiscal_year DESC, period_end DESC` — i.e. by each company's
       fiscal calendar.
    2. The period filter stays a CLOSED allowlist. fact_metrics_us/_jp also hold
       Q1..Q4/H1 rows whose values are per-quarter and misleading (a Q2 P/E is ~4x
       too high); widening the filter would silently admit them.
    """
    import inspect
    import api.routers.screener as scr
    source = inspect.getsource(scr.screener_run)
    assert "fiscal_period IN ('FY', 'TTM')" in source
    assert "(fiscal_period = 'TTM') DESC" in source
    for excluded in ("'Q1'", "'Q2'", "'Q3'", "'Q4'", "'H1'", "'Annual'"):
        assert excluded not in source, f"{excluded} must not be admitted to the metric lateral"


# ---------------------------------------------------------------- INTL TTM window

def _q(rev, equity=100.0):
    return {
        "revenue": rev,
        "earnings_before_interest_taxes_depreciation_amortization": rev * 0.3,
        "total_equity": equity,
    }


_CONTIGUOUS = {
    date(2025, 6, 30): _q(10, 100), date(2025, 9, 30): _q(11, 110),
    date(2025, 12, 31): _q(12, 120), date(2026, 3, 31): _q(13, 130),
}
_ANNUAL_END = date(2025, 12, 31)


def test_ttm_window_sums_flows_and_takes_stocks_at_latest_quarter():
    bundle, end = compute_intl._ttm_window(_CONTIGUOUS, _ANNUAL_END, None)
    assert end == date(2026, 3, 31)
    assert bundle["revenue"] == 46.0                # flows summed across the 4 quarters
    assert bundle["total_equity"] == 130.0          # stock at the newest quarter, NOT 4x=460


def test_ttm_window_rejects_short_gapped_and_semiannual_windows():
    # Fewer than four quarters.
    assert compute_intl._ttm_window(dict(list(_CONTIGUOUS.items())[:3]), _ANNUAL_END, None)[0] is None
    # A missing quarter (184-day gap) still spans ~365 days, so a span-based guard would
    # pass it and emit a 15-month "TTM". The per-gap guard is what rejects it.
    gapped = {date(2025, 3, 31): _q(10), date(2025, 6, 30): _q(11),
              date(2025, 12, 31): _q(12), date(2026, 3, 31): _q(13)}
    assert compute_intl._ttm_window(gapped, _ANNUAL_END, None)[0] is None
    # Semi-annual filers (~182-day gaps) — the majority of HKEX.
    semi = {date(2024, 6, 30): _q(10), date(2024, 12, 31): _q(11),
            date(2025, 6, 30): _q(12), date(2025, 12, 31): _q(13)}
    assert compute_intl._ttm_window(semi, _ANNUAL_END, None)[0] is None


def test_ttm_window_rejects_stale_window_and_currency_switch():
    # Newest quarter no fresher than the annual row: a company that stopped reporting
    # would otherwise publish a stale TTM that outranks its own fresher FY row.
    assert compute_intl._ttm_window(_CONTIGUOUS, date(2026, 3, 31), None)[0] is None
    ccys = {date(2025, 6, 30): "EUR", date(2025, 9, 30): "EUR",
            date(2025, 12, 31): "USD", date(2026, 3, 31): "USD"}
    assert compute_intl._ttm_window(_CONTIGUOUS, _ANNUAL_END, ccys)[0] is None


def test_every_line_item_is_classified_flow_or_stock():
    # This assertion is the registry: ref_metric_definitions.metric_type is 'FNDM' for
    # every row and carries no period semantics, so nothing else stops a new alias from
    # being silently dropped from every TTM window.
    assert (compute_intl._FLOW_LINE_ITEMS | compute_intl._STOCK_LINE_ITEMS
            == set(compute_intl._YF_LINE_ITEM_ALIASES))
    assert not (compute_intl._FLOW_LINE_ITEMS & compute_intl._STOCK_LINE_ITEMS)


def test_ttm_basis_excludes_metrics_where_it_would_be_meaningless():
    # Yahoo's trailingPE is already TTM; price_to_book / dividend_yield are already
    # live; debt-to-equity is stock-only, so a "trailing" version is a category error;
    # growth/CAGR need 8-16 contiguous quarters that yfinance does not serve.
    for metric_id in ("price_to_earnings_trailing", "price_to_book", "dividend_yield",
                      "total_financial_debt_to_equity",
                      "revenue_growth_year_over_year",
                      "revenue_compound_annual_growth_rate_3_year",
                      "free_cash_flow_growth_year_over_year"):
        assert metric_id not in compute_intl._TTM_METRICS
