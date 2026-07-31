from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.filings.helpers import clean_accession, dashed_accession, normalize_cik, parse_date, sha256_file
from xbrl_sec.sec.inst.classifier import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_REASONER_MODEL,
    LABELS,
    parse_reference_files,
)
from xbrl_sec.sec.inst.pipeline import (
    _dedupe_rows,
    _int_value,
    _iter_tsv,
    _iter_tsv_file,
    _num_value,
    _read_tsv,
    _read_tsv_file,
    _value,
    _zip_member,
)


_CORE_INDEX_DEFINITIONS = (
    ("idx_fact_13f_submission_dataset", "fact_13f_submission", "(dataset_key, accession_number)"),
    ("idx_core_13f_filing_dataset", "core_13f_filing", "(dataset_key, accession_number)"),
    ("idx_core_13f_holding_report_period", "core_13f_holding", "(report_period DESC, is_latest_amendment)"),
)
_13F_ACCESSION_BATCH_SIZE = 500
_13F_RAW_HOLDING_BATCH_SIZE = 50000
CORE_MANAGER_CLASSIFIER_PROMPT_VERSION = "13f_core_manager_classifier_v1"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _ensure_13f_security_lookup_indexes(cur) -> None:
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dim_company_us_isin_cusip
            ON dim_company_us ((upper(substring(isin from 3 for 9))))
            WHERE isin IS NOT NULL
        """
    )


def _security_name_key(value: str | None) -> str:
    text = "".join(ch if ch.isalnum() else " " for ch in (value or "").upper())
    suffixes = {
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
        "LTD", "LIMITED", "PLC", "SA", "NV", "AG", "SE", "LP", "LLC",
        "HOLDING", "HOLDINGS", "GROUP", "THE",
    }
    return " ".join(word for word in text.split() if word and word not in suffixes)


def _extract_json_object(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        start = (raw or "").find("{")
        end = (raw or "").rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}


def _ensure_13f_security_dim(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dim_13f_security_us (
            cusip TEXT PRIMARY KEY,
            cusip8 TEXT,
            cusip6 TEXT,
            isin TEXT,
            issuer_cik TEXT,
            primary_ticker TEXT,
            issuer_name TEXT,
            security_title TEXT,
            asset_bucket TEXT NOT NULL DEFAULT 'other',
            sector TEXT,
            industry_group TEXT,
            resolution_status TEXT NOT NULL DEFAULT 'unresolved',
            confidence_score NUMERIC,
            source_name TEXT,
            first_seen_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ,
            row_count BIGINT NOT NULL DEFAULT 0,
            value_observed NUMERIC,
            evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dim_13f_security_us_status ON dim_13f_security_us (resolution_status, asset_bucket)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dim_13f_security_us_ticker ON dim_13f_security_us (primary_ticker)")


def _ensure_13f_llm_comparison_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_13f_cusip_llm_comparison (
            cusip TEXT NOT NULL,
            cusip8 TEXT,
            cusip6 TEXT,
            observed_issuer_name TEXT,
            observed_security_title TEXT,
            deterministic_status TEXT,
            candidate_cik TEXT,
            candidate_ticker TEXT,
            candidate_name TEXT,
            confidence NUMERIC,
            accepted BOOLEAN NOT NULL DEFAULT false,
            rationale TEXT,
            candidate_count INTEGER,
            value_observed NUMERIC,
            row_count BIGINT,
            model TEXT NOT NULL,
            raw_response TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (cusip, model)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_13f_cusip_llm_comparison_accept
            ON fact_13f_cusip_llm_comparison (accepted, confidence DESC)
        """
    )


def _asset_bucket(title_of_class: str | None, sh_prn_flag: str | None, put_call: str | None) -> str:
    title = title_of_class or ""
    if (put_call or "").upper() in {"PUT", "CALL"}:
        return "derivatives"
    if (sh_prn_flag or "").upper() == "PRN" or any(token in title.lower() for token in ("note", "bond", "debenture", "debt", "convertible")):
        return "fixed_income"
    if any(token in title.lower() for token in ("etf", "fund", "index", "unit", "trust")):
        return "fund_etf"
    if not (put_call or "") and (sh_prn_flag or "SH") == "SH":
        return "equity"
    return "other"


def _load_13f_raw_parts(path: Path):
    if path.is_dir():
        return (
            _read_tsv_file(path / "SUBMISSION.tsv"),
            _read_tsv_file(path / "COVERPAGE.tsv"),
            _read_tsv_file(path / "SUMMARYPAGE.tsv"),
            path / "INFOTABLE.tsv",
            None,
        )
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return (
                _read_tsv(zf, _zip_member(zf, "SUBMISSION.tsv")),
                _read_tsv(zf, _zip_member(zf, "COVERPAGE.tsv")),
                _read_tsv(zf, _zip_member(zf, "SUMMARYPAGE.tsv")),
                None,
                _zip_member(zf, "INFOTABLE.tsv"),
            )
    return [], [], [], None, None


def _iter_13f_infotable(path: Path, infotable_file: Path | None, infotable_member: str | None):
    if infotable_file is not None:
        yield from _iter_tsv_file(infotable_file)
        return
    if infotable_member:
        with zipfile.ZipFile(path) as zf:
            yield from _iter_tsv(zf, infotable_member)


def _latest_13f_accessions(sub_by_acc: dict[str, tuple]) -> set[str]:
    latest: dict[tuple[str, Any], tuple[Any, int, str]] = {}
    for clean_acc, (manager_cik, _manager_name, _filing_type, filed_date, report_period, amend, _is_amendment) in sub_by_acc.items():
        key = (manager_cik, report_period)
        candidate = (filed_date, amend or 0, clean_acc)
        if key not in latest or candidate > latest[key]:
            latest[key] = candidate
    return {candidate[2] for candidate in latest.values()}


def import_manager_style_reference_core(spec_dir: Path | None = None) -> dict[str, int]:
    refs = parse_reference_files(spec_dir or (_project_root() / "spec"))
    rows = [
        (
            r.source_file,
            r.source_category,
            r.source_rank,
            r.canonical_name,
            r.normalized_name,
            json.dumps(list(r.aliases), ensure_ascii=False),
            r.domicile_or_headquarters,
            r.strategy_or_profile,
            r.target_label,
            r.confidence_policy,
            json.dumps(r.raw_payload, ensure_ascii=False),
        )
        for r in refs
    ]
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(
            cur,
            """
            INSERT INTO ref_13f_manager_style
                (source_file, source_category, source_rank, canonical_name, normalized_name,
                 aliases, domicile_or_headquarters, strategy_or_profile, target_label,
                 confidence_policy, raw_payload)
            VALUES %s
            ON CONFLICT (source_file, source_rank, canonical_name) DO UPDATE SET
                source_category = EXCLUDED.source_category,
                normalized_name = EXCLUDED.normalized_name,
                aliases = EXCLUDED.aliases,
                domicile_or_headquarters = EXCLUDED.domicile_or_headquarters,
                strategy_or_profile = EXCLUDED.strategy_or_profile,
                target_label = EXCLUDED.target_label,
                confidence_policy = EXCLUDED.confidence_policy,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            """,
            rows,
            page_size=1000,
        )
    return {"references": written}


def _ensure_13f_core_indexes(cur, *, concurrently: bool = False) -> None:
    mode = " CONCURRENTLY" if concurrently else ""
    for name, table, columns in _CORE_INDEX_DEFINITIONS:
        cur.execute(f"CREATE INDEX{mode} IF NOT EXISTS {name} ON {table} {columns}")


def ensure_13f_core_indexes() -> None:
    with connect() as conn:
        conn.commit()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_index i ON i.indexrelid = c.oid
                WHERE n.nspname = current_schema()
                  AND c.relname = 'idx_fact_13f_holdings_dataset'
                  AND NOT i.indisvalid
                """
            )
            if cur.fetchone():
                cur.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_fact_13f_holdings_dataset")
            _ensure_13f_core_indexes(cur, concurrently=True)


def _write_core_holding_raw_batch(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL work_mem = '256MB'")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_core_13f_holding_raw (
                accession_number TEXT NOT NULL,
                row_id TEXT NOT NULL,
                manager_cik TEXT NOT NULL,
                report_period DATE NOT NULL,
                filing_type TEXT,
                filed_date DATE,
                is_latest_amendment BOOLEAN NOT NULL,
                issuer_name TEXT,
                title_of_class TEXT,
                cusip TEXT,
                figi TEXT,
                cusip6 TEXT,
                issuer_cik TEXT,
                issuer_ticker TEXT,
                asset_bucket TEXT NOT NULL,
                value_reported NUMERIC,
                shares_or_principal NUMERIC(24,4),
                sh_prn_flag TEXT,
                put_call TEXT,
                investment_discretion TEXT,
                other_manager TEXT,
                voting_authority_sole BIGINT,
                voting_authority_shared BIGINT,
                voting_authority_none BIGINT,
                raw_payload JSONB NOT NULL
            ) ON COMMIT DROP
            """
        )
        execute_values(
            cur,
            """
            INSERT INTO tmp_core_13f_holding_raw
                (accession_number, row_id, manager_cik, report_period, filing_type, filed_date,
                 is_latest_amendment, issuer_name, title_of_class, cusip, figi, cusip6,
                 issuer_cik, issuer_ticker, asset_bucket, value_reported, shares_or_principal,
                 sh_prn_flag, put_call, investment_discretion, other_manager,
                 voting_authority_sole, voting_authority_shared, voting_authority_none, raw_payload)
            VALUES %s
            """,
            rows,
            page_size=10000,
        )
        cur.execute(
            """
            INSERT INTO core_13f_holding
                (accession_number, row_id, manager_cik, report_period, filed_date, is_latest_amendment,
                 issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                 asset_bucket, value_reported, price_at_filing, market_value_usd, shares_or_principal,
                 sh_prn_flag, put_call, investment_discretion, other_manager,
                 voting_authority_sole, voting_authority_shared, voting_authority_none,
                 issuer_resolution_status, price_covered, factor_covered, raw_payload)
            WITH price_targets AS (
                SELECT DISTINCT issuer_ticker, COALESCE(filed_date, report_period) AS target_date
                FROM tmp_core_13f_holding_raw
                WHERE issuer_ticker IS NOT NULL
            ),
            prices AS (
                SELECT pt.issuer_ticker, pt.target_date, p.close AS price
                FROM price_targets pt
                LEFT JOIN LATERAL (
                    SELECT p.close
                    FROM fact_prices_us p
                    WHERE p.ticker = pt.issuer_ticker
                      AND p.date <= pt.target_date
                      AND p.close IS NOT NULL
                    ORDER BY p.date DESC
                    LIMIT 1
                ) p ON true
            ),
            factor_targets AS (
                SELECT DISTINCT issuer_ticker, report_period
                FROM tmp_core_13f_holding_raw
                WHERE issuer_ticker IS NOT NULL
            ),
            factors AS (
                SELECT ft.issuer_ticker, ft.report_period, l.ticker AS matched_ticker
                FROM factor_targets ft
                LEFT JOIN LATERAL (
                    SELECT l.ticker
                    FROM fact_factor_loadings l
                    WHERE l.jurisdiction = 'US'
                      AND l.model = 'FF6'
                      AND l.ticker = ft.issuer_ticker
                      AND l.window_end <= ft.report_period
                    ORDER BY l.window_end DESC
                    LIMIT 1
                ) l ON true
            )
            SELECT h.accession_number,
                   h.row_id,
                   h.manager_cik,
                   h.report_period,
                   h.filed_date,
                   h.is_latest_amendment,
                   h.issuer_name,
                   h.title_of_class,
                   h.cusip,
                   h.figi,
                   h.cusip6,
                   h.issuer_cik,
                   h.issuer_ticker,
                   h.asset_bucket,
                   h.value_reported,
                   px.price,
                   CASE
                       WHEN px.price IS NOT NULL
                            AND h.shares_or_principal IS NOT NULL
                            AND h.value_reported > 0
                            AND COALESCE(h.put_call, '') = ''
                            AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                            AND (px.price * h.shares_or_principal) BETWEEN h.value_reported * 0.1 AND h.value_reported * 10
                       THEN px.price * h.shares_or_principal
                       ELSE h.value_reported
                   END,
                   h.shares_or_principal,
                   h.sh_prn_flag,
                   h.put_call,
                   h.investment_discretion,
                   h.other_manager,
                   h.voting_authority_sole,
                   h.voting_authority_shared,
                   h.voting_authority_none,
                   COALESCE(sec.resolution_status, 'unresolved'),
                   px.price IS NOT NULL,
                   fl.matched_ticker IS NOT NULL,
                   h.raw_payload
            FROM tmp_core_13f_holding_raw h
            LEFT JOIN dim_security_identifier_us sec ON sec.cusip = h.cusip
            LEFT JOIN prices px
              ON px.issuer_ticker = h.issuer_ticker
             AND px.target_date = COALESCE(h.filed_date, h.report_period)
            LEFT JOIN factors fl
              ON fl.issuer_ticker = h.issuer_ticker
             AND fl.report_period = h.report_period
            ON CONFLICT (accession_number, row_id) DO UPDATE SET
                manager_cik = EXCLUDED.manager_cik,
                report_period = EXCLUDED.report_period,
                filed_date = EXCLUDED.filed_date,
                is_latest_amendment = EXCLUDED.is_latest_amendment,
                issuer_name = EXCLUDED.issuer_name,
                title_of_class = EXCLUDED.title_of_class,
                cusip = EXCLUDED.cusip,
                figi = EXCLUDED.figi,
                cusip6 = EXCLUDED.cusip6,
                issuer_cik = EXCLUDED.issuer_cik,
                issuer_ticker = EXCLUDED.issuer_ticker,
                asset_bucket = EXCLUDED.asset_bucket,
                value_reported = EXCLUDED.value_reported,
                price_at_filing = EXCLUDED.price_at_filing,
                market_value_usd = EXCLUDED.market_value_usd,
                shares_or_principal = EXCLUDED.shares_or_principal,
                sh_prn_flag = EXCLUDED.sh_prn_flag,
                put_call = EXCLUDED.put_call,
                investment_discretion = EXCLUDED.investment_discretion,
                other_manager = EXCLUDED.other_manager,
                voting_authority_sole = EXCLUDED.voting_authority_sole,
                voting_authority_shared = EXCLUDED.voting_authority_shared,
                voting_authority_none = EXCLUDED.voting_authority_none,
                issuer_resolution_status = EXCLUDED.issuer_resolution_status,
                price_covered = EXCLUDED.price_covered,
                factor_covered = EXCLUDED.factor_covered,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            """
        )
        return cur.rowcount


def standardize_13f_from_raw_batched(dataset_key: str | None = None, limit: int | None = None, force: bool = False) -> dict[str, int]:
    """Load raw SEC 13F dataset files directly into core_13f_* without fact_13f_* cache tables."""
    ensure_13f_core_indexes()
    counts = {
        "datasets": 0,
        "managers": 0,
        "filings": 0,
        "core_holdings": 0,
        "standardized_datasets": 0,
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stg_13f_dataset
                (dataset_key, report_period, period_label, source_url, local_path, source_hash,
                 downloaded, parsed, downloaded_at, parsed_at, rows_parsed, download_error,
                 parse_error, raw_metadata)
            SELECT dataset_key,
                   NULLIF(metadata->>'quarter_end', '')::date,
                   period_label,
                   dataset_url,
                   local_path,
                   source_hash,
                   downloaded,
                   parsed,
                   downloaded_at,
                   parsed_at,
                   rows_parsed,
                   download_error,
                   parse_error,
                   metadata
            FROM source_13f_dataset_state
            ON CONFLICT (dataset_key) DO UPDATE SET
                report_period = EXCLUDED.report_period,
                period_label = EXCLUDED.period_label,
                source_url = EXCLUDED.source_url,
                local_path = EXCLUDED.local_path,
                source_hash = EXCLUDED.source_hash,
                downloaded = EXCLUDED.downloaded,
                downloaded_at = EXCLUDED.downloaded_at,
                download_error = EXCLUDED.download_error,
                raw_metadata = EXCLUDED.raw_metadata,
                updated_at = now()
            """
        )
        params: list[Any] = []
        where = "WHERE s.downloaded AND s.local_path IS NOT NULL"
        if dataset_key:
            where += " AND (upper(s.dataset_key) = %s OR upper(s.period_label) = %s)"
            params.extend([dataset_key.upper(), dataset_key.upper()])
        elif not force:
            where += " AND NOT COALESCE(d.standardized, false)"
        limit_sql = "LIMIT %s" if limit else ""
        if limit:
            params.append(limit)
        cur.execute(
            f"""
            SELECT s.dataset_key, s.local_path, s.period_label, s.source_hash
            FROM source_13f_dataset_state s
            LEFT JOIN stg_13f_dataset d ON d.dataset_key = s.dataset_key
            {where}
            ORDER BY s.dataset_key
            {limit_sql}
            """,
            params,
        )
        datasets = cur.fetchall()

    for key, local_path, _period_label, source_hash in datasets:
        path = Path(local_path)
        if not path.exists():
            continue
        submissions, coverpages, summaries, infotable_file, infotable_member = _load_13f_raw_parts(path)
        if not submissions:
            continue

        cover_by_acc = {}
        for cover in coverpages:
            acc = clean_accession(dashed_accession(_value(cover, "ACCESSION_NUMBER", "ACCESSIONNUMBER")))
            if acc:
                cover_by_acc[acc] = cover

        summary_by_acc = {}
        for row in summaries:
            accession = clean_accession(dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER")))
            if accession:
                summary_by_acc[accession] = (
                    _int_value(_value(row, "OTHERINCLUDEDMANAGERSCOUNT")),
                    _int_value(_value(row, "TABLEENTRYTOTAL")),
                    _int_value(_value(row, "TABLEVALUETOTAL")),
                )

        sub_by_acc = {}
        manager_rows = []
        filing_rows = []
        for row in submissions:
            accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
            clean_acc = clean_accession(accession)
            manager_cik = normalize_cik(_value(row, "CIK", "MANAGER_CIK", "FILINGMANAGER_CIK"))
            if not accession or not clean_acc or not manager_cik:
                continue
            cover = cover_by_acc.get(clean_acc, {})
            manager_name = (
                _value(row, "FILINGMANAGER_NAME", "MANAGER_NAME", "NAME")
                or _value(cover, "FILINGMANAGER_NAME", "MANAGER_NAME", "NAME")
                or manager_cik
            )
            report_period = parse_date(_value(row, "PERIODOFREPORT", "REPORTCALENDARORQUARTER", "REPORT_PERIOD"))
            filed_date = parse_date(_value(row, "FILING_DATE", "FILEDASOFDATE", "FILED_DATE"))
            filing_type = _value(row, "SUBMISSIONTYPE", "FORMTYPE", "FORM_TYPE")
            is_amendment = bool(filing_type and "/A" in filing_type)
            amend = _int_value(_value(row, "AMENDMENTNO", "AMENDMENT_NUMBER")) or (1 if is_amendment else 0)
            if report_period is None:
                continue
            sub_by_acc[clean_acc] = (manager_cik, manager_name, filing_type, filed_date, report_period, amend, is_amendment)
            manager_rows.append(
                (
                    manager_cik,
                    manager_name,
                    "submission",
                    _value(cover, "CRDNUMBER"),
                    _value(cover, "SECFILENUMBER"),
                    _value(cover, "FORM13FFILENUMBER"),
                    _value(cover, "REPORTTYPE"),
                    _value(cover, "FILINGMANAGER_STREET1"),
                    _value(cover, "FILINGMANAGER_STREET2"),
                    _value(cover, "FILINGMANAGER_CITY"),
                    _value(cover, "FILINGMANAGER_STATEORCOUNTRY"),
                    _value(cover, "FILINGMANAGER_ZIPCODE"),
                    report_period,
                    report_period,
                )
            )

        latest_accessions = _latest_13f_accessions(sub_by_acc)
        for clean_acc, (manager_cik, manager_name, filing_type, filed_date, report_period, amend, is_amendment) in sub_by_acc.items():
            summary = summary_by_acc.get(clean_acc, (None, None, None))
            filing_rows.append(
                (
                    dashed_accession(clean_acc),
                    manager_cik,
                    manager_name,
                    key,
                    filing_type,
                    filed_date,
                    report_period,
                    amend,
                    is_amendment,
                    clean_acc in latest_accessions,
                    summary[0],
                    summary[1],
                    summary[2],
                    source_hash,
                )
            )

        with connect() as conn, conn.cursor() as cur:
            managers = _dedupe_rows(manager_rows, 0)
            counts["managers"] += execute_values(
                cur,
                """
                INSERT INTO core_13f_manager
                    (manager_cik, legal_name, metadata_source, crd_number, sec_file_number,
                     form_13f_file_number, report_type, street1, street2, city, state, zip_code,
                     first_report_period, last_report_period)
                VALUES %s
                ON CONFLICT (manager_cik) DO UPDATE SET
                    legal_name = CASE
                        WHEN core_13f_manager.legal_name = core_13f_manager.manager_cik
                             AND EXCLUDED.legal_name <> EXCLUDED.manager_cik
                        THEN EXCLUDED.legal_name
                        ELSE COALESCE(NULLIF(core_13f_manager.legal_name, ''), EXCLUDED.legal_name)
                    END,
                    metadata_source = COALESCE(core_13f_manager.metadata_source, EXCLUDED.metadata_source),
                    crd_number = COALESCE(core_13f_manager.crd_number, EXCLUDED.crd_number),
                    sec_file_number = COALESCE(core_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                    form_13f_file_number = COALESCE(core_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                    report_type = COALESCE(core_13f_manager.report_type, EXCLUDED.report_type),
                    street1 = COALESCE(core_13f_manager.street1, EXCLUDED.street1),
                    street2 = COALESCE(core_13f_manager.street2, EXCLUDED.street2),
                    city = COALESCE(core_13f_manager.city, EXCLUDED.city),
                    state = COALESCE(core_13f_manager.state, EXCLUDED.state),
                    zip_code = COALESCE(core_13f_manager.zip_code, EXCLUDED.zip_code),
                    first_report_period = LEAST(COALESCE(core_13f_manager.first_report_period, EXCLUDED.first_report_period), COALESCE(EXCLUDED.first_report_period, core_13f_manager.first_report_period)),
                    last_report_period = GREATEST(COALESCE(core_13f_manager.last_report_period, EXCLUDED.last_report_period), COALESCE(EXCLUDED.last_report_period, core_13f_manager.last_report_period)),
                    updated_at = now()
                """,
                managers,
                page_size=1000,
            )
            counts["filings"] += execute_values(
                cur,
                """
                INSERT INTO core_13f_filing
                    (accession_number, manager_cik, manager_name, dataset_key, filing_type, filed_date,
                     report_period, amendment_number, is_amendment, is_latest_amendment,
                     other_included_managers_count, table_entry_total, table_value_total, source_hash)
                VALUES %s
                ON CONFLICT (accession_number) DO UPDATE SET
                    manager_cik = EXCLUDED.manager_cik,
                    manager_name = EXCLUDED.manager_name,
                    dataset_key = EXCLUDED.dataset_key,
                    filing_type = EXCLUDED.filing_type,
                    filed_date = EXCLUDED.filed_date,
                    report_period = EXCLUDED.report_period,
                    amendment_number = EXCLUDED.amendment_number,
                    is_amendment = EXCLUDED.is_amendment,
                    is_latest_amendment = EXCLUDED.is_latest_amendment,
                    other_included_managers_count = EXCLUDED.other_included_managers_count,
                    table_entry_total = EXCLUDED.table_entry_total,
                    table_value_total = EXCLUDED.table_value_total,
                    source_hash = EXCLUDED.source_hash,
                    updated_at = now()
                """,
                filing_rows,
                page_size=1000,
            )

        holdings = 0
        raw_batch = []
        for i, row in enumerate(_iter_13f_infotable(path, infotable_file, infotable_member), start=1):
            accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
            clean_acc = clean_accession(accession)
            sub = sub_by_acc.get(clean_acc)
            if not sub:
                continue
            manager_cik, _manager_name, filing_type, filed_date, report_period, _amend, _is_amendment = sub
            cusip = (_value(row, "CUSIP") or "").strip().upper() or None
            title = _value(row, "TITLEOFCLASS", "TITLE_OF_CLASS")
            sh_prn_flag = _value(row, "SSHPRNAMTTYPE", "SH_PRN_FLAG")
            put_call = _value(row, "PUTCALL", "PUT_CALL")
            raw_batch.append(
                (
                    accession,
                    _value(row, "INFOTABLE_SK", "INFOTABLESK") or str(i),
                    manager_cik,
                    report_period,
                    filing_type,
                    filed_date,
                    clean_acc in latest_accessions,
                    _value(row, "NAMEOFISSUER", "ISSUER_NAME"),
                    title,
                    cusip,
                    _value(row, "FIGI"),
                    cusip[:6] if cusip else None,
                    None,
                    None,
                    _asset_bucket(title, sh_prn_flag, put_call),
                    _int_value(_value(row, "VALUE", "VALUE_X1000")),
                    _num_value(_value(row, "SSHPRNAMT", "SHARES_OR_PRINCIPAL")),
                    sh_prn_flag,
                    put_call,
                    _value(row, "INVESTMENTDISCRETION", "INVESTMENT_DISCRETION"),
                    _value(row, "OTHERMANAGER", "OTHER_MANAGER"),
                    _int_value(_value(row, "VOTINGAUTHORITYSOLE", "SOLE")),
                    _int_value(_value(row, "VOTINGAUTHORITYSHARED", "SHARED")),
                    _int_value(_value(row, "VOTINGAUTHORITYNONE", "NONE")),
                    json.dumps(row),
                )
            )
            if len(raw_batch) >= _13F_RAW_HOLDING_BATCH_SIZE:
                holdings += _write_core_holding_raw_batch(raw_batch)
                raw_batch.clear()
        if raw_batch:
            holdings += _write_core_holding_raw_batch(raw_batch)
        counts["core_holdings"] += holdings

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_13f_dataset_state
                   SET parsed = true,
                       parsed_at = now(),
                       rows_parsed = %s,
                       parse_error = NULL,
                       updated_at = now()
                 WHERE dataset_key = %s
                """,
                (holdings, key),
            )
            cur.execute(
                """
                UPDATE stg_13f_dataset
                   SET parsed = true,
                       standardized = true,
                       parsed_at = now(),
                       standardized_at = now(),
                       rows_parsed = %s,
                       filings_parsed = %s,
                       holdings_parsed = %s,
                       parse_error = NULL,
                       standardize_error = NULL,
                       updated_at = now()
                 WHERE dataset_key = %s
                """,
                (holdings, len(filing_rows), holdings, key),
            )
            counts["standardized_datasets"] += cur.rowcount
        counts["datasets"] += 1

    return counts


def standardize_13f_from_legacy() -> dict[str, int]:
    """Promote the existing working 13F parser output into the lean core schema."""
    ensure_13f_core_indexes()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stg_13f_dataset
                (dataset_key, report_period, period_label, source_url, local_path, source_hash,
                 downloaded, parsed, downloaded_at, parsed_at, rows_parsed, download_error,
                 parse_error, raw_metadata)
            SELECT dataset_key,
                   NULLIF(metadata->>'quarter_end', '')::date,
                   period_label,
                   dataset_url,
                   local_path,
                   source_hash,
                   downloaded,
                   parsed,
                   downloaded_at,
                   parsed_at,
                   rows_parsed,
                   download_error,
                   parse_error,
                   metadata
            FROM source_13f_dataset_state
            ON CONFLICT (dataset_key) DO UPDATE SET
                report_period = EXCLUDED.report_period,
                period_label = EXCLUDED.period_label,
                source_url = EXCLUDED.source_url,
                local_path = EXCLUDED.local_path,
                source_hash = EXCLUDED.source_hash,
                downloaded = EXCLUDED.downloaded,
                parsed = EXCLUDED.parsed,
                downloaded_at = EXCLUDED.downloaded_at,
                parsed_at = EXCLUDED.parsed_at,
                rows_parsed = EXCLUDED.rows_parsed,
                download_error = EXCLUDED.download_error,
                parse_error = EXCLUDED.parse_error,
                raw_metadata = EXCLUDED.raw_metadata,
                updated_at = now()
            """
        )
        datasets = cur.rowcount

        cur.execute(
            """
            INSERT INTO core_13f_manager
                (manager_cik, legal_name, metadata_source, crd_number, sec_file_number,
                 form_13f_file_number, report_type, street1, street2, city, state, zip_code,
                 filing_count_primary, filing_count_other, filing_count_total,
                 first_report_period, last_report_period, last_seen_at)
            SELECT manager_cik, manager_name, name_source, crd_number, sec_file_number,
                   form_13f_file_number, report_type, street1, street2, city, state, zip_code,
                   filing_count_primary, filing_count_other, filing_count_total,
                   first_quarter_filed, last_quarter_filed, last_seen_at
            FROM dim_13f_manager
            ON CONFLICT (manager_cik) DO UPDATE SET
                legal_name = CASE
                    WHEN core_13f_manager.legal_name = core_13f_manager.manager_cik
                         AND EXCLUDED.legal_name <> EXCLUDED.manager_cik
                    THEN EXCLUDED.legal_name
                    ELSE COALESCE(NULLIF(core_13f_manager.legal_name, ''), EXCLUDED.legal_name)
                END,
                metadata_source = COALESCE(core_13f_manager.metadata_source, EXCLUDED.metadata_source),
                crd_number = COALESCE(core_13f_manager.crd_number, EXCLUDED.crd_number),
                sec_file_number = COALESCE(core_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                form_13f_file_number = COALESCE(core_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                report_type = COALESCE(core_13f_manager.report_type, EXCLUDED.report_type),
                street1 = COALESCE(core_13f_manager.street1, EXCLUDED.street1),
                street2 = COALESCE(core_13f_manager.street2, EXCLUDED.street2),
                city = COALESCE(core_13f_manager.city, EXCLUDED.city),
                state = COALESCE(core_13f_manager.state, EXCLUDED.state),
                zip_code = COALESCE(core_13f_manager.zip_code, EXCLUDED.zip_code),
                filing_count_primary = GREATEST(core_13f_manager.filing_count_primary, EXCLUDED.filing_count_primary),
                filing_count_other = GREATEST(core_13f_manager.filing_count_other, EXCLUDED.filing_count_other),
                filing_count_total = GREATEST(core_13f_manager.filing_count_total, EXCLUDED.filing_count_total),
                first_report_period = LEAST(COALESCE(core_13f_manager.first_report_period, EXCLUDED.first_report_period), COALESCE(EXCLUDED.first_report_period, core_13f_manager.first_report_period)),
                last_report_period = GREATEST(COALESCE(core_13f_manager.last_report_period, EXCLUDED.last_report_period), COALESCE(EXCLUDED.last_report_period, core_13f_manager.last_report_period)),
                last_seen_at = GREATEST(COALESCE(core_13f_manager.last_seen_at, EXCLUDED.last_seen_at), COALESCE(EXCLUDED.last_seen_at, core_13f_manager.last_seen_at)),
                updated_at = now()
            """
        )
        managers = cur.rowcount

        cur.execute(
            """
            INSERT INTO stg_13f_submission
                (accession_number, dataset_key, manager_cik, manager_name, filing_type, filed_date,
                 report_period, amendment_number, is_amendment, other_included_managers_count,
                 table_entry_total, table_value_total, cover_payload, summary_payload, submission_payload)
            SELECT s.accession_number, s.dataset_key, s.manager_cik, s.manager_name, s.filing_type,
                   s.filed_date, s.report_period, s.amendment_number, s.is_amendment,
                   sp.other_included_managers_count, sp.table_entry_total, sp.table_value_total,
                   COALESCE(c.raw_payload, '{}'::jsonb),
                   COALESCE(sp.raw_payload, '{}'::jsonb),
                   s.raw_payload
            FROM fact_13f_submission s
            LEFT JOIN fact_13f_coverpage c ON c.accession_number = s.accession_number
            LEFT JOIN fact_13f_summarypage sp ON sp.accession_number = s.accession_number
            ON CONFLICT (accession_number) DO UPDATE SET
                dataset_key = EXCLUDED.dataset_key,
                manager_cik = EXCLUDED.manager_cik,
                manager_name = EXCLUDED.manager_name,
                filing_type = EXCLUDED.filing_type,
                filed_date = EXCLUDED.filed_date,
                report_period = EXCLUDED.report_period,
                amendment_number = EXCLUDED.amendment_number,
                is_amendment = EXCLUDED.is_amendment,
                other_included_managers_count = EXCLUDED.other_included_managers_count,
                table_entry_total = EXCLUDED.table_entry_total,
                table_value_total = EXCLUDED.table_value_total,
                cover_payload = EXCLUDED.cover_payload,
                summary_payload = EXCLUDED.summary_payload,
                submission_payload = EXCLUDED.submission_payload,
                updated_at = now()
            """
        )
        submissions = cur.rowcount

        cur.execute(
            """
            INSERT INTO core_13f_filing
                (accession_number, manager_cik, manager_name, dataset_key, filing_type, filed_date,
                 report_period, amendment_number, is_amendment, is_latest_amendment,
                 other_included_managers_count, table_entry_total, table_value_total)
            SELECT s.accession_number, s.manager_cik, s.manager_name, s.dataset_key, s.filing_type,
                   s.filed_date, s.report_period, s.amendment_number, s.is_amendment,
                   s.is_latest_amendment, sp.other_included_managers_count, sp.table_entry_total,
                   sp.table_value_total
            FROM fact_13f_submission s
            LEFT JOIN fact_13f_summarypage sp ON sp.accession_number = s.accession_number
            WHERE s.report_period IS NOT NULL
            ON CONFLICT (accession_number) DO UPDATE SET
                manager_cik = EXCLUDED.manager_cik,
                manager_name = EXCLUDED.manager_name,
                dataset_key = EXCLUDED.dataset_key,
                filing_type = EXCLUDED.filing_type,
                filed_date = EXCLUDED.filed_date,
                report_period = EXCLUDED.report_period,
                amendment_number = EXCLUDED.amendment_number,
                is_amendment = EXCLUDED.is_amendment,
                is_latest_amendment = EXCLUDED.is_latest_amendment,
                other_included_managers_count = EXCLUDED.other_included_managers_count,
                table_entry_total = EXCLUDED.table_entry_total,
                table_value_total = EXCLUDED.table_value_total,
                updated_at = now()
            """
        )
        filings = cur.rowcount

        cur.execute(
            """
            INSERT INTO stg_13f_holding
                (accession_number, row_id, row_ordinal, manager_cik, report_period, filing_type, filed_date,
                 issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                 value_reported, shares_or_principal, sh_prn_flag, put_call, investment_discretion,
                 other_manager, voting_authority_sole, voting_authority_shared, voting_authority_none, raw_payload)
            SELECT accession_number, infotable_sk, NULL, manager_cik, report_period, filing_type, filed_date,
                   issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                   value_x1000, shares_or_principal, sh_prn_flag, put_call, investment_discretion,
                   other_manager, voting_authority_sole, voting_authority_shared, voting_authority_none, raw_payload
            FROM fact_13f_holdings
            ON CONFLICT (accession_number, row_id) DO UPDATE SET
                manager_cik = EXCLUDED.manager_cik,
                report_period = EXCLUDED.report_period,
                filing_type = EXCLUDED.filing_type,
                filed_date = EXCLUDED.filed_date,
                issuer_name = EXCLUDED.issuer_name,
                title_of_class = EXCLUDED.title_of_class,
                cusip = EXCLUDED.cusip,
                figi = EXCLUDED.figi,
                cusip6 = EXCLUDED.cusip6,
                issuer_cik = EXCLUDED.issuer_cik,
                issuer_ticker = EXCLUDED.issuer_ticker,
                value_reported = EXCLUDED.value_reported,
                shares_or_principal = EXCLUDED.shares_or_principal,
                sh_prn_flag = EXCLUDED.sh_prn_flag,
                put_call = EXCLUDED.put_call,
                investment_discretion = EXCLUDED.investment_discretion,
                other_manager = EXCLUDED.other_manager,
                voting_authority_sole = EXCLUDED.voting_authority_sole,
                voting_authority_shared = EXCLUDED.voting_authority_shared,
                voting_authority_none = EXCLUDED.voting_authority_none,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            """
        )
        stg_holdings = cur.rowcount

        cur.execute(
            """
            INSERT INTO core_13f_holding
                (accession_number, row_id, manager_cik, report_period, filed_date, is_latest_amendment,
                 issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                 asset_bucket, value_reported, price_at_filing, market_value_usd, shares_or_principal,
                 sh_prn_flag, put_call, investment_discretion, other_manager,
                 voting_authority_sole, voting_authority_shared, voting_authority_none,
                 issuer_resolution_status, price_covered, factor_covered, raw_payload)
            SELECT h.accession_number, h.infotable_sk, h.manager_cik, h.report_period, h.filed_date,
                   h.is_latest_amendment, h.issuer_name, h.title_of_class, h.cusip, h.figi, h.cusip6,
                   h.issuer_cik, h.issuer_ticker,
                   CASE
                       WHEN UPPER(COALESCE(h.put_call, '')) IN ('PUT', 'CALL') THEN 'derivatives'
                       WHEN UPPER(COALESCE(h.sh_prn_flag, '')) = 'PRN'
                            OR h.title_of_class ~* '(note|bond|debenture|debt|convertible)' THEN 'fixed_income'
                       WHEN h.title_of_class ~* '(etf|fund|index|unit|trust)' THEN 'fund_etf'
                       WHEN COALESCE(h.put_call, '') = '' AND COALESCE(h.sh_prn_flag, 'SH') = 'SH' THEN 'equity'
                       ELSE 'other'
                   END AS asset_bucket,
                   h.value_x1000,
                   px.price,
                   CASE
                       WHEN px.price IS NOT NULL
                            AND h.shares_or_principal IS NOT NULL
                            AND h.value_x1000 > 0
                            AND COALESCE(h.put_call, '') = ''
                            AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                            AND (px.price * h.shares_or_principal) BETWEEN h.value_x1000 * 0.1 AND h.value_x1000 * 10
                       THEN px.price * h.shares_or_principal
                       ELSE h.value_x1000
                   END AS market_value_usd,
                   h.shares_or_principal, h.sh_prn_flag, h.put_call, h.investment_discretion,
                   h.other_manager, h.voting_authority_sole, h.voting_authority_shared,
                   h.voting_authority_none, COALESCE(sec.resolution_status, 'unresolved'),
                   px.price IS NOT NULL,
                   fl.ticker IS NOT NULL,
                   h.raw_payload
            FROM fact_13f_holdings h
            LEFT JOIN dim_security_identifier_us sec ON sec.cusip = h.cusip
            LEFT JOIN LATERAL (
                SELECT p.close AS price
                FROM fact_prices_us p
                WHERE p.ticker = h.issuer_ticker
                  AND p.date <= COALESCE(h.filed_date, h.report_period)
                  AND p.close IS NOT NULL
                ORDER BY p.date DESC
                LIMIT 1
            ) px ON true
            LEFT JOIN LATERAL (
                SELECT ticker
                FROM fact_factor_loadings l
                WHERE l.jurisdiction = 'US'
                  AND l.model = 'FF6'
                  AND l.ticker = h.issuer_ticker
                  AND l.window_end <= h.report_period
                ORDER BY l.window_end DESC
                LIMIT 1
            ) fl ON true
            ON CONFLICT (accession_number, row_id) DO UPDATE SET
                manager_cik = EXCLUDED.manager_cik,
                report_period = EXCLUDED.report_period,
                filed_date = EXCLUDED.filed_date,
                is_latest_amendment = EXCLUDED.is_latest_amendment,
                issuer_name = EXCLUDED.issuer_name,
                title_of_class = EXCLUDED.title_of_class,
                cusip = EXCLUDED.cusip,
                issuer_cik = EXCLUDED.issuer_cik,
                issuer_ticker = EXCLUDED.issuer_ticker,
                asset_bucket = EXCLUDED.asset_bucket,
                value_reported = EXCLUDED.value_reported,
                price_at_filing = EXCLUDED.price_at_filing,
                market_value_usd = EXCLUDED.market_value_usd,
                shares_or_principal = EXCLUDED.shares_or_principal,
                sh_prn_flag = EXCLUDED.sh_prn_flag,
                put_call = EXCLUDED.put_call,
                issuer_resolution_status = EXCLUDED.issuer_resolution_status,
                price_covered = EXCLUDED.price_covered,
                factor_covered = EXCLUDED.factor_covered,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
            """
        )
        core_holdings = cur.rowcount

        cur.execute(
            """
            UPDATE stg_13f_dataset d
               SET standardized = true,
                   standardized_at = now(),
                   filings_parsed = COALESCE(f.filings, 0),
                   holdings_parsed = COALESCE(h.holdings, 0),
                   updated_at = now()
              FROM (
                    SELECT dataset_key, COUNT(*) AS filings
                    FROM core_13f_filing
                    GROUP BY dataset_key
              ) f
              LEFT JOIN (
                    SELECT dataset_key, COUNT(*) AS holdings
                    FROM core_13f_filing cf
                    JOIN core_13f_holding ch ON ch.accession_number = cf.accession_number
                    GROUP BY dataset_key
              ) h ON h.dataset_key = f.dataset_key
             WHERE d.dataset_key = f.dataset_key
            """
        )
        standardized = cur.rowcount

    return {
        "datasets": datasets,
        "managers": managers,
        "submissions": submissions,
        "filings": filings,
        "stg_holdings": stg_holdings,
        "core_holdings": core_holdings,
        "standardized_datasets": standardized,
    }


def standardize_13f_from_legacy_batched(dataset_key: str | None = None, limit: int | None = None) -> dict[str, int]:
    """Batched variant used for production runs; commits holdings one dataset at a time."""
    counts = {
        "datasets": 0,
        "managers": 0,
        "submissions": 0,
        "filings": 0,
        "stg_holdings": 0,
        "core_holdings": 0,
        "standardized_datasets": 0,
    }
    ensure_13f_core_indexes()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO stg_13f_dataset
                (dataset_key, report_period, period_label, source_url, local_path, source_hash,
                 downloaded, parsed, downloaded_at, parsed_at, rows_parsed, download_error,
                 parse_error, raw_metadata)
            SELECT dataset_key,
                   NULLIF(metadata->>'quarter_end', '')::date,
                   period_label,
                   dataset_url,
                   local_path,
                   source_hash,
                   downloaded,
                   parsed,
                   downloaded_at,
                   parsed_at,
                   rows_parsed,
                   download_error,
                   parse_error,
                   metadata
            FROM source_13f_dataset_state
            ON CONFLICT (dataset_key) DO UPDATE SET
                report_period = EXCLUDED.report_period,
                period_label = EXCLUDED.period_label,
                source_url = EXCLUDED.source_url,
                local_path = EXCLUDED.local_path,
                source_hash = EXCLUDED.source_hash,
                downloaded = EXCLUDED.downloaded,
                parsed = EXCLUDED.parsed,
                downloaded_at = EXCLUDED.downloaded_at,
                parsed_at = EXCLUDED.parsed_at,
                rows_parsed = EXCLUDED.rows_parsed,
                download_error = EXCLUDED.download_error,
                parse_error = EXCLUDED.parse_error,
                raw_metadata = EXCLUDED.raw_metadata,
                updated_at = now()
            """
        )
        counts["datasets"] = cur.rowcount
        cur.execute(
            """
            INSERT INTO core_13f_manager
                (manager_cik, legal_name, metadata_source, crd_number, sec_file_number,
                 form_13f_file_number, report_type, street1, street2, city, state, zip_code,
                 filing_count_primary, filing_count_other, filing_count_total,
                 first_report_period, last_report_period, last_seen_at)
            SELECT manager_cik, manager_name, name_source, crd_number, sec_file_number,
                   form_13f_file_number, report_type, street1, street2, city, state, zip_code,
                   filing_count_primary, filing_count_other, filing_count_total,
                   first_quarter_filed, last_quarter_filed, last_seen_at
            FROM dim_13f_manager
            ON CONFLICT (manager_cik) DO UPDATE SET
                legal_name = COALESCE(NULLIF(core_13f_manager.legal_name, ''), EXCLUDED.legal_name),
                metadata_source = COALESCE(core_13f_manager.metadata_source, EXCLUDED.metadata_source),
                filing_count_primary = GREATEST(core_13f_manager.filing_count_primary, EXCLUDED.filing_count_primary),
                filing_count_other = GREATEST(core_13f_manager.filing_count_other, EXCLUDED.filing_count_other),
                filing_count_total = GREATEST(core_13f_manager.filing_count_total, EXCLUDED.filing_count_total),
                first_report_period = LEAST(COALESCE(core_13f_manager.first_report_period, EXCLUDED.first_report_period), COALESCE(EXCLUDED.first_report_period, core_13f_manager.first_report_period)),
                last_report_period = GREATEST(COALESCE(core_13f_manager.last_report_period, EXCLUDED.last_report_period), COALESCE(EXCLUDED.last_report_period, core_13f_manager.last_report_period)),
                updated_at = now()
            """
        )
        counts["managers"] = cur.rowcount
        params: list[Any] = []
        where = "WHERE parsed"
        if dataset_key:
            where += " AND dataset_key = %s"
            params.append(dataset_key.upper())
        else:
            where += " AND NOT standardized"
        limit_sql = "LIMIT %s" if limit else ""
        if limit:
            params.append(limit)
        cur.execute(
            f"""
            SELECT dataset_key
            FROM stg_13f_dataset
            {where}
            ORDER BY dataset_key
            {limit_sql}
            """,
            params,
        )
        datasets = [row[0] for row in cur.fetchall()]
        cur.execute(
            """
            INSERT INTO stg_13f_submission
                (accession_number, dataset_key, manager_cik, manager_name, filing_type, filed_date,
                 report_period, amendment_number, is_amendment, other_included_managers_count,
                 table_entry_total, table_value_total, cover_payload, summary_payload, submission_payload)
            SELECT s.accession_number, s.dataset_key, s.manager_cik, s.manager_name, s.filing_type,
                   s.filed_date, s.report_period, s.amendment_number, s.is_amendment,
                   sp.other_included_managers_count, sp.table_entry_total, sp.table_value_total,
                   COALESCE(c.raw_payload, '{}'::jsonb),
                   COALESCE(sp.raw_payload, '{}'::jsonb),
                   s.raw_payload
            FROM fact_13f_submission s
            LEFT JOIN fact_13f_coverpage c ON c.accession_number = s.accession_number
            LEFT JOIN fact_13f_summarypage sp ON sp.accession_number = s.accession_number
            WHERE s.dataset_key = ANY(%s)
            ON CONFLICT (accession_number) DO UPDATE SET
                dataset_key = EXCLUDED.dataset_key,
                manager_cik = EXCLUDED.manager_cik,
                manager_name = EXCLUDED.manager_name,
                filing_type = EXCLUDED.filing_type,
                filed_date = EXCLUDED.filed_date,
                report_period = EXCLUDED.report_period,
                amendment_number = EXCLUDED.amendment_number,
                is_amendment = EXCLUDED.is_amendment,
                other_included_managers_count = EXCLUDED.other_included_managers_count,
                table_entry_total = EXCLUDED.table_entry_total,
                table_value_total = EXCLUDED.table_value_total,
                cover_payload = EXCLUDED.cover_payload,
                summary_payload = EXCLUDED.summary_payload,
                submission_payload = EXCLUDED.submission_payload,
                updated_at = now()
            """,
            (datasets,),
        )
        counts["submissions"] = cur.rowcount
        cur.execute(
            """
            INSERT INTO core_13f_filing
                (accession_number, manager_cik, manager_name, dataset_key, filing_type, filed_date,
                 report_period, amendment_number, is_amendment, is_latest_amendment,
                 other_included_managers_count, table_entry_total, table_value_total)
            SELECT s.accession_number, s.manager_cik, s.manager_name, s.dataset_key, s.filing_type,
                   s.filed_date, s.report_period, s.amendment_number, s.is_amendment,
                   s.is_latest_amendment, sp.other_included_managers_count, sp.table_entry_total,
                   sp.table_value_total
            FROM fact_13f_submission s
            LEFT JOIN fact_13f_summarypage sp ON sp.accession_number = s.accession_number
            WHERE s.report_period IS NOT NULL
              AND s.dataset_key = ANY(%s)
            ON CONFLICT (accession_number) DO UPDATE SET
                manager_cik = EXCLUDED.manager_cik,
                manager_name = EXCLUDED.manager_name,
                dataset_key = EXCLUDED.dataset_key,
                filing_type = EXCLUDED.filing_type,
                filed_date = EXCLUDED.filed_date,
                report_period = EXCLUDED.report_period,
                amendment_number = EXCLUDED.amendment_number,
                is_amendment = EXCLUDED.is_amendment,
                is_latest_amendment = EXCLUDED.is_latest_amendment,
                other_included_managers_count = EXCLUDED.other_included_managers_count,
                table_entry_total = EXCLUDED.table_entry_total,
                table_value_total = EXCLUDED.table_value_total,
                updated_at = now()
            """,
            (datasets,),
        )
        counts["filings"] = cur.rowcount

    for key in datasets:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT accession_number
                FROM fact_13f_submission
                WHERE dataset_key = %s
                ORDER BY accession_number
                """,
                (key,),
            )
            accessions = [row[0] for row in cur.fetchall()]

        for accession_batch in _chunks(accessions, _13F_ACCESSION_BATCH_SIZE):
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SET LOCAL work_mem = '256MB'")
                cur.execute(
                """
                INSERT INTO stg_13f_holding
                    (accession_number, row_id, row_ordinal, manager_cik, report_period, filing_type, filed_date,
                     issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                     value_reported, shares_or_principal, sh_prn_flag, put_call, investment_discretion,
                     other_manager, voting_authority_sole, voting_authority_shared, voting_authority_none, raw_payload)
                SELECT h.accession_number, h.infotable_sk, NULL, h.manager_cik, h.report_period, h.filing_type, h.filed_date,
                       h.issuer_name, h.title_of_class, h.cusip, h.figi, h.cusip6, h.issuer_cik, h.issuer_ticker,
                       h.value_x1000, h.shares_or_principal, h.sh_prn_flag, h.put_call, h.investment_discretion,
                       h.other_manager, h.voting_authority_sole, h.voting_authority_shared, h.voting_authority_none, h.raw_payload
                FROM (
                    SELECT accession_number
                    FROM fact_13f_submission
                    WHERE dataset_key = %s
                      AND accession_number = ANY(%s)
                ) s
                JOIN LATERAL (
                    SELECT *
                    FROM fact_13f_holdings h
                    WHERE h.accession_number = s.accession_number
                    OFFSET 0
                ) h ON true
                ON CONFLICT (accession_number, row_id) DO UPDATE SET
                    manager_cik = EXCLUDED.manager_cik,
                    report_period = EXCLUDED.report_period,
                    issuer_name = EXCLUDED.issuer_name,
                    title_of_class = EXCLUDED.title_of_class,
                    cusip = EXCLUDED.cusip,
                    issuer_cik = EXCLUDED.issuer_cik,
                    issuer_ticker = EXCLUDED.issuer_ticker,
                    value_reported = EXCLUDED.value_reported,
                    shares_or_principal = EXCLUDED.shares_or_principal,
                    sh_prn_flag = EXCLUDED.sh_prn_flag,
                    put_call = EXCLUDED.put_call,
                    raw_payload = EXCLUDED.raw_payload,
                    updated_at = now()
                """,
                (key, accession_batch),
            )
                counts["stg_holdings"] += cur.rowcount
                cur.execute(
                """
                INSERT INTO core_13f_holding
                    (accession_number, row_id, manager_cik, report_period, filed_date, is_latest_amendment,
                     issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
                     asset_bucket, value_reported, price_at_filing, market_value_usd, shares_or_principal,
                     sh_prn_flag, put_call, investment_discretion, other_manager,
                     voting_authority_sole, voting_authority_shared, voting_authority_none,
                     issuer_resolution_status, price_covered, factor_covered, raw_payload)
                WITH dataset_accessions AS (
                    SELECT accession_number
                    FROM fact_13f_submission
                    WHERE dataset_key = %s
                      AND accession_number = ANY(%s)
                ),
                base AS (
                    SELECT h.*
                    FROM dataset_accessions da
                    JOIN LATERAL (
                        SELECT *
                        FROM fact_13f_holdings h
                        WHERE h.accession_number = da.accession_number
                        OFFSET 0
                    ) h ON true
                ),
                price_targets AS (
                    SELECT DISTINCT issuer_ticker, COALESCE(filed_date, report_period) AS target_date
                    FROM base
                    WHERE issuer_ticker IS NOT NULL
                ),
                prices AS (
                    SELECT pt.issuer_ticker, pt.target_date, p.close AS price
                    FROM price_targets pt
                    LEFT JOIN LATERAL (
                        SELECT p.close
                        FROM fact_prices_us p
                        WHERE p.ticker = pt.issuer_ticker
                          AND p.date <= pt.target_date
                          AND p.close IS NOT NULL
                        ORDER BY p.date DESC
                        LIMIT 1
                    ) p ON true
                ),
                factor_targets AS (
                    SELECT DISTINCT issuer_ticker, report_period
                    FROM base
                    WHERE issuer_ticker IS NOT NULL
                ),
                factors AS (
                    SELECT ft.issuer_ticker, ft.report_period, l.ticker AS matched_ticker
                    FROM factor_targets ft
                    LEFT JOIN LATERAL (
                        SELECT l.ticker
                        FROM fact_factor_loadings l
                        WHERE l.jurisdiction = 'US'
                          AND l.model = 'FF6'
                          AND l.ticker = ft.issuer_ticker
                          AND l.window_end <= ft.report_period
                        ORDER BY l.window_end DESC
                        LIMIT 1
                    ) l ON true
                )
                SELECT h.accession_number, h.infotable_sk, h.manager_cik, h.report_period, h.filed_date,
                       h.is_latest_amendment, h.issuer_name, h.title_of_class, h.cusip, h.figi, h.cusip6,
                       h.issuer_cik, h.issuer_ticker,
                       CASE
                           WHEN UPPER(COALESCE(h.put_call, '')) IN ('PUT', 'CALL') THEN 'derivatives'
                           WHEN UPPER(COALESCE(h.sh_prn_flag, '')) = 'PRN'
                                OR h.title_of_class ~* '(note|bond|debenture|debt|convertible)' THEN 'fixed_income'
                           WHEN h.title_of_class ~* '(etf|fund|index|unit|trust)' THEN 'fund_etf'
                           WHEN COALESCE(h.put_call, '') = '' AND COALESCE(h.sh_prn_flag, 'SH') = 'SH' THEN 'equity'
                           ELSE 'other'
                       END,
                       h.value_x1000,
                       px.price,
                       CASE
                           WHEN px.price IS NOT NULL
                                AND h.shares_or_principal IS NOT NULL
                                AND h.value_x1000 > 0
                                AND COALESCE(h.put_call, '') = ''
                                AND COALESCE(h.sh_prn_flag, 'SH') = 'SH'
                                AND (px.price * h.shares_or_principal) BETWEEN h.value_x1000 * 0.1 AND h.value_x1000 * 10
                           THEN px.price * h.shares_or_principal
                           ELSE h.value_x1000
                       END,
                       h.shares_or_principal, h.sh_prn_flag, h.put_call, h.investment_discretion,
                       h.other_manager, h.voting_authority_sole, h.voting_authority_shared,
                       h.voting_authority_none, COALESCE(sec.resolution_status, 'unresolved'),
                       px.price IS NOT NULL,
                       fl.matched_ticker IS NOT NULL,
                       h.raw_payload
                FROM base h
                LEFT JOIN dim_security_identifier_us sec ON sec.cusip = h.cusip
                LEFT JOIN prices px
                  ON px.issuer_ticker = h.issuer_ticker
                 AND px.target_date = COALESCE(h.filed_date, h.report_period)
                LEFT JOIN factors fl
                  ON fl.issuer_ticker = h.issuer_ticker
                 AND fl.report_period = h.report_period
                ON CONFLICT (accession_number, row_id) DO UPDATE SET
                    manager_cik = EXCLUDED.manager_cik,
                    report_period = EXCLUDED.report_period,
                    filed_date = EXCLUDED.filed_date,
                    is_latest_amendment = EXCLUDED.is_latest_amendment,
                    issuer_name = EXCLUDED.issuer_name,
                    title_of_class = EXCLUDED.title_of_class,
                    cusip = EXCLUDED.cusip,
                    issuer_cik = EXCLUDED.issuer_cik,
                    issuer_ticker = EXCLUDED.issuer_ticker,
                    asset_bucket = EXCLUDED.asset_bucket,
                    value_reported = EXCLUDED.value_reported,
                    price_at_filing = EXCLUDED.price_at_filing,
                    market_value_usd = EXCLUDED.market_value_usd,
                    shares_or_principal = EXCLUDED.shares_or_principal,
                    sh_prn_flag = EXCLUDED.sh_prn_flag,
                    put_call = EXCLUDED.put_call,
                    issuer_resolution_status = EXCLUDED.issuer_resolution_status,
                    price_covered = EXCLUDED.price_covered,
                    factor_covered = EXCLUDED.factor_covered,
                    raw_payload = EXCLUDED.raw_payload,
                    updated_at = now()
                """,
                (key, accession_batch),
            )
                counts["core_holdings"] += cur.rowcount
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stg_13f_dataset
                   SET standardized = true,
                       standardized_at = now(),
                       holdings_parsed = (
                           SELECT COUNT(*)
                           FROM core_13f_holding ch
                           JOIN core_13f_filing cf ON cf.accession_number = ch.accession_number
                           WHERE cf.dataset_key = %s
                       ),
                       filings_parsed = (
                           SELECT COUNT(*) FROM core_13f_filing WHERE dataset_key = %s
                       ),
                       updated_at = now()
                 WHERE dataset_key = %s
                """,
                (key, key, key),
            )
            counts["standardized_datasets"] += cur.rowcount
    return counts


def compute_13f_manager_period() -> dict[str, int]:
    rows = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL work_mem = '512MB'")
        cur.execute(
            """
            SELECT DISTINCT report_period
            FROM core_13f_holding
            WHERE report_period IS NOT NULL
            ORDER BY report_period
            """
        )
        periods = [row[0] for row in cur.fetchall()]
        for period in periods:
            cur.execute(
                """
                SELECT DISTINCT manager_cik
                FROM core_13f_holding
                WHERE report_period = %s
                ORDER BY manager_cik
                """,
                (period,),
            )
            managers = [row[0] for row in cur.fetchall()]
            for manager_batch in _chunks(managers, 100):
                cur.execute(
            """
            INSERT INTO core_13f_manager_period
                (manager_cik, report_period, latest_accession_number, filed_date, filing_type,
                 portfolio_value_reported, portfolio_value_market, long_market_value,
                 equity_value, fixed_income_value, fund_etf_value, derivatives_value, other_value,
                 equity_pct, fixed_income_pct, fund_etf_pct, derivatives_pct, other_pct,
                 position_count, derivative_position_count, top_5_concentration, top_10_concentration,
                 max_position_weight, options_ratio, unresolved_value, unresolved_weight,
                 price_coverage_weight, factor_coverage_weight, beta_mkt, beta_smb, beta_hml,
                 beta_mom, beta_rmw, beta_cma, factor_observations, median_turnover_rate,
                 max_turnover_rate, median_options_ratio_8q, mean_position_count_8q,
                 shares_voting_sole_pct, metrics_payload)
            WITH h AS (
                SELECT ch.*, cf.filing_type, cf.filed_date AS filing_filed_date,
                       COALESCE(ch.market_value_usd, ch.value_reported, 0)::numeric AS mv
                FROM core_13f_holding ch
                JOIN core_13f_filing cf ON cf.accession_number = ch.accession_number
                WHERE ch.is_latest_amendment
                  AND cf.is_latest_amendment
                  AND ch.report_period = %s
                  AND ch.manager_cik = ANY(%s)
            ),
            totals AS (
                SELECT manager_cik, report_period,
                       MAX(accession_number) AS latest_accession_number,
                       MAX(filing_filed_date) AS filed_date,
                       MAX(filing_type) AS filing_type,
                       SUM(value_reported) AS reported_total,
                       SUM(mv) AS market_total,
                       SUM(mv) FILTER (WHERE asset_bucket <> 'derivatives') AS long_total,
                       SUM(mv) FILTER (WHERE asset_bucket = 'equity') AS equity_value,
                       SUM(mv) FILTER (WHERE asset_bucket = 'fixed_income') AS fixed_income_value,
                       SUM(mv) FILTER (WHERE asset_bucket = 'fund_etf') AS fund_etf_value,
                       SUM(mv) FILTER (WHERE asset_bucket = 'derivatives') AS derivatives_value,
                       SUM(mv) FILTER (WHERE asset_bucket = 'other') AS other_value,
                       COUNT(*) AS position_count,
                       COUNT(*) FILTER (WHERE asset_bucket = 'derivatives') AS derivative_position_count,
                       SUM(mv) FILTER (WHERE issuer_resolution_status <> 'resolved' OR issuer_resolution_status IS NULL) AS unresolved_value,
                       SUM(mv) FILTER (WHERE price_covered) AS price_covered_value,
                       SUM(mv) FILTER (WHERE factor_covered AND asset_bucket <> 'derivatives') AS factor_covered_value,
                       SUM(voting_authority_sole) AS vote_sole,
                       SUM(COALESCE(voting_authority_sole, 0) + COALESCE(voting_authority_shared, 0) + COALESCE(voting_authority_none, 0)) AS vote_total
                FROM h
                GROUP BY manager_cik, report_period
            ),
            ranked AS (
                SELECT manager_cik, report_period, mv,
                       ROW_NUMBER() OVER (PARTITION BY manager_cik, report_period ORDER BY mv DESC NULLS LAST) AS rn,
                       SUM(mv) OVER (PARTITION BY manager_cik, report_period) AS total_mv
                FROM h
                WHERE asset_bucket <> 'derivatives'
            ),
            concentration AS (
                SELECT manager_cik, report_period,
                       SUM(mv / NULLIF(total_mv, 0)) FILTER (WHERE rn <= 5) AS top5,
                       SUM(mv / NULLIF(total_mv, 0)) FILTER (WHERE rn <= 10) AS top10,
                       MAX(mv / NULLIF(total_mv, 0)) AS max_weight
                FROM ranked
                GROUP BY manager_cik, report_period
            ),
            factor_rows AS (
                SELECT h.manager_cik, h.report_period, h.mv, t.long_total,
                       fl.beta_mkt, fl.beta_smb, fl.beta_hml, fl.beta_mom, fl.beta_rmw, fl.beta_cma
                FROM h
                JOIN totals t ON t.manager_cik = h.manager_cik AND t.report_period = h.report_period
                JOIN LATERAL (
                    SELECT beta_mkt, beta_smb, beta_hml, beta_mom, beta_rmw, beta_cma
                    FROM fact_factor_loadings l
                    WHERE l.jurisdiction = 'US'
                      AND l.model = 'FF6'
                      AND l.ticker = h.issuer_ticker
                      AND l.window_end <= h.report_period
                    ORDER BY l.window_end DESC
                    LIMIT 1
                ) fl ON true
                WHERE h.asset_bucket <> 'derivatives'
            ),
            factors AS (
                SELECT manager_cik, report_period,
                       SUM((mv / NULLIF(long_total, 0)) * beta_mkt) AS beta_mkt,
                       SUM((mv / NULLIF(long_total, 0)) * beta_smb) AS beta_smb,
                       SUM((mv / NULLIF(long_total, 0)) * beta_hml) AS beta_hml,
                       SUM((mv / NULLIF(long_total, 0)) * beta_mom) AS beta_mom,
                       SUM((mv / NULLIF(long_total, 0)) * beta_rmw) AS beta_rmw,
                       SUM((mv / NULLIF(long_total, 0)) * beta_cma) AS beta_cma
                FROM factor_rows
                GROUP BY manager_cik, report_period
            ),
            ff_obs AS (
                SELECT COUNT(DISTINCT date)::int AS n
                FROM fact_fama_french
                WHERE date >= CURRENT_DATE - INTERVAL '5 years'
                  AND factor IN ('Mkt-RF', 'SMB', 'HML', 'Mom', 'RMW', 'CMA')
            ),
            fs AS (
                SELECT DISTINCT ON (manager_cik, report_period)
                       manager_cik, report_period, median_turnover_rate, max_turnover_rate,
                       median_options_ratio, mean_position_count
                FROM fact_13f_manager_feature_snapshot
                ORDER BY manager_cik, report_period, created_at DESC
            )
            SELECT t.manager_cik,
                   t.report_period,
                   t.latest_accession_number,
                   t.filed_date,
                   t.filing_type,
                   t.reported_total,
                   t.market_total,
                   t.long_total,
                   COALESCE(t.equity_value, 0),
                   COALESCE(t.fixed_income_value, 0),
                   COALESCE(t.fund_etf_value, 0),
                   COALESCE(t.derivatives_value, 0),
                   COALESCE(t.other_value, 0),
                   COALESCE(t.equity_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.fixed_income_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.fund_etf_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.derivatives_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.other_value, 0) / NULLIF(t.market_total, 0),
                   t.position_count,
                   t.derivative_position_count,
                   c.top5,
                   c.top10,
                   c.max_weight,
                   COALESCE(t.derivatives_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.unresolved_value, 0),
                   COALESCE(t.unresolved_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.price_covered_value, 0) / NULLIF(t.market_total, 0),
                   COALESCE(t.factor_covered_value, 0) / NULLIF(t.long_total, 0),
                   f.beta_mkt, f.beta_smb, f.beta_hml, f.beta_mom, f.beta_rmw, f.beta_cma,
                   ff_obs.n,
                   fs.median_turnover_rate,
                   fs.max_turnover_rate,
                   fs.median_options_ratio,
                   fs.mean_position_count,
                   t.vote_sole::numeric / NULLIF(t.vote_total, 0),
                   jsonb_build_object(
                       'basis', 'precomputed_core_13f_manager_period',
                       'factor_model', 'FF6',
                       'market_value_policy', 'shares times latest price on or before filing date when available, otherwise SEC reported value'
                   )
            FROM totals t
            LEFT JOIN concentration c ON c.manager_cik = t.manager_cik AND c.report_period = t.report_period
            LEFT JOIN factors f ON f.manager_cik = t.manager_cik AND f.report_period = t.report_period
            LEFT JOIN fs ON fs.manager_cik = t.manager_cik AND fs.report_period = t.report_period
            CROSS JOIN ff_obs
            ON CONFLICT (manager_cik, report_period) DO UPDATE SET
                latest_accession_number = EXCLUDED.latest_accession_number,
                filed_date = EXCLUDED.filed_date,
                filing_type = EXCLUDED.filing_type,
                portfolio_value_reported = EXCLUDED.portfolio_value_reported,
                portfolio_value_market = EXCLUDED.portfolio_value_market,
                long_market_value = EXCLUDED.long_market_value,
                equity_value = EXCLUDED.equity_value,
                fixed_income_value = EXCLUDED.fixed_income_value,
                fund_etf_value = EXCLUDED.fund_etf_value,
                derivatives_value = EXCLUDED.derivatives_value,
                other_value = EXCLUDED.other_value,
                equity_pct = EXCLUDED.equity_pct,
                fixed_income_pct = EXCLUDED.fixed_income_pct,
                fund_etf_pct = EXCLUDED.fund_etf_pct,
                derivatives_pct = EXCLUDED.derivatives_pct,
                other_pct = EXCLUDED.other_pct,
                position_count = EXCLUDED.position_count,
                derivative_position_count = EXCLUDED.derivative_position_count,
                top_5_concentration = EXCLUDED.top_5_concentration,
                top_10_concentration = EXCLUDED.top_10_concentration,
                max_position_weight = EXCLUDED.max_position_weight,
                options_ratio = EXCLUDED.options_ratio,
                unresolved_value = EXCLUDED.unresolved_value,
                unresolved_weight = EXCLUDED.unresolved_weight,
                price_coverage_weight = EXCLUDED.price_coverage_weight,
                factor_coverage_weight = EXCLUDED.factor_coverage_weight,
                beta_mkt = EXCLUDED.beta_mkt,
                beta_smb = EXCLUDED.beta_smb,
                beta_hml = EXCLUDED.beta_hml,
                beta_mom = EXCLUDED.beta_mom,
                beta_rmw = EXCLUDED.beta_rmw,
                beta_cma = EXCLUDED.beta_cma,
                factor_observations = EXCLUDED.factor_observations,
                median_turnover_rate = EXCLUDED.median_turnover_rate,
                max_turnover_rate = EXCLUDED.max_turnover_rate,
                median_options_ratio_8q = EXCLUDED.median_options_ratio_8q,
                mean_position_count_8q = EXCLUDED.mean_position_count_8q,
                shares_voting_sole_pct = EXCLUDED.shares_voting_sole_pct,
                metrics_payload = EXCLUDED.metrics_payload,
                computed_at = now()
            """
            ,
                    (period, manager_batch),
                )
                rows += cur.rowcount
    return {"manager_periods": rows}


def resolve_13f_securities(limit: int | None = None) -> dict[str, int]:
    """Build a 13F-specific CUSIP dimension from deterministic evidence."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL work_mem = '512MB'")
        _ensure_13f_security_dim(cur)
        _ensure_13f_security_lookup_indexes(cur)

        cur.execute("DROP TABLE IF EXISTS tmp_core_13f_security_raw")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_core_13f_security_raw (
                cusip TEXT NOT NULL,
                observed_issuer_name TEXT,
                observed_security_title TEXT,
                asset_bucket TEXT,
                issuer_cik TEXT,
                issuer_ticker TEXT,
                row_count BIGINT NOT NULL DEFAULT 0,
                value_observed NUMERIC,
                first_seen_at TIMESTAMPTZ,
                last_seen_at TIMESTAMPTZ
            ) ON COMMIT DROP
            """
        )
        if limit:
            cur.execute(
                """
                INSERT INTO tmp_core_13f_security_raw
                    (cusip, observed_issuer_name, observed_security_title, asset_bucket,
                     issuer_cik, issuer_ticker, row_count, value_observed, first_seen_at, last_seen_at)
                SELECT upper(h.cusip) AS cusip,
                       h.issuer_name AS observed_issuer_name,
                       h.title_of_class AS observed_security_title,
                       h.asset_bucket,
                       h.issuer_cik,
                       h.issuer_ticker,
                       COUNT(*)::bigint AS row_count,
                       SUM(COALESCE(h.market_value_usd, h.value_reported, 0))::numeric AS value_observed,
                       MIN(h.report_period)::timestamp AT TIME ZONE 'UTC' AS first_seen_at,
                       MAX(h.report_period)::timestamp AT TIME ZONE 'UTC' AS last_seen_at
                FROM (
                    SELECT *
                    FROM core_13f_holding
                    WHERE cusip IS NOT NULL
                      AND upper(cusip) ~ '^[A-Z0-9]{9}$'
                    LIMIT %s
                ) h
                GROUP BY upper(h.cusip), h.issuer_name, h.title_of_class, h.asset_bucket, h.issuer_cik, h.issuer_ticker
                """,
                (limit,),
            )
            period_chunks = 0
        else:
            cur.execute(
                """
                INSERT INTO tmp_core_13f_security_raw
                    (cusip, observed_issuer_name, observed_security_title, asset_bucket,
                     issuer_cik, issuer_ticker, row_count, value_observed, first_seen_at, last_seen_at)
                WITH evidence AS (
                    SELECT upper(cusip) AS cusip,
                           MAX(row_count)::bigint AS row_count,
                           MAX(value_observed)::numeric AS value_observed,
                           MIN(first_seen_at) AS first_seen_at,
                           MAX(last_seen_at) AS last_seen_at
                    FROM fact_security_identifier_evidence_us
                    WHERE cusip IS NOT NULL
                    GROUP BY upper(cusip)
                )
                SELECT upper(i.cusip) AS cusip,
                       i.issuer_name AS observed_issuer_name,
                       i.security_title AS observed_security_title,
                       CASE i.security_type
                           WHEN 'common_equity' THEN 'equity'
                           WHEN 'preferred' THEN 'equity'
                           WHEN 'adr' THEN 'equity'
                           WHEN 'etf_or_fund' THEN 'fund_etf'
                           WHEN 'debt' THEN 'fixed_income'
                           WHEN 'option_or_derivative' THEN 'derivatives'
                           ELSE 'other'
                       END AS asset_bucket,
                       NULL::text AS issuer_cik,
                       NULL::text AS issuer_ticker,
                       COALESCE(e.row_count, 0)::bigint AS row_count,
                       COALESCE(e.value_observed, 0)::numeric AS value_observed,
                       COALESCE(i.first_seen_at, e.first_seen_at) AS first_seen_at,
                       COALESCE(i.last_seen_at, e.last_seen_at) AS last_seen_at
                FROM dim_security_identifier_us i
                LEFT JOIN evidence e ON e.cusip = upper(i.cusip)
                WHERE i.cusip IS NOT NULL
                  AND upper(i.cusip) ~ '^[A-Z0-9]{9}$'
                """
            )
            period_chunks = 0
        cur.execute("CREATE INDEX ON tmp_core_13f_security_raw (cusip)")

        cur.execute("DROP TABLE IF EXISTS tmp_core_13f_security_observed")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_core_13f_security_observed ON COMMIT DROP AS
            WITH raw AS (
                SELECT cusip, observed_issuer_name, observed_security_title, asset_bucket,
                       issuer_cik, issuer_ticker,
                       SUM(row_count)::bigint AS row_count,
                       SUM(value_observed)::numeric AS value_observed,
                       MIN(first_seen_at) AS first_seen_at,
                       MAX(last_seen_at) AS last_seen_at
                FROM tmp_core_13f_security_raw
                GROUP BY cusip, observed_issuer_name, observed_security_title, asset_bucket,
                         issuer_cik, issuer_ticker
            ),
            ranked AS (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY cusip
                           ORDER BY COALESCE(value_observed, 0) DESC, row_count DESC
                       ) AS rn
                FROM raw
            ),
            asset_ranked AS (
                SELECT cusip, asset_bucket,
                       row_number() OVER (
                           PARTITION BY cusip
                           ORDER BY SUM(row_count) DESC, SUM(COALESCE(value_observed, 0)) DESC
                       ) AS rn
                FROM raw
                GROUP BY cusip, asset_bucket
            )
            SELECT r.cusip,
                   substring(r.cusip from 1 for 8) AS cusip8,
                   substring(r.cusip from 1 for 6) AS cusip6,
                   MAX(r.observed_issuer_name) FILTER (WHERE r.rn = 1) AS observed_issuer_name,
                   MAX(r.observed_security_title) FILTER (WHERE r.rn = 1) AS observed_security_title,
                   COALESCE(MAX(a.asset_bucket) FILTER (WHERE a.rn = 1), 'other') AS observed_asset_bucket,
                   SUM(r.row_count)::bigint AS row_count,
                   SUM(r.value_observed)::numeric AS value_observed,
                   MIN(r.first_seen_at) AS first_seen_at,
                   MAX(r.last_seen_at) AS last_seen_at
            FROM ranked r
            LEFT JOIN asset_ranked a ON a.cusip = r.cusip
            GROUP BY r.cusip
            """
        )
        cur.execute("CREATE INDEX ON tmp_core_13f_security_observed (cusip)")
        cur.execute("CREATE INDEX ON tmp_core_13f_security_observed (cusip8)")

        cur.execute("DROP TABLE IF EXISTS tmp_core_13f_security_evidence")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_core_13f_security_evidence (
                cusip TEXT NOT NULL,
                issuer_cik TEXT,
                primary_ticker TEXT,
                issuer_name TEXT,
                sector TEXT,
                industry_group TEXT,
                isin TEXT,
                source_name TEXT NOT NULL,
                source_priority INTEGER NOT NULL,
                confidence_score NUMERIC NOT NULL,
                row_count BIGINT NOT NULL DEFAULT 0,
                value_observed NUMERIC,
                evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb
            ) ON COMMIT DROP
            """
        )
        cur.execute("CREATE INDEX ON tmp_core_13f_security_evidence (cusip)")

        cur.execute(
            """
            INSERT INTO tmp_core_13f_security_evidence
                (cusip, issuer_cik, primary_ticker, issuer_name, sector, industry_group,
                 isin, source_name, source_priority, confidence_score, row_count,
                 value_observed, evidence_payload)
            SELECT o.cusip, d.cik, d.primary_ticker, d.name, d.gics_sector_name,
                   d.gics_industry_group_name, d.isin, 'dim_company_us.isin', 1, 100,
                   o.row_count, o.value_observed, jsonb_build_object('isin', d.isin)
            FROM tmp_core_13f_security_observed o
            JOIN dim_company_us d
              ON upper(substring(d.isin from 3 for 9)) = o.cusip
            WHERE upper(COALESCE(d.isin, '')) ~ '^US[A-Z0-9]{10}$'
            """
        )
        isin_evidence = cur.rowcount

        cur.execute(
            """
            INSERT INTO tmp_core_13f_security_evidence
                (cusip, issuer_cik, primary_ticker, issuer_name, sector, industry_group,
                 isin, source_name, source_priority, confidence_score, row_count,
                 value_observed, evidence_payload)
            SELECT o.cusip, s.issuer_cik, s.issuer_ticker, s.issuer_name,
                   d.gics_sector_name, d.gics_industry_group_name, d.isin,
                   'dim_security_identifier_us', 2, COALESCE(s.confidence_score, 90),
                   o.row_count, o.value_observed, s.evidence_payload
            FROM tmp_core_13f_security_observed o
            JOIN dim_security_identifier_us s ON s.cusip = o.cusip
            LEFT JOIN dim_company_us d ON d.cik = s.issuer_cik
            WHERE s.resolution_status = 'resolved'
              AND s.issuer_cik IS NOT NULL
              AND COALESCE(s.evidence_payload::text, '') NOT LIKE '%spec.cik-cusip-maps.csv%'
            """
        )
        identifier_evidence = cur.rowcount

        existing_core_evidence = 0
        if not limit:
            cur.execute(
                """
                INSERT INTO tmp_core_13f_security_evidence
                    (cusip, issuer_cik, primary_ticker, issuer_name, sector, industry_group,
                     isin, source_name, source_priority, confidence_score, row_count,
                     value_observed, evidence_payload)
                SELECT r.cusip, d.cik, d.primary_ticker, d.name, d.gics_sector_name,
                       d.gics_industry_group_name, d.isin, 'core_13f_holding.existing_resolution',
                       3, 75, SUM(r.row_count)::bigint,
                       SUM(COALESCE(r.value_observed, 0))::numeric,
                       jsonb_build_object('source', 'existing core_13f_holding issuer_cik')
                FROM tmp_core_13f_security_raw r
                JOIN dim_company_us d ON d.cik = r.issuer_cik
                WHERE r.issuer_cik IS NOT NULL
                GROUP BY r.cusip, d.cik, d.primary_ticker, d.name, d.gics_sector_name,
                         d.gics_industry_group_name, d.isin
                """
            )
            existing_core_evidence = cur.rowcount

        cur.execute(
            """
            INSERT INTO tmp_core_13f_security_evidence
                (cusip, issuer_cik, primary_ticker, issuer_name, sector, industry_group,
                 isin, source_name, source_priority, confidence_score, row_count,
                 value_observed, evidence_payload)
            SELECT o.cusip, d.cik, d.primary_ticker, d.name, d.gics_sector_name,
                   d.gics_industry_group_name, d.isin, 'core_13f_holding.normalized_name_match',
                   4, 55, o.row_count, o.value_observed,
                   jsonb_build_object('match', 'normalized observed issuer name equals dim_company_us.name')
            FROM tmp_core_13f_security_observed o
            JOIN dim_company_us d
              ON regexp_replace(upper(COALESCE(d.name, '')), '[^A-Z0-9]', '', 'g')
               = regexp_replace(upper(COALESCE(o.observed_issuer_name, '')), '[^A-Z0-9]', '', 'g')
            """
        )
        name_evidence = cur.rowcount

        if limit:
            cur.execute(
                """
                DELETE FROM dim_13f_security_us d
                USING tmp_core_13f_security_observed o
                WHERE d.cusip = o.cusip
                """
            )
        else:
            cur.execute("TRUNCATE dim_13f_security_us")
        cur.execute(
            """
            WITH classified AS (
                SELECT o.*,
                       upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) AS descriptor,
                       CASE
                           WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                '(ETF|EXCHANGE TRADED|SPDR|ISHARES|INDEX FUND|ETF TR|TR UNIT|UNIT SER| FUND | PORTFOLIO |S&P 500 ETF|NASDAQ 100)'
                           THEN 'fund_etf'
                           WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                '(NOTE| NOTES| NT |BOND|DEBENTURE|DUE [0-9]|[0-9]+\\.[0-9]+%)'
                           THEN 'fixed_income'
                           ELSE COALESCE(o.observed_asset_bucket, 'other')
                       END AS classified_bucket
                FROM tmp_core_13f_security_observed o
            ),
            evidence AS (
                SELECT e.*,
                       MIN(source_priority) OVER (PARTITION BY cusip) AS best_priority
                FROM tmp_core_13f_security_evidence e
                WHERE issuer_cik IS NOT NULL
            ),
            best_candidates AS (
                SELECT cusip, issuer_cik,
                       MAX(primary_ticker) AS primary_ticker,
                       MAX(issuer_name) AS issuer_name,
                       MAX(sector) AS sector,
                       MAX(industry_group) AS industry_group,
                       MAX(isin) AS isin,
                       MIN(source_priority) AS source_priority,
                       MAX(confidence_score) AS confidence_score,
                       SUM(row_count) AS row_count,
                       MAX(source_name) AS source_name
                FROM evidence
                WHERE source_priority = best_priority
                GROUP BY cusip, issuer_cik
            ),
            candidate_counts AS (
                SELECT cusip, COUNT(DISTINCT issuer_cik) AS candidate_count
                FROM best_candidates
                GROUP BY cusip
            ),
            best AS (
                SELECT DISTINCT ON (cusip)
                       cusip, issuer_cik, primary_ticker, issuer_name, sector,
                       industry_group, isin, source_priority, confidence_score, source_name
                FROM best_candidates
                ORDER BY cusip, source_priority, confidence_score DESC, row_count DESC, issuer_cik
            ),
            evidence_payload AS (
                SELECT cusip,
                       jsonb_agg(
                           jsonb_build_object(
                               'source_name', source_name,
                               'source_priority', source_priority,
                               'issuer_cik', issuer_cik,
                               'primary_ticker', primary_ticker,
                               'confidence_score', confidence_score,
                               'row_count', row_count
                           )
                           ORDER BY source_priority, confidence_score DESC
                       ) AS evidence
                FROM tmp_core_13f_security_evidence
                GROUP BY cusip
            )
            INSERT INTO dim_13f_security_us
                (cusip, cusip8, cusip6, isin, issuer_cik, primary_ticker, issuer_name,
                 security_title, asset_bucket, sector, industry_group, resolution_status,
                 confidence_score, source_name, first_seen_at, last_seen_at, row_count,
                 value_observed, evidence_payload)
            SELECT c.cusip,
                   c.cusip8,
                   c.cusip6,
                   CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.isin ELSE NULL END,
                   CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.issuer_cik ELSE NULL END,
                   CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.primary_ticker ELSE NULL END,
                   COALESCE(CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.issuer_name ELSE NULL END, c.observed_issuer_name),
                   c.observed_security_title,
                   c.classified_bucket,
                   CASE
                       WHEN c.classified_bucket = 'fund_etf' THEN 'fund_etf'
                       WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.sector
                       ELSE c.classified_bucket
                   END,
                   CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.industry_group ELSE NULL END,
                   CASE
                       WHEN COALESCE(cc.candidate_count, 0) > 1 THEN 'ambiguous'
                       WHEN COALESCE(cc.candidate_count, 0) = 1 THEN 'resolved'
                       WHEN c.classified_bucket = 'fund_etf' THEN 'fund_etf'
                       WHEN c.classified_bucket = 'fixed_income' THEN 'non_company_security'
                       ELSE 'unresolved'
                   END,
                   CASE WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.confidence_score ELSE 0 END,
                   CASE
                       WHEN COALESCE(cc.candidate_count, 0) = 1 THEN b.source_name
                       WHEN c.classified_bucket = 'fund_etf' THEN '13f_title_fund_etf_pattern'
                       ELSE NULL
                   END,
                   c.first_seen_at,
                   c.last_seen_at,
                   c.row_count,
                   c.value_observed,
                   jsonb_build_object(
                       'candidate_count', COALESCE(cc.candidate_count, 0),
                       'evidence', COALESCE(ep.evidence, '[]'::jsonb)
                   )
            FROM classified c
            LEFT JOIN candidate_counts cc ON cc.cusip = c.cusip
            LEFT JOIN best b ON b.cusip = c.cusip
            LEFT JOIN evidence_payload ep ON ep.cusip = c.cusip
            """
        )
        dim_rows = cur.rowcount

        holdings_updated = 0

        cur.execute(
            """
            SELECT resolution_status, COUNT(*)
            FROM dim_13f_security_us
            GROUP BY 1
            """
        )
        status_counts = {f"status_{status}": count for status, count in cur.fetchall()}
    return {
        "dim_rows": dim_rows,
        "holdings_updated": holdings_updated,
        "evidence_isin": isin_evidence,
        "evidence_identifier": identifier_evidence,
        "evidence_existing_core": existing_core_evidence,
        "evidence_name_match": name_evidence,
        "period_chunks": period_chunks,
        "identifier_universe": 0 if limit else 1,
        **status_counts,
    }


def compare_13f_cusip_llm(limit: int | None = None) -> dict[str, Any]:
    """One-off DeepSeek comparison for unresolved CUSIPs against dim_company_us candidates."""
    limit = limit or int(os.environ.get("DEEPSEEK_CUSIP_MATCH_LIMIT", "25"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cik, primary_ticker, name
            FROM dim_company_us
            WHERE cik IS NOT NULL
              AND primary_ticker IS NOT NULL
              AND name IS NOT NULL
            """
        )
        companies = [
            {
                "cik": normalize_cik(cik),
                "ticker": ticker,
                "name": name,
                "key": _security_name_key(name),
            }
            for cik, ticker, name in cur.fetchall()
        ]
        cur.execute(
            """
            WITH latest AS (
                SELECT MAX(report_period) AS report_period
                FROM core_13f_holding
                WHERE report_period IS NOT NULL
            ),
            observed AS (
                SELECT upper(h.cusip) AS cusip,
                       substring(upper(h.cusip) from 1 for 8) AS cusip8,
                       substring(upper(h.cusip) from 1 for 6) AS cusip6,
                       h.issuer_name,
                       h.title_of_class,
                       COALESCE(sec.resolution_status, h.issuer_resolution_status, 'unresolved') AS deterministic_status,
                       SUM(COALESCE(h.market_value_usd, h.value_reported, 0))::numeric AS value_observed,
                       COUNT(*)::bigint AS row_count,
                       row_number() OVER (
                           PARTITION BY upper(h.cusip)
                           ORDER BY SUM(COALESCE(h.market_value_usd, h.value_reported, 0)) DESC NULLS LAST,
                                    COUNT(*) DESC
                       ) AS rn
                FROM core_13f_holding h
                JOIN latest l ON l.report_period = h.report_period
                LEFT JOIN dim_13f_security_us sec ON sec.cusip = upper(h.cusip)
                WHERE h.cusip IS NOT NULL
                  AND upper(h.cusip) ~ '^[A-Z0-9]{9}$'
                  AND COALESCE(sec.resolution_status, h.issuer_resolution_status, 'unresolved') IN ('unresolved', 'ambiguous')
                GROUP BY upper(h.cusip), h.issuer_name, h.title_of_class,
                         COALESCE(sec.resolution_status, h.issuer_resolution_status, 'unresolved')
            )
            SELECT cusip, cusip8, cusip6, issuer_name, title_of_class,
                   deterministic_status, value_observed, row_count
            FROM observed
            WHERE rn = 1
            ORDER BY value_observed DESC NULLS LAST, row_count DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    try:
        from xbrl_sec.sec.sources.llm_client import get_llm_client

        client = get_llm_client()
        model = os.environ.get("DEEPSEEK_CUSIP_MATCH_MODEL") or os.environ.get("LLM_MODEL") or "deepseek-chat"
    except Exception as exc:
        return {"checked": len(rows), "matches": 0, "accepted": 0, "error": str(exc), "results": []}

    results: list[dict[str, Any]] = []
    for cusip, cusip8, cusip6, issuer_name, title, status, value_observed, row_count in rows:
        issuer_key = _security_name_key(issuer_name)
        scored = []
        for company in companies:
            score = SequenceMatcher(None, issuer_key, company["key"]).ratio() if issuer_key and company["key"] else 0.0
            if issuer_key and (issuer_key in company["key"] or company["key"] in issuer_key):
                score = max(score, 0.86)
            if score >= 0.55:
                scored.append({
                    "cik": company["cik"],
                    "ticker": company["ticker"],
                    "name": company["name"],
                    "score": round(score, 3),
                })
        candidates = sorted(scored, key=lambda item: item["score"], reverse=True)[:12]
        if not candidates:
            results.append({
                "cusip": cusip,
                "issuer_name": issuer_name,
                "security_title": title,
                "deterministic_status": status,
                "candidate_cik": None,
                "candidate_ticker": None,
                "candidate_name": None,
                "confidence": 0.0,
                "accepted": False,
                "rationale": "No dim_company_us candidate passed the lexical prefilter.",
                "candidate_count": 0,
                "value_observed": float(value_observed or 0),
                "row_count": int(row_count or 0),
            })
            continue
        messages = [
            {
                "role": "system",
                "content": (
                    "You compare SEC 13F issuer names to US public-company CIK candidates. "
                    "Choose only from the provided dim_company_us candidates. Return JSON only. "
                    "If the issuer is a fund, ETF, debt instrument, derivative, or no candidate is clearly the same issuer, return null candidate_cik."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "observed": {
                            "cusip": cusip,
                            "issuer_name": issuer_name,
                            "security_title": title,
                            "deterministic_status": status,
                        },
                        "candidates": candidates,
                        "return_shape": {
                            "candidate_cik": "10 digit CIK from candidates or null",
                            "confidence": "number 0-1",
                            "rationale": "short reason",
                        },
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]
        try:
            response = client.chat.completions.create(model=model, messages=messages, temperature=0.0, max_tokens=350)
            raw = response.choices[0].message.content or "{}"
            decision = _extract_json_object(raw)
        except Exception as exc:
            raw = json.dumps({"error": str(exc)})
            decision = {"candidate_cik": None, "confidence": 0, "rationale": f"DeepSeek failed: {exc}"}
        candidate_cik = normalize_cik(decision.get("candidate_cik")) if decision.get("candidate_cik") else None
        candidate = next((item for item in candidates if normalize_cik(item["cik"]) == candidate_cik), None)
        try:
            confidence = float(decision.get("confidence") or 0)
        except Exception:
            confidence = 0.0
        accepted = bool(candidate and confidence >= 0.78)
        results.append({
            "cusip": cusip,
            "cusip8": cusip8,
            "cusip6": cusip6,
            "issuer_name": issuer_name,
            "security_title": title,
            "deterministic_status": status,
            "candidate_cik": candidate["cik"] if candidate else None,
            "candidate_ticker": candidate["ticker"] if candidate else None,
            "candidate_name": candidate["name"] if candidate else None,
            "confidence": max(0.0, min(1.0, confidence)),
            "accepted": accepted,
            "rationale": decision.get("rationale"),
            "candidate_count": len(candidates),
            "value_observed": float(value_observed or 0),
            "row_count": int(row_count or 0),
            "raw_response": raw,
        })
    with connect() as conn, conn.cursor() as cur:
        _ensure_13f_llm_comparison_table(cur)
        if results:
            execute_values(
                cur,
                """
                INSERT INTO fact_13f_cusip_llm_comparison
                    (cusip, cusip8, cusip6, observed_issuer_name, observed_security_title,
                     deterministic_status, candidate_cik, candidate_ticker, candidate_name,
                     confidence, accepted, rationale, candidate_count, value_observed,
                     row_count, model, raw_response)
                VALUES %s
                ON CONFLICT (cusip, model) DO UPDATE SET
                    cusip8 = EXCLUDED.cusip8,
                    cusip6 = EXCLUDED.cusip6,
                    observed_issuer_name = EXCLUDED.observed_issuer_name,
                    observed_security_title = EXCLUDED.observed_security_title,
                    deterministic_status = EXCLUDED.deterministic_status,
                    candidate_cik = EXCLUDED.candidate_cik,
                    candidate_ticker = EXCLUDED.candidate_ticker,
                    candidate_name = EXCLUDED.candidate_name,
                    confidence = EXCLUDED.confidence,
                    accepted = EXCLUDED.accepted,
                    rationale = EXCLUDED.rationale,
                    candidate_count = EXCLUDED.candidate_count,
                    value_observed = EXCLUDED.value_observed,
                    row_count = EXCLUDED.row_count,
                    raw_response = EXCLUDED.raw_response,
                    updated_at = now()
                """,
                [
                    (
                        row.get("cusip"),
                        row.get("cusip8"),
                        row.get("cusip6"),
                        row.get("issuer_name"),
                        row.get("security_title"),
                        row.get("deterministic_status"),
                        row.get("candidate_cik"),
                        row.get("candidate_ticker"),
                        row.get("candidate_name"),
                        row.get("confidence"),
                        row.get("accepted"),
                        row.get("rationale"),
                        row.get("candidate_count"),
                        row.get("value_observed"),
                        row.get("row_count"),
                        model,
                        row.get("raw_response"),
                    )
                    for row in results
                ],
                page_size=1000,
            )
    return {
        "checked": len(rows),
        "matches": sum(1 for row in results if row["candidate_cik"]),
        "accepted": sum(1 for row in results if row["accepted"]),
        "model": model,
        "results": results,
    }


def report_13f_cusip_gaps(limit: int | None = None) -> dict[str, Any]:
    """Rank unresolved/ambiguous CUSIPs by observed value for review."""
    limit = limit or 50
    with connect() as conn, conn.cursor() as cur:
        _ensure_13f_security_dim(cur)
        cur.execute(
            """
            WITH candidates AS (
                SELECT d.cusip, d.cusip8, d.cusip6, d.issuer_name,
                       d.security_title, d.asset_bucket, d.resolution_status,
                       d.value_observed, d.row_count, d.source_name
                FROM dim_13f_security_us d
                WHERE d.resolution_status IN ('unresolved', 'ambiguous')
                UNION ALL
                SELECT i.cusip, i.cusip8, i.cusip6, i.issuer_name,
                       i.security_title, i.security_type AS asset_bucket,
                       i.resolution_status, NULL::numeric AS value_observed,
                       NULL::bigint AS row_count, NULL::text AS source_name
                FROM dim_security_identifier_us i
                LEFT JOIN dim_13f_security_us d ON d.cusip = i.cusip
                WHERE d.cusip IS NULL
                  AND i.resolution_status IN ('unresolved', 'ambiguous')
            ),
            evidence AS (
                SELECT cusip,
                       MAX(value_observed) AS value_observed,
                       MAX(row_count) AS row_count
                FROM fact_security_identifier_evidence_us
                GROUP BY cusip
            ),
            ranked AS (
                SELECT DISTINCT ON (c.cusip)
                       c.cusip, c.cusip8, c.cusip6, c.issuer_name,
                       c.security_title, c.asset_bucket, c.resolution_status,
                       COALESCE(c.value_observed, e.value_observed, 0) AS value_observed,
                       COALESCE(c.row_count, e.row_count, 0) AS row_count,
                       c.source_name
                FROM candidates c
                LEFT JOIN evidence e ON e.cusip = c.cusip
                ORDER BY c.cusip, COALESCE(c.value_observed, e.value_observed, 0) DESC NULLS LAST
            )
            SELECT *
            FROM ranked
            ORDER BY value_observed DESC NULLS LAST, row_count DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        rows = [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]
    return {"rows": len(rows), "gaps": rows}


def promote_13f_cusip_llm(min_confidence: float = 0.85, limit: int | None = None, force: bool = False) -> dict[str, int]:
    """Promote reviewed LLM comparison rows into dim_13f_security_us only when forced."""
    with connect() as conn, conn.cursor() as cur:
        _ensure_13f_security_dim(cur)
        _ensure_13f_llm_comparison_table(cur)
        limit_sql = "LIMIT %s" if limit else ""
        params: tuple[Any, ...] = (min_confidence, limit) if limit else (min_confidence,)
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_13f_llm_promote ON COMMIT DROP AS
            SELECT c.*, d.isin, d.gics_sector_name, d.gics_industry_group_name
            FROM fact_13f_cusip_llm_comparison c
            JOIN dim_company_us d ON d.cik = c.candidate_cik
            LEFT JOIN dim_13f_security_us s ON s.cusip = c.cusip
            WHERE c.accepted
              AND c.candidate_cik IS NOT NULL
              AND c.confidence >= %s
              AND COALESCE(s.resolution_status, 'unresolved') IN ('unresolved', 'ambiguous')
            ORDER BY c.value_observed DESC NULLS LAST, c.confidence DESC
            {limit_sql}
            """,
            params,
        )
        cur.execute("SELECT COUNT(*) FROM tmp_13f_llm_promote")
        eligible = int(cur.fetchone()[0] or 0)
        if not force or eligible == 0:
            return {"eligible": eligible, "promoted": 0, "holdings_updated": 0, "force_required": 0 if force else 1}
        cur.execute(
            """
            INSERT INTO dim_13f_security_us
                (cusip, cusip8, cusip6, isin, issuer_cik, primary_ticker, issuer_name,
                 security_title, asset_bucket, sector, industry_group, resolution_status,
                 confidence_score, source_name, row_count, value_observed, evidence_payload)
            SELECT cusip, cusip8, cusip6, isin, candidate_cik, candidate_ticker, candidate_name,
                   observed_security_title, 'equity', gics_sector_name, gics_industry_group_name,
                   'resolved', confidence * 100.0, 'deepseek.dim_company_us_comparison',
                   row_count, value_observed,
                   jsonb_build_object(
                       'candidate_count', candidate_count,
                       'rationale', rationale,
                       'model', model,
                       'source_name', 'deepseek.dim_company_us_comparison'
                   )
            FROM tmp_13f_llm_promote
            ON CONFLICT (cusip) DO UPDATE SET
                isin = EXCLUDED.isin,
                issuer_cik = EXCLUDED.issuer_cik,
                primary_ticker = EXCLUDED.primary_ticker,
                issuer_name = EXCLUDED.issuer_name,
                security_title = COALESCE(dim_13f_security_us.security_title, EXCLUDED.security_title),
                asset_bucket = EXCLUDED.asset_bucket,
                sector = EXCLUDED.sector,
                industry_group = EXCLUDED.industry_group,
                resolution_status = EXCLUDED.resolution_status,
                confidence_score = EXCLUDED.confidence_score,
                source_name = EXCLUDED.source_name,
                row_count = COALESCE(dim_13f_security_us.row_count, EXCLUDED.row_count),
                value_observed = COALESCE(dim_13f_security_us.value_observed, EXCLUDED.value_observed),
                evidence_payload = EXCLUDED.evidence_payload,
                updated_at = now()
            WHERE dim_13f_security_us.resolution_status IN ('unresolved', 'ambiguous')
            """
        )
        promoted = cur.rowcount
    return {"eligible": eligible, "promoted": promoted, "holdings_updated": 0, "force_required": 0}


CORE_MANAGER_LABEL_BY_SLUG = {
    "alternative": "Asset Management: Alternative (Speculative/Trading)",
    "traditional": "Asset Management: Traditional (Long-Term Capital)",
    "wealth_trust": "Banking: Wealth & Trust (Investment)",
    "capital_markets": "Banking: Capital Markets & Trading (Speculative)",
    "insurance": "Insurance: General Account (Long-Term Capital)",
}

CORE_MANAGER_CLASSIFIER_SYSTEM_PROMPT = """You classify SEC Form 13F institutional managers.

Return exactly one of these five primary_label values:
- "Asset Management: Alternative (Speculative/Trading)"
- "Asset Management: Traditional (Long-Term Capital)"
- "Banking: Wealth & Trust (Investment)"
- "Banking: Capital Markets & Trading (Speculative)"
- "Insurance: General Account (Long-Term Capital)"

Use the manager name, filing profile, AUM, turnover/options behavior, asset mix, concentration,
and top holdings. Force a best label; do not return null, unknown, other, or unclassified.
Foundations, endowments, family offices, and long-only investment organizations usually fit
"Asset Management: Traditional (Long-Term Capital)" unless the evidence strongly supports
another label. Large broker-dealers and universal banks with trading/options behavior usually
fit "Banking: Capital Markets & Trading (Speculative)"; wealth/private-bank/trust platforms
usually fit "Banking: Wealth & Trust (Investment)".

Return only JSON with keys:
primary_label, confidence_score, quantitative_trigger_metric, rationale."""


def _fetchone_dict(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    return {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}


def _fetchall_dicts(cur) -> list[dict[str, Any]]:
    rows = cur.fetchall()
    return [
        {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}
        for row in rows
    ]


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__float__") and value.__class__.__module__ == "decimal":
        return float(value)
    return value


def _core_manager_unlabeled_candidates(
    cur,
    *,
    min_aum: float | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = []
    if not force:
        where.append("COALESCE(c.primary_label, '') = ''")
    if min_aum is not None:
        params.append(min_aum)
        where.append(f"COALESCE(p.portfolio_value_market, p.long_market_value, 0) >= %s")
    limit_sql = ""
    if limit:
        params.append(limit)
        limit_sql = "LIMIT %s"
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    cur.execute(
        f"""
        WITH latest_period AS (
            SELECT DISTINCT ON (manager_cik) manager_cik, report_period
            FROM core_13f_manager_period
            ORDER BY manager_cik, report_period DESC
        ),
        latest_cls AS (
            SELECT c.manager_cik, c.report_period, c.primary_label, c.classification_status
            FROM core_13f_manager_classification c
            JOIN latest_period lp
              ON lp.manager_cik = c.manager_cik
             AND lp.report_period = c.report_period
        )
        SELECT p.manager_cik,
               m.legal_name,
               p.report_period,
               COALESCE(p.portfolio_value_market, p.long_market_value, 0) AS aum,
               p.portfolio_value_market,
               p.long_market_value,
               p.equity_value,
               p.fixed_income_value,
               p.fund_etf_value,
               p.derivatives_value,
               p.other_value,
               p.equity_pct,
               p.fixed_income_pct,
               p.fund_etf_pct,
               p.derivatives_pct,
               p.other_pct,
               p.position_count,
               p.derivative_position_count,
               p.top_5_concentration,
               p.top_10_concentration,
               p.max_position_weight,
               p.options_ratio,
               p.unresolved_weight,
               p.price_coverage_weight,
               p.factor_coverage_weight,
               p.beta_mkt,
               p.beta_smb,
               p.beta_hml,
               p.beta_mom,
               p.factor_var_95_1d,
               p.factor_cvar_95_1d,
               p.factor_observations,
               p.median_turnover_rate,
               p.max_turnover_rate,
               p.median_options_ratio_8q,
               p.mean_position_count_8q,
               p.shares_voting_sole_pct,
               c.primary_label AS current_primary_label,
               c.classification_status AS current_classification_status
        FROM latest_period lp
        JOIN core_13f_manager_period p
          ON p.manager_cik = lp.manager_cik
         AND p.report_period = lp.report_period
        JOIN core_13f_manager m ON m.manager_cik = p.manager_cik
        LEFT JOIN latest_cls c ON c.manager_cik = p.manager_cik
        {where_sql}
        ORDER BY COALESCE(p.portfolio_value_market, p.long_market_value, 0) DESC NULLS LAST,
                 p.manager_cik
        {limit_sql}
        """,
        params,
    )
    return _fetchall_dicts(cur)


def _core_manager_top_holdings(cur, manager_cik: str, report_period: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT ticker, issuer_name, security_title, cusip, asset_bucket,
               market_value_usd, NULL::numeric AS portfolio_weight,
               put_call,
               sh_prn_flag
        FROM (
            SELECT NULLIF(h.issuer_ticker, '') AS ticker,
                   h.issuer_name AS issuer_name,
                   h.title_of_class AS security_title,
                   upper(h.cusip) AS cusip,
                   COALESCE(h.asset_bucket, 'other') AS asset_bucket,
                   COALESCE(h.market_value_usd, h.value_reported, 0)::numeric AS market_value_usd,
                   h.put_call,
                   h.sh_prn_flag
            FROM core_13f_holding h
            WHERE h.manager_cik = %s
              AND h.report_period = %s
              AND h.is_latest_amendment
        ) rows
        WHERE COALESCE(put_call, '') = ''
          AND COALESCE(sh_prn_flag, 'SH') = 'SH'
        ORDER BY market_value_usd DESC NULLS LAST
        LIMIT %s
        """,
        (manager_cik, report_period, limit),
    )
    return _fetchall_dicts(cur)


def _core_manager_prompt_payload(cur, candidate: dict[str, Any]) -> dict[str, Any]:
    holdings = _core_manager_top_holdings(
        cur,
        candidate["manager_cik"],
        candidate["report_period"],
        limit=20,
    )
    long_value = float(candidate["long_market_value"] or candidate["portfolio_value_market"] or 0)
    if long_value:
        for holding in holdings:
            if holding.get("portfolio_weight") is None:
                holding["portfolio_weight"] = float(holding.get("market_value_usd") or 0) / long_value
    return _json_ready({
        "manager": {
            "manager_cik": candidate["manager_cik"],
            "legal_name": candidate["legal_name"],
            "report_period": candidate["report_period"],
            "aum_usd": candidate["aum"],
        },
        "latest_metrics": {
            "portfolio_value_market": candidate["portfolio_value_market"],
            "long_market_value": candidate["long_market_value"],
            "equity_value": candidate["equity_value"],
            "fixed_income_value": candidate["fixed_income_value"],
            "fund_etf_value": candidate["fund_etf_value"],
            "derivatives_value": candidate["derivatives_value"],
            "other_value": candidate["other_value"],
            "equity_pct": candidate["equity_pct"],
            "fixed_income_pct": candidate["fixed_income_pct"],
            "fund_etf_pct": candidate["fund_etf_pct"],
            "derivatives_pct": candidate["derivatives_pct"],
            "other_pct": candidate["other_pct"],
            "position_count": candidate["position_count"],
            "derivative_position_count": candidate["derivative_position_count"],
            "top_5_concentration": candidate["top_5_concentration"],
            "top_10_concentration": candidate["top_10_concentration"],
            "max_position_weight": candidate["max_position_weight"],
            "options_ratio": candidate["options_ratio"],
            "unresolved_weight": candidate["unresolved_weight"],
            "price_coverage_weight": candidate["price_coverage_weight"],
            "factor_coverage_weight": candidate["factor_coverage_weight"],
            "beta_mkt": candidate["beta_mkt"],
            "beta_smb": candidate["beta_smb"],
            "beta_hml": candidate["beta_hml"],
            "beta_mom": candidate["beta_mom"],
            "factor_var_95_1d": candidate["factor_var_95_1d"],
            "factor_cvar_95_1d": candidate["factor_cvar_95_1d"],
            "factor_observations": candidate["factor_observations"],
            "median_turnover_rate_8q": candidate["median_turnover_rate"],
            "max_turnover_rate_8q": candidate["max_turnover_rate"],
            "median_options_ratio_8q": candidate["median_options_ratio_8q"],
            "mean_position_count_8q": candidate["mean_position_count_8q"],
            "shares_voting_sole_pct": candidate["shares_voting_sole_pct"],
        },
        "top_long_holdings": holdings,
    })


def _normalize_core_manager_label(value: str | None) -> str:
    raw = (value or "").strip()
    if raw in LABELS:
        return raw
    slug = raw.lower().replace("-", "_").replace(" ", "_")
    if slug in CORE_MANAGER_LABEL_BY_SLUG:
        return CORE_MANAGER_LABEL_BY_SLUG[slug]
    lowered = raw.lower()
    for label in LABELS:
        if lowered == label.lower():
            return label
    raise ValueError(f"Invalid primary_label: {value!r}")


def _validate_core_manager_llm_json(data: dict[str, Any]) -> dict[str, Any]:
    label = _normalize_core_manager_label(data.get("primary_label"))
    confidence = data.get("confidence_score")
    if not isinstance(confidence, (int, float)):
        confidence = float(str(confidence).strip())
    confidence = float(confidence)
    if confidence > 1.0 and confidence <= 100.0:
        confidence = confidence / 100.0
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"Invalid confidence_score: {data.get('confidence_score')!r}")
    trigger = str(data.get("quantitative_trigger_metric") or "").strip()
    if not trigger:
        raise ValueError("quantitative_trigger_metric is required")
    rationale = str(data.get("rationale") or "").strip()
    return {
        "primary_label": label,
        "confidence_score": confidence,
        "quantitative_trigger_metric": trigger,
        "rationale": rationale,
    }


def _call_core_manager_deepseek(payload: dict[str, Any], *, escalated: bool) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")
    model = DEEPSEEK_REASONER_MODEL if escalated else DEEPSEEK_CHAT_MODEL
    req_body: dict[str, Any] = {
        "model": model,
        "temperature": 1.0 if escalated else 0.0,
        "messages": [
            {"role": "system", "content": CORE_MANAGER_CLASSIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Classify this 13F manager. Return only JSON.\n\n"
                    + json.dumps(payload, default=str, sort_keys=True)
                ),
            },
        ],
    }
    if not escalated:
        req_body["response_format"] = {"type": "json_object"}
    request = Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=json.dumps(req_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    text = (((response_payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    data = _extract_json_object(text)
    validated = _validate_core_manager_llm_json(data)
    validated["model"] = model
    validated["raw_response"] = response_payload
    return validated


def _classify_core_manager_with_deepseek(
    payload: dict[str, Any],
    *,
    min_confidence: float,
    max_attempts: int = 2,
) -> dict[str, Any]:
    errors: list[str] = []
    chat_result: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            chat_result = _call_core_manager_deepseek(payload, escalated=False)
            if chat_result["confidence_score"] >= min_confidence:
                chat_result["route_tier"] = "tier2_deepseek_v3"
                chat_result["route_reason"] = "DeepSeek chat classified unlabeled core 13F manager."
                return chat_result
            errors.append(f"chat attempt {attempt}: confidence {chat_result['confidence_score']:.3f} below {min_confidence:.3f}")
            break
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"chat attempt {attempt}: {type(exc).__name__}: {exc}")

    for attempt in range(1, max_attempts + 1):
        try:
            reasoner_result = _call_core_manager_deepseek(payload, escalated=True)
            reasoner_result["route_tier"] = "tier3_deepseek_r1"
            reasoner_result["route_reason"] = (
                "DeepSeek reasoner classified unlabeled core 13F manager after chat "
                "low-confidence or invalid response."
            )
            reasoner_result["prior_errors"] = errors
            return reasoner_result
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"reasoner attempt {attempt}: {type(exc).__name__}: {exc}")

    if chat_result is not None:
        chat_result["route_tier"] = "tier2_deepseek_v3_low_confidence"
        chat_result["route_reason"] = (
            "DeepSeek chat produced a valid forced label, but reasoner retry failed; "
            "accepted low-confidence forced label."
        )
        chat_result["prior_errors"] = errors
        return chat_result
    raise RuntimeError("; ".join(errors))


def _upsert_core_manager_llm_classification(
    cur,
    *,
    manager_cik: str,
    report_period: Any,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    response_json = {
        "rationale": result.get("rationale"),
        "raw_response": result.get("raw_response"),
        "prompt_payload": payload,
        "prior_errors": result.get("prior_errors", []),
    }
    cur.execute(
        """
        INSERT INTO core_13f_manager_classification
            (manager_cik, report_period, primary_label, confidence_score, route_tier,
             route_reason, quantitative_trigger_metric, evidence_source, model,
             prompt_version, classification_status, response_json, error_type, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'classified', %s::jsonb, NULL, NULL)
        ON CONFLICT (manager_cik, report_period) DO UPDATE SET
            primary_label = EXCLUDED.primary_label,
            confidence_score = EXCLUDED.confidence_score,
            route_tier = EXCLUDED.route_tier,
            route_reason = EXCLUDED.route_reason,
            quantitative_trigger_metric = EXCLUDED.quantitative_trigger_metric,
            evidence_source = EXCLUDED.evidence_source,
            model = EXCLUDED.model,
            prompt_version = EXCLUDED.prompt_version,
            classification_status = 'classified',
            response_json = EXCLUDED.response_json,
            error_type = NULL,
            error_message = NULL,
            updated_at = now()
        """,
        (
            manager_cik,
            report_period,
            result["primary_label"],
            result["confidence_score"],
            result["route_tier"],
            result["route_reason"],
            result["quantitative_trigger_metric"],
            "deepseek.core_manager_llm",
            result["model"],
            CORE_MANAGER_CLASSIFIER_PROMPT_VERSION,
            json.dumps(response_json, default=str),
        ),
    )


def _copy_core_manager_label_to_history(
    cur,
    *,
    manager_cik: str,
    report_period: Any,
    result: dict[str, Any],
) -> int:
    cur.execute(
        """
        UPDATE core_13f_manager_classification
           SET primary_label = %s,
               confidence_score = %s,
               route_tier = %s,
               route_reason = %s,
               quantitative_trigger_metric = %s,
               evidence_source = 'deepseek.core_manager_llm.history_copy',
               model = %s,
               prompt_version = %s,
               classification_status = 'classified',
               response_json = jsonb_build_object(
                   'copied_from_report_period', %s::date,
                   'source_route_tier', %s,
                   'source_model', %s
               ),
               error_type = NULL,
               error_message = NULL,
               updated_at = now()
         WHERE manager_cik = %s
           AND report_period <> %s
           AND COALESCE(primary_label, '') = ''
        """,
        (
            result["primary_label"],
            result["confidence_score"],
            f"{result['route_tier']}_history_copy",
            "Manager-level style copied from latest DeepSeek classification.",
            result["quantitative_trigger_metric"],
            result["model"],
            CORE_MANAGER_CLASSIFIER_PROMPT_VERSION,
            report_period,
            result["route_tier"],
            result["model"],
            manager_cik,
            report_period,
        ),
    )
    return int(cur.rowcount or 0)


def classify_13f_core_llm(
    *,
    limit: int | None = None,
    min_aum: float | None = None,
    min_confidence: float = 0.85,
    dry_run: bool = False,
    force: bool = False,
    copy_history: bool = True,
    workers: int = 1,
) -> dict[str, Any]:
    """Classify unlabeled core 13F managers with DeepSeek and write labels.

    The batch is deliberately ordered by latest AUM so repeated limited runs
    make forward progress through the most consequential managers first.
    """
    with connect() as conn, conn.cursor() as cur:
        candidates = _core_manager_unlabeled_candidates(
            cur,
            min_aum=min_aum,
            limit=limit,
            force=force,
        )
        if dry_run:
            previews = []
            for candidate in candidates[: min(len(candidates), 20)]:
                payload = _core_manager_prompt_payload(cur, candidate)
                previews.append({
                    "manager_cik": candidate["manager_cik"],
                    "legal_name": candidate["legal_name"],
                    "report_period": str(candidate["report_period"]),
                    "aum": float(candidate["aum"] or 0),
                    "top_holdings": [
                        {
                            "ticker": h.get("ticker"),
                            "cusip": h.get("cusip"),
                            "issuer_name": h.get("issuer_name"),
                            "asset_bucket": h.get("asset_bucket"),
                            "portfolio_weight": h.get("portfolio_weight"),
                        }
                        for h in payload.get("top_long_holdings", [])[:5]
                    ],
                    "prompt_payload_chars": len(json.dumps(payload, default=str, sort_keys=True)),
                })
            return {
                "dry_run": True,
                "candidate_count": len(candidates),
                "min_aum": min_aum,
                "limit": limit,
                "workers": workers,
                "previews": previews,
            }

        counts: dict[str, Any] = {
            "candidates": len(candidates),
            "classified": 0,
            "errors": 0,
            "history_rows_updated": 0,
            "min_aum": min_aum,
            "limit": limit,
            "workers": workers,
            "samples": [],
            "error_samples": [],
        }

        work_items = [
            {
                "candidate": candidate,
                "payload": _core_manager_prompt_payload(cur, candidate),
            }
            for candidate in candidates
        ]

        def persist_result(candidate: dict[str, Any], payload: dict[str, Any], result: dict[str, Any]) -> None:
            _upsert_core_manager_llm_classification(
                cur,
                manager_cik=candidate["manager_cik"],
                report_period=candidate["report_period"],
                payload=payload,
                result=result,
            )
            history_rows = 0
            if copy_history:
                history_rows = _copy_core_manager_label_to_history(
                    cur,
                    manager_cik=candidate["manager_cik"],
                    report_period=candidate["report_period"],
                    result=result,
                )
            conn.commit()
            counts["classified"] += 1
            counts["history_rows_updated"] += history_rows
            if len(counts["samples"]) < 10:
                counts["samples"].append({
                    "manager_cik": candidate["manager_cik"],
                    "legal_name": candidate["legal_name"],
                    "report_period": str(candidate["report_period"]),
                    "aum": float(candidate["aum"] or 0),
                    "primary_label": result["primary_label"],
                    "confidence_score": result["confidence_score"],
                    "route_tier": result["route_tier"],
                })

        def record_error(candidate: dict[str, Any], exc: Exception) -> None:
            conn.rollback()
            counts["errors"] += 1
            if len(counts["error_samples"]) < 10:
                counts["error_samples"].append({
                    "manager_cik": candidate["manager_cik"],
                    "legal_name": candidate["legal_name"],
                    "report_period": str(candidate["report_period"]),
                    "error": f"{type(exc).__name__}: {exc}",
                })

        if workers <= 1 or len(work_items) <= 1:
            for item in work_items:
                candidate = item["candidate"]
                payload = item["payload"]
                try:
                    result = _classify_core_manager_with_deepseek(
                        payload,
                        min_confidence=min_confidence,
                    )
                    persist_result(candidate, payload, result)
                except Exception as exc:
                    record_error(candidate, exc)
            return counts

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    _classify_core_manager_with_deepseek,
                    item["payload"],
                    min_confidence=min_confidence,
                ): item
                for item in work_items
            }
            for future in as_completed(futures):
                item = futures[future]
                candidate = item["candidate"]
                payload = item["payload"]
                try:
                    result = future.result()
                    persist_result(candidate, payload, result)
                except Exception as exc:
                    record_error(candidate, exc)
        return counts


def classify_13f_core(deterministic_only: bool = True) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core_13f_manager_classification
                (manager_cik, report_period, primary_label, confidence_score, route_tier,
                 route_reason, quantitative_trigger_metric, evidence_source, model,
                 prompt_version, classification_status, response_json)
            SELECT c.manager_cik, c.report_period, c.primary_label, c.confidence_score,
                   c.route_tier, c.route_reason, c.quantitative_trigger_metric,
                   c.evidence_source, c.model, c.prompt_version, c.classification_status,
                   c.response_json
            FROM fact_13f_manager_classification c
            JOIN core_13f_manager_period p ON p.manager_cik = c.manager_cik AND p.report_period = c.report_period
            WHERE c.classification_status = 'classified'
            ON CONFLICT (manager_cik, report_period) DO UPDATE SET
                primary_label = EXCLUDED.primary_label,
                confidence_score = EXCLUDED.confidence_score,
                route_tier = EXCLUDED.route_tier,
                route_reason = EXCLUDED.route_reason,
                quantitative_trigger_metric = EXCLUDED.quantitative_trigger_metric,
                evidence_source = EXCLUDED.evidence_source,
                model = EXCLUDED.model,
                prompt_version = EXCLUDED.prompt_version,
                classification_status = EXCLUDED.classification_status,
                response_json = EXCLUDED.response_json,
                updated_at = now()
            """
        )
        migrated = cur.rowcount

        cur.execute(
            """
            INSERT INTO core_13f_manager_classification
                (manager_cik, report_period, primary_label, confidence_score, route_tier,
                 route_reason, quantitative_trigger_metric, evidence_source, reference_id,
                 model, prompt_version, classification_status, response_json)
            SELECT p.manager_cik, p.report_period, em.target_label,
                   CASE WHEN em.match_type = 'exact' THEN 0.985 ELSE LEAST(0.955, GREATEST(0.94, em.match_score)) END,
                   'tier1_reference_link_' || em.match_type,
                   'Linked manager to curated 13F style reference.',
                   'reference_entity_match=' || COALESCE(em.evidence_source, em.reference_name),
                   em.evidence_source,
                   r.reference_id,
                   'deterministic',
                   '13f_manager_classifier_v1',
                   'classified',
                   jsonb_build_object('reference_name', em.reference_name, 'match_type', em.match_type, 'match_score', em.match_score)
            FROM core_13f_manager_period p
            JOIN ref_13f_manager_entity_match em ON em.manager_cik = p.manager_cik AND em.status = 'matched'
            LEFT JOIN ref_13f_manager_style r ON r.canonical_name = em.reference_name
            WHERE NOT EXISTS (
                SELECT 1 FROM core_13f_manager_classification c
                WHERE c.manager_cik = p.manager_cik AND c.report_period = p.report_period
            )
              AND em.target_label IS NOT NULL
            ON CONFLICT (manager_cik, report_period) DO NOTHING
            """
        )
        reference = cur.rowcount

        cur.execute(
            """
            INSERT INTO core_13f_manager_classification
                (manager_cik, report_period, primary_label, confidence_score, route_tier,
                 route_reason, quantitative_trigger_metric, model, prompt_version, classification_status)
            SELECT p.manager_cik,
                   p.report_period,
                   CASE
                       WHEN m.legal_name ~* '(VANGUARD|BLACKROCK|STATE STREET|FIDELITY|T\\.?\\s*ROWE)' THEN 'Asset Management: Traditional (Long-Term Capital)'
                       WHEN m.legal_name ~* '(TRUST COMPANY|PRIVATE BANK|PRIVATE WEALTH|WEALTH MANAGEMENT)' AND COALESCE(p.median_turnover_rate, 0) < 0.08 THEN 'Banking: Wealth & Trust (Investment)'
                       WHEN m.legal_name ~* '(LIFE|REINSURANCE|INSURANCE|ASSURANCE|P&C|CASUALTY)' AND COALESCE(p.options_ratio, 0) < 0.05 THEN 'Insurance: General Account (Long-Term Capital)'
                       WHEN m.legal_name ~* '(GOLDMAN|MORGAN STANLEY|JPMORGAN|JP MORGAN|UBS|CITIGROUP|CITI|BANK OF AMERICA|BARCLAYS|DEUTSCHE BANK|CREDIT SUISSE|NOMURA|JEFFERIES|SECURITIES)'
                            AND (COALESCE(p.options_ratio, 0) > 0.20 OR COALESCE(p.derivatives_pct, 0) > 0.20) THEN 'Banking: Capital Markets & Trading (Speculative)'
                       WHEN COALESCE(p.options_ratio, 0) > 0.20 OR COALESCE(p.max_turnover_rate, 0) > 0.40 THEN 'Asset Management: Alternative (Speculative/Trading)'
                       WHEN COALESCE(p.options_ratio, 0) < 0.02 AND COALESCE(p.position_count, 0) >= 50 THEN 'Asset Management: Traditional (Long-Term Capital)'
                       ELSE NULL
                   END AS label,
                   0.82,
                   'tier1_rule',
                   'Deterministic rule from precomputed core_13f_manager_period metrics.',
                   concat_ws('; ',
                       'options_ratio=' || COALESCE(round(p.options_ratio, 4)::text, 'null'),
                       'max_turnover_rate=' || COALESCE(round(p.max_turnover_rate, 4)::text, 'null'),
                       'position_count=' || COALESCE(p.position_count::text, 'null')
                   ),
                   'deterministic',
                   '13f_manager_classifier_v1',
                   'classified'
            FROM core_13f_manager_period p
            JOIN core_13f_manager m ON m.manager_cik = p.manager_cik
            WHERE NOT EXISTS (
                SELECT 1 FROM core_13f_manager_classification c
                WHERE c.manager_cik = p.manager_cik AND c.report_period = p.report_period
            )
            ON CONFLICT (manager_cik, report_period) DO NOTHING
            """
        )
        rules = cur.rowcount

        cur.execute(
            """
            WITH refreshed AS (
                SELECT c.manager_cik, c.report_period,
                       CASE
                           WHEN m.legal_name ~* '(VANGUARD|BLACKROCK|STATE STREET|FIDELITY|T\\.?\\s*ROWE)' THEN 'Asset Management: Traditional (Long-Term Capital)'
                           WHEN m.legal_name ~* '(TRUST COMPANY|PRIVATE BANK|PRIVATE WEALTH|WEALTH MANAGEMENT)' AND COALESCE(p.median_turnover_rate, 0) < 0.08 THEN 'Banking: Wealth & Trust (Investment)'
                           WHEN m.legal_name ~* '(LIFE|REINSURANCE|INSURANCE|ASSURANCE|P&C|CASUALTY)' AND COALESCE(p.options_ratio, 0) < 0.05 THEN 'Insurance: General Account (Long-Term Capital)'
                           WHEN m.legal_name ~* '(GOLDMAN|MORGAN STANLEY|JPMORGAN|JP MORGAN|UBS|CITIGROUP|CITI|BANK OF AMERICA|BARCLAYS|DEUTSCHE BANK|CREDIT SUISSE|NOMURA|JEFFERIES|SECURITIES)'
                                AND (COALESCE(p.options_ratio, 0) > 0.20 OR COALESCE(p.derivatives_pct, 0) > 0.20) THEN 'Banking: Capital Markets & Trading (Speculative)'
                           WHEN COALESCE(p.options_ratio, 0) > 0.20 OR COALESCE(p.max_turnover_rate, 0) > 0.40 THEN 'Asset Management: Alternative (Speculative/Trading)'
                           WHEN COALESCE(p.options_ratio, 0) < 0.02 AND COALESCE(p.position_count, 0) >= 50 THEN 'Asset Management: Traditional (Long-Term Capital)'
                           ELSE NULL
                       END AS label,
                       concat_ws('; ',
                           'options_ratio=' || COALESCE(round(p.options_ratio, 4)::text, 'null'),
                           'derivatives_pct=' || COALESCE(round(p.derivatives_pct, 4)::text, 'null'),
                           'max_turnover_rate=' || COALESCE(round(p.max_turnover_rate, 4)::text, 'null'),
                           'position_count=' || COALESCE(p.position_count::text, 'null')
                       ) AS trigger
                FROM core_13f_manager_classification c
                JOIN core_13f_manager_period p ON p.manager_cik = c.manager_cik AND p.report_period = c.report_period
                JOIN core_13f_manager m ON m.manager_cik = c.manager_cik
                WHERE c.route_tier = 'tier1_rule'
            )
            UPDATE core_13f_manager_classification c
               SET primary_label = r.label,
                   quantitative_trigger_metric = r.trigger,
                   updated_at = now()
              FROM refreshed r
             WHERE c.manager_cik = r.manager_cik
               AND c.report_period = r.report_period
               AND r.label IS NOT NULL
            """
        )
        refreshed_rules = cur.rowcount

        if deterministic_only:
            llm = 0
        else:
            llm = 0

        cur.execute(
            """
            UPDATE stg_13f_dataset d
               SET classified = true,
                   classified_at = now(),
                   updated_at = now()
             WHERE EXISTS (
                 SELECT 1
                 FROM core_13f_filing f
                 JOIN core_13f_manager_classification c
                   ON c.report_period = f.report_period
                  AND c.manager_cik = f.manager_cik
                 WHERE f.dataset_key = d.dataset_key
             )
            """
        )
        classified_datasets = cur.rowcount

    return {
        "migrated": migrated,
        "reference": reference,
        "rules": rules,
        "refreshed_rules": refreshed_rules,
        "llm": llm,
        "classified_datasets": classified_datasets,
    }


def recon_13f_core() -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recon_13f_period
                (report_period, dataset_count, downloaded_count, parsed_count, standardized_count,
                 classified_managers, filings, latest_filings, holdings, managers,
                 summary_table_entries, parsed_holding_rows, summary_reported_value,
                 parsed_reported_value, issuer_resolved_weight, price_coverage_weight,
                 factor_coverage_weight, warnings)
            WITH periods AS (
                SELECT DISTINCT report_period FROM core_13f_filing
                UNION
                SELECT DISTINCT report_period FROM stg_13f_dataset WHERE report_period IS NOT NULL
            ),
            dataset AS (
                SELECT report_period,
                       COUNT(*) AS dataset_count,
                       COUNT(*) FILTER (WHERE downloaded) AS downloaded_count,
                       COUNT(*) FILTER (WHERE parsed) AS parsed_count,
                       COUNT(*) FILTER (WHERE standardized) AS standardized_count
                FROM stg_13f_dataset
                WHERE report_period IS NOT NULL
                GROUP BY report_period
            ),
            filing AS (
                SELECT report_period,
                       COUNT(*) AS filings,
                       COUNT(*) FILTER (WHERE is_latest_amendment) AS latest_filings,
                       COUNT(DISTINCT manager_cik) AS managers,
                       SUM(table_entry_total) AS summary_table_entries,
                       SUM(table_value_total) AS summary_reported_value
                FROM core_13f_filing
                GROUP BY report_period
            ),
            holding AS (
                SELECT report_period,
                       COUNT(*) AS holdings,
                       SUM(value_reported) AS parsed_reported_value,
                       SUM(market_value_usd) FILTER (WHERE issuer_resolution_status = 'resolved') / NULLIF(SUM(market_value_usd), 0) AS issuer_resolved_weight,
                       SUM(market_value_usd) FILTER (WHERE price_covered) / NULLIF(SUM(market_value_usd), 0) AS price_coverage_weight,
                       SUM(market_value_usd) FILTER (WHERE factor_covered) / NULLIF(SUM(market_value_usd), 0) AS factor_coverage_weight
                FROM core_13f_holding
                WHERE is_latest_amendment
                GROUP BY report_period
            ),
            cls AS (
                SELECT report_period, COUNT(DISTINCT manager_cik) AS classified_managers
                FROM core_13f_manager_classification
                WHERE classification_status = 'classified'
                GROUP BY report_period
            )
            SELECT p.report_period,
                   COALESCE(d.dataset_count, 0),
                   COALESCE(d.downloaded_count, 0),
                   COALESCE(d.parsed_count, 0),
                   COALESCE(d.standardized_count, 0),
                   COALESCE(cls.classified_managers, 0),
                   COALESCE(f.filings, 0),
                   COALESCE(f.latest_filings, 0),
                   COALESCE(h.holdings, 0),
                   COALESCE(f.managers, 0),
                   f.summary_table_entries,
                   h.holdings,
                   f.summary_reported_value,
                   h.parsed_reported_value,
                   h.issuer_resolved_weight,
                   h.price_coverage_weight,
                   h.factor_coverage_weight,
                   CASE
                       WHEN f.summary_table_entries IS NOT NULL AND h.holdings IS NOT NULL
                            AND abs(f.summary_table_entries - h.holdings) > GREATEST(10, f.summary_table_entries * 0.01)
                       THEN jsonb_build_array('summary_entry_count_differs_from_parsed_holdings')
                       ELSE '[]'::jsonb
                   END
            FROM periods p
            LEFT JOIN dataset d ON d.report_period = p.report_period
            LEFT JOIN filing f ON f.report_period = p.report_period
            LEFT JOIN holding h ON h.report_period = p.report_period
            LEFT JOIN cls ON cls.report_period = p.report_period
            ON CONFLICT (report_period) DO UPDATE SET
                dataset_count = EXCLUDED.dataset_count,
                downloaded_count = EXCLUDED.downloaded_count,
                parsed_count = EXCLUDED.parsed_count,
                standardized_count = EXCLUDED.standardized_count,
                classified_managers = EXCLUDED.classified_managers,
                filings = EXCLUDED.filings,
                latest_filings = EXCLUDED.latest_filings,
                holdings = EXCLUDED.holdings,
                managers = EXCLUDED.managers,
                summary_table_entries = EXCLUDED.summary_table_entries,
                parsed_holding_rows = EXCLUDED.parsed_holding_rows,
                summary_reported_value = EXCLUDED.summary_reported_value,
                parsed_reported_value = EXCLUDED.parsed_reported_value,
                issuer_resolved_weight = EXCLUDED.issuer_resolved_weight,
                price_coverage_weight = EXCLUDED.price_coverage_weight,
                factor_coverage_weight = EXCLUDED.factor_coverage_weight,
                warnings = EXCLUDED.warnings,
                computed_at = now()
            """
        )
        rows = cur.rowcount
    return {"periods": rows}


def status_13f_core() -> dict[str, Any]:
    with connect() as conn, conn.cursor() as cur:
        out: dict[str, Any] = {}
        for key, table in {
            "datasets": "stg_13f_dataset",
            "filings": "core_13f_filing",
            "holdings": "core_13f_holding",
            "manager_periods": "core_13f_manager_period",
            "classifications": "core_13f_manager_classification",
            "recon_periods": "recon_13f_period",
        }.items():
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            out[key] = cur.fetchone()[0]
        cur.execute(
            """
            SELECT MIN(report_period), MAX(report_period), COUNT(*)
            FROM recon_13f_period
            """
        )
        min_period, max_period, periods = cur.fetchone()
        out["min_period"] = str(min_period) if min_period else None
        out["max_period"] = str(max_period) if max_period else None
        out["periods"] = periods
        cur.execute(
            """
            SELECT COALESCE(SUM(downloaded_count), 0), COALESCE(SUM(parsed_count), 0),
                   COALESCE(SUM(standardized_count), 0), COALESCE(SUM(classified_managers), 0)
            FROM recon_13f_period
            """
        )
        downloaded, parsed, standardized, classified = cur.fetchone()
        out["downloaded_period_rows"] = downloaded
        out["parsed_period_rows"] = parsed
        out["standardized_period_rows"] = standardized
        out["classified_manager_periods"] = classified
        return out
