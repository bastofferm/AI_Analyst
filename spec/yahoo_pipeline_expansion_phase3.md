# Yahoo pipeline expansion — Phase 3 spec (wholesale exchange mode)

Author: assistant (bastian.offermann@protonmail.com)
Date: 2026-07-07
Parent spec: [yahoo_pipeline_expansion.md](yahoo_pipeline_expansion.md)

## Goal

Ship the wholesale-exchange leg of the expansion — the piece the parent
spec called out as "explicitly not shipped" in the first pass. Wholesale
mode broadens `dim_company_intl` from a union of ~130 curated indices to
the full listed population of six large exchanges. This is the path from
the current ~10k universe to the 25k+ upper bound in the parent spec.

## Non-goals

- Point-in-time delisting handling. Wholesale seeds run when discovery
  runs; a delisted name will fail the next fundamentals fetch and be
  visible in the new health MV. No proactive backfill of historical
  status.
- Perfect coverage per exchange. Wholesale sources should return
  "listed today, main board" rather than every warrant / cross-listing /
  ETF. Adapters filter noise; residual noise falls out through the
  validator.
- Real-time refresh. Wholesale is a weekly cadence at most; daily runs
  should target `pipeline_sample_group in ('core_index','wide_index')`.

## Design

### New source adapters

1. **`YahooScreenerSource`** — walks Yahoo's public screener endpoint
   `query1.finance.yahoo.com/v1/finance/screener` with a POST body
   filtering by `region` and `exchange`. Paginates 25 rows/page up to
   a hard cap (`YahooScreenerSource.MAX_ROWS = 5000` per index). Feeds
   the wholesale indices for exchanges we cannot get free master-lists
   for.

2. **`HttpJsonSource`** — generic adapter that GETs a JSON document,
   walks a dotted path to reach an array of rows, and pulls
   `ticker`/`symbol` (+ optional `name`) fields from each row.
   Configurable at the index-config level via a small
   `HttpJsonSourceSpec` dataclass. Feeds exchanges that publish public
   JSON directories: SGX, BSE India.

Both new adapters implement the `DiscoverySource` protocol added in
Phase 2, so `discover_index` already knows how to union+dedupe their
output alongside Wikipedia/Stooq/fallback.

### New per-config extension points

Add three fields to `YahooIndexConfig`:

```python
pipeline_sample_group: str | None = None   # e.g. "core_index" | "exchange_wholesale"
screener_spec: YahooScreenerSpec | None = None
http_json_spec: HttpJsonSourceSpec | None = None
```

- `pipeline_sample_group` flows into `dim_company_intl.pipeline_sample_group`
  on upsert so fundamentals runs can filter by group.
- `screener_spec` = `YahooScreenerSpec(region: str, exchanges: tuple[str, ...])`
- `http_json_spec` = `HttpJsonSourceSpec(url: str, rows_path: tuple[str, ...],
  ticker_key: str, name_key: str | None = None)`

### Wholesale indices to seed

| Index code | Exchange | Adapter |
| --- | --- | --- |
| `LSE_ALL` | London main + AIM | YahooScreener (region=gb) |
| `TSX_ALL` | Toronto | YahooScreener (region=ca) |
| `JSE_ALL` | Johannesburg | YahooScreener (region=za) |
| `KRX_ALL` | KOSPI + KOSDAQ | YahooScreener (region=kr) |
| `SGX_ALL` | Singapore | HttpJsonSource (api.sgx.com/securities) |
| `BSE_ALL` | Bombay | HttpJsonSource (api.bseindia.com scrip codes) |

All wholesale configs get `pipeline_sample_group="exchange_wholesale"`.
Each yields ~1k-5k tickers pre-validation, ~700-4k post-validation.

### Gating

- `discover-yahoo-tickers` gains `--include-wholesale` and the default
  scope excludes wholesale.
- `resolve_index_configs` gains `include_wholesale: bool = False`. When
  the caller passes explicit index codes, wholesale codes still resolve
  (explicit opt-in wins). When the caller passes `None` (default = all),
  wholesale is included only if the flag is set.
- `fetch-yahoo-fundamentals` gains `--sample-group` filter (already
  wired for wholesale scoping via `pipeline_sample_group`).

### Health tracking

New migration `130_yahoo_ticker_health.sql`:

```sql
CREATE MATERIALIZED VIEW mv_yahoo_ticker_health AS
SELECT
    d.intl_company_id,
    d.primary_ticker,
    d.country_code,
    d.exchange_suffix,
    d.pipeline_sample_group,
    COUNT(*) FILTER (WHERE psr.status = 'failed') AS failed_runs,
    COUNT(*) FILTER (WHERE psr.status = 'ok') AS ok_runs,
    MAX(psr.finished_at) AS last_run_at
FROM dim_company_intl d
LEFT JOIN pipeline_stage_run_item psr
    ON psr.source_name = 'yahoo_global_fundamentals'
   AND psr.item_key = d.primary_ticker
GROUP BY d.intl_company_id, d.primary_ticker, d.country_code,
         d.exchange_suffix, d.pipeline_sample_group;
```

Refresh weekly. Rows with `failed_runs >= 5 AND ok_runs = 0` are
candidates for `include_in_pipeline=FALSE`; deactivation stays manual
in this phase (autoprune is Phase 4/5 material and is documented in the
parent spec).

## Expected yield

- 6 wholesale indices × ~1k-4k tickers pre-validation = ~10k-20k raw.
- After validation and dedupe against existing index membership: ~+6k to
  +12k unique companies added to `dim_company_intl`.
- Combined with Phase 1 (already shipped, 133 curated indices), the
  total validated universe lands in the 15k-25k range — inside the
  parent spec's 10x target.

## Verification

- Adapter unit tests: `YahooScreenerSource` pagination, empty page,
  `HttpJsonSource` dotted-path extraction, missing key handling.
- Config hygiene: wholesale configs all have `pipeline_sample_group`
  set; `resolve_index_configs(include_wholesale=False)` excludes them.
- End-to-end smoke (offline, mocked HTTP): `discover_index` union across
  wholesale + Wikipedia returns tickers from both and dedupes on
  `primary_ticker`.

## Out of scope for this phase (documented for next pass)

- Autoprune from `mv_yahoo_ticker_health`.
- LSE, TSX, JSE bespoke master-list adapters (they use POST/scraping,
  which is more brittle than the YahooScreener path we take here).
- Delisting reconciliation.
