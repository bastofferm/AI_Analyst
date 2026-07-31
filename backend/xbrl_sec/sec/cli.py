"""Command line entrypoint for the MZQA XBRL data layer."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.scripts.apply_schema import apply_schema
from xbrl_sec.sec.pipelines import jp, us
from xbrl_sec.sec.sources.reference_sync import sync_reference_tables
from xbrl_sec.sec.sources.registry_sync import sync_registry
from xbrl_sec.sec.sources.concept_universe import build_concept_universe, unmapped_concepts_json
from xbrl_sec.sec.sources.mapping_suggestions import (
    build_mapping_review_queue,
    review_queue_summary_json,
)
from xbrl_sec.sec.sources.concept_health import build_concept_health_review_queue
from xbrl_sec.sec.sources.concept_target_policy import build_concept_target_display_policy
from xbrl_sec.sec.sources.mapping_health import build_mapping_health_review_queue
from xbrl_sec.sec.sources.taxonomy_ingestion import ingest_all_years_with_connect
from xbrl_sec.sec.sources.llm_reranking import run_one_time_reranking
from xbrl_sec.sec.sources.review_promotion import promote_review_queue_rows
from xbrl_sec.sec.sources.market_sync import sync_metric_support
from xbrl_sec.sec.sources.yfinance_ingest import (
    fetch_prices as fetch_prices_yf,
    _active_us_tickers,
    _active_jp_tickers,
    fetch_stock_splits,
    derive_market_metrics,
    compute_betas,
)
from xbrl_sec.sec.sources.yahoo_global import (
    backfill_intl_gics,
    health_check as yahoo_global_health_check,
    ingest_fx_intl,
    run_discovery as run_yahoo_global_discovery,
    run_fundamentals as run_yahoo_global_fundamentals,
    run_global as run_yahoo_global,
    run_prices as run_yahoo_global_prices,
)
from xbrl_sec.sec.sources.yahoo_identifier_enrichment import (
    apply_accepted_evidence as apply_13f_yahoo_identifier_evidence,
    download_13f_yahoo_prices,
    promote_price_covered_evidence as promote_13f_yahoo_price_evidence,
    run_enrichment as enrich_13f_yahoo_identifiers,
    DEFAULT_EDGE_BINARY_PATH,
)
from xbrl_sec.sec.sources.openfigi_identifier_enrichment import (
    apply_accepted_evidence as apply_13f_openfigi_identifier_evidence,
    run_enrichment as enrich_13f_openfigi_identifiers,
)
from xbrl_sec.sec.sources.fred_ingest import fetch_fred
from xbrl_sec.sec.sources.cross_asset_ingest import fetch_cross_asset
from xbrl_sec.sec.sources.fama_french_ingest import cleanup_fama_french_storage, fetch_fama_french
from xbrl_sec.sec.sources.factor_model import compute_factor_model, compute_factor_model_parallel
from xbrl_sec.sec.sources.master_sync import sync_master_dimensions
from xbrl_sec.sec.sources.company_enrichment import enrich_company_master
from xbrl_sec.sec.cycle.features import build_cycle_features
from xbrl_sec.sec.cycle.baselines import train_baseline
from xbrl_sec.sec.cycle.hmm import train_hmm
from xbrl_sec.sec.cycle.ic import compute_regime_factor_ic
from xbrl_sec.sec.cycle.vae import train_vae
from xbrl_sec.sec.cycle.validation import score_cycle_state, validate_cycle_model
from xbrl_sec.sec.sources.statement_display_evidence import (
    build_us_operating_cost_evidence,
    operating_cost_audit_summary_json,
)
from xbrl_sec.sec.sources.filing_statement_display import (
    build_aar_filing_statement_display,
)
from xbrl_sec.sec.sources.llm_raw_filing_display import (
    build_llm_raw_filing_display,
    build_llm_raw_filing_display_for_available_universe,
)
from xbrl_sec.sec.sources.supplemental_text_evidence import (
    build_us_supplemental_text_evidence,
    supplemental_text_evidence_summary_json,
)
from xbrl_sec.sec.state.store import reset_downstream
from xbrl_sec.sec.std.jp_standardize import populate_jp_std
from xbrl_sec.sec.std.us_standardize import populate_us_std
from xbrl_sec.sec.quality.validate import validation_json
from xbrl_sec.sec.metrics.compute import compute_metrics
from xbrl_sec.sec.metrics.recon import build_recon
from xbrl_sec.sec.mda.pipeline import (
    discover as mda_discover,
    extract as mda_extract,
    ingest as mda_ingest,
    reextract_html as mda_reextract_html,
    status as mda_status,
)
from xbrl_sec.sec.mda.jp_pipeline import (
    discover as mda_discover_jp,
    extract as mda_extract_jp,
    ingest as mda_ingest_jp,
    status as mda_status_jp,
)
from xbrl_sec.sec.mda.intelligence import (
    keywords as mda_keywords,
    summarize as mda_summarize,
)
from xbrl_sec.sec.inst import pipeline as inst_pipeline
from xbrl_sec.sec.inst import classifier as inst_classifier
from xbrl_sec.sec.inst import lean_core as inst_lean_core
from xbrl_sec.sec.insider import pipeline as insider_pipeline
from xbrl_sec.sec.statements.compare import (
    compare_golden_statements_json,
    compare_golden_statements_review,
    compare_golden_statements_summary,
)
from xbrl_sec.sec.news.cli import configure_parser as configure_news_parser
from xbrl_sec.sec.news.cli import run as run_news_cli


def _date_arg(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _modalities_arg(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    allowed = {"macro", "market", "fundamental", "text_optional", "label_anchor"}
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    bad = sorted(set(parts) - allowed)
    if bad:
        raise argparse.ArgumentTypeError(f"Unsupported modalities: {', '.join(bad)}")
    return parts or None


_REVIEW_CLASS_CHOICES = [
    "map_candidate",
    "special_case_review",
    "likely_exclude",
    "mapped_anomaly",
    "mapped_clean",
    "unmapped_candidate",
    "audit_only",
    "display_suppressed_candidate",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="MZQA xbrl_sec.sec data layer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("apply-schema")

    reset = sub.add_parser("reset")
    reset.add_argument("jurisdiction", choices=["US", "JP"])
    reset.add_argument("--entity", action="append", default=None)

    scope = sub.add_parser("scope")
    scope.add_argument("jurisdiction", choices=["US", "JP"])
    scope.add_argument("--group", default="pilot_50_us")
    scope.add_argument("--target", type=int, default=50)
    scope.add_argument("--all-eligible", action="store_true")

    reparse = sub.add_parser("reparse")
    reparse.add_argument("jurisdiction", choices=["US", "JP"])
    reparse.add_argument("--entity", action="append", default=None)
    reparse.add_argument("--annual-10k-current-only", dest="annual_10k_current_only", action="store_true", default=True)
    reparse.add_argument("--include-comparatives", dest="annual_10k_current_only", action="store_false")
    reparse.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for US SEC forms or JP EDINET document type codes.")
    reparse.add_argument("--filed-date-max", default=None, help="JP only: exclude local XBRL filings filed after this YYYY-MM-DD date.")
    reparse.add_argument("--allow-global-reset", action="store_true", help="Allow unsafe full-jurisdiction JP reparse; prefer rebuild-local JP.")

    run = sub.add_parser("run")
    run.add_argument("jurisdiction", choices=["US", "JP"])
    run.add_argument("--download", action="store_true")
    run.add_argument("--full", action="store_true")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--max-ciks", type=int, default=None)
    run.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for US SEC forms or JP EDINET document type codes.")
    run.add_argument("--filed-date-max", default=None, help="JP only: cap EDINET indexing and local XBRL parsing at this YYYY-MM-DD date.")
    run.add_argument("--entity", action="append", default=None, help="US only: scope companyfacts refresh + parse to these CIKs.")
    run.add_argument("--since", default=None, help="US only: daily-index start date (YYYY-MM-DD) for companyfacts refresh.")
    run.add_argument("--lookback-days", type=int, default=None, help="US only: floor lookback (days) when no watermark is available.")

    master = sub.add_parser("refresh-master")
    master.add_argument("jurisdiction", choices=["US", "JP"])
    master.add_argument("--download", action="store_true")
    master.add_argument("--full", action="store_true")
    master.add_argument("--days", type=int, default=400)
    master.add_argument("--start-date", default=None)
    master.add_argument("--end-date", default=None)
    master.add_argument("--max-ciks", type=int, default=None)

    master_only = sub.add_parser("master-only")
    master_only.add_argument("jurisdiction", choices=["JP"])
    master_only.add_argument("--full", action="store_true")
    master_only.add_argument("--days", type=int, default=400)
    master_only.add_argument("--start-date", default=None)
    master_only.add_argument("--end-date", default=None)
    master_only.add_argument("--max-tickers", type=int, default=None)

    rebuild_local = sub.add_parser("rebuild-local")
    rebuild_local.add_argument("jurisdiction", choices=["JP"])
    rebuild_local.add_argument("--entity", action="append", default=None)
    rebuild_local.add_argument("--filed-date-max", default=None)
    rebuild_local.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for JP EDINET document type codes.")
    rebuild_local.add_argument("--chunk-size", type=int, default=25)
    rebuild_local.add_argument("--no-resume", dest="resume", action="store_false", default=True)
    rebuild_local.add_argument("--no-downstream", dest="downstream", action="store_false", default=True)
    rebuild_local.add_argument("--sync-index", action="store_true")
    rebuild_local.add_argument("--dry-run", action="store_true")

    download = sub.add_parser("download")
    download.add_argument("jurisdiction", choices=["US", "JP"])
    download.add_argument("--force", action="store_true")
    download.add_argument("--limit", type=int, default=None)
    download.add_argument("--doc", action="append", default=None)
    download.add_argument("--max-ciks", type=int, default=None)
    download.add_argument("--entity", action="append", default=None, help="US only: scope companyfacts refresh to these CIKs.")
    download.add_argument("--since", default=None, help="US only: daily-index start date (YYYY-MM-DD) for companyfacts refresh.")
    download.add_argument("--lookback-days", type=int, default=None, help="US only: floor lookback (days) when no watermark is available.")
    download.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for US SEC forms or JP EDINET document type codes.")

    download_raw_sec = sub.add_parser("download-raw-sec")
    download_raw_sec.add_argument("--force", action="store_true")
    download_raw_sec.add_argument("--limit", type=int, default=None, help="Limit XBRL ZIP download candidates after indexing.")
    download_raw_sec.add_argument("--max-ciks", type=int, default=None)
    download_raw_sec.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for 10-K, 10-K/A, 10-Q, 10-Q/A.")

    download_xbrl = sub.add_parser("download-xbrl")
    download_xbrl.add_argument("jurisdiction", choices=["US"])
    download_xbrl.add_argument("--entity", action="append", default=None)
    download_xbrl.add_argument("--force", action="store_true")
    download_xbrl.add_argument("--limit", type=int, default=None)
    download_xbrl.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for 10-K, 10-K/A, 10-Q, 10-Q/A.")

    extract_html = sub.add_parser("extract-html")
    extract_html.add_argument("--entity", action="append", default=None)
    extract_html.add_argument("--force", action="store_true")

    mda = sub.add_parser("mda")
    mda.add_argument("action", choices=["discover", "extract", "ingest", "status", "keywords", "summarize"])
    mda.add_argument("--jurisdiction", choices=["US", "JP"], default="US")
    mda.add_argument("--cik", default=None)
    mda.add_argument("--edinet-code", default=None)
    mda.add_argument("--years", type=int, default=None)
    mda.add_argument("--forms", default=None)
    mda.add_argument("--limit", type=int, default=None)
    mda.add_argument("--include-item-7a", dest="include_item_7a", action="store_true", default=True)
    mda.add_argument("--skip-item-7a", dest="include_item_7a", action="store_false")
    mda.add_argument("--retry-dirty", action="store_true")
    mda.add_argument("--filing-id", default=None)
    mda.add_argument("--section-id", default=None)
    mda.add_argument("--model", default="deepseek-chat")

    inst = sub.add_parser("inst")
    inst.add_argument("action", choices=[
        "discover-13f", "register-local-13f", "download-13f", "parse-13f", "resolve-13f", "resolve-13f-managers", "run-13f",
        "standardize-13f", "resolve-13f-securities", "report-13f-cusip-gaps", "compare-13f-cusip-llm", "promote-13f-cusip-llm",
        "enrich-13f-yahoo-identifiers", "apply-13f-yahoo-identifier-evidence",
        "enrich-13f-openfigi-identifiers", "apply-13f-openfigi-identifier-evidence",
        "promote-13f-yahoo-price-evidence", "download-13f-yahoo-prices",
        "compute-13f-manager-period", "classify-13f-core", "classify-13f-core-llm", "recon-13f", "run-13f-full",
        "import-manager-style-reference", "link-manager-style-reference", "classify-13f-managers",
        "discover-13dg", "download-13dg", "parse-13dg", "run-13dg", "status",
    ])
    inst.add_argument("--quarter", default=None)
    inst.add_argument("--from-year", type=int, default=2013)
    inst.add_argument("--manager", default=None)
    inst.add_argument("--cik", default=None)
    inst.add_argument("--force", action="store_true")
    inst.add_argument("--all-issuers", action="store_true")
    inst.add_argument("--all-issuer-universe", action="store_true")
    inst.add_argument("--use-llm", action="store_true")
    inst.add_argument("--deterministic-only", action="store_true")
    inst.add_argument("--reference-only", action="store_true")
    inst.add_argument("--limit", type=int, default=None)
    inst.add_argument("--row-limit", type=int, default=None)
    inst.add_argument("--min-confidence", type=float, default=0.85)
    inst.add_argument("--min-aum", type=float, default=None)
    inst.add_argument("--dry-run", action="store_true")
    inst.add_argument("--apply", action="store_true")
    inst.add_argument("--resume", action="store_true")
    inst.add_argument("--headless", dest="headless", action="store_true", default=True)
    inst.add_argument("--no-headless", dest="headless", action="store_false")
    inst.add_argument("--sleep-seconds", type=float, default=0.5)
    inst.add_argument("--edge-binary-path", default=DEFAULT_EDGE_BINARY_PATH)
    inst.add_argument("--driver-path", default=None)
    inst.add_argument("--max-symbols-per-query", type=int, default=3)
    inst.add_argument("--workers", type=int, default=4)
    inst.add_argument("--selenium-fallback", action="store_true")
    inst.add_argument("--batch-size", type=int, default=100)
    inst.add_argument("--price-period", default="5d")
    inst.add_argument("--start-date", default="2000-01-01")
    inst.add_argument("--end-date", default=None)
    inst.add_argument("--full", action="store_true")

    insider = sub.add_parser("insider")
    insider.add_argument("action", choices=["discover-local", "index-sync", "download", "parse", "run", "status"])
    insider.add_argument("--cik", default=None)
    insider.add_argument("--from-year", type=int, default=None)
    insider.add_argument("--force", action="store_true")
    insider.add_argument("--limit", type=int, default=None)

    index_api = sub.add_parser("index-api")
    index_api.add_argument("jurisdiction", choices=["US", "JP"])
    index_api.add_argument("--full", action="store_true")
    index_api.add_argument("--start-date", default=None)
    index_api.add_argument("--end-date", default=None)
    index_api.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for JP EDINET document type codes.")

    extract = sub.add_parser("extract")
    extract.add_argument("jurisdiction", choices=["US", "JP"])
    extract.add_argument("--entity", action="append", default=None)
    extract.add_argument("--doc", action="append", default=None)
    extract.add_argument("--force", action="store_true")
    extract.add_argument("--workers", type=int, default=1, help="US only: parallel XBRL ZIP extraction workers.")
    extract.add_argument(
        "--new-only",
        action="store_true",
        help="US only: skip ZIP stems that already have any extracted output.",
    )
    extract.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="JP only: repeat for EDINET document type codes.")

    index = sub.add_parser("index")
    index.add_argument("jurisdiction", choices=["US", "JP"])
    index.add_argument("--entity", action="append", default=None)
    index.add_argument("--doc", action="append", default=None)
    index.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="JP only: repeat for EDINET document type codes.")

    parse = sub.add_parser("parse")
    parse.add_argument("jurisdiction", choices=["US", "JP"])
    parse.add_argument("--entity", action="append", default=None)
    parse.add_argument("--doc", action="append", default=None)
    parse.add_argument("--force", action="store_true")
    parse.add_argument("--annual-10k-current-only", dest="annual_10k_current_only", action="store_true", default=True)
    parse.add_argument("--include-comparatives", dest="annual_10k_current_only", action="store_false")
    parse.add_argument("--filing-type", dest="filing_types", action="append", default=None, help="Repeat for US SEC forms or JP EDINET document type codes.")
    parse.add_argument("--filed-date-max", default=None, help="JP only: exclude local XBRL filings filed after this YYYY-MM-DD date.")

    sub.add_parser("sync-refs")

    sync_registry_cmd = sub.add_parser("sync-registry")
    sync_registry_cmd.add_argument("--path", default=None)
    sync_registry_cmd.add_argument("--no-formulas", action="store_true")

    concept_universe = sub.add_parser("concept-universe")
    concept_universe.add_argument("jurisdiction", choices=["US", "JP", "ALL"])
    concept_universe.add_argument("--entity", action="append", default=None)
    concept_universe.add_argument("--labels", action="store_true")

    unmapped = sub.add_parser("unmapped-concepts")
    unmapped.add_argument("jurisdiction", choices=["US", "JP"])
    unmapped.add_argument("--limit", type=int, default=100)

    review_queue = sub.add_parser("review-queue")
    review_queue.add_argument("jurisdiction", choices=["US", "JP"])
    review_queue.add_argument("--limit", type=int, default=None)
    review_queue.add_argument("--min-fact-count", type=int, default=1)
    review_queue.add_argument("--top-n", type=int, default=None, help="Deprecated compatibility option; deterministic candidate caps are ignored.")

    review_queue_summary = sub.add_parser("review-queue-summary")
    review_queue_summary.add_argument("jurisdiction", choices=["US", "JP"])

    mapping_health = sub.add_parser("mapping-health")
    mapping_health.add_argument("jurisdiction", choices=["US", "JP"])
    mapping_health.add_argument("--limit", type=int, default=None)
    mapping_health.add_argument("--dry-run", action="store_true")
    mapping_health.add_argument("--reset-existing", action="store_true")

    concept_health = sub.add_parser("concept-health")
    concept_health.add_argument("jurisdiction", choices=["US", "JP"])
    concept_health.add_argument("--limit", type=int, default=None)
    concept_health.add_argument("--min-fact-count", type=int, default=1)
    concept_health.add_argument("--dry-run", action="store_true")
    concept_health.add_argument("--reset-existing", action="store_true")

    concept_policy = sub.add_parser("concept-target-policy")
    concept_policy.add_argument("jurisdiction", choices=["US", "JP"])
    concept_policy.add_argument("--limit", type=int, default=None)
    concept_policy.add_argument("--dry-run", action="store_true")
    concept_policy.add_argument("--reset-existing", action="store_true")
    concept_policy.add_argument("--no-mapped-clean", dest="include_mapped_clean", action="store_false", default=True)

    tax_all_years = sub.add_parser("load-taxonomy-all-years")
    tax_all_years.add_argument("--spec-dir", default=None)
    tax_all_years.add_argument("--no-enrich-observations", action="store_true")

    rerank_mappings = sub.add_parser("rerank-mappings")
    rerank_mappings.add_argument("jurisdiction", choices=["US", "JP"])
    rerank_mappings.add_argument("--limit", type=int, default=None)
    rerank_mappings.add_argument("--dry-run", action="store_true")
    rerank_mappings.add_argument("--namespace-prefix", default=None)
    rerank_mappings.add_argument("--review-class", choices=_REVIEW_CLASS_CHOICES, default=None)
    rerank_mappings.add_argument("--min-fact-count", type=int, default=None)
    rerank_mappings.add_argument("--queue-modulus", type=int, default=None)
    rerank_mappings.add_argument("--queue-remainder", type=int, default=None)

    promote_versioned = sub.add_parser("promote-versioned")
    promote_versioned.add_argument("jurisdiction", choices=["US", "JP"])
    promote_versioned.add_argument("--limit", type=int, default=None)
    promote_versioned.add_argument("--dry-run", action="store_true")
    promote_versioned.add_argument("--namespace-prefix", default=None)
    promote_versioned.add_argument("--review-class", choices=_REVIEW_CLASS_CHOICES, default=None)
    promote_versioned.add_argument("--min-fact-count", type=int, default=None)
    promote_versioned.add_argument("--min-confidence", type=float, default=0.9)
    promote_versioned.add_argument("--decision", default="READY_FOR_REVIEW")
    promote_versioned.add_argument("--approved-by", default=None)

    sync_master = sub.add_parser("sync-master")
    sync_master.add_argument("jurisdiction", choices=["US", "JP", "ALL"])

    enrich_master = sub.add_parser("enrich-master")
    enrich_master.add_argument("jurisdiction", choices=["US", "JP"])
    enrich_master.add_argument("--full", action="store_true")
    enrich_master.add_argument("--max-tickers", type=int, default=None)
    enrich_master.add_argument("--isin", action="store_true")
    enrich_master.add_argument("--gics", action="store_true")
    enrich_master.add_argument("--identity", action="store_true")

    fetch_prices_cmd = sub.add_parser("fetch-prices")
    fetch_prices_cmd.add_argument("--ticker", action="append", default=None)
    fetch_prices_cmd.add_argument("--start-date", default=None)
    fetch_prices_cmd.add_argument("--end-date", default=None)
    fetch_prices_cmd.add_argument("--full", action="store_true")
    # Default: fetch BOTH US and JP. Either flag suppresses that jurisdiction.
    fetch_prices_cmd.add_argument(
        "--skip-us", dest="skip_us", action="store_true",
        help="Skip the US fetch (default: fetch both US and JP)",
    )
    fetch_prices_cmd.add_argument(
        "--skip-jp", dest="skip_jp", action="store_true",
        help="Skip the JP fetch (default: fetch both US and JP)",
    )

    fetch_splits_cmd = sub.add_parser("fetch-stock-splits")
    fetch_splits_cmd.add_argument("--jurisdiction", choices=["US", "JP", "ALL"], default="US")
    fetch_splits_cmd.add_argument("--ticker", action="append", default=None)
    fetch_splits_cmd.add_argument("--start-date", default="2008-01-01")
    fetch_splits_cmd.add_argument("--full", action="store_true")

    derive_market_cmd = sub.add_parser("derive-market-items")
    derive_market_cmd.add_argument("--ticker", action="append", default=None)
    derive_market_cmd.add_argument("--full", action="store_true")
    derive_market_cmd.add_argument("--betas", action="store_true")

    discover_yahoo_cmd = sub.add_parser("discover-yahoo-tickers")
    discover_yahoo_cmd.add_argument("--index", action="append", default=None, help="Yahoo global index code. Repeat to scope; default all non-Japan configured indices.")
    discover_yahoo_cmd.add_argument("--fallback-dir", default=None, help="Directory containing per-index fallback CSV files.")
    discover_yahoo_cmd.add_argument("--limit", type=int, default=None, help="Maximum constituents per index.")
    discover_yahoo_cmd.add_argument("--dry-run", action="store_true")
    discover_yahoo_cmd.add_argument("--no-validate", dest="validate", action="store_false", default=True)
    discover_yahoo_cmd.add_argument("--no-wikipedia", dest="use_wikipedia", action="store_false", default=True)
    discover_yahoo_cmd.add_argument("--sleep-seconds", type=float, default=0.25)
    discover_yahoo_cmd.add_argument(
        "--include-wholesale",
        action="store_true",
        help="Include wholesale-exchange indices (LSE_ALL, TSX_ALL, ...). Weekly cadence only.",
    )

    backfill_gics_cmd = sub.add_parser(
        "backfill-intl-gics",
        help="Fill GICS industry-group (and any missing sector) codes on dim_company_intl "
             "from the Yahoo sector/industry strings already stored — no network calls.",
    )
    backfill_gics_cmd.add_argument("--dry-run", action="store_true")

    fetch_yahoo_cmd = sub.add_parser("fetch-yahoo-fundamentals")
    fetch_yahoo_cmd.add_argument("--ticker", action="append", default=None)
    fetch_yahoo_cmd.add_argument("--index", action="append", default=None)
    fetch_yahoo_cmd.add_argument("--limit", type=int, default=None)
    fetch_yahoo_cmd.add_argument("--dry-run", action="store_true")
    fetch_yahoo_cmd.add_argument("--sleep-seconds", type=float, default=0.5)
    fetch_yahoo_cmd.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Bounded threadpool size for parallel fundamentals fetches (1 = serial).",
    )
    fetch_yahoo_cmd.add_argument(
        "--refresh-before-days",
        type=int,
        default=None,
        help="Skip companies whose fact_yahoo_fundamental_metric is newer than N days.",
    )
    fetch_yahoo_cmd.add_argument(
        "--sample-group",
        action="append",
        default=None,
        help="Filter targets by dim_company_intl.pipeline_sample_group (repeatable).",
    )
    fetch_yahoo_cmd.add_argument(
        "--only-missing",
        action="store_true",
        help="Only process companies with zero fundamental_metric AND statement_item rows.",
    )
    fetch_yahoo_cmd.add_argument(
        "--only-missing-quarterly",
        action="store_true",
        help="Only process companies without any quarterly statement rows.",
    )
    fetch_yahoo_cmd.add_argument(
        "--no-quarterly",
        dest="include_quarterly",
        action="store_false",
        default=True,
        help="Skip quarterly statement ingestion (annual only).",
    )
    fetch_yahoo_cmd.add_argument(
        "--rate-per-second",
        type=float,
        default=None,
        help="Global Yahoo API rate limit shared across workers (e.g. 5 = 5 req/s).",
    )
    fetch_yahoo_cmd.add_argument(
        "--country",
        action="append",
        default=None,
        help="Filter targets by dim_company_intl.country_code (ISO-2, repeatable, e.g. --country DE --country HK).",
    )

    fetch_yahoo_prices_cmd = sub.add_parser("fetch-yahoo-prices")
    fetch_yahoo_prices_cmd.add_argument("--ticker", action="append", default=None)
    fetch_yahoo_prices_cmd.add_argument("--index", action="append", default=None)
    fetch_yahoo_prices_cmd.add_argument("--start-date", default="2000-01-01")
    fetch_yahoo_prices_cmd.add_argument("--end-date", default=None)
    fetch_yahoo_prices_cmd.add_argument("--full", action="store_true")
    fetch_yahoo_prices_cmd.add_argument("--limit", type=int, default=None)
    fetch_yahoo_prices_cmd.add_argument("--dry-run", action="store_true")
    fetch_yahoo_prices_cmd.add_argument("--sleep-seconds", type=float, default=0.25)
    fetch_yahoo_prices_cmd.add_argument(
        "--sample-group",
        action="append",
        default=None,
        help="Filter targets by dim_company_intl.pipeline_sample_group (repeatable).",
    )
    fetch_yahoo_prices_cmd.add_argument(
        "--country",
        action="append",
        default=None,
        help="Filter targets by dim_company_intl.country_code (ISO-2, repeatable, e.g. --country DE --country HK).",
    )

    run_yahoo_cmd = sub.add_parser("run-yahoo-global")
    run_yahoo_cmd.add_argument("--index", action="append", default=None)
    run_yahoo_cmd.add_argument("--fallback-dir", default=None)
    run_yahoo_cmd.add_argument("--limit", type=int, default=None)
    run_yahoo_cmd.add_argument("--dry-run", action="store_true")
    run_yahoo_cmd.add_argument("--no-validate", dest="validate", action="store_false", default=True)
    run_yahoo_cmd.add_argument("--no-wikipedia", dest="use_wikipedia", action="store_false", default=True)
    run_yahoo_cmd.add_argument("--no-prices", dest="prices", action="store_false", default=True)
    run_yahoo_cmd.add_argument("--price-start-date", default="2000-01-01")
    run_yahoo_cmd.add_argument("--price-end-date", default=None)
    run_yahoo_cmd.add_argument("--full-prices", action="store_true")
    run_yahoo_cmd.add_argument("--sleep-seconds", type=float, default=0.5)
    run_yahoo_cmd.add_argument(
        "--include-wholesale",
        action="store_true",
        help="Include wholesale-exchange indices (LSE_ALL, TSX_ALL, ...).",
    )

    compute_intl_cmd = sub.add_parser("compute-intl-metrics",
        help="Compute the 10 AI-screener metrics + USD market cap for INTL (Yahoo-backed) companies.")
    compute_intl_cmd.add_argument("--limit", type=int, default=None, help="Cap to first N companies (smoke test).")
    compute_intl_cmd.add_argument("--ids", type=str, nargs="*", default=None, help="Restrict to specific intl_company_id values.")

    refresh_fx_cmd = sub.add_parser("refresh-intl-fx",
        help="Fetch USD-per-1-unit rates for every currency present in dim_company_intl into fact_fx.")
    refresh_fx_cmd.add_argument("--dry-run", action="store_true")
    refresh_fx_cmd.add_argument("--sleep-seconds", type=float, default=0.3)

    ingest_fx_cmd = sub.add_parser("ingest-fx-intl", help="Pull historical {CCY}USD=X FX into fact_fx for INTL trading currencies.")
    ingest_fx_cmd.add_argument("--ccy", action="append", default=None, help="Repeat to scope. Default: full FX_CCYS_INTL list.")
    ingest_fx_cmd.add_argument("--period", default="max", help="yfinance history period (default: max).")

    health_yahoo_cmd = sub.add_parser("health-yahoo-global")
    health_yahoo_cmd.add_argument("--ticker", action="append", default=None, help="Override the default non-Japan health-check ticker sample.")

    fetch_fred_cmd = sub.add_parser("fetch-fred")
    fetch_fred_cmd.add_argument("--series", action="append", default=None)
    fetch_fred_cmd.add_argument("--start-date", default=None)
    fetch_fred_cmd.add_argument("--full", action="store_true")

    fetch_cross_asset_cmd = sub.add_parser("fetch-cross-asset")
    fetch_cross_asset_cmd.add_argument("--full", action="store_true")

    fetch_ff_cmd = sub.add_parser("fetch-fama-french")
    fetch_ff_cmd.add_argument("--full", action="store_true")
    fetch_ff_cmd.add_argument("--full-library", action="store_true", help="Load the full Ken French catalogue instead of essential factor-model datasets only.")

    cleanup_ff_cmd = sub.add_parser("cleanup-fama-french")
    cleanup_ff_cmd.add_argument("--apply", action="store_true", help="Apply cleanup; without this flag only report estimated impact.")
    cleanup_ff_cmd.add_argument("--implied-retention-years", type=int, default=3)
    cleanup_ff_cmd.add_argument("--keep-all-implied", action="store_true")

    factor_model_cmd = sub.add_parser("compute-factor-model")
    factor_model_cmd.add_argument("--full", action="store_true")
    factor_model_cmd.add_argument("--ticker", action="append", default=None)
    factor_model_cmd.add_argument("--max-tickers", type=int, default=None)
    factor_model_cmd.add_argument("--jurisdiction", choices=["US", "JP"], default=None)
    factor_model_cmd.add_argument(
        "--models",
        default=None,
        help="Comma-separated factor models to compute; default FF3,FF4,FF5,FF6.",
    )
    factor_model_cmd.add_argument("--workers", type=int, default=1)
    factor_model_cmd.add_argument("--chunk-size", type=int, default=25)
    factor_model_cmd.add_argument("--implied-retention-years", type=int, default=3)
    factor_model_cmd.add_argument("--keep-all-implied", action="store_true")

    cycle_cmd = sub.add_parser("cycle")
    cycle_cmd.add_argument(
        "action",
        choices=["build-features", "train", "score", "validate", "compute-ic"],
    )
    cycle_cmd.add_argument("--jurisdiction", choices=["US", "JP"], required=True)
    cycle_cmd.add_argument("--model-family", choices=["pca", "dfm", "hmm", "vae"], default=None)
    cycle_cmd.add_argument("--model-version", default="v1")
    cycle_cmd.add_argument("--run-id", default=None)
    cycle_cmd.add_argument("--start", default=None)
    cycle_cmd.add_argument("--end", default=None)
    cycle_cmd.add_argument("--n-components", type=int, default=6)
    cycle_cmd.add_argument("--n-states", type=int, default=4)
    cycle_cmd.add_argument("--latent-dim", type=int, default=6)
    cycle_cmd.add_argument("--epochs", type=int, default=80)
    cycle_cmd.add_argument("--min-obs", type=int, default=25)
    cycle_cmd.add_argument("--modalities", type=_modalities_arg, default=None, help="Comma-separated feature modalities for cycle training, e.g. macro,market or macro.")
    cycle_cmd.add_argument("--stress-smooth-span", type=int, default=6)
    cycle_cmd.add_argument("--min-phase-duration", type=int, default=3)
    cycle_cmd.add_argument("--no-nber-calibration", action="store_true")
    cycle_cmd.add_argument("--probability-lookback-months", type=int, default=60)
    cycle_cmd.add_argument("--metric-family", default="all", help="IC metric family: all, accounting, quality, value, growth, market_factor")
    cycle_cmd.add_argument("--chunk-size", type=int, default=25)
    cycle_cmd.add_argument("--resume", action="store_true")
    cycle_cmd.add_argument("--date-start", default=None)
    cycle_cmd.add_argument("--date-end", default=None)
    cycle_cmd.add_argument("--full", action="store_true")
    cycle_cmd.add_argument("--status-only", action="store_true")
    cycle_cmd.add_argument("--dry-run", action="store_true")

    standardize = sub.add_parser("standardize")
    standardize.add_argument("jurisdiction", choices=["US", "JP"])
    standardize.add_argument("--entity", action="append", default=None)
    standardize.add_argument("--full", action="store_true")

    validate = sub.add_parser("validate")
    validate.add_argument("jurisdiction", choices=["US", "JP"])

    metrics = sub.add_parser("metrics")
    metrics.add_argument("jurisdiction", choices=["US", "JP"])
    metrics.add_argument("--entity", action="append", default=None)
    metrics.add_argument("--full", action="store_true")

    recon = sub.add_parser("recon")
    recon.add_argument("jurisdiction", choices=["US", "JP"])
    recon.add_argument("--entity", action="append", default=None)
    recon.add_argument("--full", action="store_true")

    display_evidence = sub.add_parser("statement-display-evidence")
    display_evidence.add_argument("jurisdiction", choices=["US"])
    display_evidence.add_argument("--entity", action="append", default=None)
    display_evidence.add_argument("--full", action="store_true")
    display_evidence.add_argument("--audit", action="store_true")
    display_evidence.add_argument("--limit", type=int, default=50)

    filing_statement_display = sub.add_parser("filing-statement-display")
    filing_statement_display.add_argument("jurisdiction", choices=["US"])
    filing_statement_display.add_argument("--entity", default="0000001750")
    filing_statement_display.add_argument("--filing-id", default="0001104659-20-108360")
    filing_statement_display.add_argument("--force", action="store_true")

    llm_raw_display = sub.add_parser("llm-raw-filing-display")
    llm_raw_display.add_argument("jurisdiction", choices=["US", "JP"])
    llm_raw_display.add_argument("--entity", default=None)
    llm_raw_display.add_argument("--ticker", default=None)
    llm_raw_display.add_argument("--filing-id", default=None)
    llm_raw_display.add_argument("--period", dest="fiscal_period", default=None)
    llm_raw_display.add_argument("--year-min", type=int, default=None)
    llm_raw_display.add_argument("--year-max", type=int, default=None)
    llm_raw_display.add_argument("--statement", action="append", choices=["BS", "IS", "CF"], default=None)
    llm_raw_display.add_argument("--force", action="store_true")
    llm_raw_display.add_argument("--model", default="deepseek-v4-flash")
    llm_raw_display.add_argument(
        "--heuristic-only",
        action="store_true",
        help="Use presentation-hierarchy fallback instead of calling the LLM.",
    )
    llm_raw_display.add_argument(
        "--available-universe",
        action="store_true",
        help="Batch over filings already present in fact_filing_statement_display.",
    )
    llm_raw_display.add_argument("--limit", type=int, default=None)

    statement_compare = sub.add_parser("statement-compare")
    statement_compare.add_argument("--jurisdiction", choices=["US", "JP"], default=None)
    statement_compare.add_argument("--ticker", action="append", default=None)
    statement_compare.add_argument(
        "--statement",
        action="append",
        choices=["balance_sheet", "income_statement", "cash_flow_statement", "BalanceSheet", "IncomeStatement", "CashFlow"],
        default=None,
    )
    statement_compare.add_argument("--period", default="FY")
    statement_compare.add_argument("--n-periods", type=int, default=5)
    statement_compare.add_argument("--summary", action="store_true")
    statement_compare.add_argument("--review", action="store_true")
    statement_compare.add_argument("--max-review-rows", type=int, default=20)

    supplemental_text = sub.add_parser("supplemental-text-evidence")
    supplemental_text.add_argument("jurisdiction", choices=["US"])
    supplemental_text.add_argument("--entity", action="append", default=None)
    supplemental_text.add_argument("--full", action="store_true")
    supplemental_text.add_argument("--audit", action="store_true")
    supplemental_text.add_argument("--limit", type=int, default=50)

    news = sub.add_parser("news")
    configure_news_parser(news)

    args = parser.parse_args()
    if args.cmd == "apply-schema":
        apply_schema()
    elif args.cmd == "reset":
        reset_downstream(args.jurisdiction, args.entity)
    elif args.cmd == "scope":
        if args.jurisdiction == "US":
            if args.all_eligible:
                group = args.group if args.group != "pilot_50_us" else "us_all_eligible_20260524"
                result = us.activate_all_eligible_us_pipeline_scope(group=group)
                print(
                    f"US scope {result['group']}: "
                    f"eligible={result['eligible']} "
                    f"active_before={result['active_before']} "
                    f"active_after={result['active_after']} "
                    f"activated_count={result['activated_count']} "
                    f"deactivated_count={result['deactivated_count']}"
                )
                return 0
            result = us.replenish_us_pipeline_scope(group=args.group, target=args.target)
            activated = ", ".join(
                f"{row['ticker']}({row['cik']})" for row in result["activated"]
            )
            recovered = ", ".join(
                f"{row['ticker']}({row['cik']})" for row in result["recovered"]
            )
            print(
                f"US scope {result['group']}: "
                f"active_before={result['active_before']} "
                f"active_after={result['active_after']} "
                f"target={result['target']} "
                f"recovered=[{recovered}] "
                f"activated=[{activated}]"
            )
        else:
            group = args.group if args.group != "pilot_50_us" else "pilot_500_jp"
            if args.all_eligible:
                group = args.group if args.group != "pilot_50_us" else "jp_active_full_20260524"
                result = jp.activate_all_eligible_jp_pipeline_scope(group=group)
                print(
                    f"JP scope {result['group']}: "
                    f"eligible={result['eligible']} "
                    f"active_before={result['active_before']} "
                    f"active_after={result['active_after']} "
                    f"activated_count={result['activated_count']} "
                    f"deactivated_count={result['deactivated_count']}"
                )
                return 0
            result = jp.replenish_jp_pipeline_scope(group=group, target=args.target)
            activated = ", ".join(
                f"{row['ticker']}({row['edinet_code']})" for row in result["activated"]
            )
            print(
                f"JP scope {result['group']}: "
                f"active_before={result['active_before']} "
                f"active_after={result['active_after']} "
                f"target={result['target']} "
                f"activated=[{activated}]"
            )
    elif args.cmd == "reparse":
        if args.jurisdiction == "US":
            us.reparse(
                args.entity,
                annual_10k_current_only=args.annual_10k_current_only,
                filing_types=args.filing_types,
            )
        else:
            if args.entity is None and not args.allow_global_reset:
                raise SystemExit(
                    "Refusing global JP reparse because it resets all JP raw/std/metrics/recon first. "
                    "Use: rebuild-local JP --filed-date-max YYYY-MM-DD"
                )
            jp.reparse(args.entity, filed_date_max=_date_arg(args.filed_date_max), filing_types=args.filing_types)
    elif args.cmd == "run":
        if args.jurisdiction == "US":
            us.run_incremental(
                download=args.download,
                max_ciks=args.max_ciks,
                filing_types=args.filing_types,
                entity_ids=args.entity,
                since=_date_arg(args.since),
                lookback_days=args.lookback_days,
            )
        else:
            jp.run_incremental(
                download=args.download,
                full=args.full,
                limit=args.limit,
                filed_date_max=_date_arg(args.filed_date_max),
                filing_types=args.filing_types,
            )
    elif args.cmd == "rebuild-local":
        if args.dry_run:
            result = jp.plan_jp_local_rebuild(
                entity_ids=args.entity,
                filed_date_max=_date_arg(args.filed_date_max),
                filing_types=args.filing_types,
                chunk_size=args.chunk_size,
                resume=args.resume,
            )
            print("JP local rebuild dry-run: " + " ".join(f"{k}={v}" for k, v in sorted(result.items())))
            return 0
        result = jp.rebuild_jp_local(
            entity_ids=args.entity,
            filed_date_max=_date_arg(args.filed_date_max),
            filing_types=args.filing_types,
            chunk_size=args.chunk_size,
            resume=args.resume,
            downstream=args.downstream,
            sync_index=args.sync_index,
        )
        print("JP local rebuild: " + " ".join(f"{k}={v}" for k, v in sorted(result.items())))
    elif args.cmd == "refresh-master":
        if args.jurisdiction == "US":
            us.refresh_us_master(download=args.download, full=args.full, max_ciks=args.max_ciks)
        else:
            jp.refresh_jp_master(
                full=args.full,
                days=args.days,
                start_date=_date_arg(args.start_date),
                end_date=_date_arg(args.end_date),
            )
    elif args.cmd == "master-only":
        result = jp.refresh_jp_master_only(
            full=args.full,
            days=args.days,
            start_date=_date_arg(args.start_date),
            end_date=_date_arg(args.end_date),
            max_tickers=args.max_tickers,
        )
        identity = result["identity"]
        gics = result["gics"]
        isin = result["isin"]
        sync = result["sync"]
        print(
            "JP master-only: "
            f"master_rows={result['master_rows']} "
            f"identity_updated={identity.get('updated')} "
            f"gics_candidates={gics.get('candidates')} "
            f"gics_updated={gics.get('updated')} "
            f"isin_candidates={isin.get('candidates')} "
            f"isin_found={isin.get('found')} "
            f"isin_name_found={isin.get('name_found')} "
            f"isin_missing={isin.get('missing')} "
            f"isin_errors={isin.get('errors')} "
            f"companies={sync['companies']} "
            f"ticker_links={sync['ticker_links']}"
        )
    elif args.cmd == "download":
        if args.jurisdiction == "US":
            result = us.download_us_sources(
                force=args.force,
                max_ciks=args.max_ciks,
                xbrl_limit=args.limit,
                filing_types=args.filing_types,
                entity_ids=args.entity,
                since=_date_arg(args.since),
                lookback_days=args.lookback_days,
            )
            print(
                "US source download: "
                f"companyfacts={result['companyfacts_downloaded']} "
                f"companyfacts_errors={result['companyfacts_errors']} "
                f"filings_indexed={result['filings_indexed']} "
                f"local_xbrl_zips={result['local_xbrl_zips']} "
                f"xbrl_candidates={result['xbrl_candidates']} "
                f"xbrl_downloaded={result['xbrl_downloaded']} "
                f"xbrl_skipped={result['xbrl_skipped']} "
                f"xbrl_not_found={result['xbrl_not_found']} "
                f"xbrl_errors={result['xbrl_errors']} "
                f"linkbases_written={result['linkbases_written']} "
                f"linkbases_missing={result['linkbases_missing']}"
            )
        else:
            jp.download_jp_xbrl(force=args.force, limit=args.limit, doc_ids=args.doc, filing_types=args.filing_types)
    elif args.cmd == "download-raw-sec":
        result = us.download_us_raw_sec_filings(
            force=args.force,
            max_ciks=args.max_ciks,
            xbrl_limit=args.limit,
            filing_types=args.filing_types,
        )
        print(
            "US raw SEC filing acquisition: "
            f"companies={result['companies']} "
            f"submission_files={result['submission_files']} "
            f"submission_errors={result['submission_errors']} "
            f"filings_indexed={result['filings_indexed']} "
            f"local_xbrl_zips={result['local_xbrl_zips']} "
            f"xbrl_candidates={result['xbrl_candidates']} "
            f"xbrl_downloaded={result['xbrl_downloaded']} "
            f"xbrl_skipped={result['xbrl_skipped']} "
            f"xbrl_not_found={result['xbrl_not_found']} "
            f"xbrl_errors={result['xbrl_errors']} "
            f"linkbases_processed={result['linkbases_processed']} "
            f"linkbases_written={result['linkbases_written']} "
            f"linkbases_missing={result['linkbases_missing']} "
            f"linkbase_errors={result['linkbase_errors']}"
        )
    elif args.cmd == "download-xbrl":
        result = us.download_us_xbrl(
            entity_ids=args.entity,
            force=args.force,
            limit=args.limit,
            filing_types=args.filing_types,
        )
        print(
            "US XBRL download: "
            f"candidates={result['candidates']} downloaded={result['downloaded']} "
            f"skipped={result['skipped']} not_found={result['not_found']} errors={result['errors']}"
        )
    elif args.cmd == "extract-html":
        result = mda_reextract_html(entity_ids=args.entity, force=args.force)
        print("US HTML extraction: " + " ".join(f"{key}={value}" for key, value in sorted(result.items())))
    elif args.cmd == "mda":
        forms = tuple(part.strip().upper() for part in args.forms.split(",") if part.strip()) if args.forms else None
        if args.jurisdiction == "JP":
            if forms:
                raise ValueError("--forms is currently only supported for US MD&A")
            if args.action == "discover":
                result = mda_discover_jp(edinet_code=args.edinet_code, years=args.years)
            elif args.action == "extract":
                result = mda_extract_jp(
                    edinet_code=args.edinet_code,
                    limit=args.limit,
                    retry_dirty=args.retry_dirty,
                )
            elif args.action == "ingest":
                result = mda_ingest_jp(edinet_code=args.edinet_code, years=args.years, limit=args.limit)
            elif args.action == "keywords":
                result = mda_keywords("JP", entity_id=args.edinet_code, filing_id=args.filing_id, limit=args.limit)
            elif args.action == "summarize":
                result = mda_summarize("JP", entity_id=args.edinet_code, filing_id=args.filing_id, section_id=args.section_id, model=args.model, limit=args.limit or 1)
            else:
                result = mda_status_jp(edinet_code=args.edinet_code)
        else:
            if args.action == "discover":
                result = mda_discover(cik=args.cik, years=args.years, forms=forms)
            elif args.action == "extract":
                result = mda_extract(
                    cik=args.cik,
                    limit=args.limit,
                    include_item_7a=args.include_item_7a,
                    retry_dirty=args.retry_dirty,
                )
            elif args.action == "ingest":
                result = mda_ingest(
                    cik=args.cik,
                    years=args.years,
                    limit=args.limit,
                    include_item_7a=args.include_item_7a,
                )
            elif args.action == "keywords":
                result = mda_keywords("US", entity_id=args.cik, filing_id=args.filing_id, limit=args.limit)
            elif args.action == "summarize":
                result = mda_summarize("US", entity_id=args.cik, filing_id=args.filing_id, section_id=args.section_id, model=args.model, limit=args.limit or 1)
            else:
                result = mda_status(cik=args.cik)
        print(f"mda {args.jurisdiction} {args.action}: " + " ".join(f"{key}={value}" for key, value in sorted(result.items())))
    elif args.cmd == "inst":
        if args.action == "discover-13f":
            result = inst_pipeline.discover_13f(from_year=args.from_year)
        elif args.action == "register-local-13f":
            result = inst_pipeline.register_local_13f(quarter=args.quarter)
        elif args.action == "download-13f":
            result = inst_pipeline.download_13f(quarter=args.quarter, force=args.force, limit=args.limit)
        elif args.action == "parse-13f":
            result = inst_pipeline.parse_13f(quarter=args.quarter, manager=args.manager, limit=args.limit, row_limit=args.row_limit, force=args.force)
        elif args.action == "resolve-13f":
            result = inst_pipeline.resolve_13f_issuers(limit=args.limit, use_llm=args.use_llm)
        elif args.action == "resolve-13f-managers":
            result = inst_pipeline.resolve_13f_managers(limit=args.limit)
        elif args.action == "run-13f":
            result = inst_pipeline.run_13f(quarter=args.quarter, from_year=args.from_year, force=args.force, limit=args.limit, row_limit=args.row_limit)
        elif args.action == "standardize-13f":
            result = inst_lean_core.standardize_13f_from_raw_batched(dataset_key=args.quarter, limit=args.limit, force=args.force)
        elif args.action == "resolve-13f-securities":
            result = inst_lean_core.resolve_13f_securities(limit=args.limit)
        elif args.action == "report-13f-cusip-gaps":
            result = inst_lean_core.report_13f_cusip_gaps(limit=args.limit)
            print(json.dumps(result, indent=2, default=str))
            return 0
        elif args.action == "compare-13f-cusip-llm":
            result = inst_lean_core.compare_13f_cusip_llm(limit=args.limit)
            print(json.dumps(result, indent=2, default=str))
            return 0
        elif args.action == "promote-13f-cusip-llm":
            result = inst_lean_core.promote_13f_cusip_llm(
                min_confidence=args.min_confidence,
                limit=args.limit,
                force=args.force,
            )
        elif args.action == "enrich-13f-yahoo-identifiers":
            result = enrich_13f_yahoo_identifiers(
                limit=args.limit,
                apply=args.apply,
                resume=args.resume,
                headless=args.headless,
                sleep_seconds=args.sleep_seconds,
                edge_binary_path=args.edge_binary_path,
                driver_path=args.driver_path,
                max_symbols_per_query=args.max_symbols_per_query,
                workers=args.workers,
                selenium_fallback=args.selenium_fallback,
            )
        elif args.action == "apply-13f-yahoo-identifier-evidence":
            result = apply_13f_yahoo_identifier_evidence(limit=args.limit)
        elif args.action == "enrich-13f-openfigi-identifiers":
            # Pass --batch-size only if the user overrode the inst-subparser default (100),
            # so the module's env-aware default (10 anon / 100 keyed) is picked otherwise.
            openfigi_batch = args.batch_size if args.batch_size != 100 else None
            result = enrich_13f_openfigi_identifiers(
                limit=args.limit,
                apply=args.apply,
                resume=args.resume,
                batch_size=openfigi_batch,
            )
        elif args.action == "apply-13f-openfigi-identifier-evidence":
            result = apply_13f_openfigi_identifier_evidence(limit=args.limit)
        elif args.action == "promote-13f-yahoo-price-evidence":
            result = promote_13f_yahoo_price_evidence(
                limit=args.limit,
                batch_size=args.batch_size,
                period=args.price_period,
            )
        elif args.action == "download-13f-yahoo-prices":
            result = download_13f_yahoo_prices(
                start_date=args.start_date,
                end_date=args.end_date,
                incremental=not args.full,
                limit=args.limit,
                batch_size=args.batch_size,
            )
        elif args.action == "compute-13f-manager-period":
            result = inst_lean_core.compute_13f_manager_period()
        elif args.action == "classify-13f-core":
            result = inst_lean_core.classify_13f_core(deterministic_only=not args.use_llm)
        elif args.action == "classify-13f-core-llm":
            result = inst_lean_core.classify_13f_core_llm(
                limit=args.limit,
                min_aum=args.min_aum,
                min_confidence=args.min_confidence,
                dry_run=args.dry_run,
                force=args.force,
                workers=args.workers,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0
        elif args.action == "recon-13f":
            result = inst_lean_core.recon_13f_core()
        elif args.action == "run-13f-full":
            result = {}
            result |= {f"discover_{k}": v for k, v in inst_pipeline.discover_13f(from_year=args.from_year).items()}
            result |= {f"register_local_{k}": v for k, v in inst_pipeline.register_local_13f(quarter=args.quarter).items()}
            result |= {f"download_{k}": v for k, v in inst_pipeline.download_13f(quarter=args.quarter, force=args.force, limit=args.limit).items()}
            result |= {f"parse_{k}": v for k, v in inst_pipeline.parse_13f(quarter=args.quarter, limit=args.limit, row_limit=args.row_limit, force=args.force).items()}
            result |= {f"resolve_{k}": v for k, v in inst_pipeline.resolve_13f_issuers().items()}
            result |= {f"resolve_managers_{k}": v for k, v in inst_pipeline.resolve_13f_managers().items()}
            result |= {f"core_ref_{k}": v for k, v in inst_lean_core.import_manager_style_reference_core().items()}
            result |= {f"standardize_{k}": v for k, v in inst_lean_core.standardize_13f_from_raw_batched(dataset_key=args.quarter, limit=args.limit, force=args.force).items()}
            result |= {f"metrics_{k}": v for k, v in inst_lean_core.compute_13f_manager_period().items()}
            result |= {f"classify_{k}": v for k, v in inst_lean_core.classify_13f_core(deterministic_only=not args.use_llm).items()}
            result |= {f"recon_{k}": v for k, v in inst_lean_core.recon_13f_core().items()}
        elif args.action == "import-manager-style-reference":
            result = inst_classifier.import_style_references() | {f"core_{k}": v for k, v in inst_lean_core.import_manager_style_reference_core().items()}
        elif args.action == "link-manager-style-reference":
            result = inst_classifier.link_manager_style_references(
                manager=args.manager,
                limit=args.limit,
                force=args.force,
                backfill_classifications=True,
            )
        elif args.action == "classify-13f-managers":
            result = inst_classifier.classify_13f_managers(
                manager=args.manager,
                limit=args.limit,
                force=args.force,
                deterministic_only=args.deterministic_only,
                reference_only=args.reference_only,
            )
        elif args.action == "discover-13dg":
            result = inst_pipeline.discover_13dg(cik=args.cik)
        elif args.action == "download-13dg":
            result = inst_pipeline.download_13dg(cik=args.cik, all_issuers=args.all_issuers, all_issuer_universe=args.all_issuer_universe, limit=args.limit)
        elif args.action == "parse-13dg":
            result = inst_pipeline.parse_13dg(cik=args.cik, limit=args.limit)
        elif args.action == "run-13dg":
            result = inst_pipeline.download_13dg(cik=args.cik, all_issuers=args.all_issuers, all_issuer_universe=args.all_issuer_universe, limit=args.limit) | inst_pipeline.discover_13dg(cik=args.cik) | inst_pipeline.parse_13dg(cik=args.cik, limit=args.limit)
        else:
            result = inst_pipeline.status() | {f"core_{k}": v for k, v in inst_lean_core.status_13f_core().items()}
        print(f"inst {args.action}: " + " ".join(f"{key}={value}" for key, value in sorted(result.items())))
    elif args.cmd == "insider":
        if args.action == "discover-local":
            result = insider_pipeline.discover_local(cik=args.cik, limit=args.limit)
        elif args.action == "index-sync":
            result = insider_pipeline.index_sync(from_year=args.from_year, limit=args.limit)
        elif args.action == "download":
            result = insider_pipeline.download(cik=args.cik, limit=args.limit, force=args.force)
        elif args.action == "parse":
            result = insider_pipeline.parse(cik=args.cik, limit=args.limit, force=args.force)
        elif args.action == "run":
            result = insider_pipeline.run(cik=args.cik, limit=args.limit)
        else:
            result = insider_pipeline.status(cik=args.cik)
        print(f"insider {args.action}: " + " ".join(f"{key}={value}" for key, value in sorted(result.items())))
    elif args.cmd == "index-api":
        if args.jurisdiction == "US":
            raise NotImplementedError("US API index is handled by refresh-master/download companyfacts.")
        else:
            jp.index_jp_api(
                full=args.full,
                start_date=_date_arg(args.start_date),
                end_date=_date_arg(args.end_date),
                filing_types=args.filing_types,
            )
    elif args.cmd == "extract":
        if args.jurisdiction == "US":
            result = us.extract_us_xbrl(
                entity_ids=args.entity,
                force=args.force,
                workers=args.workers,
                skip_existing_stems=args.new_only,
            )
            print(
                "US linkbase extraction: "
                f"processed={result['processed']} written={result['written']} "
                f"skipped={result['skipped']} missing={result['missing']} errors={result['errors']}"
                f" skipped_existing_stems={result.get('skipped_existing_stems', 0)}"
                f" skipped_completed_outputs={result.get('skipped_completed_outputs', 0)}"
                f" db_candidates={result.get('db_candidates', 0)}"
                f" candidate_files={result.get('candidate_files', 0)}"
                f" missing_local_zips={result.get('missing_local_zips', 0)}"
                f" local_xbrl_zips={result.get('local_xbrl_zips', 0)}"
            )
        else:
            jp.extract_jp_xbrl(entity_ids=args.entity, doc_ids=args.doc, force=args.force, filing_types=args.filing_types)
    elif args.cmd == "index":
        if args.jurisdiction == "US":
            us.index_us_companyfacts(args.entity)
        else:
            jp.index_jp_xbrl(entity_ids=args.entity, doc_ids=args.doc, filing_types=args.filing_types)
    elif args.cmd == "parse":
        if args.jurisdiction == "US":
            us.parse_us_raw(
                entity_ids=args.entity,
                force=args.force,
                annual_10k_current_only=args.annual_10k_current_only,
                filing_types=args.filing_types,
            )
        else:
            jp.parse_jp_raw(
                entity_ids=args.entity,
                doc_ids=args.doc,
                force=args.force,
                filed_date_max=_date_arg(args.filed_date_max),
                filing_types=args.filing_types,
            )
    elif args.cmd == "sync-refs":
        mappings, line_items = sync_reference_tables()
        metric_defs, tickers = sync_metric_support()
        print(
            f"synced {mappings} mappings, {line_items} standardized line items, "
            f"{metric_defs} metric definitions and {tickers} ticker links"
        )
    elif args.cmd == "sync-registry":
        counts = sync_registry(path=args.path, update_formulas=not args.no_formulas)
        print("registry sync: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "concept-universe":
        jurisdiction = None if args.jurisdiction == "ALL" else args.jurisdiction
        counts = build_concept_universe(
            jurisdiction=jurisdiction,
            entity_ids=args.entity,
            enrich_labels=args.labels,
        )
        print("concept universe: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "unmapped-concepts":
        print(unmapped_concepts_json(args.jurisdiction, args.limit))
    elif args.cmd == "review-queue":
        counts = build_mapping_review_queue(
            args.jurisdiction,
            limit=args.limit,
            min_fact_count=args.min_fact_count,
            top_n=args.top_n,
        )
        print("mapping review queue: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "review-queue-summary":
        print(review_queue_summary_json(args.jurisdiction))
    elif args.cmd == "mapping-health":
        counts = build_mapping_health_review_queue(
            args.jurisdiction,
            limit=args.limit,
            dry_run=args.dry_run,
            reset_existing=args.reset_existing,
        )
        mode = "dry_run" if args.dry_run else "queued"
        print(f"mapping health: jurisdiction={args.jurisdiction} {mode} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "concept-health":
        counts = build_concept_health_review_queue(
            args.jurisdiction,
            limit=args.limit,
            min_fact_count=args.min_fact_count,
            dry_run=args.dry_run,
            reset_existing=args.reset_existing,
        )
        mode = "dry_run" if args.dry_run else "queued"
        print(f"concept health: jurisdiction={args.jurisdiction} {mode} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "concept-target-policy":
        counts = build_concept_target_display_policy(
            args.jurisdiction,
            include_mapped_clean=args.include_mapped_clean,
            dry_run=args.dry_run,
            reset_existing=args.reset_existing,
            limit=args.limit,
        )
        mode = "dry_run" if args.dry_run else "written"
        print(f"concept target policy: jurisdiction={args.jurisdiction} {mode} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "load-taxonomy-all-years":
        counts = ingest_all_years_with_connect(
            Path(args.spec_dir) if args.spec_dir else None,
            enrich_observations=not args.no_enrich_observations,
        )
        print("taxonomy all-years: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "rerank-mappings":
        with connect() as conn:
            count = run_one_time_reranking(
                conn,
                args.jurisdiction,
                limit=args.limit,
                dry_run=args.dry_run,
                namespace_prefix=args.namespace_prefix,
                review_class=args.review_class,
                min_fact_count=args.min_fact_count,
                queue_modulus=args.queue_modulus,
                queue_remainder=args.queue_remainder,
            )
        mode = "dry_run" if args.dry_run else "scored"
        print(f"rerank mappings: jurisdiction={args.jurisdiction} {mode}={count}")
    elif args.cmd == "promote-versioned":
        with connect() as conn:
            result = promote_review_queue_rows(
                conn,
                args.jurisdiction,
                limit=args.limit,
                dry_run=args.dry_run,
                namespace_prefix=args.namespace_prefix,
                review_class=args.review_class,
                min_fact_count=args.min_fact_count,
                min_confidence=args.min_confidence,
                decision=args.decision,
                approved_by=args.approved_by,
            )
        mode = "dry_run" if args.dry_run else "promoted"
        print(
            f"promote versioned: jurisdiction={args.jurisdiction} {mode} "
            f"selected={result['selected']} promoted={result['promoted']} skipped={result['skipped']}"
        )
    elif args.cmd == "sync-master":
        jur = None if args.jurisdiction == "ALL" else args.jurisdiction
        counts = sync_master_dimensions(jur)
        print(
            f"synced company master support: companies={counts['companies']} "
            f"ticker_links={counts['ticker_links']}"
        )
    elif args.cmd == "enrich-master":
        if args.jurisdiction == "JP" and (args.isin or args.gics or args.identity):
            counts = {}
            if args.identity:
                counts.update({f"identity_{key}": value for key, value in jp.enrich_jp_identity(args.full).items()})
            if args.isin:
                counts.update({f"isin_{key}": value for key, value in jp.enrich_jp_isin(args.full, args.max_tickers).items()})
            if args.gics:
                counts.update({f"gics_{key}": value for key, value in jp.enrich_jp_gics(args.full, args.max_tickers).items()})
        else:
            counts = enrich_company_master(
                args.jurisdiction,
                full=args.full,
                max_tickers=args.max_tickers,
                isin=args.isin,
                gics=args.gics,
            )
        print("company master enrichment: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "fetch-prices":
        start = args.start_date or ("2000-01-01" if args.full else None)
        start_iso = start or "2000-01-01"
        explicit_tickers = bool(args.ticker)
        total_written = 0
        if not args.skip_us:
            us_tickers = args.ticker if explicit_tickers else _active_us_tickers()
            if us_tickers:
                written_us = fetch_prices_yf(
                    us_tickers,
                    start_date=start_iso,
                    end_date=args.end_date,
                    incremental=not args.full,
                    jurisdiction="US",
                )
                total_written += written_us
                print(f"fetch-prices US: tickers={len(us_tickers)} rows_written={written_us}")
            else:
                print("fetch-prices US: no tickers to process")
        if not args.skip_jp:
            jp_tickers = args.ticker if explicit_tickers else _active_jp_tickers()
            if jp_tickers:
                written_jp = fetch_prices_yf(
                    jp_tickers,
                    start_date=start_iso,
                    end_date=args.end_date,
                    incremental=not args.full,
                    jurisdiction="JP",
                )
                total_written += written_jp
                print(f"fetch-prices JP: tickers={len(jp_tickers)} rows_written={written_jp}")
            else:
                print("fetch-prices JP: no tickers to process")
        print(f"fetch-prices: total rows_written={total_written}")
    elif args.cmd == "fetch-stock-splits":
        jurisdictions = ["US", "JP"] if args.jurisdiction == "ALL" else [args.jurisdiction]
        total = 0
        for jurisdiction in jurisdictions:
            total += fetch_stock_splits(
                jurisdiction=jurisdiction,
                tickers=args.ticker,
                start_date=args.start_date,
                full=args.full,
            )
        print(f"fetch-stock-splits: jurisdictions={','.join(jurisdictions)} rows_written={total}")
    elif args.cmd == "derive-market-items":
        items = derive_market_metrics(tickers=args.ticker, full=args.full)
        betas = compute_betas(tickers=args.ticker) if args.betas else 0
        print(f"derive-market-items: market_metrics={items} betas={betas}")
    elif args.cmd == "discover-yahoo-tickers":
        counts = run_yahoo_global_discovery(
            index_codes=args.index,
            fallback_dir=Path(args.fallback_dir) if args.fallback_dir else None,
            validate=args.validate,
            use_wikipedia=args.use_wikipedia,
            dry_run=args.dry_run,
            limit=args.limit,
            sleep_seconds=args.sleep_seconds,
            include_wholesale=args.include_wholesale,
        )
        mode = "dry-run" if args.dry_run else "written"
        print(f"discover-yahoo-tickers {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "backfill-intl-gics":
        stats = backfill_intl_gics(dry_run=args.dry_run)
        mode = "dry-run" if args.dry_run else "written"
        unmapped = stats.pop("unmapped", [])
        print(f"backfill-intl-gics {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        if unmapped:
            print("  unmapped industries (left without a group, sector preserved):")
            for industry, n in sorted(unmapped, key=lambda x: -x[1]):
                print(f"    {industry} ({n})")
    elif args.cmd == "fetch-yahoo-fundamentals":
        counts = run_yahoo_global_fundamentals(
            tickers=args.ticker,
            index_codes=args.index,
            limit=args.limit,
            dry_run=args.dry_run,
            sleep_seconds=args.sleep_seconds,
            max_workers=args.max_workers,
            refresh_before_days=args.refresh_before_days,
            sample_groups=args.sample_group,
            country_codes=args.country,
            only_missing=args.only_missing,
            only_missing_quarterly=args.only_missing_quarterly,
            rate_per_second=args.rate_per_second,
            include_quarterly=args.include_quarterly,
        )
        mode = "dry-run" if args.dry_run else "written"
        print(f"fetch-yahoo-fundamentals {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "fetch-yahoo-prices":
        counts = run_yahoo_global_prices(
            tickers=args.ticker,
            index_codes=args.index,
            start_date=args.start_date,
            end_date=args.end_date,
            full=args.full,
            limit=args.limit,
            dry_run=args.dry_run,
            sleep_seconds=args.sleep_seconds,
            sample_groups=args.sample_group,
            country_codes=args.country,
        )
        mode = "dry-run" if args.dry_run else "written"
        print(f"fetch-yahoo-prices {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "run-yahoo-global":
        counts = run_yahoo_global(
            index_codes=args.index,
            fallback_dir=Path(args.fallback_dir) if args.fallback_dir else None,
            validate=args.validate,
            use_wikipedia=args.use_wikipedia,
            prices=args.prices,
            price_start_date=args.price_start_date,
            price_end_date=args.price_end_date,
            full_prices=args.full_prices,
            dry_run=args.dry_run,
            limit=args.limit,
            sleep_seconds=args.sleep_seconds,
            include_wholesale=args.include_wholesale,
        )
        mode = "dry-run" if args.dry_run else "written"
        print(f"run-yahoo-global {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "compute-intl-metrics":
        from xbrl_sec.sec.metrics.compute_intl import compute_intl_metrics
        stats = compute_intl_metrics(limit=args.limit, only_intl_company_ids=args.ids, verbose=True)
        print("compute-intl-metrics: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    elif args.cmd == "refresh-intl-fx":
        from xbrl_sec.sec.sources.yahoo_global import refresh_intl_fx
        stats = refresh_intl_fx(dry_run=args.dry_run, sleep_seconds=args.sleep_seconds)
        mode = "dry-run" if args.dry_run else "written"
        print(f"refresh-intl-fx {mode}: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    elif args.cmd == "health-yahoo-global":
        counts = yahoo_global_health_check(tickers=args.ticker)
        print("health-yahoo-global: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "ingest-fx-intl":
        counts = ingest_fx_intl(ccys=args.ccy, period=args.period)
        total = sum(counts.values())
        print(f"ingest-fx-intl: total_rows={total} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "fetch-fred":
        written = fetch_fred(
            series_ids=args.series,
            start_date=args.start_date or "2000-01-01",
            full=args.full,
        )
        print(f"fetch-fred: rows_written={written}")
    elif args.cmd == "fetch-cross-asset":
        written = fetch_cross_asset(full=args.full)
        print(f"fetch-cross-asset: rows_written={written}")
    elif args.cmd == "fetch-fama-french":
        written = fetch_fama_french(full=args.full, full_library=args.full_library)
        print(f"fetch-fama-french: rows_written={written}")
    elif args.cmd == "cleanup-fama-french":
        retention = None if args.keep_all_implied else args.implied_retention_years
        result = cleanup_fama_french_storage(apply=args.apply, implied_retention_years=retention)
        print("cleanup-fama-french: " + " ".join(f"{k}={v}" for k, v in sorted(result.items())))
    elif args.cmd == "compute-factor-model":
        models = [part.strip().upper() for part in args.models.split(",") if part.strip()] if args.models else None
        retention = None if args.keep_all_implied else args.implied_retention_years
        counts = compute_factor_model_parallel(
            full=args.full,
            tickers=args.ticker,
            max_tickers=args.max_tickers,
            jurisdiction=args.jurisdiction,
            models=models,
            workers=args.workers,
            chunk_size=args.chunk_size,
            implied_retention_years=retention,
        )
        print("compute-factor-model: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "cycle":
        if args.action == "build-features":
            result = build_cycle_features(
                args.jurisdiction,
                start=_date_arg(args.start),
                end=_date_arg(args.end),
                dry_run=args.dry_run,
            )
        elif args.action == "train":
            family = args.model_family or "pca"
            if family in {"pca", "dfm"}:
                result = train_baseline(
                    args.jurisdiction,
                    model_family=family,
                    model_version=args.model_version,
                    start=_date_arg(args.start),
                    end=_date_arg(args.end),
                    n_components=args.n_components,
                    modalities=args.modalities,
                )
            elif family == "hmm":
                result = train_hmm(
                    args.jurisdiction,
                    n_states=args.n_states,
                    model_version=args.model_version,
                    base_run_id=args.run_id,
                )
            elif family == "vae":
                result = train_vae(
                    args.jurisdiction,
                    model_version=args.model_version,
                    start=_date_arg(args.start),
                    end=_date_arg(args.end),
                    latent_dim=args.latent_dim,
                    epochs=args.epochs,
                    modalities=args.modalities,
                    stress_smooth_span=args.stress_smooth_span,
                    min_phase_duration=args.min_phase_duration,
                    calibrate_to_nber=not args.no_nber_calibration,
                )
            else:
                raise SystemExit(f"Unsupported cycle model family: {family}")
        elif args.action == "score":
            result = score_cycle_state(
                args.jurisdiction,
                run_id=args.run_id,
                model_family=args.model_family,
            )
        elif args.action == "validate":
            result = validate_cycle_model(
                args.jurisdiction,
                run_id=args.run_id,
                model_family=args.model_family,
            )
        elif args.action == "compute-ic":
            if args.status_only:
                result = compute_regime_factor_ic(
                    args.jurisdiction,
                    run_id=args.run_id,
                    metric_family=args.metric_family,
                    date_start=_date_arg(args.date_start),
                    date_end=_date_arg(args.date_end),
                    status_only=True,
                )
            else:
                result = compute_regime_factor_ic(
                    args.jurisdiction,
                    run_id=args.run_id,
                    min_obs=args.min_obs,
                    probability_lookback_months=args.probability_lookback_months,
                    metric_family=args.metric_family,
                    chunk_size=args.chunk_size,
                    resume=args.resume,
                    date_start=_date_arg(args.date_start),
                    date_end=_date_arg(args.date_end),
                    full=args.full,
                )
        else:
            raise SystemExit(f"Unsupported cycle action: {args.action}")
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    elif args.cmd == "standardize":
        if args.jurisdiction == "US":
            written = populate_us_std(entity_ids=args.entity, full=args.full)
        else:
            written = populate_jp_std(entity_ids=args.entity, full=args.full)
        print(f"wrote {written} standardized {args.jurisdiction} rows")
    elif args.cmd == "validate":
        print(validation_json(args.jurisdiction))
    elif args.cmd == "metrics":
        written = compute_metrics(args.jurisdiction, entity_ids=args.entity, full=args.full)
        print(f"wrote {written} {args.jurisdiction} metric rows")
    elif args.cmd == "recon":
        written = build_recon(args.jurisdiction, entity_ids=args.entity, full=args.full)
        print(f"wrote {written} {args.jurisdiction} recon rows")
    elif args.cmd == "statement-display-evidence":
        if args.audit:
            print(operating_cost_audit_summary_json(limit=args.limit))
        else:
            counts = build_us_operating_cost_evidence(entity_ids=args.entity, full=args.full)
            print("statement display evidence: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "filing-statement-display":
        counts = build_aar_filing_statement_display(
            entity_id=args.entity,
            filing_id=args.filing_id,
            force=args.force,
        )
        print("filing statement display: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "llm-raw-filing-display":
        if args.available_universe:
            counts = build_llm_raw_filing_display_for_available_universe(
                args.jurisdiction,
                limit=args.limit,
                force=args.force,
                model=args.model,
                heuristic_only=args.heuristic_only,
            )
        else:
            counts = build_llm_raw_filing_display(
                args.jurisdiction,
                entity_id=args.entity,
                ticker=args.ticker,
                filing_id=args.filing_id,
                fiscal_period=args.fiscal_period,
                year_min=args.year_min,
                year_max=args.year_max,
                statements=args.statement,
                force=args.force,
                model=args.model,
                heuristic_only=args.heuristic_only,
            )
        print("llm raw filing display: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "statement-compare":
        compare_kwargs = {
            "tickers": args.ticker,
            "jurisdiction": args.jurisdiction,
            "statements": args.statement,
            "fiscal_period": args.period,
            "n_periods": args.n_periods,
        }
        if args.review:
            print(compare_golden_statements_review(max_rows=args.max_review_rows, **compare_kwargs))
        elif args.summary:
            print(compare_golden_statements_summary(**compare_kwargs))
        else:
            print(compare_golden_statements_json(**compare_kwargs))
    elif args.cmd == "supplemental-text-evidence":
        if args.audit:
            print(supplemental_text_evidence_summary_json(limit=args.limit))
        else:
            counts = build_us_supplemental_text_evidence(
                entity_ids=args.entity,
                full=args.full,
                limit=args.limit if not args.full else None,
            )
            print("supplemental text evidence: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    elif args.cmd == "news":
        return run_news_cli(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
