"""US SEC downloaders and master-table refresh.

This module ports the useful source-access behavior from the legacy pipeline but
keeps all files under MZQA/market_data and all database writes in xbrl_sec.sec.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.sec_forms import normalize_form
from xbrl_sec.sec.sources.sec_filings import normalize_cik

_SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# EDGAR daily index (pipe-delimited master.idx): the "what was filed today" feed.
_SEC_DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{date}.idx"
_USER_AGENT = "MZQA XBRL pipeline contact=bastian.offermann@gmail.com"
_US_10K_10Q_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
_EXCLUDED_EXCHANGES = {"", "OTC", "OTC BULLETIN BOARD", "PINK SHEETS"}
_EXCLUDED_SIC = {"6770", "6722", "6726", "6221", "6189"}
_ENTITY_REJECT_TERMS = (
    "investment",
    "investment company",
    "investment trust",
    "mutual fund",
    "exchange-traded",
    "business development",
    "closed-end",
    "open-end",
)
_FUND_NAME_RE = re.compile(r"(^|[^A-Z0-9])(ETF|ETN|FUND|FUNDS|TRUST|PORTFOLIO|SERIES)([^A-Z0-9]|$)")
_INSTRUMENT_NAME_RE = re.compile(
    r"((^|[^A-Z0-9])(ETF|ETN)([^A-Z0-9]|$)|EXCHANGE[- ]TRADED|"
    r"INVESCO QQQ TRUST|TRUST,\s*SERIES|PPLUS TRUST|"
    r"STRUCTURED PRODUCTS|STRATS|CORTS|SEC(?:URITIES)? BACKED|"
    r"ABS CORP|DEPOSITOR INC|INDEXPLUS TRUST|"
    r"PHYSICAL .* TRUST|CARBON ALLOWANCE TRUST|ETHEREUM FUND|"
    r"BITCOIN FUND|CRYPTO|SPROTT PHYSICAL|ACQUISITION CORP)"
)


def _metadata_dir() -> Path:
    path = load_settings().market_data_root / "us_sec" / "metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _submissions_dir() -> Path:
    path = load_settings().market_data_root / "us_sec" / "submissions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _companyfacts_dir() -> Path:
    path = load_settings().market_data_root / "us_sec" / "companyfacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _submission_archive_dir(cik: str) -> Path:
    path = _submissions_dir() / f"CIK{normalize_cik(cik)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_json(url: str, retries: int = 3, delay: float = 0.2) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"})
            with urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last_error}")


def _get_text(
    url: str,
    retries: int = 3,
    delay: float = 0.2,
    missing_codes: tuple[int, ...] = (404,),
) -> str | None:
    """Fetch a text resource. Returns None when the server reports the resource is
    absent. EDGAR's ``/Archives`` serves a **403** (not 404) for files that don't
    exist (e.g. a daily index on a market holiday), so daily-index callers pass
    ``missing_codes=(403, 404)``."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"})
            with urlopen(req, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            if exc.code in missing_codes:
                return None
            last_error = exc
            time.sleep(delay * (attempt + 1))
        except (URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last_error}")


def _parse_daily_index_ciks(text: str, forms: Iterable[str] = _US_10K_10Q_FORMS) -> set[str]:
    """Parse an EDGAR ``master.idx`` body into the set of normalized CIKs that filed
    one of ``forms`` that day. The file has a free-text header, a dashed separator,
    then pipe-delimited rows: ``CIK|Company Name|Form Type|Date Filed|Filename``."""
    wanted = {str(f).strip().upper() for f in forms}
    ciks: set[str] = set()
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik_raw, _company, form, _filed, _filename = parts
        cik_raw = cik_raw.strip()
        if not cik_raw.isdigit():
            # Skips the header and the dashed separator line.
            continue
        if form.strip().upper() not in wanted:
            continue
        ciks.add(normalize_cik(cik_raw))
    return ciks


def _fetch_daily_index(day: date) -> str | None:
    """Fetch the EDGAR daily ``master.idx`` for ``day`` (None on weekends/holidays)."""
    quarter = (day.month - 1) // 3 + 1
    url = _SEC_DAILY_INDEX_URL.format(year=day.year, quarter=quarter, date=day.strftime("%Y%m%d"))
    # EDGAR returns 403 (not 404) for a daily index that doesn't exist (weekends,
    # market holidays, future dates) — treat both as "no index that day".
    text = _get_text(url, missing_codes=(403, 404))
    if text is not None:
        time.sleep(0.11)
    return text


def _in_scope_ciks(entity_ids: Iterable[str] | None = None) -> list[str]:
    """Normalized CIKs in the active pipeline scope (or an explicit entity subset)."""
    if entity_ids is not None:
        return [normalize_cik(v) for v in entity_ids if str(v).strip()]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cik
            FROM dim_company_us
            WHERE cik IS NOT NULL
              AND include_in_pipeline
            ORDER BY cik
            """
        )
        return [normalize_cik(r[0]) for r in cur.fetchall()]


def discover_changed_us_ciks(
    start_date: date,
    end_date: date,
    scope_ciks: Iterable[str] | None = None,
    forms: Iterable[str] = _US_10K_10Q_FORMS,
) -> set[str]:
    """In-scope CIKs that filed one of ``forms`` between ``start_date`` and
    ``end_date`` (inclusive), per the EDGAR daily index. Days with no published
    index (weekends/holidays/future) contribute nothing."""
    scope = set(_in_scope_ciks() if scope_ciks is None else (normalize_cik(c) for c in scope_ciks))
    changed: set[str] = set()
    day = start_date
    while day <= end_date:
        text = _fetch_daily_index(day)
        if text:
            changed |= _parse_daily_index_ciks(text, forms) & scope
        day += timedelta(days=1)
    return changed


def _us_filed_watermark() -> date | None:
    """Most recent filed_date already recorded for US filings, used as the
    incremental daily-index watermark."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(filed_date) FROM source_filing_state WHERE jurisdiction='US'")
        row = cur.fetchone()
        return row[0] if row else None


def download_company_tickers_exchange() -> Path:
    payload = _get_json(_SEC_TICKERS_EXCHANGE_URL)
    path = _metadata_dir() / "company_tickers_exchange.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # Also maintain the historical filename expected by some tooling.
    (_metadata_dir() / "company_tickers.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (_metadata_dir() / "company_tickers_last_download.txt").write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )
    return path


def _load_ticker_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "fields" in payload and "data" in payload:
        fields = payload["fields"]
        return [dict(zip(fields, row)) for row in payload["data"]]
    if isinstance(payload, dict) and all(isinstance(value, dict) for value in payload.values()):
        rows = [payload[key] for key in sorted(payload, key=lambda item: int(item) if item.isdigit() else item)]
        return rows
    raise ValueError(f"Unexpected SEC ticker format: {path}")


def _is_allowed_exchange(exchange: str | None) -> bool:
    return (exchange or "").strip().upper() not in _EXCLUDED_EXCHANGES


def _normalized_sic(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _entity_class(entity_type: str | None, name: str | None) -> str | None:
    et = (entity_type or "").lower()
    name_text = (name or "").lower()
    combined = f"{et} {name_text}"
    if any(term in et for term in _ENTITY_REJECT_TERMS):
        return None
    if "real estate investment trust" in combined or " reit" in combined or combined.endswith("reit"):
        return "REIT"
    if "limited partnership" in combined or "master limited" in combined or " lp" in combined or combined.endswith("lp"):
        return "LP"
    return "CORP"


def _ticker_row_rank(row: tuple) -> tuple:
    ticker = row[2] or ""
    exchange = row[3] or ""
    entity_class = row[5] or ""
    exchange_priority = {
        "Nasdaq": 6,
        "NYSE": 6,
        "NYSE American": 5,
        "NYSE Arca": 4,
        "Cboe BZX": 3,
    }.get(exchange, 2)
    class_priority = {"CORP": 5, "REIT": 4, "LP": 3}.get(entity_class, 0)
    common_ticker = "-" not in ticker and "." not in ticker and "^" not in ticker
    return (class_priority, exchange_priority, common_ticker, -len(ticker), ticker)


def _strict_fund_name_reject(
    entity_type: str | None,
    edgar_category: str | None,
    sic: str | None,
    name: str | None,
) -> bool:
    """Catch fund-like instruments when SEC entityType is too generic.

    This mirrors the useful narrow legacy guard: only use name terms when the
    record otherwise lacks entity/category/SIC evidence. A broad name-only
    filter would incorrectly reject operating companies with words like
    "Trust" in their legal names.
    """
    if (entity_type or "").strip().lower() != "other":
        return False
    if edgar_category or sic:
        return False
    return bool(_FUND_NAME_RE.search((name or "").upper()))


def _instrument_name_reject(name: str | None) -> bool:
    return bool(_INSTRUMENT_NAME_RE.search((name or "").upper()))


def _dedupe_master_rows(rows: list[tuple]) -> list[tuple]:
    by_cik: dict[str, tuple] = {}
    for row in rows:
        cik = row[0]
        current = by_cik.get(cik)
        if current is None or _ticker_row_rank(row) > _ticker_row_rank(current):
            by_cik[cik] = row
    return list(by_cik.values())


def _ticker_link_rows(rows: list[tuple], primary_rows: list[tuple]) -> list[tuple]:
    primary_by_cik = {row[0]: row[2] for row in primary_rows}
    seen: set[tuple[str, str]] = set()
    out: list[tuple] = []
    for row in rows:
        cik = row[0]
        ticker = row[2]
        if not cik or not ticker:
            continue
        key = (cik, ticker)
        if key in seen:
            continue
        seen.add(key)
        out.append((cik, "CIK", "US", ticker, ticker == primary_by_cik.get(cik)))
    return out


def download_submission(cik: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    cik = normalize_cik(cik)
    try:
        payload = _get_json(_SEC_SUBMISSIONS_URL.format(cik=cik))
    except HTTPError as exc:
        if exc.code == 404:
            return None, None, "404"
        raise
    path = _submissions_dir() / f"CIK{cik}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload, None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def download_submission_history(cik: str, force: bool = False) -> tuple[list[Path], str | None]:
    """Download the SEC submissions feed plus older paged history files."""
    cik = normalize_cik(cik)
    main_path = _submissions_dir() / f"CIK{cik}.json"
    try:
        if main_path.exists() and not force:
            payload = _load_json(main_path)
        else:
            main_path, payload, error = download_submission(cik)
            if error:
                return [], error
            time.sleep(0.11)
    except HTTPError as exc:
        return [], "404" if exc.code == 404 else f"HTTP {exc.code}"
    except Exception as exc:
        return [], str(exc)[:500]
    if not main_path or payload is None:
        return [], "empty_submission"

    paths = [main_path]
    archive_dir = _submission_archive_dir(cik)
    for file_info in (payload.get("filings") or {}).get("files") or []:
        name = str(file_info.get("name") or "").strip()
        if not name:
            continue
        target = archive_dir / name
        if target.exists() and not force:
            paths.append(target)
            continue
        try:
            archive_payload = _get_json(f"https://data.sec.gov/submissions/{name}")
            target.write_text(json.dumps(archive_payload, ensure_ascii=False), encoding="utf-8")
            paths.append(target)
            time.sleep(0.11)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            return paths, f"HTTP {exc.code}"
        except Exception as exc:
            return paths, str(exc)[:500]
    return paths, None


def _submission_array(payload: dict[str, Any], key: str) -> list[Any]:
    filings = payload.get("filings") if "filings" in payload else payload
    recent = filings.get("recent") if isinstance(filings, dict) else None
    source = recent if isinstance(recent, dict) else payload
    value = source.get(key) if isinstance(source, dict) else None
    return value if isinstance(value, list) else []


def _submission_rows(cik: str, path: Path, payload: dict[str, Any]) -> list[tuple]:
    cik = normalize_cik(cik)
    accessions = _submission_array(payload, "accessionNumber")
    forms = _submission_array(payload, "form")
    filed_dates = _submission_array(payload, "filingDate")
    report_dates = _submission_array(payload, "reportDate")
    primary_docs = _submission_array(payload, "primaryDocument")
    rows: list[tuple] = []
    for idx, accession in enumerate(accessions):
        accn = str(accession or "").strip()
        if not accn:
            continue
        form = normalize_form(forms[idx] if idx < len(forms) else None)
        if form not in _US_10K_10Q_FORMS:
            continue
        rows.append((
            "US", accn, cik, form,
            (filed_dates[idx] if idx < len(filed_dates) else None) or None,
            (report_dates[idx] if idx < len(report_dates) else None) or None,
            None, True, True, False, str(path),
            json.dumps({
                "source": "sec_submissions",
                "primaryDocument": primary_docs[idx] if idx < len(primary_docs) else None,
            }),
            "sec_submissions",
        ))
    return rows


def _dedupe_submission_rows(rows: list[tuple]) -> list[tuple]:
    by_key: dict[tuple[str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[1])
        current = by_key.get(key)
        if current is None:
            by_key[key] = row
            continue
        current_filed = current[4] or ""
        row_filed = row[4] or ""
        if row_filed >= current_filed:
            by_key[key] = row
    return list(by_key.values())


def sync_submissions_filing_index(
    entity_ids: list[str] | None = None,
    force: bool = False,
    include_in_pipeline: bool | None = True,
) -> dict[str, int]:
    with connect() as conn, conn.cursor() as cur:
        scope_filter = ""
        scope_params: list = []
        if include_in_pipeline is not None:
            scope_filter = "AND include_in_pipeline = %s"
            scope_params.append(include_in_pipeline)
        if entity_ids:
            params = [[normalize_cik(v) for v in entity_ids], *scope_params]
            cur.execute(
                f"""
                SELECT cik FROM dim_company_us
                WHERE cik = ANY(%s)
                  {scope_filter}
                ORDER BY cik
                """,
                tuple(params),
            )
        else:
            cur.execute(
                f"""
                SELECT cik FROM dim_company_us
                WHERE cik IS NOT NULL
                  {scope_filter}
                ORDER BY cik
                """,
                tuple(scope_params),
            )
        ciks = [row[0] for row in cur.fetchall()]

    all_rows: list[tuple] = []
    submission_files = errors = 0
    for cik in ciks:
        paths, error = download_submission_history(cik, force=force)
        submission_files += len(paths)
        if error:
            errors += 1
        for path in paths:
            try:
                all_rows.extend(_submission_rows(cik, path, _load_json(path)))
            except Exception:
                errors += 1

    all_rows = _dedupe_submission_rows(all_rows)
    indexed = 0
    if all_rows:
        with connect() as conn, conn.cursor() as cur:
            indexed = execute_values(
                cur,
                """
                INSERT INTO source_filing_state
                    (jurisdiction, filing_id, entity_id, filing_type, filed_date, period_end,
                     source_hash, downloaded, extracted, parsed, source_path, raw_payload, source_kind)
                VALUES %s
                ON CONFLICT (jurisdiction, filing_id) DO UPDATE SET
                    entity_id = EXCLUDED.entity_id,
                    filing_type = EXCLUDED.filing_type,
                    filed_date = COALESCE(EXCLUDED.filed_date, source_filing_state.filed_date),
                    period_end = COALESCE(EXCLUDED.period_end, source_filing_state.period_end),
                    downloaded = source_filing_state.downloaded OR EXCLUDED.downloaded,
                    extracted = source_filing_state.extracted OR EXCLUDED.extracted,
                    source_path = COALESCE(EXCLUDED.source_path, source_filing_state.source_path),
                    raw_payload = COALESCE(EXCLUDED.raw_payload, source_filing_state.raw_payload),
                    source_kind = CASE
                        WHEN source_filing_state.source_kind = 'companyfacts' THEN 'companyfacts'
                        ELSE EXCLUDED.source_kind
                    END,
                    updated_at = now()
                """,
                all_rows,
                page_size=5000,
            )
    return {
        "companies": len(ciks),
        "submission_files": submission_files,
        "filings_indexed": indexed,
        "errors": errors,
    }


def refresh_master(full: bool = False, max_ciks: int | None = None, download: bool = True) -> int:
    ticker_path = download_company_tickers_exchange() if download else (_metadata_dir() / "company_tickers_exchange.json")
    if not ticker_path.exists():
        ticker_path = _metadata_dir() / "company_tickers.json"
    rows_raw = [row for row in _load_ticker_rows(ticker_path) if _is_allowed_exchange(row.get("exchange"))]
    if max_ciks:
        rows_raw = rows_raw[:max_ciks]

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cik FROM dim_company_us")
        existing = {r[0] for r in cur.fetchall()}

    out = []
    for rec in rows_raw:
        cik = normalize_cik(rec.get("cik") or rec.get("cik_str"))
        if not full and cik in existing:
            continue
        ticker = (rec.get("ticker") or "").strip() or None
        name = (rec.get("name") or rec.get("title") or "").strip() or None
        exchange = (rec.get("exchange") or "").strip() or None
        entity_type = sic = sic_desc = fy_end = state = country = None
        sub_payload = None
        if download:
            _, sub_payload, error = download_submission(cik)
            if error:
                sub_payload = None
            time.sleep(0.11)
        else:
            sub_path = _submissions_dir() / f"CIK{cik}.json"
            if sub_path.exists():
                sub_payload = json.loads(sub_path.read_text(encoding="utf-8"))
        if sub_payload:
            entity_type = sub_payload.get("entityType")
            sic = _normalized_sic(sub_payload.get("sic"))
            sic_desc = sub_payload.get("sicDescription")
            fy_end = sub_payload.get("fiscalYearEnd")
            state = sub_payload.get("stateOfIncorporation")
            addresses = sub_payload.get("addresses") or {}
            business = addresses.get("business") or {}
            country = business.get("stateOrCountry") or business.get("country")
            name = sub_payload.get("name") or name
        entity_class = _entity_class(entity_type, name)
        if sic in _EXCLUDED_SIC or entity_class is None or _instrument_name_reject(name):
            continue
        if _strict_fund_name_reject(entity_type, sub_payload.get("category") if sub_payload else None, sic, name):
            continue
        out.append((
            cik, name, ticker, exchange, entity_type, entity_class,
            sic, sic_desc, fy_end, country, state,
        ))
    primary_rows = _dedupe_master_rows(out)
    ticker_rows = _ticker_link_rows(out, primary_rows)
    sql = """
        INSERT INTO dim_company_us
            (cik, name, primary_ticker, exchange, entity_type, entity_class,
             sic, sic_description, fiscal_year_end, country_code, state_of_incorporation)
        VALUES %s
        ON CONFLICT (cik) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, dim_company_us.name),
            primary_ticker = COALESCE(EXCLUDED.primary_ticker, dim_company_us.primary_ticker),
            exchange = COALESCE(EXCLUDED.exchange, dim_company_us.exchange),
            entity_type = EXCLUDED.entity_type,
            entity_class = EXCLUDED.entity_class,
            sic = EXCLUDED.sic,
            sic_description = EXCLUDED.sic_description,
            fiscal_year_end = EXCLUDED.fiscal_year_end,
            country_code = EXCLUDED.country_code,
            state_of_incorporation = EXCLUDED.state_of_incorporation,
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        company_rows = execute_values(cur, sql, primary_rows, page_size=1000)
        if full:
            cur.execute(
                """
                DELETE FROM ref_entity_ticker r
                 WHERE r.jurisdiction = 'US'
                   AND r.entity_id_type = 'CIK'
                   AND EXISTS (
                        SELECT 1
                        FROM dim_company_us c
                        WHERE c.cik = r.entity_id
                          AND (
                                COALESCE(c.exchange, '') = ''
                             OR UPPER(COALESCE(c.exchange, '')) IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                             OR c.sic IN ('6770', '6722', '6726', '6221', '6189')
                             OR c.entity_class IN ('FUND', 'TRUST')
                             OR LOWER(COALESCE(c.entity_type, '')) = 'investment'
                             OR UPPER(COALESCE(c.name, '')) ~ '((^|[^A-Z0-9])(ETF|ETN)([^A-Z0-9]|$)|EXCHANGE[- ]TRADED|INVESCO QQQ TRUST|TRUST,\\s*SERIES|PPLUS TRUST|STRUCTURED PRODUCTS|STRATS|CORTS|SEC(?:URITIES)? BACKED|ABS CORP|DEPOSITOR INC|INDEXPLUS TRUST|PHYSICAL .* TRUST|CARBON ALLOWANCE TRUST|ETHEREUM FUND|BITCOIN FUND|CRYPTO|SPROTT PHYSICAL|ACQUISITION CORP)'
                          )
                   )
                """
            )
            cur.execute(
                """
                DELETE FROM dim_company_us c
                 WHERE (
                        COALESCE(c.exchange, '') = ''
                     OR UPPER(COALESCE(c.exchange, '')) IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                     OR c.sic IN ('6770', '6722', '6726', '6221', '6189')
                     OR c.entity_class IN ('FUND', 'TRUST')
                     OR LOWER(COALESCE(c.entity_type, '')) = 'investment'
                     OR UPPER(COALESCE(c.name, '')) ~ '((^|[^A-Z0-9])(ETF|ETN)([^A-Z0-9]|$)|EXCHANGE[- ]TRADED|INVESCO QQQ TRUST|TRUST,\\s*SERIES|PPLUS TRUST|STRUCTURED PRODUCTS|STRATS|CORTS|SEC(?:URITIES)? BACKED|ABS CORP|DEPOSITOR INC|INDEXPLUS TRUST|PHYSICAL .* TRUST|CARBON ALLOWANCE TRUST|ETHEREUM FUND|BITCOIN FUND|CRYPTO|SPROTT PHYSICAL|ACQUISITION CORP)'
                   )
                """
            )
        if ticker_rows:
            cur.execute(
                """
                UPDATE ref_entity_ticker r
                   SET is_primary = false,
                       updated_at = now()
                 WHERE r.jurisdiction = 'US'
                   AND r.entity_id_type = 'CIK'
                   AND r.entity_id = ANY(%s)
                """,
                ([row[0] for row in primary_rows],),
            )
            execute_values(
                cur,
                """
                INSERT INTO ref_entity_ticker
                    (entity_id, entity_id_type, jurisdiction, ticker, is_primary)
                VALUES %s
                ON CONFLICT (entity_id, entity_id_type, ticker) DO UPDATE SET
                    jurisdiction = EXCLUDED.jurisdiction,
                    is_primary = EXCLUDED.is_primary,
                    updated_at = now()
                """,
                ticker_rows,
                page_size=5000,
            )
        return company_rows


def _companyfacts_path(cik: str) -> Path:
    return _companyfacts_dir() / f"CIK{normalize_cik(cik)}.json"


def _get_bytes(url: str, retries: int = 3, delay: float = 0.2) -> bytes:
    """Fetch a resource as raw bytes (no JSON parse). Used for large companyfacts
    payloads where parse-then-re-serialize would spike memory."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"})
            with urlopen(req, timeout=120) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                raise
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last_error}")


def download_companyfacts(cik: str, force: bool = False) -> tuple[Path | None, str | None]:
    cik = normalize_cik(cik)
    path = _companyfacts_path(cik)
    if path.exists() and not force:
        return path, None
    try:
        # Stream raw bytes straight to disk. Parsing into a dict and re-dumping
        # (the old path) tripled memory and OOM'd on mega-filer companyfacts.
        raw = _get_bytes(_SEC_COMPANYFACTS_URL.format(cik=cik))
    except HTTPError as exc:
        if exc.code == 404:
            path.with_name(path.stem + "_404.json").write_text("{}", encoding="utf-8")
            return None, "404"
        raise
    if raw.lstrip()[:1] != b"{":
        return None, "not_json"
    path.write_bytes(raw)
    time.sleep(0.11)
    return path, None


def _resolve_changed_ciks(
    scope: list[str],
    since: date | None,
    lookback_days: int | None,
) -> tuple[set[str], date, date]:
    """Compute the daily-index window and the set of in-scope CIKs that filed a
    target form within it. ``data.sec.gov`` exposes no conditional-GET on
    companyfacts, so the EDGAR daily index is the "what changed" signal."""
    settings = load_settings()
    end_date = date.today()
    if since is not None:
        start_date = since
    else:
        floor_days = lookback_days if lookback_days is not None else settings.us_companyfacts_lookback_days
        # Floor = minimum lookback: always cover at least this many days even when the
        # watermark is right up to date.
        floor_start = end_date - timedelta(days=max(floor_days, 0))
        watermark = _us_filed_watermark()
        if watermark is None:
            start_date = floor_start
        else:
            overlap_start = watermark - timedelta(days=settings.us_daily_index_overlap_days)
            # Extend further back than the floor when the watermark is stale (catch-up),
            # but never less than the floor.
            start_date = min(overlap_start, floor_start)
        # Bound the catch-up scan so a very old/empty watermark can't scan years of index.
        max_start = end_date - timedelta(days=settings.us_max_lookback_days)
        if start_date < max_start:
            print(
                f"companyfacts incremental: capping daily-index lookback at "
                f"{settings.us_max_lookback_days}d (watermark stale); run --force for a full refresh",
                flush=True,
            )
            start_date = max_start
    if start_date > end_date:
        start_date = end_date
    changed = discover_changed_us_ciks(start_date, end_date, scope_ciks=scope)
    return changed, start_date, end_date


def download_companyfacts_for_master(
    force: bool = False,
    max_ciks: int | None = None,
    entity_ids: Iterable[str] | None = None,
    changed_ciks: Iterable[str] | None = None,
    since: date | None = None,
    lookback_days: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Refresh SEC companyfacts JSON for the active scope.

    Full mode (``force=True``) refetches every in-scope CIK. Incremental mode
    refetches only CIKs that (a) filed a target form recently per the EDGAR daily
    index, or (b) have no local file yet — leaving unchanged files untouched so the
    downstream content-hash logic correctly skips them.

    Returns honest counters: ``candidates`` (CIKs considered), ``fetched`` (existing
    files refreshed from SEC), ``new`` (first-time downloads), ``skipped_unchanged``,
    ``not_found`` (SEC 404), ``errors`` (real failures).
    """
    ciks = _in_scope_ciks(entity_ids)
    if max_ciks:
        ciks = ciks[:max_ciks]
    total = len(ciks)

    window: tuple[date, date] | None = None
    if force:
        refetch: set[str] | None = None  # everything
    elif changed_ciks is not None:
        refetch = {normalize_cik(c) for c in changed_ciks}
    else:
        refetch, start_date, end_date = _resolve_changed_ciks(ciks, since, lookback_days)
        window = (start_date, end_date)

    candidates = total
    fetched = new = skipped_unchanged = not_found = errors = 0
    last_emit = 0.0

    def emit(phase: str, current_cik: str | None = None, force_emit: bool = False) -> None:
        nonlocal last_emit
        if progress_callback is None:
            return
        processed = fetched + new + skipped_unchanged + not_found + errors
        now = time.monotonic()
        if not force_emit and total > 5 and processed % 100 != 0 and now - last_emit < 15:
            return
        last_emit = now
        progress_callback({
            "event_type": "stage_progress" if phase == "progress" else f"stage_{phase}",
            "message": (
                f"US CompanyFacts {phase}: {processed}/{total} "
                f"fetched={fetched} new={new} skipped_unchanged={skipped_unchanged} "
                f"not_found={not_found} errors={errors}"
            ),
            "phase": phase,
            "rows_in": total,
            "rows_out": fetched + new,
            "total": total,
            "processed": processed,
            "fetched": fetched,
            "new": new,
            "skipped_unchanged": skipped_unchanged,
            "not_found": not_found,
            "errors": errors,
            "current": {"cik": current_cik} if current_cik else None,
            "filters": {
                "force": force,
                "max_ciks": max_ciks,
                "window": [w.isoformat() for w in window] if window else None,
            },
        })

    emit("started", force_emit=True)
    for cik in ciks:
        existed = _companyfacts_path(cik).exists()
        should_fetch = force or refetch is None or cik in refetch or not existed
        if not should_fetch:
            skipped_unchanged += 1
            emit("progress", current_cik=cik)
            continue
        # force=True bypasses download_companyfacts' exists-guard so we actually refetch.
        _, error = download_companyfacts(cik, force=True)
        if error == "404":
            not_found += 1
            emit("error", current_cik=cik, force_emit=True)
        elif error:
            errors += 1
            emit("error", current_cik=cik, force_emit=True)
        elif existed:
            fetched += 1
            emit("progress", current_cik=cik)
        else:
            new += 1
            emit("progress", current_cik=cik)
    emit("finished", force_emit=True)
    return {
        "candidates": candidates,
        "fetched": fetched,
        "new": new,
        "skipped_unchanged": skipped_unchanged,
        "not_found": not_found,
        "errors": errors,
        "window": [w.isoformat() for w in window] if window else None,
    }
