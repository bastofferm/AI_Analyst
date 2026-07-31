"""Yahoo Finance global equity discovery and fundamentals ingestion.

Yahoo Finance is an unofficial source. This module keeps its data in
Yahoo-specific international tables so SEC/EDINET XBRL facts remain untouched.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
import math
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running


SOURCE_DISCOVERY = "yahoo_global_discovery"
SOURCE_FUNDAMENTALS = "yahoo_global_fundamentals"
SOURCE_PRICES = "yahoo_global_prices"
INTL_ID_PREFIX = "YFINTL:"
JAPAN_INDEX_CODES = {"NIKKEI_225", "NIKKEI225", "TOPIX", "JPX400", "JPX_400"}
JAPAN_SUFFIXES = {".T"}


@dataclass(frozen=True)
class YahooScreenerSpec:
    """Filter spec for the public Yahoo Finance screener endpoint.

    ``region`` is a lowercase two-letter ISO country code (``gb``, ``ca``,
    ``za``, ``kr``). ``exchanges`` optionally narrows to Yahoo exchange
    codes (``LSE``, ``TOR``, ``JNB``, ``KSC``+``KOE``). ``symbol_regex``
    optionally keeps only normalized tickers matching the pattern — used to
    drop non-company securities the screener mislabels as equities (e.g. HKEX
    5-digit derivative warrants / CBBCs / RMB counters vs 4-digit companies).
    """

    region: str
    exchanges: tuple[str, ...] = ()
    max_rows: int = 5000
    symbol_regex: str | None = None


@dataclass(frozen=True)
class HttpJsonSourceSpec:
    """Generic HTTP-JSON adapter spec.

    ``rows_path`` is a dotted path descended through the JSON payload to
    reach the row array. ``ticker_key`` names the row field carrying the
    raw ticker. ``name_key`` optionally names the row field carrying the
    company name.
    """

    url: str
    rows_path: tuple[str, ...] = ()
    ticker_key: str = "symbol"
    name_key: str | None = "name"


@dataclass(frozen=True)
class YahooIndexConfig:
    index_code: str
    name: str
    region: str
    default_suffix: str | None
    country_code: str | None = None
    country_name: str | None = None
    wikipedia_url: str | None = None
    yahoo_symbol: str | None = None
    alt_suffixes: tuple[str, ...] = ()
    notes: str | None = None
    stooq_code: str | None = None
    extra_source_urls: tuple[str, ...] = ()
    pipeline_sample_group: str | None = None
    screener_spec: YahooScreenerSpec | None = None
    http_json_spec: HttpJsonSourceSpec | None = None
    is_wholesale: bool = False
    # When True, a validated ticker is only kept if its Yahoo profile is actually
    # domiciled in this config's country (profile country == country_name, or the
    # ISIN carries this config's country_code prefix). Filters out the many foreign
    # companies cross-listed on a national exchange (e.g. US names on Xetra).
    screener_domicile_only: bool = False
    # When True, a validated company is skipped if its ISIN already exists in
    # dim_company_intl under a different ticker — i.e. this is a secondary listing
    # (e.g. a Frankfurt .F counter of a company already held via its Xetra .DE line).
    dedupe_by_isin: bool = False


@dataclass(frozen=True)
class DiscoveredTicker:
    index_code: str
    raw_ticker: str
    primary_ticker: str
    name: str | None
    country_code: str | None
    exchange_suffix: str | None
    source_name: str
    source_url: str | None
    source_rank: int
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class CompanyTarget:
    intl_company_id: str
    primary_ticker: str
    currency: str | None = None
    region: str | None = None
    country_code: str | None = None
    exchange: str | None = None


@dataclass(frozen=True)
class FundamentalPayload:
    target: CompanyTarget
    profile: dict[str, Any]
    metrics: list[tuple[str, Decimal | None, str | None, dict[str, Any]]]
    statements: list[tuple[str, str, date, int | None, str, Decimal | None, str | None]]


def _cfg(
    index_code: str,
    name: str,
    region: str,
    suffix: str | None,
    country_code: str | None,
    country_name: str | None,
    wikipedia_path: str | None,
    yahoo_symbol: str | None = None,
    alt_suffixes: tuple[str, ...] = (),
    notes: str | None = None,
    stooq_code: str | None = None,
    extra_source_urls: tuple[str, ...] = (),
    pipeline_sample_group: str | None = None,
    screener_spec: YahooScreenerSpec | None = None,
    http_json_spec: HttpJsonSourceSpec | None = None,
    is_wholesale: bool = False,
    screener_domicile_only: bool = False,
    dedupe_by_isin: bool = False,
) -> YahooIndexConfig:
    url = f"https://en.wikipedia.org/wiki/{wikipedia_path}" if wikipedia_path else None
    return YahooIndexConfig(
        index_code,
        name,
        region,
        suffix,
        country_code,
        country_name,
        url,
        yahoo_symbol,
        alt_suffixes,
        notes,
        stooq_code,
        extra_source_urls,
        pipeline_sample_group,
        screener_spec,
        http_json_spec,
        is_wholesale,
        screener_domicile_only,
        dedupe_by_isin,
    )


INDEX_CONFIGS: tuple[YahooIndexConfig, ...] = (
    _cfg("DAX", "DAX", "Europe", ".DE", "DE", "Germany", "DAX", "^GDAXI"),
    _cfg("MDAX", "MDAX", "Europe", ".DE", "DE", "Germany", "MDAX", "^MDAXI"),
    _cfg("TECDAX", "TecDAX", "Europe", ".DE", "DE", "Germany", "TecDAX", "^TECDAX"),
    _cfg("CAC_40", "CAC 40", "Europe", ".PA", "FR", "France", "CAC_40", "^FCHI"),
    _cfg("CAC_NEXT_20", "CAC Next 20", "Europe", ".PA", "FR", "France", "CAC_Next_20", None),
    _cfg("FTSE_100", "FTSE 100", "Europe", ".L", "GB", "United Kingdom", "FTSE_100_Index", "^FTSE"),
    _cfg("FTSE_250", "FTSE 250", "Europe", ".L", "GB", "United Kingdom", "FTSE_250_Index", "^FTMC"),
    _cfg("AEX", "AEX", "Europe", ".AS", "NL", "Netherlands", "AEX_index", "^AEX"),
    _cfg("SMI", "Swiss Market Index", "Europe", ".SW", "CH", "Switzerland", "Swiss_Market_Index", "^SSMI"),
    _cfg("FTSE_MIB", "FTSE MIB", "Europe", ".MI", "IT", "Italy", "FTSE_MIB", "FTSEMIB.MI"),
    _cfg("IBEX_35", "IBEX 35", "Europe", ".MC", "ES", "Spain", "IBEX_35", "^IBEX"),
    _cfg("STOXX_EUROPE_600", "STOXX Europe 600", "Europe", None, None, None, "STOXX_Europe_600", "^STOXX"),
    _cfg("STOXX_EUROPE_600_TECH", "STOXX Europe 600 Technology", "Europe", None, None, None, "STOXX_Europe_600", None),
    _cfg("HANG_SENG", "Hang Seng", "Asia", ".HK", "HK", "Hong Kong", "Hang_Seng_Index", "^HSI"),
    _cfg("HANG_SENG_TECH", "Hang Seng Tech", "Asia", ".HK", "HK", "Hong Kong", "Hang_Seng_TECH_Index", "^HSTECH"),
    _cfg("KOSPI", "KOSPI", "Asia", ".KS", "KR", "South Korea", "KOSPI", "^KS11"),
    _cfg("KOSDAQ", "KOSDAQ", "Asia", ".KQ", "KR", "South Korea", "KOSDAQ", "^KQ11"),
    _cfg("NIFTY_50", "NIFTY 50", "Asia", ".NS", "IN", "India", "NIFTY_50", "^NSEI"),
    _cfg("NIFTY_NEXT_50", "NIFTY Next 50", "Asia", ".NS", "IN", "India", "NIFTY_Next_50", None),
    _cfg("NIFTY_IT", "NIFTY IT", "Asia", ".NS", "IN", "India", "NIFTY_IT", None),
    _cfg("SSE_COMPOSITE", "SSE Composite", "Asia", ".SS", "CN", "China", "SSE_Composite_Index", "000001.SS"),
    _cfg("SZSE_COMPONENT", "SZSE Component", "Asia", ".SZ", "CN", "China", "SZSE_Component_Index", "399001.SZ"),
    _cfg("TWSE", "TWSE", "Asia", ".TW", "TW", "Taiwan", "Taiwan_Capitalization_Weighted_Stock_Index", "^TWII"),
    _cfg("STI", "Straits Times Index", "Asia", ".SI", "SG", "Singapore", "Straits_Times_Index", "^STI"),
    _cfg("BOVESPA", "Bovespa", "South America", ".SA", "BR", "Brazil", "Indice_Bovespa", "^BVSP"),
    _cfg("IBRX_100", "IBrX 100", "South America", ".SA", "BR", "Brazil", "IBrX_100", None),
    _cfg("IPC_MEXICO", "IPC Mexico", "South America", ".MX", "MX", "Mexico", "Indice_de_Precios_y_Cotizaciones", "^MXX"),
    _cfg("FTSE_BIVA", "FTSE BIVA", "South America", ".MX", "MX", "Mexico", "FTSE_BIVA", None,
         notes="Wikipedia stub — may return no constituents; ships as scaffold for CSV fallback."),
    _cfg("IPSA", "IPSA", "South America", ".SN", "CL", "Chile", "S%26P/CLX_IPSA", "^IPSA"),
    _cfg("MERVAL", "MERVAL", "South America", ".BA", "AR", "Argentina", "S%26P_MERVAL_Index", "^MERV"),
    _cfg("COLCAP", "COLCAP", "South America", ".CN", "CO", "Colombia", "COLCAP", None),
    _cfg("SPBVL", "SPBVL", "South America", ".LM", "PE", "Peru", "S%26P/BVL_Peru_General_Index", None),
    # -- Phase 1 expansion (2026-07-07): broaden country/size/sector coverage.
    # Europe -- Nordics
    _cfg("OMXS30", "OMX Stockholm 30", "Europe", ".ST", "SE", "Sweden", "OMX_Stockholm_30", "^OMX"),
    _cfg("OMXSPI", "OMX Stockholm PI", "Europe", ".ST", "SE", "Sweden", "OMX_Stockholm_PI", "^OMXSPI"),
    _cfg("OMXC25", "OMX Copenhagen 25", "Europe", ".CO", "DK", "Denmark", "OMX_Copenhagen_25", "^OMXC25"),
    _cfg("OMXH25", "OMX Helsinki 25", "Europe", ".HE", "FI", "Finland", "OMX_Helsinki_25", "^OMXH25"),
    _cfg("OBX", "OBX Index", "Europe", ".OL", "NO", "Norway", "OBX_Index", "OBX.OL"),
    _cfg("OMXI15", "OMX Iceland 15", "Europe", ".IC", "IS", "Iceland", "OMX_Iceland_15", None),
    # Europe -- UK small / tech
    _cfg("FTSE_SMALLCAP", "FTSE SmallCap", "Europe", ".L", "GB", "United Kingdom", "FTSE_SmallCap_Index", "^FTSC"),
    _cfg("FTSE_AIM_100", "FTSE AIM 100", "Europe", ".L", "GB", "United Kingdom", "FTSE_AIM_100_Index", "^AIM1"),
    _cfg("FTSE_AIM_ALL_SHARE", "FTSE AIM All-Share", "Europe", ".L", "GB", "United Kingdom", "FTSE_AIM_All-Share_Index", "^AXX"),
    _cfg("FTSE_TECHMARK", "FTSE techMARK", "Europe", ".L", "GB", "United Kingdom", "TechMARK", None),
    # Europe -- Germany small
    _cfg("SDAX", "SDAX", "Europe", ".DE", "DE", "Germany", "SDAX", "^SDAXI"),
    _cfg("DAX_50_ESG", "DAX 50 ESG", "Europe", ".DE", "DE", "Germany", "DAX_50_ESG", None),
    # Europe -- France mid / small
    _cfg("SBF_120", "SBF 120", "Europe", ".PA", "FR", "France", "SBF_120", "^SBF120"),
    _cfg("CAC_MID_60", "CAC Mid 60", "Europe", ".PA", "FR", "France", "CAC_Mid_60", None),
    _cfg("CAC_SMALL", "CAC Small", "Europe", ".PA", "FR", "France", "CAC_Small", None),
    # Europe -- Iberia / Italy small
    _cfg("IBEX_MEDIUM_CAP", "IBEX Medium Cap", "Europe", ".MC", "ES", "Spain", "IBEX_Medium_Cap", None),
    _cfg("IBEX_SMALL_CAP", "IBEX Small Cap", "Europe", ".MC", "ES", "Spain", "IBEX_Small_Cap", None),
    _cfg("FTSE_ITALIA_STAR", "FTSE Italia STAR", "Europe", ".MI", "IT", "Italy", "FTSE_Italia_STAR", None),
    _cfg("FTSE_ITALIA_MID_CAP", "FTSE Italia Mid Cap", "Europe", ".MI", "IT", "Italy", "FTSE_Italia_Mid_Cap", None),
    # Europe -- Benelux
    _cfg("BEL_20", "BEL 20", "Europe", ".BR", "BE", "Belgium", "BEL_20", "^BFX"),
    _cfg("BEL_MID", "BEL Mid", "Europe", ".BR", "BE", "Belgium", "BEL_Mid", None),
    _cfg("AMX", "AMX", "Europe", ".AS", "NL", "Netherlands", "AMX_index", "^AMX"),
    _cfg("ASCX", "AScX", "Europe", ".AS", "NL", "Netherlands", "AScX", "^ASCX"),
    # Europe -- CEE / Balkans
    _cfg("WIG20", "WIG20", "Europe", ".WA", "PL", "Poland", "WIG20", "WIG20.WA"),
    _cfg("MWIG40", "mWIG40", "Europe", ".WA", "PL", "Poland", "MWIG40", None),
    _cfg("SWIG80", "sWIG80", "Europe", ".WA", "PL", "Poland", "SWIG80", None),
    _cfg("PX", "PX Prague", "Europe", ".PR", "CZ", "Czechia", "PX_(Prague_Stock_Exchange)", "^PX"),
    _cfg("BUX", "BUX", "Europe", ".BD", "HU", "Hungary", "Budapest_Stock_Exchange", "^BUX.BD"),
    _cfg("ATX", "ATX", "Europe", ".VI", "AT", "Austria", "Austrian_Traded_Index", "^ATX"),
    _cfg("BET", "BET (Bucharest)", "Europe", ".RO", "RO", "Romania", "BET_(Bucharest_Stock_Exchange)", None),
    _cfg("SOFIX", "SOFIX", "Europe", ".SF", "BG", "Bulgaria", "SOFIX", None),
    # Europe -- SE Mediterranean
    _cfg("ATHEX_LARGE_CAP", "FTSE/Athex Large Cap", "Europe", ".AT", "GR", "Greece", "FTSE/Athex_Large_Cap", "GD.AT"),
    _cfg("BIST_30", "BIST 30", "Europe", ".IS", "TR", "Turkey", "BIST_30", "XU030.IS"),
    _cfg("BIST_50", "BIST 50", "Europe", ".IS", "TR", "Turkey", "BIST_50", "XU050.IS"),
    _cfg("BIST_100", "BIST 100", "Europe", ".IS", "TR", "Turkey", "BIST_100", "XU100.IS"),
    _cfg("TA_35", "TA-35", "Europe", ".TA", "IL", "Israel", "TA-35_Index", "TA35.TA"),
    _cfg("TA_125", "TA-125", "Europe", ".TA", "IL", "Israel", "TA-125_Index", "TA125.TA"),
    # Europe -- STOXX sub-indices
    _cfg("STOXX_EUROPE_600_BANKS", "STOXX Europe 600 Banks", "Europe", None, None, None, "STOXX_Europe_600#Sector_indices", None),
    _cfg("STOXX_EUROPE_600_HEALTH", "STOXX Europe 600 Health Care", "Europe", None, None, None, "STOXX_Europe_600#Sector_indices", None),
    _cfg("STOXX_EUROPE_600_ENERGY", "STOXX Europe 600 Oil & Gas", "Europe", None, None, None, "STOXX_Europe_600#Sector_indices", None),
    # Asia -- India broader
    _cfg("NIFTY_100", "NIFTY 100", "Asia", ".NS", "IN", "India", "NIFTY_100", "^CNX100", alt_suffixes=(".BO",)),
    _cfg("NIFTY_200", "NIFTY 200", "Asia", ".NS", "IN", "India", "NIFTY_200", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_500", "NIFTY 500", "Asia", ".NS", "IN", "India", "NIFTY_500", "^CRSLDX", alt_suffixes=(".BO",)),
    _cfg("NIFTY_MIDCAP_100", "NIFTY Midcap 100", "Asia", ".NS", "IN", "India", "NIFTY_Midcap_100", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_MIDCAP_150", "NIFTY Midcap 150", "Asia", ".NS", "IN", "India", "NIFTY_Midcap_150", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_SMALLCAP_100", "NIFTY Smallcap 100", "Asia", ".NS", "IN", "India", "NIFTY_Smallcap_100", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_SMALLCAP_250", "NIFTY Smallcap 250", "Asia", ".NS", "IN", "India", "NIFTY_Smallcap_250", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_BANK", "NIFTY Bank", "Asia", ".NS", "IN", "India", "NIFTY_Bank", "^NSEBANK", alt_suffixes=(".BO",)),
    _cfg("NIFTY_PHARMA", "NIFTY Pharma", "Asia", ".NS", "IN", "India", "NIFTY_Pharma", None, alt_suffixes=(".BO",)),
    _cfg("NIFTY_AUTO", "NIFTY Auto", "Asia", ".NS", "IN", "India", "NIFTY_Auto", None, alt_suffixes=(".BO",)),
    _cfg("BSE_500", "S&P BSE 500", "Asia", ".BO", "IN", "India", "S%26P_BSE_500", "BSE-500.BO", alt_suffixes=(".NS",)),
    _cfg("BSE_MIDCAP", "S&P BSE MidCap", "Asia", ".BO", "IN", "India", "S%26P_BSE_MidCap", None, alt_suffixes=(".NS",)),
    _cfg("BSE_SMALLCAP", "S&P BSE SmallCap", "Asia", ".BO", "IN", "India", "S%26P_BSE_SmallCap", None, alt_suffixes=(".NS",)),
    # Asia -- China A-shares broader
    _cfg("CSI_300", "CSI 300", "Asia", ".SS", "CN", "China", "CSI_300_Index", "000300.SS", alt_suffixes=(".SZ",)),
    _cfg("CSI_500", "CSI 500", "Asia", ".SS", "CN", "China", "CSI_500_Index", "000905.SS", alt_suffixes=(".SZ",)),
    _cfg("CSI_1000", "CSI 1000", "Asia", ".SS", "CN", "China", "CSI_1000_Index", "000852.SS", alt_suffixes=(".SZ",)),
    _cfg("CHINEXT", "ChiNext", "Asia", ".SZ", "CN", "China", "ChiNext_Price_Index", "399006.SZ", alt_suffixes=(".SS",)),
    _cfg("STAR_50", "STAR 50", "Asia", ".SS", "CN", "China", "SSE_STAR_50_Component_Index", "000688.SS", alt_suffixes=(".SZ",)),
    # Asia -- HK / TW broader
    _cfg("HANG_SENG_COMPOSITE", "Hang Seng Composite", "Asia", ".HK", "HK", "Hong Kong", "Hang_Seng_Composite_Index", "^HSCI"),
    _cfg("HANG_SENG_SMALLCAP", "Hang Seng SmallCap", "Asia", ".HK", "HK", "Hong Kong", "Hang_Seng_Composite_Index", None),
    _cfg("TAIWAN_50", "Taiwan 50", "Asia", ".TW", "TW", "Taiwan", "FTSE_TWSE_Taiwan_50_Index", "0050.TW", alt_suffixes=(".TWO",)),
    _cfg("TPEX", "TPEx 50", "Asia", ".TWO", "TW", "Taiwan", "Taipei_Exchange", None, alt_suffixes=(".TW",)),
    # Asia -- Korea broader
    _cfg("KOSPI_200", "KOSPI 200", "Asia", ".KS", "KR", "South Korea", "KOSPI_200", "^KS200"),
    _cfg("KRX_300", "KRX 300", "Asia", ".KS", "KR", "South Korea", "KRX_300", None, alt_suffixes=(".KQ",)),
    _cfg("KOSDAQ_150", "KOSDAQ 150", "Asia", ".KQ", "KR", "South Korea", "KOSDAQ_150", None, alt_suffixes=(".KS",)),
    # Asia -- ASEAN
    _cfg("KLCI", "FTSE Bursa Malaysia KLCI", "Asia", ".KL", "MY", "Malaysia", "FTSE_Bursa_Malaysia_KLCI", "^KLSE"),
    _cfg("KLCI_HIJRAH", "FTSE Bursa Malaysia Hijrah Shariah", "Asia", ".KL", "MY", "Malaysia", "FTSE_Bursa_Malaysia_Hijrah_Shariah_Index", None),
    _cfg("SET50", "SET50", "Asia", ".BK", "TH", "Thailand", "SET50_Index", "^SET.BK"),
    _cfg("SET100", "SET100", "Asia", ".BK", "TH", "Thailand", "SET100_Index", None),
    _cfg("VN30", "VN30", "Asia", ".VN", "VN", "Vietnam", "VN30", "^VN30"),
    _cfg("HNX30", "HNX30", "Asia", ".VN", "VN", "Vietnam", "HNX30", None),
    _cfg("PSEI", "PSEi", "Asia", ".PS", "PH", "Philippines", "PSEi", "PSEI.PS"),
    _cfg("IDX30", "IDX30", "Asia", ".JK", "ID", "Indonesia", "IDX30", None),
    _cfg("LQ45", "LQ45", "Asia", ".JK", "ID", "Indonesia", "LQ45", None),
    _cfg("IDX80", "IDX80", "Asia", ".JK", "ID", "Indonesia", "IDX80", None),
    # South America -- Brazil sub-indices
    _cfg("IBRX_50", "IBrX 50", "South America", ".SA", "BR", "Brazil", "IBrX_50", None),
    _cfg("IBRA", "IBrA", "South America", ".SA", "BR", "Brazil", "IBrA", None),
    _cfg("SMLL", "SMLL (Brazil Small Cap)", "South America", ".SA", "BR", "Brazil", "SMLL", None),
    _cfg("IDIV", "IDIV (Brazil Dividend)", "South America", ".SA", "BR", "Brazil", "IDIV", None),
    _cfg("ICON", "ICON (Brazil Consumer)", "South America", ".SA", "BR", "Brazil", "ICON", None),
    # South America -- Others
    _cfg("BMV_IMC30", "S&P/BMV IMC30", "South America", ".MX", "MX", "Mexico", "IMC30", None),
    _cfg("SPBVL_PERU_SELECT", "S&P/BVL Peru Select", "South America", ".LM", "PE", "Peru", "S%26P/BVL_Peru_Select_Index", None),
    _cfg("MERVAL_25", "MERVAL 25", "South America", ".BA", "AR", "Argentina", "S%26P_MERVAL_Index", None),
    _cfg("SP_CLX_65", "S&P/CLX 65", "South America", ".SN", "CL", "Chile", "S%26P/CLX_IPSA", None),
    # Africa
    _cfg("JSE_TOP_40", "JSE Top 40", "Africa", ".JO", "ZA", "South Africa", "FTSE/JSE_Top_40_Index", "^JTOPI"),
    _cfg("JSE_ALL_SHARE", "JSE All Share", "Africa", ".JO", "ZA", "South Africa", "FTSE/JSE_All_Share_Index", "^J203.JO"),
    _cfg("JSE_SMALL_CAP", "JSE Small Cap", "Africa", ".JO", "ZA", "South Africa", "FTSE/JSE_Small_Cap_Index", None),
    _cfg("EGX_30", "EGX 30", "Africa", ".CA", "EG", "Egypt", "EGX_30", "^CASE30"),
    _cfg("EGX_70", "EGX 70", "Africa", ".CA", "EG", "Egypt", "EGX_70", None),
    _cfg("MASI", "MASI", "Africa", ".CS", "MA", "Morocco", "MASI_index", None),
    _cfg("NGX_30", "NGX 30", "Africa", ".LG", "NG", "Nigeria", "Nigerian_Exchange_Group", None,
         notes="Yahoo coverage of Lagos is patchy; expect high invalid ratio."),
    _cfg("NSE_20", "NSE 20 (Kenya)", "Africa", ".NR", "KE", "Kenya", "Nairobi_Securities_Exchange", None,
         notes="Yahoo coverage of Nairobi is patchy; expect high invalid ratio."),
    # MENA / GCC
    _cfg("TASI", "TASI", "MENA", ".SR", "SA", "Saudi Arabia", "Tadawul_All_Share_Index", "^TASI.SR"),
    _cfg("DFMGI", "DFM General Index", "MENA", ".DU", "AE", "United Arab Emirates", "Dubai_Financial_Market", "^DFMGI"),
    _cfg("ADX", "ADX General Index", "MENA", ".AD", "AE", "United Arab Emirates", "Abu_Dhabi_Securities_Exchange", "^ADI"),
    _cfg("QSI", "QSI (Qatar)", "MENA", ".QA", "QA", "Qatar", "Qatar_Exchange", None),
    _cfg("KWSE", "Kuwait Premier Market", "MENA", ".KW", "KW", "Kuwait", "Boursa_Kuwait", None),
    _cfg("BHSE", "Bahrain All Share", "MENA", ".BH", "BH", "Bahrain", "Bahrain_Bourse", None),
    # North America -- Canada (non-SEC)
    _cfg("TSX_60", "S&P/TSX 60", "North America", ".TO", "CA", "Canada", "S%26P/TSX_60", "^TX60"),
    _cfg("TSX_COMPOSITE", "S&P/TSX Composite", "North America", ".TO", "CA", "Canada", "S%26P/TSX_Composite_Index", "^GSPTSE"),
    _cfg("TSX_VENTURE_50", "TSX Venture 50", "North America", ".V", "CA", "Canada", "TSX_Venture_Exchange", None,
         alt_suffixes=(".TO",)),
    # -- Phase 3 (2026-07-07): wholesale exchange populations --
    # Gated behind --include-wholesale; each config carries an adapter spec
    # rather than a Wikipedia URL. pipeline_sample_group="exchange_wholesale"
    # so fundamentals runs can filter this scope in or out.
    _cfg(
        "LSE_ALL", "London Stock Exchange (wholesale)", "Europe", ".L", "GB", "United Kingdom",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="gb", exchanges=("LSE", "AIM")),
        is_wholesale=True,
        notes="Wholesale UK equities via Yahoo screener. Weekly cadence.",
    ),
    _cfg(
        "XETRA_ALL", "Xetra / Deutsche Börse (wholesale)", "Europe", ".DE", "DE", "Germany",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="de", exchanges=("GER",), max_rows=3000),
        is_wholesale=True,
        screener_domicile_only=True,
        notes="Wholesale German equities (Xetra, code GER → .DE) via Yahoo screener. Covers "
              "DAX / MDAX / TecDAX / SDAX / Scale / General Standard in one pull. "
              "screener_domicile_only drops the many foreign names cross-listed on Xetra.",
    ),
    _cfg(
        "FRANKFURT_FREIVERKEHR", "Frankfurt Freiverkehr / Open Market (wholesale)", "Europe",
        ".F", "DE", "Germany",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="de", exchanges=("FRA",), max_rows=12000),
        is_wholesale=True,
        screener_domicile_only=True,
        dedupe_by_isin=True,
        notes="German companies on the Frankfurt Open Market (Freiverkehr, code FRA → .F) not "
              "already held via Xetra. screener_domicile_only drops the (majority) foreign names "
              "cross-listed on Frankfurt; dedupe_by_isin drops .F secondary counters of Xetra .DE "
              "lines, leaving genuinely Frankfurt-only German lower-segment firms.",
    ),
    _cfg(
        "TSX_ALL", "Toronto Stock Exchange (wholesale)", "North America", ".TO", "CA", "Canada",
        None, None,
        alt_suffixes=(".V",),
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="ca", exchanges=("TOR", "VAN")),
        is_wholesale=True,
    ),
    _cfg(
        "JSE_ALL_WHOLESALE", "Johannesburg (wholesale)", "Africa", ".JO", "ZA", "South Africa",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="za", exchanges=("JNB",)),
        is_wholesale=True,
    ),
    _cfg(
        "KRX_ALL", "KRX (wholesale)", "Asia", ".KS", "KR", "South Korea",
        None, None,
        alt_suffixes=(".KQ",),
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(region="kr", exchanges=("KSC", "KOE")),
        is_wholesale=True,
    ),
    _cfg(
        "HKEX_ALL", "Hong Kong Exchange (wholesale)", "Asia", ".HK", "HK", "Hong Kong",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        screener_spec=YahooScreenerSpec(
            region="hk", max_rows=20000, symbol_regex=r"^\d{4}\.HK$"),
        is_wholesale=True,
        # No screener_domicile_only: HKEX is overwhelmingly Greater-China operating
        # companies (H-shares / red chips whose Yahoo domicile reads "China" or a
        # Cayman/Bermuda incorporation), and an HKEX listing is a "Hong Kong stock"
        # by convention — same treatment as the Hang Seng index configs. A strict
        # HK-only domicile filter would wrongly drop most of the exchange.
        # symbol_regex keeps the 4-digit company codes (main board + GEM) and drops
        # the ~6k+ 5-digit derivative warrants / CBBCs / duplicate RMB counters that
        # Yahoo's region=hk screen mislabels as equities — i.e. every .HK company.
        notes="Wholesale Hong Kong companies (HKEX → .HK) via Yahoo screener; 4-digit "
              "codes only (excludes warrants/CBBCs/RMB counters). Hang Seng + mainboard + GEM.",
    ),
    _cfg(
        "SGX_ALL", "Singapore Exchange (wholesale)", "Asia", ".SI", "SG", "Singapore",
        None, None,
        pipeline_sample_group="exchange_wholesale",
        http_json_spec=HttpJsonSourceSpec(
            url="https://api.sgx.com/securities/v1.1?params=nc,cn,s",
            rows_path=("data", "prices"),
            ticker_key="nc",
            name_key="cn",
        ),
        is_wholesale=True,
    ),
    _cfg(
        "BSE_ALL", "BSE India (wholesale)", "Asia", ".BO", "IN", "India",
        None, None,
        alt_suffixes=(".NS",),
        pipeline_sample_group="exchange_wholesale",
        http_json_spec=HttpJsonSourceSpec(
            url="https://api.bseindia.com/BseIndiaAPI/api/ListofScripCode/w",
            rows_path=("Table",),
            ticker_key="scrip_cd",
            name_key="scrip_name",
        ),
        is_wholesale=True,
        notes="BSE scrip codes; some numeric codes may not have Yahoo coverage.",
    ),
)

INDEX_BY_CODE = {cfg.index_code: cfg for cfg in INDEX_CONFIGS}

TICKER_COLUMN_TOKENS = ("ticker", "symbol", "code", "epic", "ric")
NAME_COLUMN_TOKENS = ("company", "name", "constituent", "security")
PROFILE_METRICS = (
    "marketCap",
    "enterpriseValue",
    "trailingPE",
    "forwardPE",
    "priceToBook",
    "beta",
    "dividendYield",
    "profitMargins",
    "grossMargins",
    "operatingMargins",
    "returnOnEquity",
    "returnOnAssets",
    "revenueGrowth",
    "earningsGrowth",
    "sharesOutstanding",
    "floatShares",
    "bookValue",
    "trailingEps",
    "forwardEps",
)
YAHOO_TO_GICS_SECTOR: dict[str, tuple[str, str]] = {
    # Yahoo returns 11 sector strings; these map 1:1 onto GICS 2-digit codes.
    "Basic Materials":       ("15", "Materials"),
    "Communication Services":("50", "Communication Services"),
    "Consumer Cyclical":     ("25", "Consumer Discretionary"),
    "Consumer Defensive":    ("30", "Consumer Staples"),
    "Energy":                ("10", "Energy"),
    "Financial Services":    ("40", "Financials"),
    "Healthcare":            ("35", "Health Care"),
    "Industrials":           ("20", "Industrials"),
    "Real Estate":           ("60", "Real Estate"),
    "Technology":            ("45", "Information Technology"),
    "Utilities":             ("55", "Utilities"),
}


def yahoo_sector_to_gics(sector: str | None) -> tuple[str | None, str | None]:
    """Map a Yahoo sector string to (gics_sector_code, gics_sector_name).

    Returns (None, None) if the sector is missing or unrecognized.
    """
    if not sector:
        return (None, None)
    hit = YAHOO_TO_GICS_SECTOR.get(sector.strip())
    if hit is None:
        return (None, None)
    return hit


# GICS sector-code → name (GICS 2023 vintage, matching dim_company_us/jp naming).
GICS_SECTOR_NAMES: dict[str, str] = {
    "10": "Energy", "15": "Materials", "20": "Industrials",
    "25": "Consumer Discretionary", "30": "Consumer Staples", "35": "Health Care",
    "40": "Financials", "45": "Information Technology", "50": "Communication Services",
    "55": "Utilities", "60": "Real Estate",
}

# GICS industry-group code → name (GICS 2023 vintage).
GICS_INDUSTRY_GROUP_NAMES: dict[str, str] = {
    "1010": "Energy",
    "1510": "Materials",
    "2010": "Capital Goods",
    "2020": "Commercial & Professional Services",
    "2030": "Transportation",
    "2510": "Automobiles & Components",
    "2520": "Consumer Durables & Apparel",
    "2530": "Consumer Services",
    "2550": "Consumer Discretionary Distribution & Retail",
    "3010": "Consumer Staples Distribution & Retail",
    "3020": "Food, Beverage & Tobacco",
    "3030": "Household & Personal Products",
    "3510": "Health Care Equipment & Services",
    "3520": "Pharmaceuticals, Biotechnology & Life Sciences",
    "4010": "Banks",
    "4020": "Financial Services",
    "4030": "Insurance",
    "4510": "Software & Services",
    "4520": "Technology Hardware & Equipment",
    "4530": "Semiconductors & Semiconductor Equipment",
    "5010": "Telecommunication Services",
    "5020": "Media & Entertainment",
    "5510": "Utilities",
    "6010": "Equity Real Estate Investment Trusts (REITs)",
    "6020": "Real Estate Management & Development",
}

# Yahoo `industry` string → GICS industry-group code. Every industry is placed in a
# group whose 2-digit sector prefix matches the GICS sector its Yahoo *sector* already
# maps to (see YAHOO_TO_GICS_SECTOR), so the backfill never contradicts the existing
# gics_sector_code and the warehouse's sector==left(group,2) invariant is preserved.
# "Education & Training Services" is deliberately omitted — Yahoo files it under
# Consumer Defensive (staples), which has no sensible in-sector group, so it keeps its
# sector and is left without a group rather than mis-classified.
YAHOO_INDUSTRY_TO_GICS_GROUP: dict[str, str] = {
    # -- Energy (10) --
    "Oil & Gas Drilling": "1010", "Oil & Gas E&P": "1010",
    "Oil & Gas Equipment & Services": "1010", "Oil & Gas Integrated": "1010",
    "Oil & Gas Midstream": "1010", "Oil & Gas Refining & Marketing": "1010",
    "Thermal Coal": "1010", "Uranium": "1010",
    # -- Materials (15) --
    "Agricultural Inputs": "1510", "Aluminum": "1510", "Building Materials": "1510",
    "Chemicals": "1510", "Coking Coal": "1510", "Copper": "1510", "Gold": "1510",
    "Lumber & Wood Production": "1510", "Other Industrial Metals & Mining": "1510",
    "Other Precious Metals & Mining": "1510", "Paper & Paper Products": "1510",
    "Silver": "1510", "Specialty Chemicals": "1510", "Steel": "1510",
    # -- Industrials (20) --
    "Aerospace & Defense": "2010", "Building Products & Equipment": "2010",
    "Conglomerates": "2010", "Electrical Equipment & Parts": "2010",
    "Engineering & Construction": "2010", "Farm & Heavy Construction Machinery": "2010",
    "Industrial Distribution": "2010", "Infrastructure Operations": "2010",
    "Metal Fabrication": "2010", "Specialty Industrial Machinery": "2010",
    "Tools & Accessories": "2010",
    "Business Equipment & Supplies": "2020", "Consulting Services": "2020",
    "Pollution & Treatment Controls": "2020", "Rental & Leasing Services": "2020",
    "Security & Protection Services": "2020", "Specialty Business Services": "2020",
    "Staffing & Employment Services": "2020", "Waste Management": "2020",
    "Airlines": "2030", "Airports & Air Services": "2030",
    "Integrated Freight & Logistics": "2030", "Marine Shipping": "2030",
    "Railroads": "2030", "Trucking": "2030",
    # -- Consumer Discretionary (25) --
    "Auto Manufacturers": "2510", "Auto Parts": "2510",
    "Apparel Manufacturing": "2520", "Footwear & Accessories": "2520",
    "Furnishings, Fixtures & Appliances": "2520", "Luxury Goods": "2520",
    "Packaging & Containers": "2520", "Recreational Vehicles": "2520",
    "Residential Construction": "2520", "Textile Manufacturing": "2520",
    "Gambling": "2530", "Leisure": "2530", "Lodging": "2530",
    "Personal Services": "2530", "Resorts & Casinos": "2530",
    "Restaurants": "2530", "Travel Services": "2530",
    "Apparel Retail": "2550", "Auto & Truck Dealerships": "2550",
    "Department Stores": "2550", "Home Improvement Retail": "2550",
    "Internet Retail": "2550", "Specialty Retail": "2550",
    # -- Consumer Staples (30) --
    "Discount Stores": "3010", "Food Distribution": "3010", "Grocery Stores": "3010",
    "Beverages - Brewers": "3020", "Beverages - Non-Alcoholic": "3020",
    "Beverages - Wineries & Distilleries": "3020", "Confectioners": "3020",
    "Farm Products": "3020", "Packaged Foods": "3020", "Tobacco": "3020",
    "Household & Personal Products": "3030",
    # -- Health Care (35) --
    "Diagnostics & Research": "3510", "Health Information Services": "3510",
    "Medical Care Facilities": "3510", "Medical Devices": "3510",
    "Medical Distribution": "3510", "Medical Instruments & Supplies": "3510",
    "Pharmaceutical Retailers": "3510",
    "Biotechnology": "3520", "Drug Manufacturers - General": "3520",
    "Drug Manufacturers - Specialty & Generic": "3520",
    # -- Financials (40) --
    "Banks - Diversified": "4010", "Banks - Regional": "4010",
    "Asset Management": "4020", "Capital Markets": "4020", "Credit Services": "4020",
    "Financial Conglomerates": "4020", "Financial Data & Stock Exchanges": "4020",
    "Mortgage Finance": "4020", "Shell Companies": "4020",
    "Insurance - Diversified": "4030", "Insurance - Life": "4030",
    "Insurance - Property & Casualty": "4030", "Insurance - Reinsurance": "4030",
    "Insurance - Specialty": "4030", "Insurance Brokers": "4030",
    # -- Information Technology (45) --
    "Information Technology Services": "4510", "Software - Application": "4510",
    "Software - Infrastructure": "4510",
    "Communication Equipment": "4520", "Computer Hardware": "4520",
    "Consumer Electronics": "4520", "Electronic Components": "4520",
    "Electronics & Computer Distribution": "4520",
    "Scientific & Technical Instruments": "4520", "Solar": "4520",
    "Semiconductor Equipment & Materials": "4530", "Semiconductors": "4530",
    # -- Communication Services (50) --
    "Telecom Services": "5010",
    "Advertising Agencies": "5020", "Broadcasting": "5020",
    "Electronic Gaming & Multimedia": "5020", "Entertainment": "5020",
    "Internet Content & Information": "5020", "Publishing": "5020",
    # -- Utilities (55) --
    "Utilities - Diversified": "5510", "Utilities - Independent Power Producers": "5510",
    "Utilities - Regulated Electric": "5510", "Utilities - Regulated Gas": "5510",
    "Utilities - Regulated Water": "5510", "Utilities - Renewable": "5510",
    # -- Real Estate (60) --
    "REIT - Diversified": "6010", "REIT - Healthcare Facilities": "6010",
    "REIT - Hotel & Motel": "6010", "REIT - Industrial": "6010",
    "REIT - Office": "6010", "REIT - Residential": "6010",
    "REIT - Retail": "6010", "REIT - Specialty": "6010",
    "Real Estate - Development": "6020", "Real Estate - Diversified": "6020",
    "Real Estate Services": "6020",
}


def yahoo_industry_to_gics_group(industry: str | None) -> tuple[str | None, str | None]:
    """Map a Yahoo industry string to (gics_industry_group_code, group_name).

    Returns (None, None) when the industry is missing or has no in-sector group.
    """
    if not industry:
        return (None, None)
    code = YAHOO_INDUSTRY_TO_GICS_GROUP.get(industry.strip())
    if code is None:
        return (None, None)
    return (code, GICS_INDUSTRY_GROUP_NAMES.get(code))


def backfill_intl_gics(*, dry_run: bool = False) -> dict[str, Any]:
    """Fill GICS industry-group (and any missing sector) codes on dim_company_intl
    from the Yahoo `industry`/`sector` strings already stored on each row.

    Pure warehouse transformation — no Yahoo network calls. Idempotent: only touches
    rows whose gics_industry_group_code is still NULL. The derived sector is written
    with COALESCE so an existing gics_sector_code is never overwritten (and the two
    always agree by construction). Returns per-run counts plus any unmapped industries.
    """
    stats: dict[str, Any] = {
        "industries_seen": 0,
        "industries_mapped": 0,
        "rows_group_set": 0,
        "unmapped": [],
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT btrim(industry) AS industry, COUNT(*) AS n
            FROM   dim_company_intl
            WHERE  primary_ticker IS NOT NULL
              AND  COALESCE(include_in_pipeline, true)
              AND  industry IS NOT NULL AND btrim(industry) <> ''
              AND  gics_industry_group_code IS NULL
            GROUP  BY 1
            ORDER  BY n DESC
            """
        )
        pending = cur.fetchall()
        for industry, n in pending:
            stats["industries_seen"] += 1
            group_code, group_name = yahoo_industry_to_gics_group(industry)
            if not group_code:
                stats["unmapped"].append((industry, int(n)))
                continue
            stats["industries_mapped"] += 1
            sector_code = group_code[:2]
            sector_name = GICS_SECTOR_NAMES.get(sector_code)
            if dry_run:
                stats["rows_group_set"] += int(n)
                continue
            cur.execute(
                """
                UPDATE dim_company_intl
                   SET gics_industry_group_code = %s,
                       gics_industry_group_name = %s,
                       gics_sector_code = COALESCE(gics_sector_code, %s),
                       gics_sector_name = COALESCE(gics_sector_name, %s),
                       updated_at = now()
                 WHERE primary_ticker IS NOT NULL
                   AND COALESCE(include_in_pipeline, true)
                   AND btrim(industry) = %s
                   AND gics_industry_group_code IS NULL
                """,
                (group_code, group_name, sector_code, sector_name, industry),
            )
            stats["rows_group_set"] += cur.rowcount
    return stats


STATEMENT_ATTRS: dict[str, dict[str, tuple[str, ...]]] = {
    "income_statement": {
        "annual": ("financials", "income_stmt", "income_statement"),
        "quarterly": ("quarterly_financials", "quarterly_income_stmt"),
    },
    "balance_sheet": {
        "annual": ("balance_sheet", "balancesheet"),
        "quarterly": ("quarterly_balance_sheet", "quarterly_balancesheet"),
    },
    "cash_flow": {
        "annual": ("cashflow", "cash_flow"),
        "quarterly": ("quarterly_cashflow", "quarterly_cash_flow"),
    },
}


def intl_company_id(primary_ticker: str) -> str:
    ticker = normalize_existing_yahoo_ticker(primary_ticker)
    if not ticker:
        raise ValueError("primary_ticker is required")
    return f"{INTL_ID_PREFIX}{ticker}"


def normalize_existing_yahoo_ticker(value: Any) -> str | None:
    text = _clean_ticker_text(value)
    if not text:
        return None
    if _is_japan_ticker(text):
        return None
    return text


def normalize_yahoo_ticker(value: Any, default_suffix: str | None = None) -> str | None:
    text = _clean_ticker_text(value)
    if not text:
        return None
    if "." in text and re.search(r"\.[A-Z]{1,4}$", text):
        if _is_japan_ticker(text):
            return None
        # Zero-pad already-suffixed HK numeric codes to the 4-digit convention
        # (e.g. "700.HK" -> "0700.HK") so screener/wikipedia forms dedupe against
        # existing rows. Genuine 5-digit codes (warrants/CBBCs/RMB counters) don't
        # match \d{1,4} and pass through unchanged for downstream filtering.
        hk = re.match(r"^(\d{1,4})\.HK$", text)
        if hk:
            return f"{int(hk.group(1)):04d}.HK"
        return text
    suffix = (default_suffix or "").strip().upper()
    if not suffix:
        return None
    if suffix in JAPAN_SUFFIXES:
        return None
    base = text
    if suffix == ".HK" and base.isdigit():
        base = base.zfill(4)
    return f"{base}{suffix}"


def resolve_index_configs(
    index_codes: Iterable[str] | None = None,
    *,
    include_wholesale: bool = False,
) -> list[YahooIndexConfig]:
    """Resolve index codes to configs.

    When ``index_codes`` is None (i.e. "all"), wholesale-exchange configs
    are excluded unless ``include_wholesale=True``. When the caller passes
    explicit codes, wholesale configs are returned as-is (explicit
    opt-in wins).
    """
    if not index_codes:
        if include_wholesale:
            return list(INDEX_CONFIGS)
        return [cfg for cfg in INDEX_CONFIGS if not cfg.is_wholesale]
    out: list[YahooIndexConfig] = []
    for raw in index_codes:
        code = str(raw or "").strip().upper().replace("-", "_")
        if code in JAPAN_INDEX_CODES:
            raise ValueError(f"Japan index {code} is out of scope for yahoo_global")
        try:
            out.append(INDEX_BY_CODE[code])
        except KeyError as exc:
            known = ", ".join(sorted(INDEX_BY_CODE))
            raise ValueError(f"Unknown Yahoo global index {raw!r}. Known: {known}") from exc
    return out


def discover_wikipedia_index(
    config: YahooIndexConfig,
    *,
    read_html: Callable[[str], list[Any]] | None = None,
) -> list[DiscoveredTicker]:
    if not config.wikipedia_url:
        return []
    if read_html is None:
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("pandas is required for Wikipedia discovery") from exc
        read_html = lambda url: pd.read_html(StringIO(_fetch_wikipedia_html(url)))

    tables = read_html(config.wikipedia_url)
    discovered: list[DiscoveredTicker] = []
    seen: set[str] = set()
    for table_idx, table in enumerate(tables):
        columns = [_column_name(col) for col in getattr(table, "columns", [])]
        ticker_cols = _ticker_columns(columns)
        if not ticker_cols:
            continue
        name_col = _name_column(columns, ticker_cols)
        for row_idx, row in table.iterrows():
            for ticker_col in ticker_cols:
                raw_ticker = row.iloc[ticker_col] if hasattr(row, "iloc") else row[ticker_col]
                primary_ticker = normalize_yahoo_ticker(raw_ticker, config.default_suffix)
                if not primary_ticker or primary_ticker in seen:
                    continue
                seen.add(primary_ticker)
                name = None
                if name_col is not None:
                    raw_name = row.iloc[name_col] if hasattr(row, "iloc") else row[name_col]
                    name = _clean_name(raw_name)
                raw_payload = _row_payload(row, columns)
                discovered.append(
                    DiscoveredTicker(
                        index_code=config.index_code,
                        raw_ticker=str(raw_ticker),
                        primary_ticker=primary_ticker,
                        name=name,
                        country_code=config.country_code,
                        exchange_suffix=_suffix(primary_ticker),
                        source_name="wikipedia",
                        source_url=config.wikipedia_url,
                        source_rank=(table_idx * 10000) + int(row_idx) + 1,
                        raw_payload=raw_payload,
                    )
                )
    return discovered


def discover_fallback_index(config: YahooIndexConfig, fallback_dir: Path | str | None = None) -> list[DiscoveredTicker]:
    path = _fallback_path(config, fallback_dir)
    if path is None:
        return []
    out: list[DiscoveredTicker] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader, start=1):
            raw = row.get("primary_ticker") or row.get("yahoo_ticker") or row.get("ticker") or row.get("symbol")
            primary_ticker = normalize_yahoo_ticker(raw, config.default_suffix)
            if not primary_ticker or primary_ticker in seen:
                continue
            seen.add(primary_ticker)
            out.append(
                DiscoveredTicker(
                    index_code=config.index_code,
                    raw_ticker=str(raw or ""),
                    primary_ticker=primary_ticker,
                    name=_clean_name(row.get("name") or row.get("company") or row.get("constituent")),
                    country_code=row.get("country_code") or config.country_code,
                    exchange_suffix=_suffix(primary_ticker),
                    source_name="fallback_csv",
                    source_url=str(path),
                    source_rank=idx,
                    raw_payload={k: v for k, v in row.items() if v not in (None, "")},
                )
            )
    return out


class DiscoverySource:
    """Base class for index-constituent discovery adapters.

    Subclasses set ``name`` and implement ``discover``. ``discover_index``
    walks all configured sources in order, unions the results, and dedupes
    by ``primary_ticker`` (first-seen wins, so declaration order matters).
    """

    name: str = "unknown"

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:  # pragma: no cover - abstract
        raise NotImplementedError


class WikipediaSource(DiscoverySource):
    name = "wikipedia"

    def __init__(self, read_html: Callable[[str], list[Any]] | None = None) -> None:
        self._read_html = read_html

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:
        return discover_wikipedia_index(config, read_html=self._read_html)


class FallbackCsvSource(DiscoverySource):
    name = "fallback_csv"

    def __init__(self, fallback_dir: Path | str | None = None) -> None:
        self._fallback_dir = fallback_dir

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:
        return discover_fallback_index(config, fallback_dir=self._fallback_dir)


class StooqSource(DiscoverySource):
    """Scaffold for pulling index constituents from stooq.com CSV endpoints.

    Requires ``config.stooq_code``. The Stooq endpoint returns a CSV whose
    first non-header column is the constituent ticker. Ticker normalization
    still runs through ``normalize_yahoo_ticker`` so Yahoo suffixes are
    applied consistently.
    """

    name = "stooq"
    URL_TEMPLATE = "https://stooq.com/q/i/?s={code}&f=sd2t2ohlcv&h&e=csv"

    def __init__(self, fetch: Callable[[str], str] | None = None) -> None:
        self._fetch = fetch or _fetch_text

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:
        code = (config.stooq_code or "").strip()
        if not code:
            return []
        url = self.URL_TEMPLATE.format(code=code)
        try:
            body = self._fetch(url)
        except Exception:
            return []
        rows: list[DiscoveredTicker] = []
        seen: set[str] = set()
        reader = csv.reader(StringIO(body))
        header: list[str] | None = None
        for idx, raw in enumerate(reader, start=1):
            if not raw:
                continue
            if header is None:
                header = [c.strip().lower() for c in raw]
                continue
            row = dict(zip(header, raw))
            raw_ticker = row.get("symbol") or row.get("ticker") or row.get("code")
            primary_ticker = normalize_yahoo_ticker(raw_ticker, config.default_suffix)
            if not primary_ticker or primary_ticker in seen:
                continue
            seen.add(primary_ticker)
            rows.append(
                DiscoveredTicker(
                    index_code=config.index_code,
                    raw_ticker=str(raw_ticker or ""),
                    primary_ticker=primary_ticker,
                    name=row.get("name") or None,
                    country_code=config.country_code,
                    exchange_suffix=_suffix(primary_ticker),
                    source_name=self.name,
                    source_url=url,
                    source_rank=idx,
                    raw_payload={k: v for k, v in row.items() if v not in (None, "")},
                )
            )
        return rows


class YahooScreenerSource(DiscoverySource):
    """Walks Yahoo Finance's public screener via yfinance's built-in helper.

    yfinance handles crumb auth for us. Paginates ``PAGE_SIZE`` rows/page up
    to ``config.screener_spec.max_rows``. Filters by ``region`` (and optional
    ``exchanges``) from the spec.
    """

    name = "yahoo_screener"
    PAGE_SIZE = 250

    def __init__(self, screen: Callable[..., Any] | None = None) -> None:
        self._screen = screen  # tests inject a mock; production loads yfinance lazily

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:
        spec = config.screener_spec
        if spec is None:
            return []
        screen_fn, query_cls = self._resolve_screener()
        if screen_fn is None or query_cls is None:
            return []
        query = self._build_query(spec, query_cls)
        symbol_re = re.compile(spec.symbol_regex) if spec.symbol_regex else None
        # Use the shared curl_cffi Chrome-impersonated session for screener pages too
        # (production only; tests inject a bare stub). Without it Yahoo rate-limits the
        # deep pagination and silently truncates the result set around offset ~1000.
        session = _yahoo_session() if self._screen is None else None
        rows: list[DiscoveredTicker] = []
        seen: set[str] = set()
        offset = 0
        max_rows = max(1, int(spec.max_rows))
        while offset < max_rows:
            result = None
            for attempt in range(4):
                try:
                    result = (
                        screen_fn(query=query, size=self.PAGE_SIZE, offset=offset, session=session)
                        if session is not None
                        else screen_fn(query=query, size=self.PAGE_SIZE, offset=offset)
                    )
                    break
                except Exception:
                    if attempt >= 3:
                        result = None
                    else:
                        time.sleep(0.75 * (2 ** attempt))
            quotes = result.get("quotes") if isinstance(result, dict) else None
            if not quotes:
                break
            for record in quotes:
                if not isinstance(record, dict):
                    continue
                symbol = record.get("symbol") or record.get("Symbol")
                if not symbol:
                    continue
                normalized = normalize_yahoo_ticker(symbol, config.default_suffix)
                if not normalized or normalized in seen:
                    continue
                if symbol_re is not None and not symbol_re.match(normalized):
                    continue
                seen.add(normalized)
                rows.append(
                    DiscoveredTicker(
                        index_code=config.index_code,
                        raw_ticker=str(symbol),
                        primary_ticker=normalized,
                        name=(record.get("longName")
                              or record.get("shortName")
                              or record.get("displayName")),
                        country_code=config.country_code,
                        exchange_suffix=_suffix(normalized),
                        source_name=self.name,
                        source_url=f"yfinance.screen(region={spec.region})",
                        source_rank=offset + len(rows) + 1,
                        raw_payload={
                            "symbol": symbol,
                            "longName": record.get("longName"),
                            "shortName": record.get("shortName"),
                            "exchange": record.get("exchange"),
                            "marketCap": record.get("marketCap"),
                        },
                    )
                )
            if len(quotes) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE
        return rows

    def _resolve_screener(self) -> tuple[Callable[..., Any] | None, Any]:
        if self._screen is not None:
            return self._screen, _StubEquityQuery
        try:
            import yfinance as yf
        except ImportError:
            return None, None
        return getattr(yf, "screen", None), getattr(yf, "EquityQuery", None)

    def _build_query(self, spec: YahooScreenerSpec, query_cls: Any) -> Any:
        region_q = query_cls("eq", ["region", spec.region.lower()])
        if not spec.exchanges:
            return region_q
        exchange_operands = [query_cls("eq", ["exchange", ex]) for ex in spec.exchanges]
        exchange_q = exchange_operands[0] if len(exchange_operands) == 1 else query_cls("or", exchange_operands)
        return query_cls("and", [region_q, exchange_q])


class _StubEquityQuery:
    """Test-only stand-in for yfinance.EquityQuery so injected screens can inspect arguments."""

    def __init__(self, operator: str, operands: list[Any]) -> None:
        self.operator = operator
        self.operands = operands


class HttpJsonSource(DiscoverySource):
    """Generic HTTP-JSON adapter driven by ``config.http_json_spec``."""

    name = "http_json"

    def __init__(self, fetch: Callable[[str], str] | None = None) -> None:
        self._fetch = fetch or _fetch_text

    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]:
        spec = config.http_json_spec
        if spec is None:
            return []
        try:
            raw = self._fetch(spec.url)
        except Exception:
            return []
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return []
        rows_data = _walk_json_path(payload, spec.rows_path)
        if not isinstance(rows_data, list):
            return []
        rows: list[DiscoveredTicker] = []
        seen: set[str] = set()
        for idx, record in enumerate(rows_data, start=1):
            if not isinstance(record, dict):
                continue
            raw_ticker = record.get(spec.ticker_key)
            if raw_ticker is None:
                continue
            normalized = normalize_yahoo_ticker(raw_ticker, config.default_suffix)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            rows.append(
                DiscoveredTicker(
                    index_code=config.index_code,
                    raw_ticker=str(raw_ticker),
                    primary_ticker=normalized,
                    name=str(record.get(spec.name_key)) if spec.name_key and record.get(spec.name_key) else None,
                    country_code=config.country_code,
                    exchange_suffix=_suffix(normalized),
                    source_name=self.name,
                    source_url=spec.url,
                    source_rank=idx,
                    raw_payload={k: _jsonable(v) for k, v in record.items() if v not in (None, "")},
                )
            )
        return rows


def _screener_page_quotes(payload: Any) -> list[dict[str, Any]]:
    finance = payload.get("finance") if isinstance(payload, dict) else None
    if not isinstance(finance, dict):
        return []
    result = finance.get("result")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        quotes = result[0].get("quotes")
        if isinstance(quotes, list):
            return [q for q in quotes if isinstance(q, dict)]
    quotes = finance.get("quotes")
    return [q for q in quotes if isinstance(q, dict)] if isinstance(quotes, list) else []


def _walk_json_path(payload: Any, path: tuple[str, ...]) -> Any:
    cursor: Any = payload
    for step in path:
        if isinstance(cursor, dict):
            cursor = cursor.get(step)
        else:
            return None
        if cursor is None:
            return None
    return cursor


def _post_json(url: str, body: bytes, *, timeout: float = 30.0) -> str:
    request = Request(
        url,
        data=body,
        headers={
            "User-Agent": "AI_Analyst Yahoo global research pipeline (personal use; contact: local)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def build_default_sources(
    *,
    fallback_dir: Path | str | None = None,
    use_wikipedia: bool = True,
    use_stooq: bool = True,
    use_screener: bool = True,
    use_http_json: bool = True,
    read_html: Callable[[str], list[Any]] | None = None,
) -> list[DiscoverySource]:
    sources: list[DiscoverySource] = []
    if use_wikipedia:
        sources.append(WikipediaSource(read_html=read_html))
    if use_stooq:
        sources.append(StooqSource())
    if use_screener:
        sources.append(YahooScreenerSource())
    if use_http_json:
        sources.append(HttpJsonSource())
    sources.append(FallbackCsvSource(fallback_dir=fallback_dir))
    return sources


def discover_index(
    config: YahooIndexConfig,
    *,
    fallback_dir: Path | str | None = None,
    use_wikipedia: bool = True,
    read_html: Callable[[str], list[Any]] | None = None,
    sources: list[DiscoverySource] | None = None,
) -> list[DiscoveredTicker]:
    """Discover constituents by walking configured sources and deduping.

    Preserves the legacy contract: if only Wikipedia is enabled and returns
    rows, those rows are used; otherwise the fallback CSV supplements them.
    New callers should pass an explicit ``sources`` list.
    """
    if sources is None:
        sources = build_default_sources(
            fallback_dir=fallback_dir,
            use_wikipedia=use_wikipedia,
            read_html=read_html,
        )
    seen: set[str] = set()
    out: list[DiscoveredTicker] = []
    for source in sources:
        try:
            rows = source.discover(config)
        except Exception:
            rows = []
        for row in rows:
            if row.primary_ticker in seen:
                continue
            seen.add(row.primary_ticker)
            out.append(row)
    return out


def _fetch_text(url: str, *, timeout: float = 30.0) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "AI_Analyst Yahoo global research pipeline (personal use; contact: local)",
            "Accept": "text/csv,text/plain,application/json,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _fetch_wikipedia_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "AI_Analyst Yahoo global research pipeline (personal use; contact: local)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


class YahooAuthError(RuntimeError):
    """Raised when Yahoo returns 401/429 for a profile/info call.

    Distinct from missing-data so ``_with_backoff`` can retry with a fresh
    session while genuine 404s / empty payloads pass through as returned {}.
    """


_AUTH_ERROR_MARKERS = ("invalid crumb", "unauthorized", "too many requests", "rate limit")


def _looks_like_auth_error(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


_YF_SESSION_LOCK = threading.Lock()
_YF_SESSION: Any = None


def _yahoo_session(*, force_new: bool = False) -> Any:
    """Return a shared curl_cffi Chrome-impersonated session, or None if unavailable.

    yfinance accepts ``session=`` on Ticker, and curl_cffi's browser
    impersonation makes Yahoo's crumb-auth wall pass reliably. Falls back to
    None (yfinance's built-in session) if curl_cffi isn't installed.
    """
    global _YF_SESSION
    with _YF_SESSION_LOCK:
        if _YF_SESSION is not None and not force_new:
            return _YF_SESSION
        try:
            from curl_cffi import requests as _cc_requests  # type: ignore
        except ImportError:
            _YF_SESSION = None
            return None
        _YF_SESSION = _cc_requests.Session(impersonate="chrome")
        return _YF_SESSION


class _RateLimiter:
    """Simple thread-safe token bucket. ``rate`` = requests per second."""

    def __init__(self, rate: float) -> None:
        self._rate = max(0.0, float(rate))
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def acquire(self) -> None:
        if self._rate <= 0:
            return
        interval = 1.0 / self._rate
        with self._lock:
            now = time.monotonic()
            wait = self._next_ok - now
            if wait > 0:
                time.sleep(wait)
                self._next_ok = now + wait + interval
            else:
                self._next_ok = now + interval


_YF_RATE_LIMITER = _RateLimiter(rate=0.0)


def set_yahoo_rate_limit(rate_per_second: float) -> None:
    """Reset the global Yahoo Finance rate limiter (thread-safe)."""
    global _YF_RATE_LIMITER
    _YF_RATE_LIMITER = _RateLimiter(rate=rate_per_second)


def fetch_yahoo_profile(symbol: str, *, yf_module: Any | None = None) -> dict[str, Any]:
    """Fetch the yfinance profile for ``symbol``.

    Raises ``YahooAuthError`` on crumb/rate-limit failures so callers wrapped
    in ``_with_backoff`` can retry with a fresh session. Genuine empty
    payloads (delisted, no coverage) return ``{}`` without raising.
    """
    yf = yf_module or _import_yfinance()
    session = _yahoo_session()
    _YF_RATE_LIMITER.acquire()
    ticker_kwargs: dict[str, Any] = {}
    if session is not None:
        ticker_kwargs["session"] = session
    try:
        ticker = yf.Ticker(symbol, **ticker_kwargs)
    except TypeError:
        ticker = yf.Ticker(symbol)

    info: dict[str, Any] = {}
    info_exc_message: str | None = None
    try:
        raw_info = getattr(ticker, "info", None)
        info = dict(raw_info or {})
    except Exception as exc:
        info_exc_message = f"{type(exc).__name__}: {exc}"
        info = {}

    if not info and hasattr(ticker, "get_info"):
        try:
            info = dict(ticker.get_info() or {})
        except Exception as exc:
            info_exc_message = info_exc_message or f"{type(exc).__name__}: {exc}"

    if not info and info_exc_message and _looks_like_auth_error(info_exc_message):
        _yahoo_session(force_new=True)
        raise YahooAuthError(info_exc_message)

    if not info and info_exc_message:
        # Preserve prior diagnostic behavior for downstream logging.
        info = {"_info_error": info_exc_message}

    try:
        fast_info = getattr(ticker, "fast_info", None)
        if fast_info:
            fast_payload = dict(fast_info)
            info["_fast_info"] = _jsonable(fast_payload)
            for key, value in fast_payload.items():
                info.setdefault(key, value)
    except Exception:
        pass
    info.setdefault("symbol", symbol)
    return info


def validate_yahoo_ticker(
    symbol: str,
    *,
    yf_module: Any | None = None,
    attempts: int = 4,
    base_sleep_seconds: float = 0.75,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    profile = _with_backoff(
        lambda: fetch_yahoo_profile(symbol, yf_module=yf_module),
        attempts=attempts,
        base_sleep_seconds=base_sleep_seconds,
        sleeper=sleeper,
    )
    return profile if _profile_is_valid(profile, symbol) else None


def _load_existing_isins() -> dict[str, str]:
    """Map every known ISIN on dim_company_intl to its primary_ticker (upper-cased).

    Used by dedupe_by_isin configs to recognise secondary listings of companies
    already held under another ticker.
    """
    out: dict[str, str] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT upper(isin), primary_ticker FROM dim_company_intl "
            "WHERE isin IS NOT NULL AND btrim(isin) <> ''"
        )
        for isin, ticker in cur.fetchall():
            if isin and ticker:
                out.setdefault(isin, ticker)
    return out


def _load_existing_tickers() -> set[str]:
    """Every primary_ticker already on dim_company_intl — lets a re-run of an
    interrupted wholesale crawl skip companies a prior pass already committed."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT primary_ticker FROM dim_company_intl WHERE primary_ticker IS NOT NULL")
        return {r[0] for r in cur.fetchall() if r[0]}


# Discovery persists accepted companies in batches of this size (rather than only
# at the very end) so an interrupted long crawl keeps the progress it already made.
_DISCOVERY_BATCH = 100


def _flush_discovery_batch(
    config: YahooIndexConfig,
    ctx_run_id: str | None,
    batch: list[DiscoveredTicker],
    batch_profiles: dict[str, dict[str, Any]],
    counts: dict[str, int],
    *,
    dry_run: bool,
) -> None:
    if not batch:
        return
    if dry_run:
        counts["companies"] += len(batch)
        counts["constituents"] += len(batch)
    else:
        assert ctx_run_id is not None
        _stage_discovery_rows(ctx_run_id, batch)
        counts["companies"] += _upsert_companies_from_discovery(config, batch, batch_profiles)
        counts["constituents"] += _upsert_index_constituents(config, batch)
    batch.clear()
    batch_profiles.clear()


def run_discovery(
    *,
    index_codes: list[str] | None = None,
    fallback_dir: Path | str | None = None,
    validate: bool = True,
    use_wikipedia: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_seconds: float = 0.25,
    yf_module: Any | None = None,
    read_html: Callable[[str], list[Any]] | None = None,
    include_wholesale: bool = False,
) -> dict[str, int]:
    configs = resolve_index_configs(index_codes, include_wholesale=include_wholesale)
    counts = {
        "indexes": len(configs),
        "discovered": 0,
        "validated": 0,
        "invalid": 0,
        "domicile_skipped": 0,
        "isin_duplicate_skipped": 0,
        "companies": 0,
        "constituents": 0,
    }

    # ISIN → existing primary_ticker, loaded once when any config dedupes by ISIN.
    # Grows as new companies are accepted so within-run duplicates are caught too.
    existing_isins: dict[str, str] = (
        _load_existing_isins() if any(c.dedupe_by_isin for c in configs) else {}
    )

    # Tickers already committed (populated for wholesale crawls) so re-running an
    # interrupted long crawl skips companies an earlier pass already wrote.
    already_have: set[str] = (
        _load_existing_tickers()
        if (validate and not dry_run and any(c.is_wholesale for c in configs))
        else set()
    )

    def process(ctx_run_id: str | None = None) -> None:
        if not dry_run:
            _upsert_index_configs(configs)
        for config in configs:
            rows = discover_index(config, fallback_dir=fallback_dir, use_wikipedia=use_wikipedia, read_html=read_html)
            if limit is not None:
                rows = rows[: max(limit, 0)]
            counts["discovered"] += len(rows)
            batch: list[DiscoveredTicker] = []
            batch_profiles: dict[str, dict[str, Any]] = {}
            for row in rows:
                if row.primary_ticker in already_have:
                    continue
                profile: dict[str, Any] = {}
                accepted_row = row
                if validate:
                    variants = _ticker_variants(row.primary_ticker, config)
                    for variant in variants:
                        try:
                            candidate = validate_yahoo_ticker(
                                variant,
                                yf_module=yf_module,
                                base_sleep_seconds=max(sleep_seconds, 0.0),
                            ) or {}
                        except Exception:
                            candidate = {}
                        if candidate:
                            profile = candidate
                            if variant != row.primary_ticker:
                                accepted_row = _rewrite_ticker(row, variant)
                            break
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)
                    if not profile:
                        counts["invalid"] += 1
                        continue
                    if config.screener_domicile_only and not _profile_matches_domicile(profile, config):
                        counts["domicile_skipped"] += 1
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)
                        continue
                    if config.dedupe_by_isin:
                        isin = str(profile.get("isin") or "").strip().upper()
                        if isin and existing_isins.get(isin, accepted_row.primary_ticker) != accepted_row.primary_ticker:
                            counts["isin_duplicate_skipped"] += 1
                            if sleep_seconds > 0:
                                time.sleep(sleep_seconds)
                            continue
                        if isin:
                            existing_isins[isin] = accepted_row.primary_ticker
                    counts["validated"] += 1
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                batch_profiles[accepted_row.primary_ticker] = profile
                batch.append(accepted_row)
                already_have.add(accepted_row.primary_ticker)
                if len(batch) >= _DISCOVERY_BATCH:
                    _flush_discovery_batch(config, ctx_run_id, batch, batch_profiles, counts, dry_run=dry_run)
            _flush_discovery_batch(config, ctx_run_id, batch, batch_profiles, counts, dry_run=dry_run)

    if dry_run:
        process(None)
        return counts

    with market_run(SOURCE_DISCOVERY, False, {"indexes": [cfg.index_code for cfg in configs]}) as ctx:
        for config in configs:
            mark_item_running(ctx, SOURCE_DISCOVERY, config.index_code, source_url=config.wikipedia_url)
        try:
            process(str(ctx.run_id))
        except Exception as exc:
            for config in configs:
                mark_item_done(ctx, SOURCE_DISCOVERY, config.index_code, status="failed", error=str(exc)[:1000])
            raise
        for config in configs:
            mark_item_done(ctx, SOURCE_DISCOVERY, config.index_code, rows_out=counts["constituents"])
    return counts


def fetch_yahoo_fundamentals_for_ticker(
    target: CompanyTarget,
    *,
    yf_module: Any | None = None,
    profile_attempts: int = 5,
    profile_base_sleep_seconds: float = 1.0,
    include_quarterly: bool = True,
) -> FundamentalPayload:
    yf = yf_module or _import_yfinance()
    session = _yahoo_session()
    ticker_kwargs: dict[str, Any] = {"session": session} if session is not None else {}
    try:
        ticker = yf.Ticker(target.primary_ticker, **ticker_kwargs)
    except TypeError:
        ticker = yf.Ticker(target.primary_ticker)

    profile = _with_backoff(
        lambda: fetch_yahoo_profile(target.primary_ticker, yf_module=yf),
        attempts=profile_attempts,
        base_sleep_seconds=profile_base_sleep_seconds,
        sleeper=time.sleep,
    )
    currency = profile.get("financialCurrency") or profile.get("currency") or target.currency
    metrics = _profile_metrics(profile, currency)
    statements = _statement_rows(ticker, currency, include_quarterly=include_quarterly)
    return FundamentalPayload(target, profile, metrics, statements)


def run_fundamentals(
    *,
    tickers: list[str] | None = None,
    index_codes: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sleep_seconds: float = 0.5,
    yf_module: Any | None = None,
    max_workers: int = 1,
    refresh_before_days: int | None = None,
    sample_groups: list[str] | None = None,
    country_codes: list[str] | None = None,
    only_missing: bool = False,
    only_missing_quarterly: bool = False,
    rate_per_second: float | None = None,
    include_quarterly: bool = True,
) -> dict[str, int]:
    if rate_per_second is not None:
        set_yahoo_rate_limit(rate_per_second)
    if dry_run and tickers:
        normalized = [normalize_existing_yahoo_ticker(t) for t in tickers]
        normalized = [t for t in normalized if t]
        return {"companies": len(normalized), "metrics": 0, "statement_items": 0, "failed": 0, "skipped": 0}
    targets = _select_company_targets(
        tickers=tickers,
        index_codes=index_codes,
        limit=limit,
        sample_groups=sample_groups,
        country_codes=country_codes,
        only_missing_fundamentals=only_missing,
        only_missing_quarterly=only_missing_quarterly,
    )
    skipped = 0
    if refresh_before_days is not None and refresh_before_days > 0 and targets:
        cutoff = date.today() - timedelta(days=refresh_before_days)
        fresh = _fundamentals_freshness_map([t.intl_company_id for t in targets])
        filtered: list[CompanyTarget] = []
        for target in targets:
            last = fresh.get(target.intl_company_id)
            if last is not None and last >= cutoff:
                skipped += 1
                continue
            filtered.append(target)
        targets = filtered
    counts = {
        "companies": len(targets),
        "metrics": 0,
        "statement_items": 0,
        "failed": 0,
        "skipped": skipped,
    }
    if dry_run:
        return counts
    workers = max(1, int(max_workers))
    with market_run(SOURCE_FUNDAMENTALS, False, {"tickers": [t.primary_ticker for t in targets]}) as ctx:
        for target in targets:
            mark_item_running(ctx, SOURCE_FUNDAMENTALS, target.primary_ticker)
        if workers <= 1:
            iterator: Iterable[tuple[CompanyTarget, FundamentalPayload | None, BaseException | None]] = (
                _fetch_fundamentals_wrapped(target, yf_module, include_quarterly) for target in targets
            )
        else:
            iterator = _parallel_fundamentals(targets, yf_module, workers, include_quarterly)
        for target, payload, exc in iterator:
            if exc is not None or payload is None:
                counts["failed"] += 1
                mark_item_done(
                    ctx,
                    SOURCE_FUNDAMENTALS,
                    target.primary_ticker,
                    status="failed",
                    error=str(exc)[:1000] if exc else "empty payload",
                )
                continue
            try:
                _stage_fundamental_payload(str(ctx.run_id), payload)
                _update_company_profile(payload)
                counts["metrics"] += _upsert_profile_metrics(payload)
                counts["statement_items"] += _upsert_statement_items(payload)
                mark_item_done(
                    ctx,
                    SOURCE_FUNDAMENTALS,
                    target.primary_ticker,
                    rows_out=len(payload.metrics) + len(payload.statements),
                )
            except Exception as write_exc:
                counts["failed"] += 1
                mark_item_done(
                    ctx,
                    SOURCE_FUNDAMENTALS,
                    target.primary_ticker,
                    status="failed",
                    error=str(write_exc)[:1000],
                )
            if workers <= 1 and sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return counts


def _fetch_fundamentals_wrapped(
    target: CompanyTarget,
    yf_module: Any | None,
    include_quarterly: bool = True,
) -> tuple[CompanyTarget, FundamentalPayload | None, BaseException | None]:
    try:
        payload = fetch_yahoo_fundamentals_for_ticker(
            target, yf_module=yf_module, include_quarterly=include_quarterly,
        )
        return target, payload, None
    except BaseException as exc:  # noqa: BLE001 - reported via mark_item_done
        return target, None, exc


def _parallel_fundamentals(
    targets: list[CompanyTarget],
    yf_module: Any | None,
    max_workers: int,
    include_quarterly: bool = True,
) -> Iterable[tuple[CompanyTarget, FundamentalPayload | None, BaseException | None]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_fundamentals_wrapped, target, yf_module, include_quarterly): target
            for target in targets
        }
        for future in as_completed(futures):
            yield future.result()


def _fundamentals_freshness_map(intl_company_ids: list[str]) -> dict[str, date]:
    if not intl_company_ids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT intl_company_id, MAX(updated_at)::date AS last_updated
              FROM fact_yahoo_fundamental_metric
             WHERE intl_company_id = ANY(%s)
             GROUP BY intl_company_id
            """,
            (intl_company_ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}


def run_prices(
    *,
    tickers: list[str] | None = None,
    index_codes: list[str] | None = None,
    start_date: str = "2000-01-01",
    end_date: str | None = None,
    full: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    sleep_seconds: float = 0.25,
    yf_module: Any | None = None,
    sample_groups: list[str] | None = None,
    country_codes: list[str] | None = None,
) -> dict[str, int]:
    if dry_run and tickers:
        normalized = [normalize_existing_yahoo_ticker(t) for t in tickers]
        normalized = [t for t in normalized if t]
        return {"companies": len(normalized), "rows": 0, "failed": 0, "skipped": 0}

    targets = _select_company_targets(
        tickers=tickers, index_codes=index_codes, limit=limit, sample_groups=sample_groups,
        country_codes=country_codes,
    )
    counts = {"companies": len(targets), "rows": 0, "failed": 0, "skipped": 0}
    if dry_run:
        return counts
    if not targets:
        return counts

    yf = yf_module or _import_yfinance()
    start_default = date.fromisoformat(start_date)
    end_str = end_date or date.today().isoformat()
    end_day = date.fromisoformat(end_str)
    latest = {} if full else _latest_intl_price_dates()
    fx_cache: dict[str | None, dict[date, float]] = {}

    with market_run(
        SOURCE_PRICES,
        full,
        {"tickers": [target.primary_ticker for target in targets], "start_date": start_date, "end_date": end_str},
    ) as ctx:
        for target in targets:
            mark_item_running(ctx, SOURCE_PRICES, target.primary_ticker)
            latest_date = latest.get(target.intl_company_id)
            start = start_default if full or latest_date is None else latest_date + timedelta(days=1)
            if start >= end_day:
                counts["skipped"] += 1
                mark_item_done(
                    ctx,
                    SOURCE_PRICES,
                    target.primary_ticker,
                    status="skipped",
                    min_date=latest_date,
                    max_date=latest_date,
                )
                continue
            try:
                df = _download_price_frame(
                    yf,
                    target.primary_ticker,
                    start=start,
                    end=end_day,
                )
                fx_series = fx_cache.get(target.currency)
                if fx_series is None:
                    fx_series = _load_fx_series(target.currency or "")
                    fx_cache[target.currency] = fx_series
                price_rows = _price_rows_from_frame(target, df, fx_by_date=fx_series)
                if not price_rows:
                    counts["skipped"] += 1
                    mark_item_done(ctx, SOURCE_PRICES, target.primary_ticker, status="skipped")
                else:
                    _stage_price_rows(str(ctx.run_id), price_rows)
                    written = _merge_price_rows(str(ctx.run_id), target.intl_company_id)
                    counts["rows"] += written
                    dates = [row[0] for row in price_rows]
                    mark_item_done(
                        ctx,
                        SOURCE_PRICES,
                        target.primary_ticker,
                        rows_in=len(price_rows),
                        rows_out=written,
                        min_date=min(dates),
                        max_date=max(dates),
                    )
            except Exception as exc:
                counts["failed"] += 1
                mark_item_done(ctx, SOURCE_PRICES, target.primary_ticker, status="failed", error=str(exc)[:1000])
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return counts


def refresh_intl_fx(*, dry_run: bool = False, sleep_seconds: float = 0.3) -> dict[str, int]:
    """Fetch and upsert USD-per-1-unit rates for every currency present in
    dim_company_intl into fact_fx, using yfinance's `USDXYZ=X` tickers.

    Idempotent: writes one row per (ccy, today). Runs before `compute_intl_metrics`
    so the FX map is fresh at conversion time.
    """
    from xbrl_sec.sec.db.connection import connect as _connect
    from xbrl_sec.sec.db.bulk import execute_values as _execute_values
    import datetime as _dt
    import time as _time

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT currency FROM dim_company_intl
            WHERE currency IS NOT NULL AND currency <> '' AND COALESCE(include_in_pipeline, true)
            """
        )
        ccys = sorted({str(row[0]).upper() for row in cur.fetchall() if row[0]})
    stats = {"currencies_seen": len(ccys), "fetched": 0, "failed": 0, "written": 0, "skipped_usd": 0}
    today = _dt.date.today()
    rows: list[tuple[str, _dt.date, float]] = []

    import yfinance as yf
    for ccy in ccys:
        if ccy == "USD":
            rows.append(("USD", today, 1.0))
            stats["skipped_usd"] += 1
            continue
        # GBp (British pence) is a common Yahoo currency label; store it separately as
        # GBP-per-100 via GBP rate. compute_intl handles the /100 on the consumer side.
        yahoo_ccy = "GBP" if ccy in ("GBp", "GBX") else ccy
        try:
            hist = yf.Ticker(f"USD{yahoo_ccy}=X").history(period="5d")
            if hist.empty:
                stats["failed"] += 1
                continue
            close = float(hist["Close"].iloc[-1])
            if close <= 0 or close != close:  # zero or NaN
                stats["failed"] += 1
                continue
            usd_per_unit = 1.0 / close
            # Preserve the ccy label as-is (GBp stays GBp; compute_intl uses GBP rate/100).
            rows.append((ccy, today, usd_per_unit if ccy not in ("GBp", "GBX") else usd_per_unit))
            stats["fetched"] += 1
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
        _time.sleep(sleep_seconds)

    if not dry_run and rows:
        with _connect() as conn, conn.cursor() as cur:
            stats["written"] = _execute_values(
                cur,
                "INSERT INTO fact_fx (ccy, fx_date, usd_per_unit) VALUES %s "
                "ON CONFLICT (ccy, fx_date) DO UPDATE SET usd_per_unit = EXCLUDED.usd_per_unit, updated_at = now()",
                rows,
            )
    return stats


def run_global(
    *,
    index_codes: list[str] | None = None,
    fallback_dir: Path | str | None = None,
    validate: bool = True,
    use_wikipedia: bool = True,
    prices: bool = True,
    price_start_date: str = "2000-01-01",
    price_end_date: str | None = None,
    full_prices: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    sleep_seconds: float = 0.5,
    include_wholesale: bool = False,
    refresh_fx: bool = True,
) -> dict[str, int]:
    discovery = run_discovery(
        index_codes=index_codes,
        fallback_dir=fallback_dir,
        validate=validate,
        use_wikipedia=use_wikipedia,
        dry_run=dry_run,
        limit=limit,
        sleep_seconds=sleep_seconds,
        include_wholesale=include_wholesale,
    )
    if dry_run:
        fundamentals = {"companies": discovery.get("companies", 0), "metrics": 0, "statement_items": 0, "failed": 0}
        price_counts = {"companies": discovery.get("companies", 0), "rows": 0, "failed": 0, "skipped": 0}
        out = {f"discovery_{k}": v for k, v in discovery.items()} | {f"fundamentals_{k}": v for k, v in fundamentals.items()}
        return out | ({f"prices_{k}": v for k, v in price_counts.items()} if prices else {})
    fundamentals = run_fundamentals(
        index_codes=index_codes,
        limit=limit,
        dry_run=dry_run,
        sleep_seconds=sleep_seconds,
    )
    price_counts = (
        run_prices(
            index_codes=index_codes,
            start_date=price_start_date,
            end_date=price_end_date,
            full=full_prices,
            limit=limit,
            dry_run=dry_run,
            sleep_seconds=sleep_seconds,
        )
        if prices
        else {}
    )
    out = {f"discovery_{k}": v for k, v in discovery.items()} | {f"fundamentals_{k}": v for k, v in fundamentals.items()}
    out |= {f"prices_{k}": v for k, v in price_counts.items()}
    # Refresh FX rates last: needs the freshly-updated dim_company_intl.currency values.
    if refresh_fx and not dry_run:
        try:
            fx_stats = refresh_intl_fx(dry_run=False)
            out |= {f"fx_{k}": v for k, v in fx_stats.items()}
        except Exception as exc:  # noqa: BLE001 - FX refresh is non-blocking
            print(f"run_global: FX refresh failed: {exc}", flush=True)
            out["fx_error"] = 1
    return out


HEALTH_CHECK_SAMPLE: tuple[str, ...] = (
    # Legacy core
    "SAP.DE", "ASML.AS", "0005.HK", "INFY.NS", "PETR4.SA",
    # Phase 1 expansion -- one per new suffix (validates Yahoo actually covers it)
    "VOLV-B.ST",       # Sweden
    "NOVO-B.CO",       # Denmark
    "NOKIA.HE",        # Finland
    "EQNR.OL",         # Norway
    "PKO.WA",          # Poland
    "CEZ.PR",          # Czechia
    "OTP.BD",          # Hungary
    "EBS.VI",          # Austria
    "OPAP.AT",         # Greece
    "AKBNK.IS",        # Turkey
    "TEVA.TA",         # Israel
    "NPN.JO",          # South Africa
    "2222.SR",         # Saudi Arabia
    "EMAAR.DU",        # Dubai
    "TSLA.MX",         # Mexico
    "RY.TO",           # Canada
    "SHOP.TO",         # Canada
    "COMI.CA",         # Egypt
    "600519.SS",       # China Shanghai
    "000858.SZ",       # China Shenzhen
    "005930.KS",       # Korea KOSPI
    "091990.KQ",       # Korea KOSDAQ
    "2330.TW",         # Taiwan
    "MAYBANK.KL",      # Malaysia
    "PTT.BK",          # Thailand
    "VNM.VN",          # Vietnam
    "SM.PS",           # Philippines
    "BBCA.JK",         # Indonesia
    "RELIANCE.BO",     # India (BSE)
)


FX_CCYS_INTL: tuple[str, ...] = (
    # Currencies that appear as trading currencies in the INTL universe but
    # were missing from fact_fx. Ordered roughly by universe coverage.
    "KRW", "CNY", "SGD", "ZAR", "INR", "ILS", "THB", "MYR", "IDR", "TRY",
    "PLN", "TWD", "MXN", "SAR", "AED", "QAR", "KWD", "BHD", "BRL", "EGP",
    "MAD", "NGN", "KES", "CZK", "HUF", "RON", "ISK", "PHP", "VND", "NZD",
    "CLP", "COP", "PEN", "ARS", "BGN",
)

# Subunit currencies: (subunit_code, parent_ccy, divisor). Prices quoted in
# subunits must be divided by ``divisor`` before applying the parent's FX.
FX_SUBUNITS: dict[str, tuple[str, float]] = {
    "GBp": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0),
}


def ingest_fx_intl(
    ccys: Iterable[str] | None = None,
    *,
    period: str = "max",
    yf_module: Any | None = None,
) -> dict[str, int]:
    """Pull daily {CCY}USD=X history for every currency in ``ccys`` into fact_fx.

    Each Yahoo pair returns USD per 1 unit of the local currency, matching the
    ``fact_fx.usd_per_unit`` semantic. Skips currencies whose Yahoo response is
    empty; returns per-currency row counts.
    """
    yf = yf_module or _import_yfinance()
    session = _yahoo_session()
    ticker_kwargs: dict[str, Any] = {"session": session} if session is not None else {}
    to_ingest = tuple(ccys) if ccys else FX_CCYS_INTL
    counts: dict[str, int] = {}
    all_rows: list[tuple[str, date, float]] = []
    for ccy in to_ingest:
        pair = f"{ccy}USD=X"
        try:
            ticker = yf.Ticker(pair, **ticker_kwargs)
        except TypeError:
            ticker = yf.Ticker(pair)
        try:
            history = ticker.history(period=period, auto_adjust=True)
        except Exception as exc:  # noqa: BLE001
            counts[ccy] = 0
            continue
        if history is None or getattr(history, "empty", True) or "Close" not in history.columns:
            counts[ccy] = 0
            continue
        rows_before = len(all_rows)
        for idx, row in history.iterrows():
            day = idx.date() if hasattr(idx, "date") else idx
            close = row.get("Close")
            try:
                value = float(close)
            except (TypeError, ValueError):
                continue
            if math.isnan(value) or math.isinf(value) or value <= 0:
                continue
            all_rows.append((ccy, day, value))
        counts[ccy] = len(all_rows) - rows_before
    if not all_rows:
        return counts
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_fx (ccy, fx_date, usd_per_unit) VALUES %s
            ON CONFLICT (ccy, fx_date) DO UPDATE SET
                usd_per_unit = EXCLUDED.usd_per_unit,
                updated_at = NOW()
            """,
            all_rows,
        )
    return counts


def health_check(
    *,
    tickers: list[str] | None = None,
    yf_module: Any | None = None,
) -> dict[str, int]:
    sample = tickers or list(HEALTH_CHECK_SAMPLE)
    counts = {"tickers": len(sample), "valid": 0, "invalid": 0}
    for symbol in sample:
        try:
            profile = validate_yahoo_ticker(symbol, yf_module=yf_module)
        except Exception:
            profile = None
        if profile:
            counts["valid"] += 1
        else:
            counts["invalid"] += 1
    return counts


def default_fallback_dir() -> Path:
    return load_settings().market_data_root / "yahoo_global" / "fallbacks"


def _clean_ticker_text(value: Any) -> str | None:
    if value is None:
        return None
    raw_text = str(value)
    if "%" in raw_text:
        return None
    text = raw_text.replace("\xa0", " ").strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if ":" in text:
        text = text.split(":")[-1]
    text = re.split(r"\s+", text.strip())[0]
    text = text.strip().strip(",;")
    text = text.replace("/", "-").upper()
    text = re.sub(r"[^A-Z0-9.\-=]", "", text)
    if text in {"", "-", "N-A", "NA", "NAN", "TICKER", "SYMBOL", "CODE"}:
        return None
    if re.fullmatch(r"\d+\.\d+", text):
        return None
    return text


def _is_japan_ticker(ticker: str) -> bool:
    return any(ticker.endswith(suffix) for suffix in JAPAN_SUFFIXES)


def _suffix(ticker: str) -> str | None:
    match = re.search(r"(\.[A-Z]{1,4})$", ticker)
    return match.group(1) if match else None


def _ticker_variants(primary_ticker: str, config: YahooIndexConfig) -> list[str]:
    """Return the primary ticker plus alt-suffix variants for validation retries.

    Preserves ordering: primary first, then alt_suffixes in declaration order.
    Skips Japan suffixes and duplicates.
    """
    variants: list[str] = [primary_ticker]
    current_suffix = _suffix(primary_ticker)
    stem = primary_ticker[: -len(current_suffix)] if current_suffix else primary_ticker
    for alt in config.alt_suffixes:
        alt_norm = (alt or "").strip().upper()
        if not alt_norm or alt_norm in JAPAN_SUFFIXES or alt_norm == current_suffix:
            continue
        candidate = f"{stem}{alt_norm}"
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _rewrite_ticker(row: DiscoveredTicker, new_ticker: str) -> DiscoveredTicker:
    """Return a copy of ``row`` with ``primary_ticker`` and derived fields updated."""
    return DiscoveredTicker(
        index_code=row.index_code,
        raw_ticker=row.raw_ticker,
        primary_ticker=new_ticker,
        name=row.name,
        country_code=row.country_code,
        exchange_suffix=_suffix(new_ticker),
        source_name=row.source_name,
        source_url=row.source_url,
        source_rank=row.source_rank,
        raw_payload=row.raw_payload,
    )


def _column_name(column: Any) -> str:
    if isinstance(column, tuple):
        parts = [str(part) for part in column if part is not None and not str(part).startswith("Unnamed")]
        return " ".join(parts).strip()
    return str(column).strip()


def _ticker_columns(columns: list[str]) -> list[int]:
    out: list[int] = []
    for idx, column in enumerate(columns):
        lowered = column.lower()
        tokens = set(re.split(r"[^a-z0-9]+", lowered))
        tokens.discard("")
        if {"ticker", "symbol", "epic", "ric"} & tokens:
            out.append(idx)
            continue
        if "code" in tokens and (
            len(tokens) == 1
            or {"stock", "security", "constituent", "company", "instrument", "exchange"} & tokens
        ):
            out.append(idx)
    return out


def _name_column(columns: list[str], ticker_cols: list[int]) -> int | None:
    for idx, column in enumerate(columns):
        if idx in ticker_cols:
            continue
        lowered = column.lower()
        if any(token in lowered for token in NAME_COLUMN_TOKENS):
            return idx
    return None


def _clean_name(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\[[^\]]*\]", "", str(value)).replace("\xa0", " ").strip()
    return text or None


def _row_payload(row: Any, columns: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for idx, column in enumerate(columns):
        try:
            value = row.iloc[idx] if hasattr(row, "iloc") else row[idx]
        except Exception:
            continue
        payload[column] = _jsonable(value)
    return payload


def _fallback_path(config: YahooIndexConfig, fallback_dir: Path | str | None) -> Path | None:
    root = Path(fallback_dir) if fallback_dir else default_fallback_dir()
    candidates = [
        root / f"{config.index_code}.csv",
        root / f"{config.index_code.lower()}.csv",
        root / f"{config.index_code.lower().replace('_', '-')}.csv",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _import_yfinance() -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for Yahoo global ingestion. Install backend requirements.") from exc
    cache_dir = Path(__file__).resolve().parents[3] / ".cache" / "yfinance"
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        from yfinance import cache as yf_cache
        yf_cache.set_cache_location(str(cache_dir))
    except Exception:
        pass
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(str(cache_dir))
    return yf


def _with_backoff(
    func: Callable[[], Any],
    *,
    attempts: int,
    base_sleep_seconds: float,
    sleeper: Callable[[float], None],
) -> Any:
    last_exc: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            delay = base_sleep_seconds * (2 ** attempt)
            delay += random.uniform(0, max(base_sleep_seconds, 0.01))
            sleeper(delay)
    assert last_exc is not None
    raise last_exc


def _profile_is_valid(profile: dict[str, Any], symbol: str) -> bool:
    if not profile:
        return False
    if profile.get("_info_error") and len(profile) <= 2:
        return False
    quote_type = str(profile.get("quoteType") or "").upper()
    if quote_type and quote_type not in {"EQUITY", "ETF", "MUTUALFUND"}:
        return False
    profile_symbol = normalize_existing_yahoo_ticker(profile.get("symbol") or symbol)
    if profile_symbol and profile_symbol != symbol:
        # yfinance sometimes echoes aliases; still allow records with real names.
        return bool(profile.get("longName") or profile.get("shortName"))
    return any(profile.get(key) for key in ("longName", "shortName", "marketCap", "regularMarketPrice", "currency"))


def _profile_matches_domicile(profile: dict[str, Any], config: YahooIndexConfig) -> bool:
    """True if the profile is domiciled in the config's country.

    Uses the Yahoo profile country (e.g. "Germany") matched against the config's
    country_name, with an ISIN-prefix fallback (a German company keeps its DE-prefixed
    ISIN even when cross-listed). Foreign names trading on the national exchange fail
    both checks and are dropped by the caller.
    """
    want_name = (config.country_name or "").strip().lower()
    if want_name and str(profile.get("country") or "").strip().lower() == want_name:
        return True
    want_cc = (config.country_code or "").strip().upper()
    isin = str(profile.get("isin") or "").strip().upper()
    if want_cc and len(want_cc) == 2 and isin[:2] == want_cc:
        return True
    return False


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, default=str)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        try:
            return value.date()
        except Exception:
            pass
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _upsert_index_configs(configs: list[YahooIndexConfig]) -> int:
    rows = [
        (
            cfg.index_code,
            cfg.name,
            cfg.region,
            cfg.country_code,
            cfg.country_name,
            cfg.default_suffix,
            cfg.wikipedia_url,
            cfg.yahoo_symbol,
        )
        for cfg in configs
    ]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO ref_yahoo_index
                (index_code, name, region, country_code, country_name,
                 default_suffix, wikipedia_url, yahoo_symbol)
            VALUES %s
            ON CONFLICT (index_code) DO UPDATE SET
                name=EXCLUDED.name,
                region=EXCLUDED.region,
                country_code=EXCLUDED.country_code,
                country_name=EXCLUDED.country_name,
                default_suffix=EXCLUDED.default_suffix,
                wikipedia_url=EXCLUDED.wikipedia_url,
                yahoo_symbol=EXCLUDED.yahoo_symbol,
                is_active=TRUE,
                updated_at=now()
            """,
            rows,
        )


def _stage_discovery_rows(run_id: str, rows: list[DiscoveredTicker]) -> int:
    stage_rows = [
        (
            run_id,
            row.index_code,
            row.source_name,
            row.source_url,
            row.source_rank,
            row.raw_ticker,
            row.primary_ticker,
            row.name,
            row.country_code,
            row.exchange_suffix,
            _json_dumps(row.raw_payload),
        )
        for row in rows
    ]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO stage_yahoo_discovery
                (run_id, index_code, source_name, source_url, source_rank,
                 raw_ticker, primary_ticker, constituent_name, country_code,
                 exchange_suffix, raw_payload)
            VALUES %s
            ON CONFLICT (run_id, index_code, primary_ticker) DO UPDATE SET
                source_name=EXCLUDED.source_name,
                source_url=EXCLUDED.source_url,
                source_rank=EXCLUDED.source_rank,
                raw_ticker=EXCLUDED.raw_ticker,
                constituent_name=EXCLUDED.constituent_name,
                raw_payload=EXCLUDED.raw_payload
            """,
            stage_rows,
        )


def _upsert_companies_from_discovery(
    config: YahooIndexConfig,
    rows: list[DiscoveredTicker],
    profile_by_ticker: dict[str, dict[str, Any]],
) -> int:
    company_rows = []
    for row in rows:
        profile = profile_by_ticker.get(row.primary_ticker, {})
        company_rows.append(_company_row(config, row, profile))
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, _COMPANY_UPSERT_SQL, company_rows, page_size=1000)


def _company_row(
    config: YahooIndexConfig,
    row: DiscoveredTicker,
    profile: dict[str, Any],
) -> tuple[Any, ...]:
    name = profile.get("longName") or profile.get("shortName") or row.name
    gics_code, gics_name = yahoo_sector_to_gics(profile.get("sector"))
    grp_code, grp_name = yahoo_industry_to_gics_group(profile.get("industry"))
    return (
        intl_company_id(row.primary_ticker),
        name,
        profile.get("longName") or name,
        row.primary_ticker,
        profile.get("exchange") or profile.get("fullExchangeName"),
        row.exchange_suffix,
        config.region,
        row.country_code or config.country_code,
        profile.get("country") or config.country_name,
        # dim_company_intl.currency = the *trading* currency (used for market-cap
        # FX conversion). Prefer .info['currency'] over financialCurrency — Yahoo's
        # financialCurrency is the reporting currency (used by fact_yahoo_statement_item),
        # which differs for tickers like AMMN.JK (trading IDR, reporting USD).
        profile.get("currency") or profile.get("financialCurrency"),
        profile.get("quoteType"),
        profile.get("isin"),
        profile.get("lei"),
        profile.get("website"),
        profile.get("sector"),
        profile.get("industry"),
        gics_code,
        gics_name or profile.get("sector"),
        grp_code,
        grp_name,
        profile.get("sector"),
        _decimal_or_none(profile.get("marketCap")),
        _decimal_or_none(profile.get("sharesOutstanding") or profile.get("impliedSharesOutstanding")),
        True,
        True,
        config.pipeline_sample_group or f"yahoo_global_{config.region.lower().replace(' ', '_')}",
        "yahoo_finance" if profile else row.source_name,
        _json_dumps(profile),
    )


_COMPANY_UPSERT_SQL = """
    INSERT INTO dim_company_intl
        (intl_company_id, name, name_en, primary_ticker, exchange, exchange_suffix,
         region, country_code, country_name, currency, quote_type, isin, lei,
         website, sector, industry, gics_sector_code, gics_sector_name,
         gics_industry_group_code, gics_industry_group_name, mapping_sector,
         market_cap, shares_outstanding, is_active, include_in_pipeline,
         pipeline_sample_group, source, raw_profile)
    VALUES %s
    ON CONFLICT (intl_company_id) DO UPDATE SET
        name=COALESCE(EXCLUDED.name, dim_company_intl.name),
        name_en=COALESCE(EXCLUDED.name_en, dim_company_intl.name_en),
        primary_ticker=EXCLUDED.primary_ticker,
        exchange=COALESCE(EXCLUDED.exchange, dim_company_intl.exchange),
        exchange_suffix=COALESCE(EXCLUDED.exchange_suffix, dim_company_intl.exchange_suffix),
        region=COALESCE(EXCLUDED.region, dim_company_intl.region),
        country_code=COALESCE(EXCLUDED.country_code, dim_company_intl.country_code),
        country_name=COALESCE(EXCLUDED.country_name, dim_company_intl.country_name),
        currency=COALESCE(EXCLUDED.currency, dim_company_intl.currency),
        quote_type=COALESCE(EXCLUDED.quote_type, dim_company_intl.quote_type),
        isin=COALESCE(EXCLUDED.isin, dim_company_intl.isin),
        lei=COALESCE(EXCLUDED.lei, dim_company_intl.lei),
        website=COALESCE(EXCLUDED.website, dim_company_intl.website),
        sector=COALESCE(EXCLUDED.sector, dim_company_intl.sector),
        industry=COALESCE(EXCLUDED.industry, dim_company_intl.industry),
        gics_sector_code=COALESCE(EXCLUDED.gics_sector_code, dim_company_intl.gics_sector_code),
        gics_sector_name=COALESCE(EXCLUDED.gics_sector_name, dim_company_intl.gics_sector_name),
        gics_industry_group_code=COALESCE(EXCLUDED.gics_industry_group_code, dim_company_intl.gics_industry_group_code),
        gics_industry_group_name=COALESCE(EXCLUDED.gics_industry_group_name, dim_company_intl.gics_industry_group_name),
        mapping_sector=COALESCE(EXCLUDED.mapping_sector, dim_company_intl.mapping_sector),
        market_cap=COALESCE(EXCLUDED.market_cap, dim_company_intl.market_cap),
        shares_outstanding=COALESCE(EXCLUDED.shares_outstanding, dim_company_intl.shares_outstanding),
        is_active=EXCLUDED.is_active,
        include_in_pipeline=dim_company_intl.include_in_pipeline,
        pipeline_sample_group=COALESCE(dim_company_intl.pipeline_sample_group, EXCLUDED.pipeline_sample_group),
        source=EXCLUDED.source,
        raw_profile=CASE
            WHEN EXCLUDED.raw_profile <> '{}'::jsonb THEN EXCLUDED.raw_profile
            ELSE dim_company_intl.raw_profile
        END,
        updated_at=now()
"""


def _upsert_index_constituents(config: YahooIndexConfig, rows: list[DiscoveredTicker]) -> int:
    values = [
        (
            row.index_code,
            intl_company_id(row.primary_ticker),
            row.primary_ticker,
            row.name,
            row.country_code or config.country_code,
            row.exchange_suffix,
            row.source_name,
            row.source_url,
            row.source_rank,
            _json_dumps(row.raw_payload),
            True,
        )
        for row in rows
    ]
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ref_yahoo_index_constituent SET is_active=FALSE WHERE index_code=%s", (config.index_code,))
        return execute_values(
            cur,
            """
            INSERT INTO ref_yahoo_index_constituent
                (index_code, intl_company_id, primary_ticker, constituent_name,
                 country_code, exchange_suffix, source_name, source_url,
                 source_rank, raw_payload, is_active)
            VALUES %s
            ON CONFLICT (index_code, intl_company_id) DO UPDATE SET
                primary_ticker=EXCLUDED.primary_ticker,
                constituent_name=COALESCE(EXCLUDED.constituent_name, ref_yahoo_index_constituent.constituent_name),
                country_code=COALESCE(EXCLUDED.country_code, ref_yahoo_index_constituent.country_code),
                exchange_suffix=COALESCE(EXCLUDED.exchange_suffix, ref_yahoo_index_constituent.exchange_suffix),
                source_name=EXCLUDED.source_name,
                source_url=EXCLUDED.source_url,
                source_rank=EXCLUDED.source_rank,
                raw_payload=EXCLUDED.raw_payload,
                last_seen_at=now(),
                is_active=TRUE
            """,
            values,
            page_size=1000,
        )


def _latest_intl_price_dates() -> dict[str, date]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT intl_company_id, MAX(date) FROM fact_prices_intl GROUP BY intl_company_id")
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}


def _download_price_frame(yf: Any, ticker: str, *, start: date, end: date) -> Any:
    return _with_backoff(
        lambda: yf.download(
            ticker,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=False,
            progress=False,
            threads=False,
        ),
        attempts=4,
        base_sleep_seconds=0.75,
        sleeper=time.sleep,
    )


def _price_rows_from_frame(
    target: CompanyTarget,
    df: Any,
    fx_by_date: dict[date, float] | None = None,
) -> list[tuple[Any, ...]]:
    """Convert a yfinance download frame into staged price tuples.

    When ``fx_by_date`` is provided (usd_per_unit for target's trading currency),
    USD-normalized close/adj_close/returns are computed alongside the
    trading-currency values. FX resolution rules:
        - Trading currency == USD           -> fx_rate = 1.0
        - Trading currency in FX_SUBUNITS   -> divide by divisor, use parent FX
        - Trading currency in fx_by_date    -> use that day's rate (with fallback
                                               to most recent prior available)
        - Otherwise                         -> USD columns NULL
    """
    df = _single_ticker_price_frame(df, target.primary_ticker)
    if df is None or getattr(df, "empty", True) or "Close" not in getattr(df, "columns", []):
        return []

    df = df.dropna(subset=["Close"])
    if df.empty:
        return []

    adj_closes = df["Adj Close"].astype(float) if "Adj Close" in df.columns else df["Close"].astype(float)
    raw_closes = df["Close"].astype(float)
    volumes = df.get("Volume")
    ccy = (target.currency or "").strip()
    subunit_scale = 1.0
    lookup_ccy = ccy
    if ccy in FX_SUBUNITS:
        parent, divisor = FX_SUBUNITS[ccy]
        subunit_scale = divisor
        lookup_ccy = parent
    fx_lookup = fx_by_date or {}
    sorted_fx_dates = sorted(fx_lookup.keys()) if fx_lookup else []

    def _fx_for(day: date) -> float | None:
        if lookup_ccy == "USD":
            return 1.0
        if not fx_lookup:
            return None
        rate = fx_lookup.get(day)
        if rate is not None:
            return rate
        import bisect
        idx = bisect.bisect_right(sorted_fx_dates, day) - 1
        if idx < 0:
            return None
        return fx_lookup[sorted_fx_dates[idx]]

    out: list[tuple[Any, ...]] = []
    prev_adj: float | None = None
    prev_adj_usd: float | None = None

    for i, (idx, _row) in enumerate(df.iterrows()):
        day = idx.date() if hasattr(idx, "date") else idx
        close_v = float(raw_closes.iloc[i])
        adj_v = float(adj_closes.iloc[i])
        if math.isnan(close_v):
            continue
        vol_v = None
        if volumes is not None:
            try:
                volume = float(volumes.iloc[i])
                vol_v = int(volume) if not math.isnan(volume) else None
            except (TypeError, ValueError):
                vol_v = None

        if prev_adj is not None and not math.isnan(adj_v) and prev_adj > 0 and adj_v > 0:
            ret = (adj_v - prev_adj) / prev_adj
            log_ret = math.log(adj_v / prev_adj)
            abs_diff = adj_v - prev_adj
        else:
            ret = log_ret = abs_diff = None
        if not math.isnan(adj_v):
            prev_adj = adj_v

        fx_rate = _fx_for(day)
        close_usd: float | None = None
        adj_close_usd: float | None = None
        ret_usd: float | None = None
        log_ret_usd: float | None = None
        abs_diff_usd: float | None = None
        if fx_rate is not None and fx_rate > 0:
            close_usd = (close_v / subunit_scale) * fx_rate
            if not math.isnan(adj_v):
                adj_close_usd = (adj_v / subunit_scale) * fx_rate
                if prev_adj_usd is not None and prev_adj_usd > 0 and adj_close_usd > 0:
                    ret_usd = (adj_close_usd - prev_adj_usd) / prev_adj_usd
                    log_ret_usd = math.log(adj_close_usd / prev_adj_usd)
                    abs_diff_usd = adj_close_usd - prev_adj_usd
                prev_adj_usd = adj_close_usd

        out.append(
            (
                day,
                target.intl_company_id,
                target.primary_ticker,
                close_v,
                adj_v if not math.isnan(adj_v) else None,
                ret,
                log_ret,
                abs_diff,
                vol_v,
                target.currency,
                target.region,
                target.country_code,
                target.exchange,
                close_usd,
                adj_close_usd,
                ret_usd,
                log_ret_usd,
                abs_diff_usd,
                fx_rate,
            )
        )
    return out


def _single_ticker_price_frame(raw: Any, ticker: str) -> Any | None:
    if raw is None or getattr(raw, "empty", True):
        return None
    try:
        columns = raw.columns
        if getattr(columns, "nlevels", 1) > 1:
            level0 = columns.get_level_values(0)
            if ticker in level0:
                return raw[ticker].copy()
            level1 = columns.get_level_values(1)
            if ticker in level1:
                return raw.xs(ticker, axis=1, level=1).copy()
            # Single-ticker yfinance builds may return price-field first.
            if "Close" in level0:
                return raw.xs(ticker, axis=1, level=1).copy() if ticker in level1 else raw.droplevel(1, axis=1).copy()
            return None
        return raw.copy()
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _stage_price_rows(run_id: str, rows: list[tuple[Any, ...]]) -> int:
    stage_rows = [(run_id, *row) for row in rows]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO stage_yahoo_prices
                (run_id, date, intl_company_id, ticker, close, adj_close, return,
                 log_return, abs_diff, volume, currency, region, country_code, exchange,
                 close_usd, adj_close_usd, return_usd, log_return_usd, abs_diff_usd,
                 fx_rate_usd_per_unit)
            VALUES %s
            ON CONFLICT (run_id, intl_company_id, date) DO UPDATE SET
                ticker=EXCLUDED.ticker,
                close=EXCLUDED.close,
                adj_close=EXCLUDED.adj_close,
                return=EXCLUDED.return,
                log_return=EXCLUDED.log_return,
                abs_diff=EXCLUDED.abs_diff,
                volume=EXCLUDED.volume,
                currency=EXCLUDED.currency,
                region=EXCLUDED.region,
                country_code=EXCLUDED.country_code,
                exchange=EXCLUDED.exchange,
                close_usd=EXCLUDED.close_usd,
                adj_close_usd=EXCLUDED.adj_close_usd,
                return_usd=EXCLUDED.return_usd,
                log_return_usd=EXCLUDED.log_return_usd,
                abs_diff_usd=EXCLUDED.abs_diff_usd,
                fx_rate_usd_per_unit=EXCLUDED.fx_rate_usd_per_unit
            """,
            stage_rows,
            page_size=5000,
        )


def _merge_price_rows(run_id: str, intl_company_id_value: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_prices_intl
                (date, intl_company_id, ticker, close, adj_close, return, log_return,
                 abs_diff, volume, currency, region, country_code, exchange,
                 close_usd, adj_close_usd, return_usd, log_return_usd, abs_diff_usd,
                 fx_rate_usd_per_unit)
            SELECT date, intl_company_id, ticker, close, adj_close, return, log_return,
                   abs_diff, volume, currency, region, country_code, exchange,
                   close_usd, adj_close_usd, return_usd, log_return_usd, abs_diff_usd,
                   fx_rate_usd_per_unit
              FROM stage_yahoo_prices
             WHERE run_id=%s AND intl_company_id=%s
            ON CONFLICT (intl_company_id, date) DO UPDATE SET
                ticker=EXCLUDED.ticker,
                close=EXCLUDED.close,
                adj_close=EXCLUDED.adj_close,
                return=EXCLUDED.return,
                log_return=EXCLUDED.log_return,
                abs_diff=EXCLUDED.abs_diff,
                volume=EXCLUDED.volume,
                currency=EXCLUDED.currency,
                region=EXCLUDED.region,
                country_code=EXCLUDED.country_code,
                exchange=EXCLUDED.exchange,
                close_usd=EXCLUDED.close_usd,
                adj_close_usd=EXCLUDED.adj_close_usd,
                return_usd=EXCLUDED.return_usd,
                log_return_usd=EXCLUDED.log_return_usd,
                abs_diff_usd=EXCLUDED.abs_diff_usd,
                fx_rate_usd_per_unit=EXCLUDED.fx_rate_usd_per_unit,
                updated_at=now()
            """,
            (run_id, intl_company_id_value),
        )
        return cur.rowcount


def _load_fx_series(ccy: str) -> dict[date, float]:
    """Return {fx_date -> usd_per_unit} for a currency, or {} if none exists."""
    if not ccy or ccy == "USD":
        return {}
    lookup_ccy = FX_SUBUNITS[ccy][0] if ccy in FX_SUBUNITS else ccy
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fx_date, usd_per_unit FROM fact_fx WHERE ccy=%s ORDER BY fx_date",
            (lookup_ccy,),
        )
        return {row[0]: float(row[1]) for row in cur.fetchall() if row[1] is not None}


def _select_company_targets(
    *,
    tickers: list[str] | None,
    index_codes: list[str] | None,
    limit: int | None,
    sample_groups: list[str] | None = None,
    country_codes: list[str] | None = None,
    only_missing_fundamentals: bool = False,
    only_missing_quarterly: bool = False,
) -> list[CompanyTarget]:
    params: list[Any] = []
    joins = ""
    where = ["d.primary_ticker IS NOT NULL", "COALESCE(d.include_in_pipeline, TRUE)", "COALESCE(d.is_active, TRUE)"]
    if tickers:
        normalized = [normalize_existing_yahoo_ticker(t) for t in tickers]
        normalized = [t for t in normalized if t]
        params.append(normalized)
        where.append("d.primary_ticker = ANY(%s)")
    if country_codes:
        params.append([c.strip().upper() for c in country_codes])
        where.append("d.country_code = ANY(%s)")
    if index_codes:
        configs = resolve_index_configs(index_codes, include_wholesale=True)
        params.append([cfg.index_code for cfg in configs])
        joins = "JOIN ref_yahoo_index_constituent c ON c.intl_company_id=d.intl_company_id AND c.is_active"
        where.append("c.index_code = ANY(%s)")
    if sample_groups:
        params.append(sample_groups)
        where.append("d.pipeline_sample_group = ANY(%s)")
    if only_missing_fundamentals:
        # Only companies that have zero metric AND zero statement rows.
        where.append("""NOT EXISTS (SELECT 1 FROM fact_yahoo_fundamental_metric m
                                     WHERE m.intl_company_id = d.intl_company_id)""")
        where.append("""NOT EXISTS (SELECT 1 FROM fact_yahoo_statement_item s
                                     WHERE s.intl_company_id = d.intl_company_id)""")
    if only_missing_quarterly:
        # Companies that already have annual coverage but lack any quarterly row.
        where.append("""NOT EXISTS (SELECT 1 FROM fact_yahoo_statement_item q
                                     WHERE q.intl_company_id = d.intl_company_id
                                       AND q.period_type = 'quarterly')""")
    limit_sql = ""
    if limit is not None:
        params.append(max(limit, 0))
        limit_sql = f"LIMIT %s"
    sql = f"""
        SELECT DISTINCT d.intl_company_id, d.primary_ticker, d.currency,
               d.region, d.country_code, d.exchange
        FROM dim_company_intl d
        {joins}
        WHERE {' AND '.join(where)}
        ORDER BY d.primary_ticker
        {limit_sql}
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        return [CompanyTarget(row[0], row[1], row[2], row[3], row[4], row[5]) for row in cur.fetchall()]


def _profile_metrics(profile: dict[str, Any], currency: str | None) -> list[tuple[str, Decimal | None, str | None, dict[str, Any]]]:
    rows: list[tuple[str, Decimal | None, str | None, dict[str, Any]]] = []
    for metric_id in PROFILE_METRICS:
        if metric_id not in profile:
            continue
        value = profile.get(metric_id)
        numeric = _decimal_or_none(value)
        text = None if numeric is not None else (str(value) if value is not None else None)
        rows.append((metric_id, numeric, text, {"raw_value": _jsonable(value), "currency": currency}))
    return rows


def _statement_rows(
    ticker_obj: Any,
    currency: str | None,
    *,
    include_quarterly: bool = True,
) -> list[tuple[str, str, date, int | None, str, Decimal | None, str | None]]:
    """Return statement rows for one ticker.

    Walks every statement type and, for each, both the annual and (optionally)
    quarterly attributes exposed by yfinance. Yields tuples of
    ``(statement_type, period_type, period_end, fiscal_year, line_item, value, currency)``.
    """
    # Dedupe on the DB PK columns (statement_type, period_type, period_end, line_item).
    # yfinance frames occasionally repeat line-item labels (localization variants,
    # multi-level index collapses) which would otherwise fail ON CONFLICT.
    dedup: dict[tuple[str, str, date, str], tuple[str, str, date, int | None, str, Decimal | None, str | None]] = {}
    period_types: tuple[str, ...] = ("annual", "quarterly") if include_quarterly else ("annual",)
    for statement_type, per_period_attrs in STATEMENT_ATTRS.items():
        for period_type in period_types:
            attrs = per_period_attrs.get(period_type, ())
            frame = None
            for attr in attrs:
                try:
                    candidate = getattr(ticker_obj, attr)
                except Exception:
                    candidate = None
                if candidate is not None and not getattr(candidate, "empty", True):
                    frame = candidate
                    break
            if frame is None:
                continue
            for line_item, series in frame.iterrows():
                clean_item = str(line_item).strip()
                if not clean_item:
                    continue
                for period, value in series.items():
                    period_end = _date_or_none(period)
                    numeric = _decimal_or_none(value)
                    if period_end is None or numeric is None:
                        continue
                    key = (statement_type, period_type, period_end, clean_item)
                    dedup[key] = (
                        statement_type,
                        period_type,
                        period_end,
                        period_end.year,
                        clean_item,
                        numeric,
                        currency,
                    )
    return list(dedup.values())


def _stage_fundamental_payload(run_id: str, payload: FundamentalPayload) -> int:
    today = date.today()
    rows: list[tuple[Any, ...]] = []
    rows.append((
        run_id,
        payload.target.intl_company_id,
        payload.target.primary_ticker,
        "profile",
        "raw_profile",
        today,
        None,
        None,
        payload.target.currency,
        _json_dumps(payload.profile),
    ))
    for metric_id, value, value_text, raw_payload in payload.metrics:
        rows.append((
            run_id,
            payload.target.intl_company_id,
            payload.target.primary_ticker,
            "metric",
            metric_id,
            today,
            value,
            value_text,
            payload.target.currency,
            _json_dumps(raw_payload),
        ))
    seen_stage_keys: set[tuple[Any, ...]] = set()
    for statement_type, period_type, period_end, _, line_item, value, currency in payload.statements:
        # Encode period_type into item_key so annual + quarterly rows for the same
        # (statement_type, period_end, line_item) don't collide on the stage PK.
        item_key = f"{line_item} [{period_type}]" if period_type != "annual" else line_item
        dedup_key = (statement_type, item_key, period_end)
        if dedup_key in seen_stage_keys:
            continue
        seen_stage_keys.add(dedup_key)
        rows.append((
            run_id,
            payload.target.intl_company_id,
            payload.target.primary_ticker,
            statement_type,
            item_key,
            period_end,
            value,
            None,
            currency,
            "{}",
        ))
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO stage_yahoo_fundamentals
                (run_id, intl_company_id, primary_ticker, payload_type, item_key,
                 period_end, value, value_text, currency, raw_payload)
            VALUES %s
            ON CONFLICT (run_id, intl_company_id, payload_type, item_key, period_end)
            DO UPDATE SET
                value=EXCLUDED.value,
                value_text=EXCLUDED.value_text,
                currency=EXCLUDED.currency,
                raw_payload=EXCLUDED.raw_payload
            """,
            rows,
            page_size=2000,
        )


def _update_company_profile(payload: FundamentalPayload) -> None:
    profile = payload.profile
    gics_code, gics_name = yahoo_sector_to_gics(profile.get("sector"))
    grp_code, grp_name = yahoo_industry_to_gics_group(profile.get("industry"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dim_company_intl
               SET name=COALESCE(%s, name),
                   name_en=COALESCE(%s, name_en),
                   exchange=COALESCE(%s, exchange),
                   currency=COALESCE(%s, currency),
                   quote_type=COALESCE(%s, quote_type),
                   isin=COALESCE(%s, isin),
                   lei=COALESCE(%s, lei),
                   website=COALESCE(%s, website),
                   sector=COALESCE(%s, sector),
                   industry=COALESCE(%s, industry),
                   gics_sector_code=COALESCE(%s, gics_sector_code),
                   gics_sector_name=COALESCE(%s, gics_sector_name),
                   gics_industry_group_code=COALESCE(%s, gics_industry_group_code),
                   gics_industry_group_name=COALESCE(%s, gics_industry_group_name),
                   mapping_sector=COALESCE(%s, mapping_sector),
                   market_cap=COALESCE(%s, market_cap),
                   shares_outstanding=COALESCE(%s, shares_outstanding),
                   raw_profile=%s::jsonb,
                   source='yahoo_finance',
                   updated_at=now()
             WHERE intl_company_id=%s
            """,
            (
                profile.get("longName") or profile.get("shortName"),
                profile.get("longName"),
                profile.get("exchange") or profile.get("fullExchangeName"),
                # dim_company_intl.currency = the *trading* currency (used for market-cap
        # FX conversion). Prefer .info['currency'] over financialCurrency — Yahoo's
        # financialCurrency is the reporting currency (used by fact_yahoo_statement_item),
        # which differs for tickers like AMMN.JK (trading IDR, reporting USD).
        profile.get("currency") or profile.get("financialCurrency"),
                profile.get("quoteType"),
                profile.get("isin"),
                profile.get("lei"),
                profile.get("website"),
                profile.get("sector"),
                profile.get("industry"),
                gics_code,
                gics_name or profile.get("sector"),
                grp_code,
                grp_name,
                profile.get("sector"),
                _decimal_or_none(profile.get("marketCap")),
                _decimal_or_none(profile.get("sharesOutstanding") or profile.get("impliedSharesOutstanding")),
                _json_dumps(profile),
                payload.target.intl_company_id,
            ),
        )


def _upsert_profile_metrics(payload: FundamentalPayload) -> int:
    today = date.today()
    rows = [
        (
            payload.target.intl_company_id,
            today,
            metric_id,
            value,
            value_text,
            payload.target.currency,
            _json_dumps(raw_payload),
        )
        for metric_id, value, value_text, raw_payload in payload.metrics
    ]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO fact_yahoo_fundamental_metric
                (intl_company_id, as_of_date, metric_id, value, value_text, currency, raw_payload)
            VALUES %s
            ON CONFLICT (intl_company_id, as_of_date, metric_id) DO UPDATE SET
                value=EXCLUDED.value,
                value_text=EXCLUDED.value_text,
                currency=EXCLUDED.currency,
                raw_payload=EXCLUDED.raw_payload,
                updated_at=now()
            """,
            rows,
            page_size=1000,
        )


def _upsert_statement_items(payload: FundamentalPayload) -> int:
    rows = [
        (
            payload.target.intl_company_id,
            statement_type,
            period_type,
            period_end,
            fiscal_year,
            line_item,
            value,
            currency,
        )
        for statement_type, period_type, period_end, fiscal_year, line_item, value, currency in payload.statements
    ]
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO fact_yahoo_statement_item
                (intl_company_id, statement_type, period_type, period_end,
                 fiscal_year, line_item, value, currency)
            VALUES %s
            ON CONFLICT (intl_company_id, statement_type, period_type, period_end, line_item)
            DO UPDATE SET
                fiscal_year=EXCLUDED.fiscal_year,
                value=EXCLUDED.value,
                currency=EXCLUDED.currency,
                updated_at=now()
            """,
            rows,
            page_size=5000,
        )
