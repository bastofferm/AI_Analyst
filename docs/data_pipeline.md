# Data acquisition & warehouse setup

This app itself never writes to the database — the FastAPI backend
(`backend/api/`) only reads from Postgres. But the code that **builds and
updates** the `xbrl_sec` warehouse the app reads from is carried into this
repo too, under `backend/xbrl_sec/`. This document covers how to stand up an
empty warehouse and how to run the ingestion pipeline that fills it.

> **This tooling is manual CLI only.** There is no cron job, Windows Task
> Scheduler entry, or GitHub Action anywhere in this repo that runs it
> automatically — `docs/qlib_integration.md`'s mention of "the existing
> APScheduler dependency" is aspirational; `apscheduler` isn't in
> `backend/requirements.txt` and isn't installed. Every ingestion command
> below is something a person runs from a terminal.

## 1. Create the database schema

The whole schema is defined as 130+ sequential, idempotent SQL migration
files in [`backend/xbrl_sec/sec/sql/`](../backend/xbrl_sec/sec/sql/)
(`001_initial_schema.sql` → `133_noncurrent_asset_mapping_aliases.sql`).
Each file guards its DDL with `IF NOT EXISTS`, so re-running the whole set
against an already-current database is safe and a normal way to pick up new
migrations.

Apply them with the bundled runner:

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python -m xbrl_sec.sec.cli apply-schema
```

(equivalently `python -m xbrl_sec.sec.scripts.apply_schema`, which is what
`apply-schema` calls). It connects with `XBRL_SEC_DATABASE_URL` /
`XBRL_SEC_SCHEMA` (see `.env.example`), runs every `sql/*.sql` file in
lexical order in its own transaction, and prints `applied <file>` or
`FAILED <file>: <error>` per file — a failure in one migration doesn't abort
the rest, so check the summary line at the end for `failed=0`.

This only creates tables/columns/indexes — it does not load any data.

**Note on `MZQA_SKIP_SCHEMA`:** `.env.example` and `README.md` set
`MZQA_SKIP_SCHEMA=1`, a convention carried over from the parent MZQA
monorepo (where the terminal's own startup path checks this flag before
auto-applying schema). Nothing in this repo actually reads that variable —
`backend/api/main.py`'s startup (`lifespan()`) never calls `apply_schema()`
at all, so this standalone app never touches schema regardless of the flag.
Setting/unsetting it here is a no-op; it's kept only as documentation intent
inherited from the monorepo. Schema management is always the manual step
above.

## 2. Populate the warehouse

Data acquisition lives under `backend/xbrl_sec/sec/` (US/JP fundamentals,
prices, MD&A, insider/13F, news, macro/factor data) and
`backend/xbrl_sec/etf/` (ETF holdings/prices), and is driven by two CLIs.
All commands assume:

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python -m xbrl_sec.sec.cli <subcommand> [args]
.\.venv\Scripts\python -m xbrl_sec.etf.cli <subcommand> [args]
```

Run `... cli --help` (or `<subcommand> --help`) to see the current, full
flag list — it's the source of truth; what follows is a map of what each
group of commands does and which tables it writes, not a complete reference.

### US / JP company fundamentals (SEC EDGAR / EDINET XBRL)

The core pipeline, per jurisdiction, runs as an ordered set of stages
(`backend/xbrl_sec/sec/pipelines/us.py` / `jp.py`):

```
master → companyfacts/zip download → xbrl package download → filing index
  → linkbase extract → raw parse → standardize → ticker map → metrics
  → recon → validate
```

```powershell
# One-shot: run every stage for US (add --download to also fetch what's missing)
python -m xbrl_sec.sec.cli run US --download

# Same for Japan
python -m xbrl_sec.sec.cli run JP --download

# Just refresh the company master list (new listings/delistings) without a full run
python -m xbrl_sec.sec.cli refresh-master US --download

# Scope a run to specific entities (US: CIKs) instead of the whole universe
python -m xbrl_sec.sec.cli run US --entity 0000320193 --download
```

Individual stages (`download`, `download-raw-sec`, `download-xbrl`,
`extract`, `parse`, `standardize`, `metrics`, `recon`, `validate`,
`index-api`, `index`) can also be run one at a time — useful for resuming a
partial run or re-processing after a code fix. Writes go to
`dim_company_us`/`dim_company_jp`, `fact_fundamentals_us`/`_jp`,
`fact_fundamentals_std_us`/`_jp`, `fact_metrics_us`/`_jp`,
`fact_metrics_recon_us`/`_jp`, `source_filing_state`, `ref_entity_ticker`.

Requires `SEC_HTTP_USER_AGENT` (SEC EDGAR requires an identifying user
agent) and `EDINET_API_KEY` for JP.

### International equities (Yahoo Finance)

`backend/xbrl_sec/sec/sources/yahoo_global.py` discovers and ingests
non-US/JP equities (index constituents scraped from Wikipedia + Yahoo's
screener, then fundamentals/prices via `yfinance`):

```powershell
python -m xbrl_sec.sec.cli discover-yahoo-tickers   # populate dim_company_intl, ref_yahoo_index*
python -m xbrl_sec.sec.cli fetch-yahoo-fundamentals  # -> fact_yahoo_fundamental_metric, fact_yahoo_statement_item
python -m xbrl_sec.sec.cli fetch-yahoo-prices        # -> fact_prices_intl
python -m xbrl_sec.sec.cli compute-intl-metrics      # -> fact_metrics_intl
python -m xbrl_sec.sec.cli ingest-fx-intl            # -> fact_fx (historical) / refresh-intl-fx for latest only
python -m xbrl_sec.sec.cli backfill-intl-gics        # fill GICS codes from stored sector/industry strings, no network
python -m xbrl_sec.sec.cli health-yahoo-global       # coverage/QA report (source of the output/yahoo_* files)
python -m xbrl_sec.sec.cli run-yahoo-global          # runs discovery -> fundamentals -> prices -> metrics in sequence
```

`output/yahoo_debug/`, `output/yahoo_missing_fundamentals.txt`, and
`output/yahoo_intl_no_metric_rows.csv` in this checkout are QA artifacts
from a past `health-yahoo-global`/`run-yahoo-global` run, not fixtures —
they're evidence this pipeline has actually executed against the shared
warehouse. `spec/yahoo_pipeline_expansion.md` and `_phase3.md` describe
**planned** work (broader index coverage, a scheduler-friendly
`refresh_before_days` skip, concurrency) that has not been built yet —
today's coverage and cadence are whatever the commands above were last run
with, by hand.

### US price history + splits (all US tickers, yfinance)

```powershell
python -m xbrl_sec.sec.cli fetch-prices US            # -> fact_prices_us / stage_prices
python -m xbrl_sec.sec.cli fetch-stock-splits US       # -> fact_stock_split_event
python -m xbrl_sec.sec.cli derive-market-items US      # -> fact_market_metrics (betas, etc.)
```

### MD&A section extraction (the `fact_mda_sections_us`/`_jp` behind the
app's sentiment coverage)

```powershell
python -m xbrl_sec.sec.cli mda discover --jurisdiction US
python -m xbrl_sec.sec.cli mda extract --jurisdiction US
python -m xbrl_sec.sec.cli mda ingest --jurisdiction US   # LLM-scored -> fact_mda_sections_us
python -m xbrl_sec.sec.cli mda status --jurisdiction US   # coverage report
```

Same `action`s with `--jurisdiction JP`. `ingest` calls an LLM (`--model`,
default `deepseek-chat`) to score/summarize each section, so it needs the
matching provider key.

### Institutional (13F) and insider (Form 3/4/5) filings

```powershell
python -m xbrl_sec.sec.cli inst run --from-year 2013     # -> fact_institutional_narrative, dim_13f_manager
python -m xbrl_sec.sec.cli insider run                    # -> fact_insider_filing, fact_insider_transaction_*
```

`inst` has many actions (discovery, CUSIP/ticker matching via LLM or
OpenFIGI, price backfill) — see `python -m xbrl_sec.sec.cli inst --help`.

### News + sentiment

```powershell
python -m xbrl_sec.sec.news.cli ingest --feed <name>   # -> news.articles, news.ingestion_runs
python -m xbrl_sec.sec.news.cli score                   # -> news.sentiment_scores
python -m xbrl_sec.sec.news.cli watchlist add AAPL --market US
```

Sentiment scoring uses a local Ollama model by default
(`MZQA_NEWS_REASONING_BACKEND=qwen_ollama`, `MZQA_NEWS_OLLAMA_URL`) or
DeepSeek (`--backend deepseek`).

### Macro / factor data

```powershell
python -m xbrl_sec.sec.cli fetch-fred              # -> fact_macro_fred   (needs FRED_API_KEY)
python -m xbrl_sec.sec.cli fetch-cross-asset        # -> fact_cross_asset
python -m xbrl_sec.sec.cli fetch-fama-french        # -> fact_fama_french
python -m xbrl_sec.sec.cli compute-factor-model      # -> fact_factor_loadings, fact_factor_reg_meta
python -m xbrl_sec.sec.cli cycle <action>            # HMM/VAE macro-regime model -> fact_cycle_*
```

A number of narrower central-bank/statistics-office feeds exist as
standalone modules under `backend/xbrl_sec/sec/sources/`
(`boj_ingest.py`, `ecb_ingest.py`, `snb_ingest.py`, `mof_jgb_ingest.py`,
`cao_jp_ingest.py`, `meti_jp_ingest.py`, `phillyfed_ingest.py`,
`nyfed_ingest.py`, `statjp_dbnomics_ingest.py`, `cepr_ecoin_ingest.py`,
`euroframe_ingest.py`, `aqr_factors_ingest.py`, `eightk_ingest.py`,
`us_monthly_buybacks.py`) — most aren't wired into `cli.py` as subcommands
and would be run as `python -m xbrl_sec.sec.sources.<module>` directly if
needed.

### ETFs

Separate CLI, same connection settings:

```powershell
python -m xbrl_sec.etf.cli run       # firds -> xetra -> prices, the standard refresh
python -m xbrl_sec.etf.cli status    # print table row counts
```

Individual stages (`firds`, `xetra`, `profile`, `bond-ratings`, `factors`,
`prices`, `holdings`, `providers`, `justetf-metadata`, `etf-yahoo-resolve`,
`etf-yahoo-promote(-best)`) write to `sec.dim_etf*`, `sec.etf_*`,
`sec.fact_prices_etf`, `sec.fact_etf_factor_loadings`.

## 3. A from-scratch bootstrap, in order

For a brand-new warehouse, a reasonable order is:

```powershell
$env:PYTHONPATH = "$PWD\backend"

# 1. Schema
python -m xbrl_sec.sec.cli apply-schema

# 2. Reference/taxonomy tables the standardizers depend on
python -m xbrl_sec.sec.cli sync-refs
python -m xbrl_sec.sec.cli sync-registry
python -m xbrl_sec.sec.cli load-taxonomy-all-years

# 3. Core fundamentals (slow — full SEC/EDINET history)
python -m xbrl_sec.sec.cli run US --download --full
python -m xbrl_sec.sec.cli run JP --download --full

# 4. Prices
python -m xbrl_sec.sec.cli fetch-prices US
python -m xbrl_sec.sec.cli fetch-stock-splits US

# 5. International equities
python -m xbrl_sec.sec.cli run-yahoo-global

# 6. Everything else as needed: mda, inst, insider, news, fetch-fred,
#    fetch-fama-french, compute-factor-model, cycle, etf.cli run
```

A `--full` US/JP run downloads and parses the entire filing history and can
take hours; day-to-day refreshes should use `run <jurisdiction> --download`
(incremental, driven by each source's watermark/lookback settings —
`XBRL_SEC_US_LOOKBACK_DAYS`, `us_daily_index_overlap_days`, etc. in
`backend/xbrl_sec/sec/settings.py`) instead of `--full`.

## 4. Keeping it up to date

There's no scheduler in this repo, so "keeping it up to date" today means
re-running the relevant commands from §2 by hand or from your own cron /
Task Scheduler job, e.g.:

```powershell
# Nightly: incremental US/JP refresh + prices
python -m xbrl_sec.sec.cli run US --download
python -m xbrl_sec.sec.cli run JP --download
python -m xbrl_sec.sec.cli fetch-prices US

# Weekly: international equities + ETFs
python -m xbrl_sec.sec.cli run-yahoo-global
python -m xbrl_sec.etf.cli run
```

If you do wire this into Task Scheduler/cron, redirect stdout (the CLI
prints per-item progress) to a log file and check exit status — commands
generally continue past individual item failures (same pattern as
`apply-schema`) and rely on the printed summary/`status`/`health-*`
subcommands to reveal gaps, rather than a non-zero exit code.

## 5. Required environment variables

Beyond the DB connection (`XBRL_SEC_DATABASE_URL`, `XBRL_SEC_SCHEMA` — see
`.env.example`), individual sources need their own credentials:

| Variable | Used by |
|---|---|
| `SEC_HTTP_USER_AGENT` | SEC EDGAR downloads (required — SEC blocks unidentified clients) |
| `EDINET_API_KEY` | JP EDINET downloads |
| `FRED_API_KEY` | `fetch-fred` |
| `OPENFIGI_API_KEY` / `OPEN_FIGI_API_KEY` | 13F CUSIP→ticker matching |
| `MOODYS_API_KEY`, `SP_API_KEY`, `FITCH_API_KEY` | ETF bond credit-quality ratings |
| `DEEPSEEK_API_KEY` (or another provider key) | MD&A scoring, 13F LLM matching, news sentiment |
| `MZQA_NEWS_OLLAMA_URL` | news sentiment if using the local Ollama backend (default) |

**`MZQA_ROOT`** (`backend/xbrl_sec/sec/settings.py:52`) defaults to a
machine-specific path, `C:\Users\Bastian Offermann\Desktop\MZQA` — a
leftover from the parent monorepo this app was carved out of. It backs
`project_root`, used for cache/scratch file locations by a few sources. Set
it explicitly (e.g. to this repo's root) if you hit a `FileNotFoundError`
pointing at that path when running ingestion on a different machine.

## 6. Why the running app can't do any of this

`backend/api/main.py` only mounts the read-oriented routers (`meta`,
`sector`, `prices`, `kpis`, `company`, `fx`, `screener`, `screener_agent`,
`ai_committee`, `ai_committee_group`, `llm_meta`, `quant`). An HTTP wrapper
around this whole CLI exists — `backend/api/routers/pipeline.py`, backed by
the command catalog in `backend/api/pipeline_catalog.py` — but it is **not
imported in `main.py`**, so it isn't reachable while the app is running; it,
and ~25 other unmounted routers (`pipeline_approval`, `mda`, `institutional`,
`insider`, `etf`, `macro`, `auth`, `billing`, …), are inherited surface from
the parent terminal that this standalone app doesn't expose. Treat the
commands in this document as separate, manually-invoked maintenance of the
shared warehouse — not something the AI_Analyst web app itself triggers.
