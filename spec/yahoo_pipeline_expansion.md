# Yahoo Finance pipeline expansion spec

Author: assistant (bastian.offermann@protonmail.com)
Date: 2026-07-07

## Goal

Increase the number of unique companies processed by the Yahoo Finance
fundamental-data pipeline (`backend/xbrl_sec/sec/sources/yahoo_global.py`)
by 5-10x. Priority is broader coverage — small/mid-cap segments, tech
sub-indices, and countries that are currently opaque (Nordics, CEE, MENA,
Africa, ASEAN, LatAm beyond the majors). Not a data-quality or
frequency-of-refresh initiative.

## Non-goals

- Displacing Japan's EDINET/JPX-driven path. Japan stays out of
  `yahoo_global` (guarded by `JAPAN_INDEX_CODES` / `JAPAN_SUFFIXES`).
- Displacing US SEC/XBRL. US stays in the SEC pipeline; only ADR/dual-list
  cases arrive here as a byproduct of exchange-wholesale scoping.
- Real-time / intraday data. This pipeline stays end-of-day fundamentals
  and daily OHLCV.
- Replacing yfinance with a paid vendor.

## Baseline (as of 2026-07-07)

- Universe: 32 indices in `INDEX_CONFIGS`
  ([yahoo_global.py:94-127](../backend/xbrl_sec/sec/sources/yahoo_global.py#L94)).
- Discovery: single Wikipedia table per index, fallback to
  `D:\market_data\yahoo_global\fallbacks\{INDEX_CODE}.csv` (no CSVs
  present today).
- Validation: `yfinance.Ticker(sym).info` accepted if it looks like a
  live equity/ETF/mutual fund.
- Storage: `dim_company_intl`, `ref_yahoo_index`,
  `ref_yahoo_index_constituent`, `fact_yahoo_fundamental_metric`,
  `fact_yahoo_statement_item`, `fact_prices_intl` +
  `stage_yahoo_*` scratch tables
  ([sql/129_yahoo_global_fundamentals.sql](../backend/xbrl_sec/sec/sql/129_yahoo_global_fundamentals.sql)).
- Realistic dedupe'd yield today: ~1,800-2,500 companies (STOXX 600
  overlap trims a lot; several configs fail because their Wikipedia
  tables don't expose a plain "Ticker" column).

## Target

- 10,000-25,000 unique validated companies in `dim_company_intl` with
  `include_in_pipeline=TRUE`.
- Coverage in every populated country from at least one index, plus an
  optional exchange-wholesale universe for the six exchanges where full
  listings are freely downloadable.

## Phases

### Phase 1 — Extend `INDEX_CONFIGS` (small change, big yield)

Append new indices to the existing tuple. Each row needs:

- `index_code` (upper-snake) — new
- `name`, `region`
- `default_suffix` (Yahoo suffix, e.g. `.ST`, `.WA`, `.JO`)
- `country_code`, `country_name`
- `wikipedia_path` — the path after `en.wikipedia.org/wiki/`
- optional `yahoo_symbol` — for the index itself (used elsewhere)

Two structural additions to `YahooIndexConfig`:

- `alt_suffixes: tuple[str, ...] = ()` — for markets with more than one
  Yahoo suffix (India `.NS`/`.BO`, Taiwan `.TW`/`.TWO`, China
  `.SS`/`.SZ`). `normalize_yahoo_ticker` tries default first; validator
  fallbacks to alt.
- `notes: str | None = None` — free-text for future readers (e.g., why an
  index is disabled, or the exact Wikipedia section anchor).

Indices to add — full list at bottom of this spec.

Expected marginal yield after dedupe: **+6,000-9,000 unique tickers**.

### Phase 2 — Pluggable source adapters

Refactor `discover_index` so a `YahooIndexConfig` can list any number of
sources in priority order. Each source implements:

```python
class DiscoverySource(Protocol):
    name: str
    def discover(self, config: YahooIndexConfig) -> list[DiscoveredTicker]: ...
```

Adapters:

- `WikipediaSource` — current behavior, extracted.
- `FallbackCsvSource` — current behavior, extracted.
- `StooqSource` — `stooq.com/q/i/?s=<code>&f=sd2t2ohlcv` for index
  membership. Best single unlock for global small/mid-cap coverage.
- `YahooScreenerSource` — `query1.finance.yahoo.com/v1/finance/screener`
  paginated. Filters by region/exchange/market cap; feeds "wholesale"
  universes.
- `ExchangeMasterListSource` — one impl per exchange. Free downloads:
  LSE ISS, SGX, BSE India, TSX, JSE, GPW, Deutsche Börse.

`discover_index` walks sources in priority order, unions results, dedupes
on `primary_ticker`. Each `DiscoveredTicker` retains `source_name` so
`pipeline_stage_run` can report per-source counts.

Ship this phase with WikipediaSource + FallbackCsvSource + StooqSource
initially; leave a scaffold for the exchange master-list adapters.

### Phase 3 — Wholesale exchange mode

For each exchange with a master-list adapter, seed a synthetic
`<EXCH>_ALL` index (`LSE_ALL`, `TSX_ALL`, `KRX_ALL`, `JSE_ALL`,
`WSE_ALL`, `BSE_ALL`, `SGX_ALL`, `SET_ALL`) whose "constituents" are
every listed company from the exchange dump. Constituents get
`pipeline_sample_group='exchange_wholesale'` so operators can gate
fundamentals/prices runs to real indices only (cheaper) or include
wholesale (expensive; Yahoo will 404 on 10-25% of names).

Expected marginal yield: **+8,000-15,000 unique tickers**.

### Phase 4 — Throughput + operability

Current serial loop with `sleep_seconds=0.5` ≈ 1 ticker/sec across
`run_fundamentals` and `run_prices`. At 25k companies × 3 calls, this
takes ~20 hours. Changes:

- `ThreadPoolExecutor(max_workers=8)` on the ticker loop. Existing
  `_with_backoff` already handles 429s with exponential+jitter.
- `refresh_before_days: int | None` — skip companies whose
  `fact_yahoo_fundamental_metric.updated_at` is newer than
  `now() - N days`. Cron-friendly.
- Named `pipeline_sample_group` values: `core_index`, `wide_index`,
  `exchange_wholesale`. Daily cron hits `core_index` + `wide_index`,
  weekly cron adds `exchange_wholesale`.
- `mv_yahoo_ticker_health` — materialized view over
  `pipeline_stage_run` failure counts per `(country_code,
  exchange_suffix)`, used to auto-deactivate consistently-dead tickers
  from wholesale scope.

### Phase 5 — Verification

- Regional health-check samples: extend
  `health_check` ([yahoo_global.py:634](../backend/xbrl_sec/sec/sources/yahoo_global.py#L634))
  with one live ticker per new suffix (`.ST`, `.CO`, `.HE`, `.OL`,
  `.WA`, `.PR`, `.BD`, `.VI`, `.AT`, `.IS`, `.TA`, `.JO`, `.SR`, `.KL`,
  `.BK`, `.JK`, `.PS`, `.V`, ...).
- Table-driven config test: every entry in `INDEX_CONFIGS` resolves via
  `resolve_index_configs`, has either a `wikipedia_url` or a fallback
  CSV path or a `stooq_code`, and no two configs collide on
  `index_code`.
- Optional online marker: `pytest.mark.online` smoke test against 3-5
  Wikipedia URLs so table-shape breakage surfaces early.

## Order of operations

1. Phase 1 (config expansion) + Phase 5 config-hygiene test in one PR.
2. Phase 2 refactor + Stooq adapter in a second PR.
3. Phase 3 exchange master-list adapters, one PR per adapter.
4. Phase 4 concurrency/incremental once universe > 5,000 companies.

## Open questions / decisions logged

- **Dedup policy across dual-listings.** Kept per-exchange for now:
  `dim_company_intl.primary_ticker` is unique, so `SAP.DE` and `SAP` (US
  ADR) stay separate. Acceptable for prices; ISIN/LEI enrichment can
  fold them later.
- **Wholesale default.** Gated behind `--include-wholesale`; not on by
  default.
- **Prune-vs-keep for patchy Africa/MENA.** Keep. Failures show up in
  `mv_yahoo_ticker_health` and can be pruned later; don't hand-tune now.

## Concrete Phase-1 index catalogue

### Europe — Nordics

- OMXS30, OMXS_ALL_SHARE (Sweden, `.ST`)
- OMXC25 (Denmark, `.CO`)
- OMXH25 (Finland, `.HE`)
- OBX (Norway, `.OL`)
- OMXI15 (Iceland, `.IC`)

### Europe — UK small/tech

- FTSE_SMALLCAP, FTSE_AIM_100, FTSE_AIM_ALL_SHARE, FTSE_TECHMARK (`.L`)

### Europe — Germany small

- SDAX, DAX_50_ESG (`.DE`)

### Europe — France mid/small

- SBF_120, CAC_MID_60, CAC_SMALL (`.PA`)

### Europe — Iberia/Italy small

- IBEX_MEDIUM_CAP, IBEX_SMALL_CAP (`.MC`)
- FTSE_ITALIA_STAR, FTSE_ITALIA_MID_CAP (`.MI`)

### Europe — Benelux

- BEL_20, BEL_MID (`.BR`)
- AMX, ASCX (`.AS`)

### Europe — CEE

- WIG20, MWIG40, SWIG80 (`.WA`)
- PX (Prague, `.PR`)
- BUX (Budapest, `.BD`)
- ATX (Vienna, `.VI`)
- BET (Bucharest, `.RO`)
- SOFIX (Sofia, `.SF`)

### Europe — SE Med

- FTSE_ATHEX_LARGE_CAP (Athens, `.AT`)
- BIST_30, BIST_50, BIST_100 (Istanbul, `.IS`)
- TA_35, TA_125 (Tel Aviv, `.TA`)

### Europe — pan-EU tech / sector

- Fix STOXX_EUROPE_600 discovery — Wikipedia table exposes tickers with
  suffixes already; today's `default_suffix=None` still works for those
  rows, but a good chunk of the table uses bare tickers. Approach: after
  Phase 2, list two sources for STOXX 600 — Wikipedia first, Stooq
  `^STOXX` as a redundant feed to catch missing rows.
- STOXX_EUROPE_600_BANKS, STOXX_EUROPE_600_HEALTH, STOXX_EUROPE_600_ENERGY
  (all Wikipedia-listed sub-indices).

### Asia — India

- NIFTY_100, NIFTY_200, NIFTY_500 (`.NS`)
- NIFTY_MIDCAP_100, NIFTY_MIDCAP_150 (`.NS`)
- NIFTY_SMALLCAP_100, NIFTY_SMALLCAP_250 (`.NS`)
- NIFTY_BANK, NIFTY_PHARMA, NIFTY_AUTO (`.NS`)
- BSE_500, BSE_MIDCAP, BSE_SMALLCAP (`.BO`)

### Asia — China A-shares

- CSI_300, CSI_500, CSI_1000 (`.SS`/`.SZ` mixed)
- CHINEXT (`.SZ`)
- STAR_50 (`.SS`)

### Asia — HK/TW

- HANG_SENG_COMPOSITE, HANG_SENG_SMALLCAP (`.HK`)
- TAIWAN_50 (`.TW`)
- TPEX (`.TWO`)

### Asia — Korea

- KOSPI_200, KRX_300, KOSDAQ_150 (`.KS`/`.KQ`)

### Asia — ASEAN

- FTSE_BURSA_MALAYSIA_KLCI, KLCI_HIJRAH_SHARIAH (`.KL`)
- SET50, SET100 (`.BK`)
- VN30, HNX30 (`.VN`)
- PSEI (`.PS`)
- IDX30, LQ45, IDX80 (`.JK`)

### South America — Brazil small/sector

- IBRX_50, IBRA, SMLL, IDIV, ICON, ICO2 (`.SA`)

### South America — Others small

- BMV_IMC30 (`.MX`)
- SPBVL_PERU_SELECT (`.LM`)
- MERVAL_25 (`.BA`)
- SP_CLX_65 (`.SN`)

### Africa

- JSE_TOP_40, JSE_ALL_SHARE, JSE_SMALL_CAP (`.JO`)
- EGX_30, EGX_70, EGX_100 (`.CA`)
- MASI (Morocco, `.CS`)
- NGX_30 (Nigeria, `.LG`)
- NSE_20 (Kenya, `.NR`)

### MENA / GCC

- TASI (Saudi, `.SR`)
- DFMGI (Dubai, `.DU`)
- ADX (Abu Dhabi, `.AD`)
- QSI (Qatar, `.QA`)
- KWSE (Kuwait, `.KW`)
- BHSE (Bahrain, `.BH`)

### North America — non-SEC

- TSX_60, TSX_COMPOSITE (`.TO`)
- TSX_VENTURE_50 (`.V`)

## Traceability

- Source of truth for indices: `INDEX_CONFIGS` in
  [yahoo_global.py](../backend/xbrl_sec/sec/sources/yahoo_global.py).
- Source of truth for CLI: `run-yahoo-global` /
  `discover-yahoo-tickers` in
  [xbrl_sec/sec/cli.py](../backend/xbrl_sec/sec/cli.py).
- Source of truth for pipeline catalog: `yahoo_global.*` commands in
  [backend/api/pipeline_catalog.py](../backend/api/pipeline_catalog.py).
- Tests: [backend/xbrl_sec/sec/tests/test_yahoo_global.py](../backend/xbrl_sec/sec/tests/test_yahoo_global.py).
