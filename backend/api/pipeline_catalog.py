from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ParamType = Literal["string", "integer", "number", "boolean", "string_list", "multi_choice"]


@dataclass(frozen=True)
class PipelineParam:
    name: str
    label: str
    param_type: ParamType = "string"
    flag: str | None = None
    positional: bool = False
    choices: list[str] | None = None
    choice_descriptions: dict[str, str] | None = None
    default: Any = None
    help: str | None = None
    multiple: bool = False

    def public(self, help_text: str | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.param_type,
            "flag": self.flag,
            "positional": self.positional,
            "choices": self.choices,
            "choice_descriptions": self.choice_descriptions,
            "default": self.default,
            "help": help_text or self.help,
            "multiple": self.multiple,
        }


@dataclass(frozen=True)
class PipelineCommand:
    key: str
    label: str
    category: str
    base: list[str]
    description: str
    params: list[PipelineParam] = field(default_factory=list)
    destructive: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "base": self.base,
            "description": self.description,
            "destructive": self.destructive,
            "params": [p.public(_param_help(self.key, p)) for p in self.params],
        }


PARAM_HELP: dict[str, str] = {
    "jurisdiction": "Market or filing jurisdiction passed to the existing CLI command.",
    "entity": "Specific CIKs for US or EDINET codes for JP. Leave blank to use the pipeline scope.",
    "ticker": "Specific tickers to process. Leave blank to use the active/default ticker universe.",
    "download": "Acquire new source data from upstream APIs or remote files before downstream processing.",
    "full": "Ignore incremental state where supported and process the full applicable scope for this command.",
    "limit": "Maximum number of items to process in this run.",
    "max_ciks": "Maximum number of US CIKs to include in the command scope.",
    "filed_date_max": "JP only: do not process filings after this YYYY-MM-DD filed date.",
    "since": "US only: daily-index start date (YYYY-MM-DD) for the companyfacts refresh window. Blank = derive from the parsed-filings watermark.",
    "lookback_days": "US only: minimum daily-index lookback in days when no watermark exists. Blank = configured default (14).",
    "days": "JP master refresh lookback window in calendar days.",
    "start_date": "Inclusive start date in YYYY-MM-DD format.",
    "end_date": "Inclusive end date in YYYY-MM-DD format. Leave blank for the command default.",
    "group": "Named pipeline sample group to activate or replenish.",
    "target": "Target number of entities for scope activation.",
    "all_eligible": "Activate every eligible entity instead of only filling to the target count.",
    "include_comparatives": "US parse mode: include comparative periods instead of only current 10-K annual facts.",
    "filing_types": "US SEC forms to include for acquisition/parsing.",
    "jp_filing_types": "JP EDINET document type codes to include for indexing, acquisition, and parsing.",
    "allow_global_reset": "Allow an unsafe full-jurisdiction JP reparse. Prefer entity-scoped runs or JP rebuild-local.",
    "chunk_size": "Number of entities/tickers handled by each processing chunk.",
    "no_resume": "Disable resume behavior and start JP rebuild-local chunks from the beginning.",
    "no_downstream": "Skip downstream standardization, metrics, and recon after raw rebuild.",
    "sync_index": "Refresh the local filing index before processing local JP XBRL files.",
    "dry_run": "Preview work without writing changes where the command supports dry-run mode.",
    "force": "Ignore existing local state/files and rerun the acquisition or processing step.",
    "doc": "Specific JP EDINET document IDs or source document identifiers to process.",
    "workers": "Parallel worker count used by commands that support multiprocessing.",
    "new_only": "US extraction: skip ZIP stems that already have extracted output.",
    "path": "Optional local registry/spec path passed to the sync command.",
    "no_formulas": "Sync registry metadata without updating formula definitions.",
    "max_tickers": "Maximum number of tickers to include in enrichment or risk computations.",
    "isin": "Run ISIN enrichment for the selected company master.",
    "gics": "Run GICS sector/industry enrichment for the selected company master.",
    "identity": "Run JP issuer identity enrichment.",
    "series": "Specific macro/FRED series IDs to fetch. Leave blank for configured defaults.",
    "betas": "After deriving market items, also compute beta metrics.",
    "full_library": "Fama-French only: fetch the entire Ken French CSV catalogue, not only essential factor datasets.",
    "apply": "Commit the cleanup or mutation. Without this, cleanup-style commands report a dry-run preview.",
    "implied_retention_years": "Number of years of implied factor-return rows to retain.",
    "keep_all_implied": "Keep all implied factor-return rows instead of applying the retention window.",
    "models": "Comma-separated factor model families to compute, such as FF3,FF4,FF5,FF6.",
    "index": "Yahoo global index codes to process. Leave blank to use the full configured non-Japan universe.",
    "fallback_dir": "Directory containing local fallback CSV files named by Yahoo global index code.",
    "validate": "Validate discovered tickers through Yahoo Finance before writing dim_company_intl and membership rows.",
    "use_wikipedia": "Use Wikipedia tables as the primary discovery source before local CSV fallback files.",
    "sleep_seconds": "Polite delay between Yahoo Finance requests.",
    "prices": "Also acquire Yahoo global historical prices into fact_prices_intl.",
    "price_start_date": "Inclusive historical price start date for Yahoo global price acquisition.",
    "price_end_date": "Exclusive historical price end date for Yahoo global price acquisition. Leave blank for today.",
    "full_prices": "Ignore existing fact_prices_intl history and refetch from the configured start date.",
}


COMMAND_PARAM_HELP: dict[tuple[str, str], str] = {
    ("market.fetch_fama_french", "full"): "Redownload and replace the selected Ken French datasets instead of appending only new observations.",
    ("market.fetch_fama_french", "full_library"): "Fetch all discovered Ken French CSV datasets. Leave off for the smaller production essential factor set.",
    ("market.fetch_prices", "full"): "Fetch full price history from the command default start date instead of incremental updates.",
    ("market.fetch_stock_splits", "full"): "Fetch the full configured split-event history instead of only incremental updates.",
    ("market.fetch_cross_asset", "full"): "Fetch full cross-asset history from 2000-01-01. Leave off for per-ticker incremental updates from each ticker's latest stored date.",
    ("market.derive_market_items", "full"): "Rebuild derived market metrics for the selected scope instead of incremental derivation.",
    ("risk.compute_factor_model", "full"): "Recompute factor models and implied returns for the selected universe instead of incremental computation.",
    ("fundamentals.run", "full"): "Run the jurisdiction pipeline in full mode where supported by its stages.",
    ("fundamentals.refresh_master", "full"): "Refresh the full company master scope instead of only the incremental lookback window.",
    ("fundamentals.standardize", "full"): "Rebuild standardized fundamentals for the selected jurisdiction/entity scope.",
    ("fundamentals.metrics", "full"): "Recompute metrics for the selected jurisdiction/entity scope.",
    ("fundamentals.recon", "full"): "Rebuild recon tables for the selected jurisdiction/entity scope.",
}


TYPE_HELP: dict[ParamType, str] = {
    "boolean": "Enable this CLI flag for the selected command.",
    "integer": "Integer value passed to the selected CLI command.",
    "number": "Numeric value passed to the selected CLI command.",
    "string": "Optional text value passed to the selected CLI command.",
    "string_list": "Comma or newline separated values passed repeatedly to the selected CLI command.",
    "multi_choice": "Selected checkbox values passed repeatedly to the selected CLI command.",
}

FORM_ALIASES = {
    "10K": "10-K",
    "10K/A": "10-K/A",
    "10Q": "10-Q",
    "10Q/A": "10-Q/A",
}

US_FILING_TYPE_CHOICES = ["10-K", "10-K/A", "10-Q", "10-Q/A"]
US_FILING_TYPE_DESCRIPTIONS = {
    "10-K": "Annual report with audited full-year financial statements.",
    "10-K/A": "Amendment or restatement of a 10-K annual report.",
    "10-Q": "Quarterly report with unaudited interim financial statements.",
    "10-Q/A": "Amendment or restatement of a 10-Q quarterly report.",
}
JP_FILING_TYPE_CHOICES = ["010", "020", "030", "040", "050", "120", "130", "140", "150", "160", "170"]
JP_FILING_TYPE_DESCRIPTIONS = {
    "010": "Securities notification filing.",
    "020": "Change notification for a securities notification.",
    "030": "Securities registration statement for issuance disclosure.",
    "040": "Amendment to a securities registration statement.",
    "050": "Withdrawal request for a registration statement.",
    "120": "Annual Securities Report: full-year financial report.",
    "130": "Amendment to an Annual Securities Report; parsed as restated facts.",
    "140": "Quarterly report: legacy interim financial report.",
    "150": "Amendment to a quarterly report; parsed as restated quarterly facts.",
    "160": "Semiannual report: half-year interim financial report.",
    "170": "Amendment to a semiannual report; parsed as restated H1 facts.",
}


def _param_help(command_key: str, param: PipelineParam) -> str:
    if param.help:
        return param.help
    return COMMAND_PARAM_HELP.get((command_key, param.name)) or PARAM_HELP.get(param.name) or TYPE_HELP[param.param_type]


def _canonical_param_value(param: PipelineParam, value: Any) -> Any:
    if param.name == "filing_types":
        text = str(value).strip().upper()
        return FORM_ALIASES.get(text, text)
    if param.name == "jp_filing_types":
        return str(value).strip().zfill(3)
    return value


def _effective_jurisdiction(command: PipelineCommand, params: dict[str, Any]) -> str | None:
    raw = params.get("jurisdiction")
    if raw is None:
        jur_param = next((param for param in command.params if param.name == "jurisdiction"), None)
        raw = jur_param.default if jur_param else None
    return str(raw).strip().upper() if raw else None


def _param_applicable(command: PipelineCommand, param: PipelineParam, params: dict[str, Any]) -> bool:
    jurisdiction = _effective_jurisdiction(command, params)
    if param.name == "filing_types":
        return jurisdiction == "US" or "raw_sec" in command.key or "xbrl_us" in command.key
    if param.name == "jp_filing_types":
        return jurisdiction == "JP"
    # US-only incremental companyfacts controls (daily-index window). The CLI only
    # consumes these for US run/download; never emit them for a JP run.
    if param.name in ("since", "lookback_days"):
        return jurisdiction == "US"
    if param.name == "entity" and command.key in ("fundamentals.run", "fundamentals.download"):
        return jurisdiction == "US"
    return True


def _jur(default: str | None = None) -> PipelineParam:
    return PipelineParam("jurisdiction", "Jurisdiction", positional=True, choices=["US", "JP"], default=default)


def _entity() -> PipelineParam:
    return PipelineParam("entity", "Entity IDs", "string_list", "--entity", multiple=True, help="CIKs for US or EDINET codes for JP.")


def _ticker() -> PipelineParam:
    return PipelineParam("ticker", "Tickers", "string_list", "--ticker", multiple=True)


def _yahoo_index() -> PipelineParam:
    return PipelineParam("index", "Yahoo global indices", "string_list", "--index", multiple=True)


def _fallback_dir() -> PipelineParam:
    return PipelineParam("fallback_dir", "Fallback CSV directory", "string", "--fallback-dir")


def _sleep_seconds(default: float) -> PipelineParam:
    return PipelineParam("sleep_seconds", "Sleep seconds", "number", "--sleep-seconds", default=default)


def _default_on(name: str, label: str, flag: str, help_text: str | None = None) -> PipelineParam:
    return PipelineParam(name, label, "boolean", flag, default=True, help=help_text)


def _since() -> PipelineParam:
    return PipelineParam(
        "since", "US since (YYYY-MM-DD)", "string", "--since",
        help="US only: daily-index start date for the companyfacts refresh window. Leave blank to derive it from the parsed-filings watermark.",
    )


def _lookback_days() -> PipelineParam:
    return PipelineParam(
        "lookback_days", "US lookback days", "integer", "--lookback-days",
        help="US only: minimum daily-index lookback (days) when no watermark is available. Leave blank for the configured default (14).",
    )


def _bool(name: str, label: str, flag: str, help_text: str | None = None) -> PipelineParam:
    return PipelineParam(name, label, "boolean", flag, default=False, help=help_text)


def _int(name: str, label: str, flag: str, default: int | None = None, help_text: str | None = None) -> PipelineParam:
    return PipelineParam(name, label, "integer", flag, default=default, help=help_text)


def _str(name: str, label: str, flag: str, default: str | None = None, help_text: str | None = None) -> PipelineParam:
    return PipelineParam(name, label, "string", flag, default=default, help=help_text)


def _us_filing_types() -> PipelineParam:
    return PipelineParam(
        "filing_types",
        "US filing types",
        "multi_choice",
        "--filing-type",
        choices=US_FILING_TYPE_CHOICES,
        choice_descriptions=US_FILING_TYPE_DESCRIPTIONS,
        default=US_FILING_TYPE_CHOICES,
        multiple=True,
        help="Select which SEC forms to include for this US run.",
    )


def _jp_filing_types() -> PipelineParam:
    return PipelineParam(
        "jp_filing_types",
        "JP filing types",
        "multi_choice",
        "--filing-type",
        choices=JP_FILING_TYPE_CHOICES,
        choice_descriptions=JP_FILING_TYPE_DESCRIPTIONS,
        default=JP_FILING_TYPE_CHOICES,
        multiple=True,
        help="Select which EDINET document type codes to include for this JP run.",
    )


COMMANDS: list[PipelineCommand] = [
    PipelineCommand(
        "admin.apply_schema",
        "Apply Pipeline Schema",
        "admin",
        ["apply-schema"],
        "Apply the existing xbrl_sec SQL migrations, including orchestration tables.",
    ),
    PipelineCommand(
        "fundamentals.run",
        "Run Fundamentals Pipeline",
        "fundamentals",
        ["run"],
        "End-to-end: refresh master, acquire/use-local sources, extract, parse, standardize, metrics, and recon for US or JP.",
        [_jur(), _bool("download", "Acquire from source APIs", "--download"), _bool("full", "Full mode", "--full"), _int("limit", "Download limit", "--limit"), _int("max_ciks", "Max CIKs", "--max-ciks"), _entity(), _since(), _lookback_days(), _us_filing_types(), _jp_filing_types(), _str("filed_date_max", "JP filed date max", "--filed-date-max")],
    ),
    PipelineCommand(
        "fundamentals.refresh_master",
        "Step 1: Refresh Master",
        "fundamentals",
        ["refresh-master"],
        "Refresh US/JP company master and ticker support (run_incremental stage 1).",
        [_jur(), _bool("download", "Download source metadata", "--download"), _bool("full", "Full refresh", "--full"), _int("days", "JP lookback days", "--days", 400), _str("start_date", "Start date", "--start-date"), _str("end_date", "End date", "--end-date"), _int("max_ciks", "Max CIKs", "--max-ciks")],
    ),
    PipelineCommand(
        "fundamentals.enrich_master",
        "Step 2: Enrich Master",
        "fundamentals",
        ["enrich-master"],
        "Enrich company master with ISIN, GICS sector/industry, and JP issuer identity (JP download path stage 2-3).",
        [_jur(), _bool("full", "Full enrichment", "--full"), _int("max_tickers", "Max tickers", "--max-tickers"), _bool("isin", "ISIN", "--isin"), _bool("gics", "GICS", "--gics"), _bool("identity", "Identity", "--identity")],
    ),
    PipelineCommand(
        "fundamentals.index_api",
        "Index JP API Filings (JP only)",
        "fundamentals",
        ["index-api"],
        "Index JP EDINET filings from the API into source_filings (JP download path; runs between Step 2 and Step 3 when downloading).",
        [PipelineParam("jurisdiction", "Jurisdiction", positional=True, choices=["JP"], default="JP"), _bool("full", "Full index", "--full"), _str("start_date", "Start date", "--start-date"), _str("end_date", "End date", "--end-date"), _jp_filing_types()],
    ),
    PipelineCommand(
        "fundamentals.download",
        "Step 3: Download XBRL Sources",
        "fundamentals",
        ["download"],
        "Download US SEC submissions/XBRL or JP EDINET XBRL ZIP packages (run_incremental download stage).",
        [_jur(), _bool("force", "Force redownload", "--force"), _int("limit", "Limit", "--limit"), PipelineParam("doc", "Document IDs", "string_list", "--doc", multiple=True), _int("max_ciks", "Max CIKs", "--max-ciks"), _entity(), _since(), _lookback_days(), _us_filing_types(), _jp_filing_types()],
    ),
    PipelineCommand(
        "fundamentals.sync_master",
        "Step 4: Sync Master Dimensions",
        "fundamentals",
        ["sync-master"],
        "Sync company master support dimensions used by downstream stages (JP run_incremental pre-extract).",
        [PipelineParam("jurisdiction", "Jurisdiction", positional=True, choices=["US", "JP", "ALL"], default="ALL")],
    ),
    PipelineCommand(
        "fundamentals.extract",
        "Step 5: Extract XBRL",
        "fundamentals",
        ["extract"],
        "Unpack JP EDINET ZIPs into companyfacts/{edinet_code}/ PublicDoc files, or extract US linkbases/HTML for parsing.",
        [_jur(), _entity(), PipelineParam("doc", "Document IDs", "string_list", "--doc", multiple=True), _bool("force", "Force extract", "--force"), _int("workers", "US workers", "--workers", 1), _bool("new_only", "US new output stems only", "--new-only"), _jp_filing_types()],
    ),
    PipelineCommand(
        "fundamentals.index",
        "Step 6: Index Local Facts",
        "fundamentals",
        ["index"],
        "Index local companyfacts/XBRL files so the raw parser knows what to process.",
        [_jur(), _entity(), PipelineParam("doc", "Document IDs", "string_list", "--doc", multiple=True), _jp_filing_types()],
    ),
    PipelineCommand(
        "fundamentals.parse",
        "Step 7: Parse Raw Facts",
        "fundamentals",
        ["parse"],
        "Parse local XBRL into fact_fundamentals_raw_* and mark source_filings parsed.",
        [_jur(), _entity(), PipelineParam("doc", "Document IDs", "string_list", "--doc", multiple=True), _bool("force", "Force parse", "--force"), _bool("include_comparatives", "US include comparatives", "--include-comparatives"), _us_filing_types(), _jp_filing_types(), _str("filed_date_max", "JP filed date max", "--filed-date-max")],
    ),
    PipelineCommand(
        "fundamentals.standardize",
        "Step 8: Standardize",
        "fundamentals",
        ["standardize"],
        "Populate fact_fundamentals_std_* from raw facts using line-item mappings. Incremental upsert by default; --full or entity scope deletes the targeted rows first.",
        [_jur(), _entity(), _bool("full", "Full rebuild", "--full")],
    ),
    PipelineCommand(
        "fundamentals.metrics",
        "Step 9: Compute Metrics",
        "fundamentals",
        ["metrics"],
        "Compute fact_metrics_* from standardized fundamentals. Incremental upsert by default; --full or entity scope deletes the targeted rows first.",
        [_jur(), _entity(), _bool("full", "Full rebuild", "--full")],
    ),
    PipelineCommand(
        "fundamentals.recon",
        "Step 10: Build Recon",
        "fundamentals",
        ["recon"],
        "Build fact_metrics_recon_* reconciliation tables. Incremental by default; --full or entity scope deletes the targeted rows first.",
        [_jur(), _entity(), _bool("full", "Full rebuild", "--full")],
    ),
    PipelineCommand("fundamentals.validate", "Validate", "fundamentals", ["validate"], "Run jurisdiction validation.", [_jur()]),
    PipelineCommand(
        "fundamentals.scope",
        "Manage Pipeline Scope",
        "fundamentals",
        ["scope"],
        "Activate or replenish include_in_pipeline scope for US/JP.",
        [_jur(), _str("group", "Scope group", "--group", "pilot_50_us"), _int("target", "Target entities", "--target", 50), _bool("all_eligible", "Activate all eligible", "--all-eligible")],
    ),
    PipelineCommand(
        "fundamentals.reset",
        "Reset Downstream",
        "fundamentals",
        ["reset"],
        "Delete raw/std/metrics/recon state globally or for selected entities.",
        [_jur(), _entity()],
        destructive=True,
    ),
    PipelineCommand(
        "fundamentals.reparse",
        "Reparse Fundamentals",
        "fundamentals",
        ["reparse"],
        "Reset and rebuild downstream facts for selected scope.",
        [_jur(), _entity(), _bool("include_comparatives", "US include comparatives", "--include-comparatives"), _us_filing_types(), _jp_filing_types(), _str("filed_date_max", "JP filed date max", "--filed-date-max"), _bool("allow_global_reset", "Allow global JP reset", "--allow-global-reset")],
        destructive=True,
    ),
    PipelineCommand(
        "fundamentals.rebuild_local_jp",
        "JP Rebuild Local",
        "fundamentals",
        ["rebuild-local"],
        "Safely rebuild JP from local XBRL files in resumable chunks.",
        [PipelineParam("jurisdiction", "Jurisdiction", positional=True, choices=["JP"], default="JP"), _entity(), _str("filed_date_max", "Filed date max", "--filed-date-max"), _jp_filing_types(), _int("chunk_size", "Chunk size", "--chunk-size", 25), _bool("no_resume", "Disable resume", "--no-resume"), _bool("no_downstream", "Skip downstream", "--no-downstream"), _bool("sync_index", "Sync local index", "--sync-index"), _bool("dry_run", "Dry run", "--dry-run")],
    ),
    PipelineCommand(
        "fundamentals.download_raw_sec",
        "Download Raw SEC",
        "fundamentals",
        ["download-raw-sec"],
        "SEC submissions/XBRL/linkbase acquisition only; no downstream parse.",
        [_bool("force", "Force redownload", "--force"), _int("limit", "XBRL limit", "--limit"), _int("max_ciks", "Max CIKs", "--max-ciks"), _us_filing_types()],
    ),
    PipelineCommand(
        "fundamentals.download_xbrl_us",
        "Download US XBRL ZIPs",
        "fundamentals",
        ["download-xbrl"],
        "Download US accession-matched XBRL ZIPs.",
        [PipelineParam("jurisdiction", "Jurisdiction", positional=True, choices=["US"], default="US"), _entity(), _bool("force", "Force redownload", "--force"), _int("limit", "Limit", "--limit"), _us_filing_types()],
    ),
    PipelineCommand("fundamentals.sync_refs", "Sync References", "fundamentals", ["sync-refs"], "Sync mappings, line items, metric defs, ticker support."),
    PipelineCommand("fundamentals.sync_registry", "Sync Registry", "fundamentals", ["sync-registry"], "Sync local registry/spec mappings.", [_str("path", "Registry path", "--path"), _bool("no_formulas", "Do not update formulas", "--no-formulas")]),
    PipelineCommand(
        "yahoo_global.discover_tickers",
        "Discover Yahoo Global Tickers",
        "yahoo_global",
        ["discover-yahoo-tickers"],
        "Discover non-Japan international index constituents into dim_company_intl and ref_yahoo_index_constituent.",
        [
            _yahoo_index(),
            _fallback_dir(),
            _bool("include_wholesale", "Include wholesale-exchange indices", "--include-wholesale"),
            _int("limit", "Limit per index", "--limit"),
            _bool("dry_run", "Dry run", "--dry-run"),
            _default_on("validate", "Validate tickers", "--no-validate"),
            _default_on("use_wikipedia", "Use Wikipedia", "--no-wikipedia"),
            _sleep_seconds(0.25),
        ],
    ),
    PipelineCommand(
        "yahoo_global.fetch_fundamentals",
        "Fetch Yahoo Global Fundamentals",
        "yahoo_global",
        ["fetch-yahoo-fundamentals"],
        "Fetch Yahoo Finance profile metrics and statement rows for dim_company_intl companies.",
        [
            _ticker(),
            _yahoo_index(),
            _int("limit", "Limit", "--limit"),
            _bool("dry_run", "Dry run", "--dry-run"),
            _sleep_seconds(0.5),
            _int("max_workers", "Max workers", "--max-workers", 1),
            _int("refresh_before_days", "Skip if fresher than N days", "--refresh-before-days"),
            PipelineParam("sample_group", "Sample groups", "string_list", "--sample-group", multiple=True),
            _bool("only_missing", "Only companies without any fundamentals", "--only-missing"),
            _bool("only_missing_quarterly", "Only companies without quarterly rows", "--only-missing-quarterly"),
            _bool("no_quarterly", "Skip quarterly statements", "--no-quarterly"),
            PipelineParam("rate_per_second", "Global rate limit (req/s)", "number", "--rate-per-second"),
        ],
    ),
    PipelineCommand(
        "yahoo_global.fetch_prices",
        "Fetch Yahoo Global Prices",
        "yahoo_global",
        ["fetch-yahoo-prices"],
        "Fetch historical Yahoo Finance OHLCV prices for dim_company_intl companies into fact_prices_intl.",
        [
            _ticker(),
            _yahoo_index(),
            _str("start_date", "Start date", "--start-date", "2000-01-01"),
            _str("end_date", "End date", "--end-date"),
            _bool("full", "Full history", "--full"),
            _int("limit", "Limit", "--limit"),
            _bool("dry_run", "Dry run", "--dry-run"),
            _sleep_seconds(0.25),
            PipelineParam("sample_group", "Sample groups", "string_list", "--sample-group", multiple=True),
        ],
    ),
    PipelineCommand(
        "yahoo_global.run",
        "Run Yahoo Global Pipeline",
        "yahoo_global",
        ["run-yahoo-global"],
        "Discover non-Japan tickers and then fetch Yahoo Finance fundamentals for the resulting international universe.",
        [
            _yahoo_index(),
            _fallback_dir(),
            _bool("include_wholesale", "Include wholesale-exchange indices", "--include-wholesale"),
            _int("limit", "Limit", "--limit"),
            _bool("dry_run", "Dry run", "--dry-run"),
            _default_on("validate", "Validate tickers", "--no-validate"),
            _default_on("use_wikipedia", "Use Wikipedia", "--no-wikipedia"),
            _default_on("prices", "Fetch prices", "--no-prices"),
            _str("price_start_date", "Price start date", "--price-start-date", "2000-01-01"),
            _str("price_end_date", "Price end date", "--price-end-date"),
            _bool("full_prices", "Full price history", "--full-prices"),
            _sleep_seconds(0.5),
        ],
    ),
    PipelineCommand(
        "yahoo_global.health",
        "Yahoo Global Health Check",
        "yahoo_global",
        ["health-yahoo-global"],
        "Validate a small non-Japan Yahoo Finance ticker sample to catch source-shape or rate-limit breakage.",
        [_ticker()],
    ),
    PipelineCommand(
        "market.fetch_prices",
        "Fetch Prices",
        "market",
        ["fetch-prices"],
        "Fetch yfinance prices into fact_prices_us / fact_prices_jp. By default fetches both markets; uncheck a box to skip a jurisdiction.",
        [
            # default-checked checkboxes; the underlying CLI flags are
            # --skip-us / --skip-jp emitted only when the box is UNchecked.
            PipelineParam("fetch_us", "Fetch US prices", "boolean", "--skip-us", default=True, help="Uncheck to skip US."),
            PipelineParam("fetch_jp", "Fetch JP prices", "boolean", "--skip-jp", default=True, help="Uncheck to skip JP."),
            _ticker(),
            _str("start_date", "Start date", "--start-date"),
            _str("end_date", "End date", "--end-date"),
            _bool("full", "Full history", "--full"),
        ],
    ),
    PipelineCommand("market.fetch_stock_splits", "Fetch Stock Splits", "market", ["fetch-stock-splits"], "Fetch stock split events.", [PipelineParam("jurisdiction", "Jurisdiction", flag="--jurisdiction", choices=["US", "JP", "ALL"], default="US"), _ticker(), _str("start_date", "Start date", "--start-date"), _bool("full", "Full history", "--full")]),
    PipelineCommand("market.derive_market_items", "Derive Market Items", "market", ["derive-market-items"], "Derive fact_market_metrics price/market cap and optional betas.", [_ticker(), _bool("full", "Full derive", "--full"), _bool("betas", "Compute betas", "--betas")]),
    PipelineCommand("market.fetch_fred", "Fetch FRED", "market", ["fetch-fred"], "Fetch configured or selected FRED macro series.", [PipelineParam("series", "Series IDs", "string_list", "--series", multiple=True), _str("start_date", "Start date", "--start-date"), _bool("full", "Full history", "--full")]),
    PipelineCommand("market.fetch_cross_asset", "Fetch Cross Asset", "market", ["fetch-cross-asset"], "Fetch configured cross-asset prices.", [_bool("full", "Full history", "--full")]),
    PipelineCommand("market.fetch_fama_french", "Fetch Fama-French", "market", ["fetch-fama-french"], "Load Ken French factor datasets.", [_bool("full", "Full refresh", "--full"), _bool("full_library", "Full library", "--full-library")]),
    PipelineCommand("market.cleanup_fama_french", "Cleanup Fama-French", "risk", ["cleanup-fama-french"], "Cleanup implied Fama-French storage.", [_bool("apply", "Apply cleanup", "--apply"), _int("implied_retention_years", "Implied retention years", "--implied-retention-years", 3), _bool("keep_all_implied", "Keep all implied", "--keep-all-implied")], destructive=True),
    PipelineCommand("risk.compute_factor_model", "Compute Factor Model", "risk", ["compute-factor-model"], "Compute FF factor model loadings/implied returns.", [_bool("full", "Full recompute", "--full"), _ticker(), _int("max_tickers", "Max tickers", "--max-tickers"), PipelineParam("jurisdiction", "Jurisdiction", flag="--jurisdiction", choices=["US", "JP"]), _str("models", "Models", "--models", "FF3,FF4,FF5,FF6"), _int("workers", "Workers", "--workers", 1), _int("chunk_size", "Chunk size", "--chunk-size", 25), _int("implied_retention_years", "Implied retention years", "--implied-retention-years", 3), _bool("keep_all_implied", "Keep all implied", "--keep-all-implied")], destructive=True),
]


COMMAND_BY_KEY = {cmd.key: cmd for cmd in COMMANDS}


def command_catalog() -> list[dict[str, Any]]:
    return [cmd.public() for cmd in COMMANDS]


def build_cli_argv(command_key: str, params: dict[str, Any]) -> tuple[PipelineCommand, list[str]]:
    try:
        command = COMMAND_BY_KEY[command_key]
    except KeyError as exc:
        raise ValueError(f"Unknown command key: {command_key}") from exc

    allowed = {p.name for p in command.params}
    unknown = sorted(set(params) - allowed - {"raw_extra_args"})
    if unknown:
        raise ValueError(f"Unknown parameter(s) for {command_key}: {', '.join(unknown)}")

    argv = list(command.base)
    for param in command.params:
        if not _param_applicable(command, param, params):
            continue
        value = params.get(param.name, param.default)
        if value is None or value == "" or value == []:
            continue
        if param.param_type == "boolean":
            # Emit the flag iff the value DIFFERS from the param's default.
            # For default=False params this preserves the old behavior
            # (emit when checked). For default=True params this gives an
            # inverted ("skip") flag — emit only when the user UNchecks the
            # default-on checkbox.
            if bool(value) != bool(param.default):
                if not param.flag:
                    raise ValueError(f"Boolean parameter {param.name} has no flag")
                argv.append(param.flag)
            continue
        if param.choices:
            values = [_canonical_param_value(param, v) for v in (value if isinstance(value, list) else [value])]
            bad = [str(v) for v in values if str(v) not in param.choices]
            if bad:
                raise ValueError(f"{param.name} must be one of {param.choices}; got {bad}")
            value = values if isinstance(value, list) else values[0]
        if param.param_type == "multi_choice":
            if not param.flag:
                raise ValueError(f"Parameter {param.name} has no flag")
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is None or str(item).strip() == "":
                    continue
                argv.extend([param.flag, str(item)])
            continue
        if param.positional:
            argv.append(str(value))
            continue
        if not param.flag:
            raise ValueError(f"Parameter {param.name} has no flag")
        if param.multiple:
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item is None or str(item).strip() == "":
                    continue
                argv.extend([param.flag, str(item)])
        else:
            argv.extend([param.flag, str(value)])

    extra = params.get("raw_extra_args") or []
    if extra:
        if not isinstance(extra, list) or not all(isinstance(v, str) for v in extra):
            raise ValueError("raw_extra_args must be a string list")
        argv.extend(extra)
    return command, argv
