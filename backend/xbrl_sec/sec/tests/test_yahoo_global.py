from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from api.pipeline_catalog import build_cli_argv
from xbrl_sec.sec.sources.yahoo_global import (
    INDEX_BY_CODE,
    INDEX_CONFIGS,
    FallbackCsvSource,
    HttpJsonSource,
    HttpJsonSourceSpec,
    StooqSource,
    WikipediaSource,
    YahooIndexConfig,
    YahooScreenerSource,
    YahooScreenerSpec,
    build_default_sources,
    discover_fallback_index,
    discover_index,
    discover_wikipedia_index,
    intl_company_id,
    normalize_existing_yahoo_ticker,
    normalize_yahoo_ticker,
    resolve_index_configs,
    validate_yahoo_ticker,
)


def test_normalize_yahoo_ticker_adds_suffix_and_pads_hk():
    assert normalize_yahoo_ticker("SAP", ".DE") == "SAP.DE"
    assert normalize_yahoo_ticker("5", ".HK") == "0005.HK"
    assert normalize_yahoo_ticker("PETR4.SA", ".SA") == "PETR4.SA"
    assert normalize_yahoo_ticker("7203.T", ".T") is None
    assert normalize_yahoo_ticker("SAP", None) is None


def test_existing_ticker_normalization_excludes_japan_suffix_only():
    assert normalize_existing_yahoo_ticker("INFY.NS") == "INFY.NS"
    assert normalize_existing_yahoo_ticker("7203.T") is None


def test_intl_company_id_is_deterministic_from_yahoo_ticker():
    assert intl_company_id("sap.de") == "YFINTL:SAP.DE"


def test_index_config_universe_excludes_japan():
    assert all("JAPAN" not in cfg.country_name.upper() for cfg in INDEX_CONFIGS if cfg.country_name)
    assert all(not (cfg.default_suffix == ".T") for cfg in INDEX_CONFIGS)
    with pytest.raises(ValueError, match="out of scope"):
        resolve_index_configs(["NIKKEI_225"])


def test_discover_wikipedia_index_extracts_tickers_from_table():
    cfg = YahooIndexConfig(
        index_code="DAX",
        name="DAX",
        region="Europe",
        default_suffix=".DE",
        country_code="DE",
        country_name="Germany",
        wikipedia_url="https://example.test/dax",
    )
    table = pd.DataFrame(
        {
            "Company": ["SAP", "Siemens"],
            "Ticker": ["SAP", "SIE"],
        }
    )

    rows = discover_wikipedia_index(cfg, read_html=lambda _url: [table])

    assert [row.primary_ticker for row in rows] == ["SAP.DE", "SIE.DE"]
    assert rows[0].name == "SAP"
    assert rows[0].source_name == "wikipedia"


def test_discover_fallback_index_reads_local_csv(tmp_path: Path):
    (tmp_path / "DAX.csv").write_text("ticker,name\nSAP,SAP SE\nSIE,Siemens\n", encoding="utf-8")
    cfg = YahooIndexConfig("DAX", "DAX", "Europe", ".DE", "DE", "Germany")

    rows = discover_fallback_index(cfg, fallback_dir=tmp_path)

    assert [row.primary_ticker for row in rows] == ["SAP.DE", "SIE.DE"]
    assert rows[1].name == "Siemens"
    assert rows[1].source_name == "fallback_csv"


class _FakeTicker:
    def __init__(self, info):
        self.info = info


class _FakeYF:
    def __init__(self, info_by_symbol):
        self.info_by_symbol = info_by_symbol

    def Ticker(self, symbol):
        return _FakeTicker(self.info_by_symbol.get(symbol, {}))


def test_validate_yahoo_ticker_accepts_mocked_equity_profile():
    yf = _FakeYF({"SAP.DE": {"symbol": "SAP.DE", "quoteType": "EQUITY", "longName": "SAP SE"}})

    profile = validate_yahoo_ticker("SAP.DE", yf_module=yf, sleeper=lambda _delay: None)

    assert profile is not None
    assert profile["longName"] == "SAP SE"


def test_validate_yahoo_ticker_rejects_empty_profile():
    yf = _FakeYF({"SAP.DE": {}})

    assert validate_yahoo_ticker("SAP.DE", yf_module=yf, sleeper=lambda _delay: None) is None


def test_pipeline_catalog_builds_yahoo_global_args():
    _command, argv = build_cli_argv(
        "yahoo_global.discover_tickers",
        {
            "index": ["DAX"],
            "dry_run": True,
            "validate": False,
            "use_wikipedia": False,
            "sleep_seconds": 0.1,
        },
    )

    assert argv == [
        "discover-yahoo-tickers",
        "--index",
        "DAX",
        "--dry-run",
        "--no-validate",
        "--no-wikipedia",
        "--sleep-seconds",
        "0.1",
    ]


def test_all_new_index_configs_are_hygienic():
    """Phase 1 expansion hygiene: no duplicate codes, no Japan, no empty regions."""
    seen: set[str] = set()
    for cfg in INDEX_CONFIGS:
        assert cfg.index_code not in seen, f"duplicate index_code {cfg.index_code}"
        seen.add(cfg.index_code)
        assert cfg.region, f"empty region for {cfg.index_code}"
        assert cfg.default_suffix != ".T", f"Japan suffix leaked into {cfg.index_code}"
        for alt in cfg.alt_suffixes:
            assert alt.startswith("."), f"alt_suffix {alt!r} missing leading dot on {cfg.index_code}"
            assert alt != ".T", f"Japan alt-suffix leaked into {cfg.index_code}"
        assert (
            cfg.wikipedia_url
            or cfg.stooq_code
            or cfg.extra_source_urls
            or cfg.screener_spec
            or cfg.http_json_spec
        ), f"{cfg.index_code} has no discovery source"
        if cfg.is_wholesale:
            assert cfg.pipeline_sample_group == "exchange_wholesale", (
                f"{cfg.index_code} wholesale flag set but pipeline_sample_group is {cfg.pipeline_sample_group!r}"
            )


def test_phase1_expansion_covers_target_regions():
    regions = {cfg.region for cfg in INDEX_CONFIGS}
    for expected in ("Europe", "Asia", "South America", "Africa", "MENA", "North America"):
        assert expected in regions, f"missing region {expected}"
    assert "JSE_TOP_40" in INDEX_BY_CODE
    assert "TASI" in INDEX_BY_CODE
    assert "TSX_60" in INDEX_BY_CODE
    assert "NIFTY_500" in INDEX_BY_CODE
    assert "OMXS30" in INDEX_BY_CODE


def test_alt_suffix_variants_generated_in_order():
    from xbrl_sec.sec.sources.yahoo_global import _ticker_variants
    cfg = INDEX_BY_CODE["NIFTY_500"]
    variants = _ticker_variants("RELIANCE.NS", cfg)
    assert variants[0] == "RELIANCE.NS"
    assert "RELIANCE.BO" in variants


def test_discover_index_unions_multiple_sources_and_dedupes():
    cfg = YahooIndexConfig("TEST", "Test", "Europe", ".DE", "DE", "Germany")

    class _StaticSource(WikipediaSource):
        def __init__(self, name, tickers):
            self.name = name
            self._tickers = tickers
        def discover(self, config):
            from xbrl_sec.sec.sources.yahoo_global import DiscoveredTicker
            return [
                DiscoveredTicker(
                    index_code=config.index_code,
                    raw_ticker=t,
                    primary_ticker=t,
                    name=None,
                    country_code=None,
                    exchange_suffix=None,
                    source_name=self.name,
                    source_url=None,
                    source_rank=idx,
                    raw_payload={},
                )
                for idx, t in enumerate(self._tickers, start=1)
            ]

    src_a = _StaticSource("a", ["SAP.DE", "SIE.DE"])
    src_b = _StaticSource("b", ["SIE.DE", "BAS.DE"])

    rows = discover_index(cfg, sources=[src_a, src_b])
    tickers = [r.primary_ticker for r in rows]

    assert tickers == ["SAP.DE", "SIE.DE", "BAS.DE"]
    assert rows[1].source_name == "a"  # first-seen wins


def test_build_default_sources_orders_wiki_stooq_screener_httpjson_csv():
    sources = build_default_sources()
    names = [s.name for s in sources]
    assert names == ["wikipedia", "stooq", "yahoo_screener", "http_json", "fallback_csv"]


def test_stooq_source_returns_empty_without_code():
    cfg = YahooIndexConfig("TEST", "Test", "Europe", ".DE", "DE", "Germany")
    assert StooqSource(fetch=lambda _url: "").discover(cfg) == []


def test_stooq_source_parses_csv_when_code_present():
    cfg = YahooIndexConfig(
        "TEST_STOOQ",
        "Test",
        "Europe",
        ".DE",
        "DE",
        "Germany",
        stooq_code="^dax",
    )
    body = "Symbol,Name\nSAP,SAP SE\nSIE,Siemens\n"
    rows = StooqSource(fetch=lambda _url: body).discover(cfg)
    assert [r.primary_ticker for r in rows] == ["SAP.DE", "SIE.DE"]
    assert rows[0].source_name == "stooq"


def test_resolve_index_configs_default_excludes_wholesale():
    all_default = resolve_index_configs()
    wholesale = [c for c in INDEX_CONFIGS if c.is_wholesale]
    assert wholesale, "expected at least one wholesale config"
    assert not any(c.is_wholesale for c in all_default)
    all_with_wholesale = resolve_index_configs(include_wholesale=True)
    assert len(all_with_wholesale) == len(INDEX_CONFIGS)


def test_resolve_index_configs_explicit_codes_pass_through_wholesale():
    configs = resolve_index_configs(["LSE_ALL"])
    assert [c.index_code for c in configs] == ["LSE_ALL"]
    assert configs[0].is_wholesale


def test_yahoo_screener_source_paginates_and_dedupes():
    cfg = INDEX_BY_CODE["LSE_ALL"]
    page_size = YahooScreenerSource.PAGE_SIZE

    pages = [
        {"quotes": [{"symbol": f"AAA{n}.L", "longName": f"Alpha {n}"} for n in range(page_size)]},
        {"quotes": [
            {"symbol": "AAA0.L", "longName": "Alpha 0"},  # dup — dropped
            {"symbol": "BBB.L", "longName": "Beta"},
        ]},
    ]
    call_offsets: list[int] = []

    def _screen(*, query, size, offset):
        call_offsets.append(int(offset))
        page_idx = min(len(call_offsets) - 1, len(pages) - 1)
        return pages[page_idx]

    rows = YahooScreenerSource(screen=_screen).discover(cfg)

    assert call_offsets == [0, page_size]
    tickers = [r.primary_ticker for r in rows]
    assert "AAA0.L" in tickers
    assert "BBB.L" in tickers
    assert len(tickers) == len(set(tickers))
    assert rows[0].source_name == "yahoo_screener"


def test_yahoo_screener_source_empty_when_spec_missing():
    cfg = YahooIndexConfig("TEST", "Test", "Europe", ".DE", "DE", "Germany")
    assert YahooScreenerSource(screen=lambda **_: {"quotes": []}).discover(cfg) == []


def test_http_json_source_walks_dotted_path():
    cfg = YahooIndexConfig(
        "TEST_HTTP",
        "Test",
        "Asia",
        ".SI",
        "SG",
        "Singapore",
        http_json_spec=HttpJsonSourceSpec(
            url="https://example.test/securities",
            rows_path=("data", "prices"),
            ticker_key="nc",
            name_key="cn",
        ),
    )
    body = '{"data":{"prices":[{"nc":"D05","cn":"DBS"},{"nc":"O39","cn":"OCBC"}]}}'

    rows = HttpJsonSource(fetch=lambda _url: body).discover(cfg)

    assert [r.primary_ticker for r in rows] == ["D05.SI", "O39.SI"]
    assert rows[0].name == "DBS"
    assert rows[0].source_name == "http_json"


def test_http_json_source_returns_empty_when_path_missing():
    cfg = YahooIndexConfig(
        "TEST_HTTP",
        "Test",
        "Asia",
        ".SI",
        "SG",
        "Singapore",
        http_json_spec=HttpJsonSourceSpec(url="https://x.test", rows_path=("data", "prices"), ticker_key="nc"),
    )
    assert HttpJsonSource(fetch=lambda _url: '{"data":{"other":[]}}').discover(cfg) == []


def test_build_default_sources_includes_screener_and_http_json():
    sources = build_default_sources()
    names = [s.name for s in sources]
    assert names == ["wikipedia", "stooq", "yahoo_screener", "http_json", "fallback_csv"]


def test_wholesale_configs_are_tagged():
    wholesale_codes = {c.index_code for c in INDEX_CONFIGS if c.is_wholesale}
    for code in ("LSE_ALL", "TSX_ALL", "JSE_ALL_WHOLESALE", "KRX_ALL", "SGX_ALL", "BSE_ALL"):
        assert code in wholesale_codes, f"missing wholesale seed for {code}"


def test_yahoo_to_gics_mapping_covers_all_11_yahoo_sectors():
    from xbrl_sec.sec.sources.yahoo_global import YAHOO_TO_GICS_SECTOR, yahoo_sector_to_gics
    assert len(YAHOO_TO_GICS_SECTOR) == 11
    assert yahoo_sector_to_gics("Technology") == ("45", "Information Technology")
    assert yahoo_sector_to_gics("Financial Services") == ("40", "Financials")
    assert yahoo_sector_to_gics("Real Estate") == ("60", "Real Estate")
    assert yahoo_sector_to_gics(None) == (None, None)
    assert yahoo_sector_to_gics("Unknown Made Up") == (None, None)
    # Every 2-digit GICS sector code that maps from Yahoo must be a valid GICS 2-digit code.
    valid_gics = {"10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60"}
    codes = {code for code, _ in YAHOO_TO_GICS_SECTOR.values()}
    assert codes.issubset(valid_gics)


def test_statement_rows_iterates_annual_and_quarterly():
    from xbrl_sec.sec.sources.yahoo_global import _statement_rows
    import pandas as _pd
    from datetime import date as _date

    class _FakeTicker:
        financials = _pd.DataFrame(
            {_date(2024, 12, 31): [100.0]},
            index=["Total Revenue"],
        )
        quarterly_financials = _pd.DataFrame(
            {_date(2024, 12, 31): [30.0], _date(2024, 9, 30): [25.0]},
            index=["Total Revenue"],
        )
        balance_sheet = _pd.DataFrame()
        quarterly_balance_sheet = _pd.DataFrame()
        cashflow = _pd.DataFrame()
        quarterly_cashflow = _pd.DataFrame()

    rows_both = _statement_rows(_FakeTicker(), "USD", include_quarterly=True)
    kinds = {(r[0], r[1]) for r in rows_both}
    assert ("income_statement", "annual") in kinds
    assert ("income_statement", "quarterly") in kinds

    rows_annual = _statement_rows(_FakeTicker(), "USD", include_quarterly=False)
    kinds_annual = {r[1] for r in rows_annual}
    assert kinds_annual == {"annual"}


def test_fetch_yahoo_profile_raises_on_auth_error():
    from xbrl_sec.sec.sources.yahoo_global import (
        YahooAuthError, fetch_yahoo_profile,
    )

    class _FakeTicker:
        info = None
        def __init__(self, *_a, **_kw): pass
        @property
        def get_info(self):
            def _raise(*_a, **_kw):
                raise RuntimeError("HTTP Error 401: Invalid Crumb")
            return _raise

    class _FakeYF:
        def Ticker(self, symbol, **_kwargs):
            t = _FakeTicker()
            # Force ``.info`` access to raise the auth error
            def _get_info(_):
                raise RuntimeError("Invalid Crumb")
            type(t).info = property(_get_info)
            return t

    with pytest.raises(YahooAuthError):
        fetch_yahoo_profile("SAP.DE", yf_module=_FakeYF())


def test_rate_limiter_paces_calls():
    from xbrl_sec.sec.sources.yahoo_global import _RateLimiter
    import time as _time
    limiter = _RateLimiter(rate=10.0)  # 10 req/s → interval 100ms
    start = _time.monotonic()
    for _ in range(3):
        limiter.acquire()
    elapsed = _time.monotonic() - start
    # First call is free; 2nd and 3rd each wait ~100ms.
    assert elapsed >= 0.15, f"expected >=150ms, got {elapsed*1000:.0f}ms"


def test_pipeline_catalog_builds_yahoo_price_args():
    _command, argv = build_cli_argv(
        "yahoo_global.fetch_prices",
        {
            "ticker": ["SAP.DE"],
            "start_date": "2010-01-01",
            "full": True,
            "dry_run": True,
            "sleep_seconds": 0.0,
        },
    )

    assert argv == [
        "fetch-yahoo-prices",
        "--ticker",
        "SAP.DE",
        "--start-date",
        "2010-01-01",
        "--full",
        "--dry-run",
        "--sleep-seconds",
        "0.0",
    ]
