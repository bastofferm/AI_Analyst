from __future__ import annotations

import csv
from difflib import SequenceMatcher
import io
import json
import os
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.filings.helpers import (
    clean_accession,
    dashed_accession,
    download_url,
    normalize_cik,
    parse_date,
    sec_request,
    sha256_file,
    us_sec_root,
)
from xbrl_sec.sec.state.store import finish_run, start_run


_DATASET_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
_DATASET_CATALOG_API = "https://catalog.data.gov/api/3/action/package_show?id=form-13f-data-sets"
_DATASET_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"
_FORMS_13DG = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}


def _inst_root() -> Path:
    return us_sec_root() / "institutional" / "13f"


def _dg_root() -> Path:
    return us_sec_root() / "institutional" / "13dg"


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _value(row: dict[str, str], *names: str) -> str | None:
    normed = {_norm_key(k): v for k, v in row.items()}
    for name in names:
        val = normed.get(_norm_key(name))
        if val not in (None, ""):
            return val
    return None


def _int_value(value: str | None):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return None


def _num_value(value: str | None):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _dedupe_rows(rows: list[tuple], key_index: int = 0) -> list[tuple]:
    out = {}
    for row in rows:
        key = row[key_index]
        if key not in out:
            out[key] = row
    return list(out.values())


def _quarter_end_from_label(label: str) -> date | None:
    match = re.search(r"(20\d{2})\s*[qQ]([1-4])", label or "")
    if not match:
        return None
    year = int(match.group(1))
    q = int(match.group(2))
    return date(year, q * 3, {1: 31, 2: 30, 3: 30, 4: 31}[q])


def _generated_dataset_urls(start_year: int = 2013, end_year: int | None = None) -> list[tuple[str, str, str]]:
    today = date.today()
    if end_year is None:
        end_year = today.year
    if today.month <= 3:
        max_year, max_q = today.year - 1, 4
    elif today.month <= 6:
        max_year, max_q = today.year, 1
    elif today.month <= 9:
        max_year, max_q = today.year, 2
    else:
        max_year, max_q = today.year, 3
    out = []
    for year in range(start_year, end_year + 1):
        for q in range(1, 5):
            if year == 2013 and q == 1:
                continue
            if (year, q) > (max_year, max_q):
                continue
            key = f"{year}Q{q}"
            out.append((key, f"{_DATASET_BASE}/{year}q{q}_form13f.zip", key))
    return out


def _discover_dataset_urls_from_sec() -> list[tuple[str, str, str]]:
    try:
        with urlopen(sec_request(_DATASET_PAGE), timeout=60) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return []
    links = sorted(set(re.findall(r'https://www\.sec\.gov/files/structureddata/data/form-13f-data-sets/[^"\']+?\.zip', html)))
    out = []
    for url in links:
        name = Path(url).name
        key = name.removesuffix("_form13f.zip").upper()
        out.append((key, url, key))
    return out


def _discover_dataset_urls_from_catalog() -> list[tuple[str, str, str]]:
    try:
        with urlopen(sec_request(_DATASET_CATALOG_API), timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception:
        return []
    resources = (payload.get("result") or {}).get("resources") or []
    out = []
    for resource in resources:
        url = resource.get("downloadURL") or resource.get("url")
        if not url or not str(url).lower().endswith(".zip") or "form13f" not in str(url).lower():
            continue
        name = Path(str(url)).name
        key = name.removesuffix("_form13f.zip").upper()
        label = resource.get("name") or resource.get("description") or key
        out.append((key, str(url), str(label)))
    return sorted(set(out))


def discover_13f(from_year: int = 2013, to_year: int | None = None) -> dict[str, int]:
    ctx = start_run("US_13F", "discover_13f", "incremental")
    try:
        discovered = _discover_dataset_urls_from_sec() or _discover_dataset_urls_from_catalog()
        if not discovered:
            discovered = _generated_dataset_urls(from_year, to_year)
        rows = []
        for dataset_key, url, label in discovered:
            q_end = _quarter_end_from_label(label)
            rows.append((dataset_key, url, label, json.dumps({"quarter_end": str(q_end) if q_end else None})))
        with connect() as conn, conn.cursor() as cur:
            written = execute_values(
                cur,
                """
                INSERT INTO source_13f_dataset_state
                    (dataset_key, dataset_url, period_label, metadata)
                VALUES %s
                ON CONFLICT (dataset_key) DO UPDATE SET
                    dataset_url = EXCLUDED.dataset_url,
                    period_label = EXCLUDED.period_label,
                    updated_at = now()
                """,
                rows,
            )
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=written)
        return {"datasets": written}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def download_13f(quarter: str | None = None, force: bool = False, limit: int | None = None) -> dict[str, int]:
    params: list = []
    where = ""
    if quarter:
        where = "WHERE upper(dataset_key) = %s OR upper(period_label) = %s"
        params = [quarter.upper(), quarter.upper()]
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    ctx = start_run("US_13F", "download_13f", "incremental")
    downloaded = skipped = errors = 0
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT dataset_key, dataset_url
                FROM source_13f_dataset_state
                {where}
                ORDER BY dataset_key
                {limit_sql}
                """,
                params,
            )
            rows = cur.fetchall()
        state_rows = []
        for dataset_key, url in rows:
            quarter_dir = _inst_root() / str(dataset_key)
            existing_tsv = quarter_dir / "INFOTABLE.tsv"
            dest = quarter_dir / Path(url or f"{dataset_key}_form13f.zip").name
            if existing_tsv.exists() and not force:
                skipped += 1
                state_rows.append((str(quarter_dir), True, None, sha256_file(existing_tsv), dataset_key))
                continue
            if dest.exists() and not force:
                skipped += 1
                state_rows.append((str(dest), True, None, sha256_file(dest), dataset_key))
                continue
            ok, error = download_url(url, dest, force=force)
            if ok:
                downloaded += 1
            else:
                errors += 1
            state_rows.append((str(dest), ok or dest.exists(), error, sha256_file(dest), dataset_key))
        with connect() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                UPDATE source_13f_dataset_state AS s
                   SET local_path = v.local_path,
                       downloaded = v.downloaded,
                       downloaded_at = CASE WHEN v.downloaded THEN now() ELSE s.downloaded_at END,
                       download_error = v.download_error,
                       source_hash = v.source_hash,
                       updated_at = now()
                  FROM (VALUES %s) AS v(local_path, downloaded, download_error, source_hash, dataset_key)
                 WHERE s.dataset_key = v.dataset_key
                """,
                state_rows,
            )
        finish_run(ctx, "succeeded", rows_in=len(rows), rows_out=downloaded)
        return {"candidates": len(rows), "downloaded": downloaded, "skipped": skipped, "errors": errors}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def register_local_13f(quarter: str | None = None) -> dict[str, int]:
    """Register local extracted 13F folders or ZIPs in source_13f_dataset_state."""
    root = _inst_root()
    rows = []
    if not root.exists():
        return {"registered": 0}
    for quarter_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        dataset_key = quarter_dir.name.upper()
        if quarter and dataset_key != quarter.upper():
            continue
        infotable = quarter_dir / "INFOTABLE.tsv"
        local_path = None
        source_hash = None
        if infotable.exists():
            local_path = quarter_dir
            source_hash = sha256_file(infotable)
        else:
            zips = sorted(quarter_dir.glob("*_form13f.zip"))
            if zips:
                local_path = zips[0]
                source_hash = sha256_file(local_path)
        if local_path is None:
            continue
        rows.append((
            dataset_key,
            f"{_DATASET_BASE}/{dataset_key.lower()}_form13f.zip",
            dataset_key,
            str(local_path),
            True,
            datetime.now(timezone.utc),
            None,
            source_hash,
            json.dumps({"quarter_end": str(_quarter_end_from_label(dataset_key)) if _quarter_end_from_label(dataset_key) else None}),
        ))
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO source_13f_dataset_state
                (dataset_key, dataset_url, period_label, local_path, downloaded, downloaded_at,
                 download_error, source_hash, metadata)
            VALUES %s
            ON CONFLICT (dataset_key) DO UPDATE SET
                dataset_url = EXCLUDED.dataset_url,
                period_label = EXCLUDED.period_label,
                local_path = EXCLUDED.local_path,
                downloaded = true,
                downloaded_at = COALESCE(source_13f_dataset_state.downloaded_at, now()),
                download_error = NULL,
                source_hash = EXCLUDED.source_hash,
                metadata = source_13f_dataset_state.metadata || EXCLUDED.metadata,
                updated_at = now()
        """, rows)
    return {"registered": written}


def _zip_member(zf: zipfile.ZipFile, suffix: str) -> str | None:
    suffix = suffix.lower()
    for name in zf.namelist():
        if Path(name).name.lower() == suffix:
            return name
    for name in zf.namelist():
        if Path(name).name.lower().endswith(suffix):
            return name
    return None


def _read_tsv(zf: zipfile.ZipFile, member: str | None) -> list[dict[str, str]]:
    if not member:
        return []
    data = zf.read(member).decode("utf-8-sig", errors="ignore")
    return list(csv.DictReader(io.StringIO(data), delimiter="\t"))


def _iter_tsv(zf: zipfile.ZipFile, member: str | None):
    if not member:
        return
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="ignore", newline="")
        yield from csv.DictReader(text, delimiter="\t")


def _read_tsv_file(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _iter_tsv_file(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as fh:
        yield from csv.DictReader(fh, delimiter="\t")


_INSERT_HOLDINGS_SQL = """
    INSERT INTO fact_13f_holdings
        (accession_number, infotable_sk, manager_cik, report_period, filing_type, filed_date,
         issuer_name, title_of_class, cusip, figi, cusip6, issuer_cik, issuer_ticker,
         value_x1000, shares_or_principal, sh_prn_flag, put_call, investment_discretion,
         other_manager, voting_authority_sole, voting_authority_shared, voting_authority_none,
         is_amendment, amendment_number, dataset_key, raw_payload)
    VALUES %s
    ON CONFLICT (accession_number, infotable_sk) DO UPDATE SET
         manager_cik = EXCLUDED.manager_cik,
         report_period = EXCLUDED.report_period,
         filing_type = EXCLUDED.filing_type,
         filed_date = EXCLUDED.filed_date,
         issuer_name = EXCLUDED.issuer_name,
         title_of_class = EXCLUDED.title_of_class,
         cusip = EXCLUDED.cusip,
         figi = EXCLUDED.figi,
         cusip6 = EXCLUDED.cusip6,
         value_x1000 = EXCLUDED.value_x1000,
         shares_or_principal = EXCLUDED.shares_or_principal,
         sh_prn_flag = EXCLUDED.sh_prn_flag,
         put_call = EXCLUDED.put_call,
         investment_discretion = EXCLUDED.investment_discretion,
         other_manager = EXCLUDED.other_manager,
         voting_authority_sole = EXCLUDED.voting_authority_sole,
         voting_authority_shared = EXCLUDED.voting_authority_shared,
         voting_authority_none = EXCLUDED.voting_authority_none,
         is_amendment = EXCLUDED.is_amendment,
         amendment_number = EXCLUDED.amendment_number,
         raw_payload = EXCLUDED.raw_payload,
         updated_at = now()
"""


def _latest_amendments(cur, affected_pairs: list[tuple[str, date]] | None = None) -> None:
    if affected_pairs:
        execute_values(
            cur,
            """
            WITH affected(manager_cik, report_period) AS (VALUES %s),
            ranked AS (
                SELECT s.manager_cik, s.report_period, s.accession_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.manager_cik, s.report_period
                           ORDER BY s.filed_date DESC NULLS LAST, s.amendment_number DESC, s.accession_number DESC
                       ) AS rn
                FROM fact_13f_submission s
                JOIN affected a
                  ON a.manager_cik = s.manager_cik
                 AND a.report_period = s.report_period
            )
            UPDATE fact_13f_submission s
               SET is_latest_amendment = (r.rn = 1),
                   updated_at = now()
              FROM ranked r
             WHERE s.accession_number = r.accession_number
            """,
            affected_pairs,
        )
    else:
        cur.execute(
            """
            WITH ranked AS (
                SELECT manager_cik, report_period, accession_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY manager_cik, report_period
                           ORDER BY filed_date DESC NULLS LAST, amendment_number DESC, accession_number DESC
                       ) AS rn
                FROM fact_13f_submission
            )
            UPDATE fact_13f_submission s
               SET is_latest_amendment = (r.rn = 1),
                   updated_at = now()
              FROM ranked r
             WHERE s.accession_number = r.accession_number
            """
        )
    if affected_pairs:
        execute_values(
            cur,
            """
            WITH affected(manager_cik, report_period) AS (VALUES %s),
            affected_submissions AS (
                SELECT s.accession_number, s.is_latest_amendment
                FROM fact_13f_submission s
                JOIN affected a
                  ON a.manager_cik = s.manager_cik
                 AND a.report_period = s.report_period
            )
            UPDATE fact_13f_holdings h
               SET is_latest_amendment = s.is_latest_amendment,
                   updated_at = now()
              FROM affected_submissions s
             WHERE s.accession_number = h.accession_number
               AND h.is_latest_amendment IS DISTINCT FROM s.is_latest_amendment
            """,
            affected_pairs,
        )
        execute_values(
            cur,
            """
            WITH affected(manager_cik, report_period) AS (VALUES %s),
            affected_submissions AS (
                SELECT s.accession_number, s.is_latest_amendment
                FROM fact_13f_submission s
                JOIN affected a
                  ON a.manager_cik = s.manager_cik
                 AND a.report_period = s.report_period
            )
            UPDATE source_13f_filing_state st
               SET is_latest_amendment = s.is_latest_amendment,
                   updated_at = now()
              FROM affected_submissions s
             WHERE s.accession_number = st.accession_number
               AND st.is_latest_amendment IS DISTINCT FROM s.is_latest_amendment
            """,
            affected_pairs,
        )
        return
    cur.execute(
        """
        UPDATE fact_13f_holdings h
           SET is_latest_amendment = s.is_latest_amendment,
               updated_at = now()
          FROM fact_13f_submission s
         WHERE s.accession_number = h.accession_number
           AND h.is_latest_amendment IS DISTINCT FROM s.is_latest_amendment
        """
    )
    cur.execute(
        """
        UPDATE source_13f_filing_state st
           SET is_latest_amendment = s.is_latest_amendment,
               updated_at = now()
          FROM fact_13f_submission s
         WHERE s.accession_number = st.accession_number
           AND st.is_latest_amendment IS DISTINCT FROM s.is_latest_amendment
        """
    )


def parse_13f(
    quarter: str | None = None,
    manager: str | None = None,
    limit: int | None = None,
    row_limit: int | None = None,
    force: bool = False,
) -> dict[str, int]:
    params: list = []
    where = "WHERE downloaded AND local_path IS NOT NULL"
    if quarter:
        where += " AND (upper(dataset_key) = %s OR upper(period_label) = %s)"
        params.extend([quarter.upper(), quarter.upper()])
    if limit:
        where += " LIMIT %s"
        params.append(limit)
    ctx = start_run("US_13F", "parse_13f", "incremental")
    datasets = skipped = holdings_written = submissions_written = managers_written = 0
    complete_parse = manager is None and row_limit is None
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT dataset_key, local_path, parsed FROM source_13f_dataset_state {where}", params)
            states = cur.fetchall()
        for dataset_key, local_path, was_parsed in states:
            if was_parsed and not force:
                skipped += 1
                continue
            path = Path(local_path)
            if not path.exists():
                continue
            datasets += 1
            if path.is_dir():
                submissions = _read_tsv_file(path / "SUBMISSION.tsv")
                coverpages = _read_tsv_file(path / "COVERPAGE.tsv")
                summaries = _read_tsv_file(path / "SUMMARYPAGE.tsv")
                infotable_member = None
                infotable_file = path / "INFOTABLE.tsv"
            elif zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    submissions = _read_tsv(zf, _zip_member(zf, "SUBMISSION.tsv"))
                    coverpages = _read_tsv(zf, _zip_member(zf, "COVERPAGE.tsv"))
                    summaries = _read_tsv(zf, _zip_member(zf, "SUMMARYPAGE.tsv"))
                    infotable_member = _zip_member(zf, "INFOTABLE.tsv")
                infotable_file = None
            else:
                continue

            cover_by_acc = {}
            for cover in coverpages:
                acc = clean_accession(dashed_accession(_value(cover, "ACCESSION_NUMBER", "ACCESSIONNUMBER")))
                if acc:
                    cover_by_acc[acc] = cover

            sub_by_acc = {}
            manager_rows = []
            submission_rows = []
            state_rows = []
            for row in submissions:
                accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
                manager_cik = normalize_cik(_value(row, "CIK", "MANAGER_CIK", "FILINGMANAGER_CIK"))
                if manager and manager_cik != normalize_cik(manager):
                    continue
                manager_name = _value(row, "FILINGMANAGER_NAME", "MANAGER_NAME", "NAME")
                report_period = parse_date(_value(row, "PERIODOFREPORT", "REPORTCALENDARORQUARTER", "REPORT_PERIOD"))
                filed_date = parse_date(_value(row, "FILING_DATE", "FILEDASOFDATE", "FILED_DATE"))
                filing_type = _value(row, "SUBMISSIONTYPE", "FORMTYPE", "FORM_TYPE")
                is_amendment = bool(filing_type and "/A" in filing_type)
                amend = _int_value(_value(row, "AMENDMENTNO", "AMENDMENT_NUMBER")) or (1 if is_amendment else 0)
                if not accession or not manager_cik:
                    continue
                cover = cover_by_acc.get(clean_accession(accession), {})
                manager_name = manager_name or _value(cover, "FILINGMANAGER_NAME", "MANAGER_NAME", "NAME")
                sub_by_acc[clean_accession(accession)] = (manager_cik, manager_name, filing_type, filed_date, report_period, amend, is_amendment)
                manager_rows.append((
                    manager_cik,
                    manager_name or manager_cik,
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
                    1,
                    1,
                    report_period,
                    report_period,
                    date.today(),
                ))
                submission_rows.append((
                    accession, manager_cik, manager_name, filing_type, filed_date, report_period,
                    amend, is_amendment, dataset_key, json.dumps(row),
                ))
                if report_period:
                    state_rows.append((manager_cik, report_period, accession, filing_type, filed_date, dataset_key, complete_parse, 0, None, is_amendment, amend))

            cover_rows = []
            for row in coverpages:
                accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
                if accession:
                    cover_rows.append((accession, _value(row, "CRDNUMBER"), _value(row, "SECFILENUMBER"), _value(row, "REPORTTYPE"), _value(row, "FORM13FFILENUMBER"), json.dumps(row)))
            summary_rows = []
            for row in summaries:
                accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
                if accession:
                    summary_rows.append((
                        accession,
                        _int_value(_value(row, "OTHERINCLUDEDMANAGERSCOUNT")),
                        _int_value(_value(row, "TABLEENTRYTOTAL")),
                        _int_value(_value(row, "TABLEVALUETOTAL")),
                        json.dumps(row),
                    ))

            with connect() as conn, conn.cursor() as cur:
                manager_rows = _dedupe_rows(manager_rows, 0)
                cover_rows = _dedupe_rows(cover_rows, 0)
                summary_rows = _dedupe_rows(summary_rows, 0)
                managers_written += execute_values(cur, """
                    INSERT INTO dim_13f_manager
                        (manager_cik, manager_name, name_source,
                         crd_number, sec_file_number, form_13f_file_number, report_type,
                         street1, street2, city, state, zip_code,
                         filing_count_primary, filing_count_total,
                         first_quarter_filed, last_quarter_filed, last_seen_at)
                    VALUES %s
                    ON CONFLICT (manager_cik) DO UPDATE SET
                        manager_name = CASE
                            WHEN dim_13f_manager.manager_name = dim_13f_manager.manager_cik
                                 AND EXCLUDED.manager_name <> EXCLUDED.manager_cik
                            THEN EXCLUDED.manager_name
                            ELSE COALESCE(NULLIF(dim_13f_manager.manager_name, ''), EXCLUDED.manager_name)
                        END,
                        name_source = CASE
                            WHEN dim_13f_manager.name_source IN ('both', 'other_manager') THEN 'both'
                            ELSE 'submission'
                        END,
                        crd_number = COALESCE(dim_13f_manager.crd_number, EXCLUDED.crd_number),
                        sec_file_number = COALESCE(dim_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                        form_13f_file_number = COALESCE(dim_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                        report_type = COALESCE(dim_13f_manager.report_type, EXCLUDED.report_type),
                        street1 = COALESCE(dim_13f_manager.street1, EXCLUDED.street1),
                        street2 = COALESCE(dim_13f_manager.street2, EXCLUDED.street2),
                        city = COALESCE(dim_13f_manager.city, EXCLUDED.city),
                        state = COALESCE(dim_13f_manager.state, EXCLUDED.state),
                        zip_code = COALESCE(dim_13f_manager.zip_code, EXCLUDED.zip_code),
                        last_seen_at = now(),
                        first_quarter_filed = LEAST(
                            COALESCE(dim_13f_manager.first_quarter_filed, EXCLUDED.first_quarter_filed),
                            COALESCE(EXCLUDED.first_quarter_filed, dim_13f_manager.first_quarter_filed)
                        ),
                        last_quarter_filed = GREATEST(
                            COALESCE(dim_13f_manager.last_quarter_filed, EXCLUDED.last_quarter_filed),
                            COALESCE(EXCLUDED.last_quarter_filed, dim_13f_manager.last_quarter_filed)
                        ),
                        updated_at = now()
                """, manager_rows)
                submissions_written += execute_values(cur, """
                    INSERT INTO fact_13f_submission
                        (accession_number, manager_cik, manager_name, filing_type, filed_date, report_period,
                         amendment_number, is_amendment, dataset_key, raw_payload)
                    VALUES %s
                    ON CONFLICT (accession_number) DO UPDATE SET
                        manager_cik = EXCLUDED.manager_cik,
                        manager_name = EXCLUDED.manager_name,
                        filing_type = EXCLUDED.filing_type,
                        filed_date = EXCLUDED.filed_date,
                        report_period = EXCLUDED.report_period,
                        amendment_number = EXCLUDED.amendment_number,
                        is_amendment = EXCLUDED.is_amendment,
                        dataset_key = EXCLUDED.dataset_key,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = now()
                """, submission_rows)
                execute_values(cur, """
                    INSERT INTO source_13f_filing_state
                        (manager_cik, report_period, accession_number, filing_type, filed_date, dataset_key,
                         parsed, rows_parsed, parse_error, is_amendment, amendment_number)
                    VALUES %s
                    ON CONFLICT (manager_cik, report_period, accession_number) DO UPDATE SET
                        filing_type = EXCLUDED.filing_type,
                        filed_date = EXCLUDED.filed_date,
                        dataset_key = EXCLUDED.dataset_key,
                        parsed = EXCLUDED.parsed,
                        rows_parsed = EXCLUDED.rows_parsed,
                        parse_error = EXCLUDED.parse_error,
                        is_amendment = EXCLUDED.is_amendment,
                        amendment_number = EXCLUDED.amendment_number,
                        updated_at = now()
                """, state_rows)
                execute_values(cur, """
                    INSERT INTO fact_13f_coverpage
                        (accession_number, crd_number, sec_file_number, report_type, form_13f_file_number, raw_payload)
                    VALUES %s
                    ON CONFLICT (accession_number) DO UPDATE SET
                        crd_number = EXCLUDED.crd_number,
                        sec_file_number = EXCLUDED.sec_file_number,
                        report_type = EXCLUDED.report_type,
                        form_13f_file_number = EXCLUDED.form_13f_file_number,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = now()
                """, cover_rows)
                execute_values(cur, """
                    INSERT INTO fact_13f_summarypage
                        (accession_number, other_included_managers_count, table_entry_total, table_value_total, raw_payload)
                    VALUES %s
                    ON CONFLICT (accession_number) DO UPDATE SET
                        other_included_managers_count = EXCLUDED.other_included_managers_count,
                        table_entry_total = EXCLUDED.table_entry_total,
                        table_value_total = EXCLUDED.table_value_total,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = now()
                """, summary_rows)
                dataset_holdings = 0
                zf_ctx = None
                try:
                    if infotable_file is not None:
                        row_iter = _iter_tsv_file(infotable_file)
                    else:
                        zf_ctx = zipfile.ZipFile(path)
                        row_iter = _iter_tsv(zf_ctx, infotable_member)
                    chunk = []
                    for i, row in enumerate(row_iter, start=1):
                        if row_limit and dataset_holdings >= row_limit:
                            break
                        accession = dashed_accession(_value(row, "ACCESSION_NUMBER", "ACCESSIONNUMBER"))
                        sub = sub_by_acc.get(clean_accession(accession))
                        if not sub:
                            continue
                        manager_cik, _manager_name, filing_type, filed_date, report_period, amend, is_amendment = sub
                        if manager and manager_cik != normalize_cik(manager):
                            continue
                        cusip = (_value(row, "CUSIP") or "").strip().upper() or None
                        chunk.append((
                            accession,
                            _value(row, "INFOTABLE_SK", "INFOTABLESK") or str(i),
                            manager_cik,
                            report_period,
                            filing_type,
                            filed_date,
                            _value(row, "NAMEOFISSUER", "ISSUER_NAME"),
                            _value(row, "TITLEOFCLASS", "TITLE_OF_CLASS"),
                            cusip,
                            _value(row, "FIGI"),
                            cusip[:6] if cusip else None,
                            None,
                            None,
                            _int_value(_value(row, "VALUE", "VALUE_X1000")),
                            _num_value(_value(row, "SSHPRNAMT", "SHARES_OR_PRINCIPAL")),
                            _value(row, "SSHPRNAMTTYPE", "SH_PRN_FLAG"),
                            _value(row, "PUTCALL", "PUT_CALL"),
                            _value(row, "INVESTMENTDISCRETION", "INVESTMENT_DISCRETION"),
                            _value(row, "OTHERMANAGER", "OTHER_MANAGER"),
                            _int_value(_value(row, "VOTINGAUTHORITYSOLE", "SOLE")),
                            _int_value(_value(row, "VOTINGAUTHORITYSHARED", "SHARED")),
                            _int_value(_value(row, "VOTINGAUTHORITYNONE", "NONE")),
                            is_amendment,
                            amend,
                            dataset_key,
                            json.dumps(row),
                        ))
                        if len(chunk) >= 10000:
                            written = execute_values(cur, _INSERT_HOLDINGS_SQL, chunk, page_size=10000)
                            holdings_written += written
                            dataset_holdings += written
                            chunk.clear()
                    if chunk:
                        written = execute_values(cur, _INSERT_HOLDINGS_SQL, chunk, page_size=10000)
                        holdings_written += written
                        dataset_holdings += written
                finally:
                    if zf_ctx is not None:
                        zf_ctx.close()
                if complete_parse:
                    cur.execute(
                        "UPDATE source_13f_dataset_state SET parsed = true, parsed_at = now(), rows_parsed = %s, parse_error = NULL, updated_at = now() WHERE dataset_key = %s",
                        (dataset_holdings, dataset_key),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE source_13f_dataset_state
                           SET parsed = false,
                               rows_parsed = %s,
                               parse_error = 'partial manager or row_limit parse; dataset not marked parsed',
                               updated_at = now()
                         WHERE dataset_key = %s
                        """,
                        (dataset_holdings, dataset_key),
                    )
                affected_pairs = sorted({(row[0], row[1]) for row in state_rows if row[0] and row[1]})
                _latest_amendments(cur, affected_pairs)
        finish_run(ctx, "succeeded", rows_in=datasets, rows_out=holdings_written)
        return {
            "datasets": datasets,
            "skipped": skipped,
            "managers": managers_written,
            "submissions": submissions_written,
            "holdings": holdings_written,
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _norm_cusip_part(value: str | None, length: int) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return cleaned if len(cleaned) == length else None


def _load_local_cik_cusip_rows() -> list[tuple[str, str | None, str | None]]:
    path = _repo_root() / "spec" / "cik-cusip-maps.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                cik = str(int(float(str(row.get("cik") or "").strip()))).zfill(10)
            except Exception:
                continue
            cusip6 = _norm_cusip_part(row.get("cusip6"), 6)
            cusip8 = _norm_cusip_part(row.get("cusip8"), 8)
            if cusip6 or cusip8:
                rows.append((cik, cusip6, cusip8))
    return rows


def _security_name_key(value: str | None) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", (value or "").upper())
    suffixes = {
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
        "PLC", "SA", "NV", "AG", "THE", "NEW", "DEL", "COM", "CLASS", "CL",
    }
    words = [word for word in text.split() if word and word not in suffixes]
    return " ".join(words)


def _extract_json_object(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw or "", flags=re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except Exception:
            return {}


def _deepseek_security_name_evidence(cur, limit: int | None = None) -> int:
    """Use DeepSeek to resolve high-value ambiguous CUSIP/name candidates."""

    from xbrl_sec.sec.sources.llm_client import get_llm_client

    cur.execute("""
        SELECT cik, primary_ticker, name
        FROM dim_company_us
        WHERE name IS NOT NULL AND cik IS NOT NULL
    """)
    companies = [
        {
            "cik": cik,
            "ticker": ticker,
            "name": name,
            "key": _security_name_key(name),
        }
        for cik, ticker, name in cur.fetchall()
    ]
    cur.execute(
        """
        WITH high_priority AS (
            SELECT DISTINCT cusip
            FROM fact_security_identifier_evidence_us
            WHERE source_priority <= 2
              AND candidate_cik IS NOT NULL
        )
        SELECT o.cusip, o.cusip8, o.cusip6, o.observed_issuer_name,
               o.observed_security_title, o.row_count, o.value_observed,
               o.first_seen_at, o.last_seen_at
        FROM tmp_observed_security_us o
        LEFT JOIN high_priority hp ON hp.cusip = o.cusip
        WHERE hp.cusip IS NULL
          AND o.observed_issuer_name IS NOT NULL
        ORDER BY COALESCE(o.value_observed, 0) DESC, o.row_count DESC
        LIMIT %s
        """,
        (limit or int(os.environ.get("DEEPSEEK_CUSIP_MATCH_LIMIT", "100")),),
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    client = get_llm_client()
    model = os.environ.get("DEEPSEEK_CUSIP_MATCH_MODEL") or os.environ.get("LLM_MODEL") or "deepseek-chat"
    evidence_rows = []
    for cusip, cusip8, cusip6, issuer_name, title, row_count, value_observed, first_seen_at, last_seen_at in rows:
        issuer_key = _security_name_key(issuer_name)
        if not issuer_key:
            continue
        candidates = []
        for company in companies:
            score = SequenceMatcher(None, issuer_key, company["key"]).ratio()
            if issuer_key in company["key"] or company["key"] in issuer_key:
                score = max(score, 0.86)
            if score >= 0.55:
                candidates.append({
                    "cik": company["cik"],
                    "ticker": company["ticker"],
                    "name": company["name"],
                    "score": round(score, 3),
                })
        candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)[:12]
        if not candidates:
            continue
        messages = [
            {
                "role": "system",
                "content": (
                    "You match SEC 13F issuer names to US public-company CIK candidates. "
                    "Choose only from the provided candidates. Return JSON only. "
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
                        },
                        "candidates": candidates,
                        "return_shape": {
                            "candidate_cik": "10 digit CIK or null",
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
            decision = {"candidate_cik": None, "confidence": 0, "rationale": f"DeepSeek failed: {exc}"}
            raw = json.dumps(decision)
        candidate_cik = normalize_cik(decision.get("candidate_cik"))
        try:
            confidence = float(decision.get("confidence") or 0)
        except Exception:
            confidence = 0.0
        candidate = next((item for item in candidates if normalize_cik(item["cik"]) == candidate_cik), None)
        if not candidate or confidence < 0.78:
            continue
        evidence_rows.append((
            cusip,
            cusip8,
            cusip6,
            candidate_cik,
            candidate.get("ticker"),
            candidate.get("name"),
            issuer_name,
            title,
            "deepseek.name_match",
            f"{cusip}:{candidate_cik}",
            3,
            max(0, min(100, confidence * 100)),
            row_count,
            value_observed,
            first_seen_at,
            last_seen_at,
            json.dumps({
                "model": model,
                "rationale": decision.get("rationale"),
                "raw_response": raw,
                "candidates": candidates,
            }, ensure_ascii=False, default=str),
        ))
    if not evidence_rows:
        return 0
    return execute_values(cur, """
        INSERT INTO fact_security_identifier_evidence_us
            (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
             observed_issuer_name, observed_security_title, source_name, source_key,
             source_priority, confidence_score, row_count, value_observed,
             first_seen_at, last_seen_at, evidence_payload)
        VALUES %s
    """, evidence_rows)


def rebuild_security_identifier_us(use_llm: bool = False, llm_limit: int | None = None) -> dict[str, int]:
    """Rebuild governed US CUSIP evidence and resolved CUSIP-to-CIK map."""

    csv_rows = _load_local_cik_cusip_rows()
    ctx = start_run("US_13F", "rebuild_security_identifier_us", "full_refresh")
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE dim_security_identifier_us, fact_security_identifier_evidence_us RESTART IDENTITY")
            cur.execute("""
                CREATE TEMP TABLE tmp_observed_security_us ON COMMIT DROP AS
                WITH raw AS (
                    SELECT upper(cusip) AS cusip,
                           issuer_name AS observed_issuer_name,
                           title_of_class AS observed_security_title,
                           COUNT(*)::bigint AS row_count,
                           SUM(value_x1000)::numeric AS value_observed,
                           MIN(report_period)::timestamp AT TIME ZONE 'UTC' AS first_seen_at,
                           MAX(report_period)::timestamp AT TIME ZONE 'UTC' AS last_seen_at
                    FROM fact_13f_holdings
                    WHERE cusip IS NOT NULL
                      AND upper(cusip) ~ '^[A-Z0-9]{9}$'
                    GROUP BY upper(cusip), issuer_name, title_of_class
                    UNION ALL
                    SELECT upper(cusip) AS cusip,
                           issuer_name AS observed_issuer_name,
                           title_of_class AS observed_security_title,
                           COUNT(*)::bigint AS row_count,
                           NULL::numeric AS value_observed,
                           MIN(filed_date)::timestamp AT TIME ZONE 'UTC' AS first_seen_at,
                           MAX(filed_date)::timestamp AT TIME ZONE 'UTC' AS last_seen_at
                    FROM fact_13dg_ownership
                    WHERE cusip IS NOT NULL
                      AND upper(cusip) ~ '^[A-Z0-9]{9}$'
                    GROUP BY upper(cusip), issuer_name, title_of_class
                ),
                ranked AS (
                    SELECT *,
                           row_number() OVER (
                               PARTITION BY cusip
                               ORDER BY COALESCE(value_observed, 0) DESC, row_count DESC
                           ) AS rn
                    FROM raw
                )
                SELECT cusip,
                       substring(cusip from 1 for 8) AS cusip8,
                       substring(cusip from 1 for 6) AS cusip6,
                       MAX(observed_issuer_name) FILTER (WHERE rn = 1) AS observed_issuer_name,
                       MAX(observed_security_title) FILTER (WHERE rn = 1) AS observed_security_title,
                       SUM(row_count)::bigint AS row_count,
                       SUM(value_observed)::numeric AS value_observed,
                       MIN(first_seen_at) AS first_seen_at,
                       MAX(last_seen_at) AS last_seen_at
                FROM ranked
                GROUP BY cusip
            """)
            cur.execute("CREATE INDEX ON tmp_observed_security_us (cusip)")
            cur.execute("CREATE INDEX ON tmp_observed_security_us (cusip8)")
            cur.execute("CREATE INDEX ON tmp_observed_security_us (cusip6)")

            cur.execute("""
                INSERT INTO fact_security_identifier_evidence_us
                    (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
                     observed_issuer_name, observed_security_title, source_name, source_key,
                     source_priority, confidence_score, row_count, value_observed,
                     first_seen_at, last_seen_at, evidence_payload)
                SELECT upper(substring(d.isin from 3 for 9)) AS cusip,
                       upper(substring(d.isin from 3 for 8)) AS cusip8,
                       upper(substring(d.isin from 3 for 6)) AS cusip6,
                       d.cik,
                       d.primary_ticker,
                       d.name,
                       o.observed_issuer_name,
                       o.observed_security_title,
                       'dim_company_us.isin',
                       d.cik,
                       1,
                       100,
                       COALESCE(o.row_count, 0),
                       o.value_observed,
                       o.first_seen_at,
                       o.last_seen_at,
                       jsonb_build_object('isin', d.isin)
                FROM dim_company_us d
                LEFT JOIN tmp_observed_security_us o
                  ON o.cusip = upper(substring(d.isin from 3 for 9))
                WHERE upper(COALESCE(d.isin, '')) ~ '^US[A-Z0-9]{10}$'
            """)
            isin_evidence = cur.rowcount

            cur.execute("""
                CREATE TEMP TABLE tmp_cik_cusip_csv (
                    cik TEXT NOT NULL,
                    cusip6 TEXT,
                    cusip8 TEXT
                ) ON COMMIT DROP
            """)
            csv_evidence = 0
            if csv_rows:
                execute_values(cur, "INSERT INTO tmp_cik_cusip_csv (cik, cusip6, cusip8) VALUES %s", csv_rows, page_size=10000)
                cur.execute("CREATE INDEX ON tmp_cik_cusip_csv (cusip8)")
                cur.execute("CREATE INDEX ON tmp_cik_cusip_csv (cusip6)")
                cur.execute("CREATE INDEX ON tmp_cik_cusip_csv (cik)")
                cur.execute("""
                    WITH csv_counts AS (
                        SELECT cusip8, COUNT(DISTINCT cik) AS cik_count
                        FROM tmp_cik_cusip_csv
                        WHERE cusip8 IS NOT NULL
                        GROUP BY cusip8
                    )
                    INSERT INTO fact_security_identifier_evidence_us
                        (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
                         observed_issuer_name, observed_security_title, source_name, source_key,
                         source_priority, confidence_score, row_count, value_observed,
                         first_seen_at, last_seen_at, evidence_payload)
                    SELECT o.cusip,
                           o.cusip8,
                           o.cusip6,
                           d.cik,
                           d.primary_ticker,
                           d.name,
                           o.observed_issuer_name,
                           o.observed_security_title,
                           'spec.cik-cusip-maps.csv',
                           c.cusip8 || ':' || c.cik,
                           2,
                           CASE WHEN cc.cik_count = 1 THEN 85 ELSE 45 END,
                           o.row_count,
                           o.value_observed,
                           o.first_seen_at,
                           o.last_seen_at,
                           jsonb_build_object('csv_cusip6', c.cusip6, 'csv_cusip8', c.cusip8, 'csv_cik_count_for_cusip8', cc.cik_count)
                    FROM tmp_observed_security_us o
                    JOIN tmp_cik_cusip_csv c ON c.cusip8 = o.cusip8
                    JOIN csv_counts cc ON cc.cusip8 = c.cusip8
                    JOIN dim_company_us d ON d.cik = c.cik
                """)
                csv_evidence = cur.rowcount

            cur.execute("""
                INSERT INTO fact_security_identifier_evidence_us
                    (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
                     observed_issuer_name, observed_security_title, source_name, source_key,
                     source_priority, confidence_score, row_count, value_observed,
                     first_seen_at, last_seen_at, evidence_payload)
                SELECT upper(h.cusip),
                       substring(upper(h.cusip) from 1 for 8),
                       substring(upper(h.cusip) from 1 for 6),
                       d.cik,
                       d.primary_ticker,
                       d.name,
                       MAX(h.issuer_name),
                       MAX(h.title_of_class),
                       'fact_13f_holdings.existing_resolution',
                       upper(h.cusip) || ':' || d.cik,
                       4,
                       70,
                       COUNT(*)::bigint,
                       SUM(h.value_x1000)::numeric,
                       MIN(h.report_period)::timestamp AT TIME ZONE 'UTC',
                       MAX(h.report_period)::timestamp AT TIME ZONE 'UTC',
                       jsonb_build_object('source', 'existing fact_13f_holdings issuer_cik')
                FROM fact_13f_holdings h
                JOIN dim_company_us d ON d.cik = h.issuer_cik
                WHERE h.cusip IS NOT NULL
                  AND upper(h.cusip) ~ '^[A-Z0-9]{9}$'
                  AND h.issuer_cik IS NOT NULL
                GROUP BY upper(h.cusip), d.cik, d.primary_ticker, d.name
            """)
            existing_13f_evidence = cur.rowcount

            cur.execute("""
                INSERT INTO fact_security_identifier_evidence_us
                    (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
                     observed_issuer_name, observed_security_title, source_name, source_key,
                     source_priority, confidence_score, row_count, value_observed,
                     first_seen_at, last_seen_at, evidence_payload)
                SELECT upper(o.cusip),
                       substring(upper(o.cusip) from 1 for 8),
                       substring(upper(o.cusip) from 1 for 6),
                       d.cik,
                       d.primary_ticker,
                       d.name,
                       MAX(o.issuer_name),
                       MAX(o.title_of_class),
                       'fact_13dg_ownership.existing_resolution',
                       upper(o.cusip) || ':' || d.cik,
                       5,
                       65,
                       COUNT(*)::bigint,
                       NULL::numeric,
                       MIN(o.filed_date)::timestamp AT TIME ZONE 'UTC',
                       MAX(o.filed_date)::timestamp AT TIME ZONE 'UTC',
                       jsonb_build_object('source', 'existing fact_13dg_ownership issuer_cik')
                FROM fact_13dg_ownership o
                JOIN dim_company_us d ON d.cik = o.issuer_cik
                WHERE o.cusip IS NOT NULL
                  AND upper(o.cusip) ~ '^[A-Z0-9]{9}$'
                  AND o.issuer_cik IS NOT NULL
                GROUP BY upper(o.cusip), d.cik, d.primary_ticker, d.name
            """)
            existing_13dg_evidence = cur.rowcount

            cur.execute("""
                INSERT INTO fact_security_identifier_evidence_us
                    (cusip, cusip8, cusip6, candidate_cik, candidate_ticker, candidate_name,
                     observed_issuer_name, observed_security_title, source_name, source_key,
                     source_priority, confidence_score, row_count, value_observed,
                     first_seen_at, last_seen_at, evidence_payload)
                SELECT upper(h.cusip),
                       substring(upper(h.cusip) from 1 for 8),
                       substring(upper(h.cusip) from 1 for 6),
                       d.cik,
                       d.primary_ticker,
                       d.name,
                       MAX(h.issuer_name),
                       MAX(h.title_of_class),
                       'fact_13f_holdings.normalized_name_match',
                       upper(h.cusip) || ':' || d.cik,
                       6,
                       55,
                       COUNT(*)::bigint,
                       SUM(h.value_x1000)::numeric,
                       MIN(h.report_period)::timestamp AT TIME ZONE 'UTC',
                       MAX(h.report_period)::timestamp AT TIME ZONE 'UTC',
                       jsonb_build_object('match', 'normalized issuer_name equals dim_company_us.name')
                FROM fact_13f_holdings h
                JOIN dim_company_us d
                  ON regexp_replace(upper(COALESCE(d.name, '')), '[^A-Z0-9]', '', 'g')
                   = regexp_replace(upper(COALESCE(h.issuer_name, '')), '[^A-Z0-9]', '', 'g')
                WHERE h.cusip IS NOT NULL
                  AND upper(h.cusip) ~ '^[A-Z0-9]{9}$'
                  AND h.issuer_cik IS NULL
                GROUP BY upper(h.cusip), d.cik, d.primary_ticker, d.name
            """)
            name_evidence = cur.rowcount
            llm_evidence = _deepseek_security_name_evidence(cur, llm_limit) if use_llm else 0

            cur.execute("""
                WITH evidence AS (
                    SELECT e.*,
                           MIN(source_priority) OVER (PARTITION BY cusip) AS best_priority
                    FROM fact_security_identifier_evidence_us e
                    WHERE e.cusip IS NOT NULL
                      AND e.cusip ~ '^[A-Z0-9]{9}$'
                      AND e.candidate_cik IS NOT NULL
                ),
                best_candidates AS (
                    SELECT cusip, candidate_cik,
                           MAX(candidate_ticker) AS candidate_ticker,
                           MAX(candidate_name) AS candidate_name,
                           MIN(source_priority) AS source_priority,
                           MAX(confidence_score) AS confidence_score,
                           SUM(row_count) AS row_count
                    FROM evidence
                    WHERE source_priority = best_priority
                    GROUP BY cusip, candidate_cik
                ),
                candidate_summary AS (
                    SELECT cusip,
                           COUNT(DISTINCT candidate_cik) AS candidate_count
                    FROM best_candidates
                    GROUP BY cusip
                ),
                best AS (
                    SELECT DISTINCT ON (cusip)
                           cusip, candidate_cik, candidate_ticker, candidate_name,
                           source_priority, confidence_score
                    FROM best_candidates
                    ORDER BY cusip, source_priority, confidence_score DESC, row_count DESC, candidate_cik
                ),
                evidence_payload AS (
                    SELECT cusip,
                           jsonb_agg(
                               jsonb_build_object(
                                   'source_name', source_name,
                                   'source_priority', source_priority,
                                   'candidate_cik', candidate_cik,
                                   'candidate_ticker', candidate_ticker,
                                   'confidence_score', confidence_score,
                                   'row_count', row_count
                               )
                               ORDER BY source_priority, confidence_score DESC
                           ) AS evidence
                    FROM (
                        SELECT DISTINCT ON (cusip, source_name, candidate_cik)
                               cusip, source_name, source_priority, candidate_cik,
                               candidate_ticker, confidence_score, row_count
                        FROM fact_security_identifier_evidence_us
                        WHERE cusip IS NOT NULL AND cusip ~ '^[A-Z0-9]{9}$'
                        ORDER BY cusip, source_name, candidate_cik, source_priority, confidence_score DESC
                    ) x
                    GROUP BY cusip
                ),
                classified AS (
                    SELECT o.*,
                           CASE
                               WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                    '(ETF|EXCHANGE TRADED|SPDR|ISHARES|INDEX FUND|ETF TR|TR UNIT|UNIT SER| FUND | PORTFOLIO |S&P 500 ETF|NASDAQ 100)'
                               THEN 'etf_or_fund'
                               WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                    '(NOTE| NOTES| NT |BOND|DEBENTURE|DUE [0-9]|[0-9]+\\.[0-9]+%)'
                               THEN 'debt'
                               WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                    '(PREFERRED| PFD | PREF |PRF)'
                               THEN 'preferred'
                               WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                    '( ADR| ADS|AMERICAN DEPOSITARY)'
                               THEN 'adr'
                               WHEN upper(COALESCE(o.observed_security_title, '') || ' ' || COALESCE(o.observed_issuer_name, '')) ~
                                    '( COM|COMMON|CL A|CL B|CLASS A|CLASS B|ORD| SHS|STOCK)'
                               THEN 'common_equity'
                               ELSE 'unknown'
                           END AS security_type
                    FROM tmp_observed_security_us o
                ),
                resolved AS (
                    SELECT c.*,
                           b.candidate_cik,
                           b.candidate_ticker,
                           b.candidate_name,
                           b.source_priority,
                           b.confidence_score,
                           COALESCE(cs.candidate_count, 0) AS candidate_count,
                           ep.evidence
                    FROM classified c
                    LEFT JOIN best b ON b.cusip = c.cusip
                    LEFT JOIN candidate_summary cs ON cs.cusip = c.cusip
                    LEFT JOIN evidence_payload ep ON ep.cusip = c.cusip
                )
                INSERT INTO dim_security_identifier_us
                    (cusip, cusip8, cusip6, issuer_cik, issuer_ticker, issuer_name,
                     security_title, security_type, resolution_status, confidence_score,
                     source_priority, first_seen_at, last_seen_at, evidence_payload)
                SELECT cusip,
                       cusip8,
                       cusip6,
                       CASE WHEN candidate_count = 1 THEN candidate_cik ELSE NULL END,
                       CASE WHEN candidate_count = 1 THEN candidate_ticker ELSE NULL END,
                       COALESCE(CASE WHEN candidate_count = 1 THEN candidate_name ELSE NULL END, observed_issuer_name),
                       observed_security_title,
                       security_type,
                       CASE
                           WHEN candidate_count > 1 THEN 'ambiguous'
                           WHEN candidate_count = 1 THEN 'resolved'
                           WHEN security_type IN ('etf_or_fund', 'debt', 'option_or_derivative') THEN 'non_company_security'
                           ELSE 'unresolved'
                       END,
                       COALESCE(confidence_score, 0),
                       source_priority,
                       first_seen_at,
                       last_seen_at,
                       jsonb_build_object(
                           'candidate_count', candidate_count,
                           'observed_row_count', row_count,
                           'observed_value', value_observed,
                           'evidence', COALESCE(evidence, '[]'::jsonb)
                       )
                FROM resolved
            """)
            dim_rows = cur.rowcount

            cur.execute("SELECT COUNT(*) FROM fact_security_identifier_evidence_us")
            evidence_rows = cur.fetchone()[0]
            cur.execute("SELECT resolution_status, COUNT(*) FROM dim_security_identifier_us GROUP BY 1")
            status_counts = {f"map_{status}": count for status, count in cur.fetchall()}

        finish_run(ctx, "succeeded", rows_in=len(csv_rows), rows_out=dim_rows)
        return {
            "csv_rows": len(csv_rows),
            "evidence_rows": evidence_rows,
            "evidence_isin": isin_evidence,
            "evidence_csv": csv_evidence,
            "evidence_13f_existing": existing_13f_evidence,
            "evidence_13dg_existing": existing_13dg_evidence,
            "evidence_name_match": name_evidence,
            "evidence_deepseek": llm_evidence,
            "map_rows": dim_rows,
            **status_counts,
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def resolve_13f_issuers(limit: int | None = None, use_llm: bool = False) -> dict[str, int]:
    result = {f"security_{key}": value for key, value in rebuild_security_identifier_us(use_llm=use_llm, llm_limit=limit).items()}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fact_13f_holdings h
               SET issuer_cik = m.issuer_cik,
                   issuer_ticker = m.issuer_ticker,
                   updated_at = now()
              FROM dim_security_identifier_us m
             WHERE upper(h.cusip) = m.cusip
               AND m.resolution_status = 'resolved'
               AND (h.issuer_cik IS DISTINCT FROM m.issuer_cik
                    OR h.issuer_ticker IS DISTINCT FROM m.issuer_ticker)
            """
        )
        result["holdings_updated"] = max(cur.rowcount, 0)
        cur.execute(
            """
            UPDATE fact_13dg_ownership o
               SET issuer_cik = m.issuer_cik,
                   issuer_ticker = m.issuer_ticker,
                   updated_at = now()
              FROM dim_security_identifier_us m
             WHERE upper(o.cusip) = m.cusip
               AND m.resolution_status = 'resolved'
               AND (o.issuer_cik IS DISTINCT FROM m.issuer_cik
                    OR o.issuer_ticker IS DISTINCT FROM m.issuer_ticker)
            """
        )
        result["ownership_13dg_updated"] = max(cur.rowcount, 0)
        cur.execute("""
            WITH best AS (
                SELECT accession_number, MAX(issuer_cik) AS issuer_cik
                FROM fact_13dg_ownership
                WHERE issuer_cik IS NOT NULL
                GROUP BY accession_number
            )
            UPDATE source_13dg_filing_state s
               SET issuer_cik = COALESCE(s.issuer_cik, best.issuer_cik),
                   updated_at = now()
              FROM best
             WHERE s.accession_number = best.accession_number
               AND s.issuer_cik IS NULL
        """)
        result["source_13dg_updated"] = max(cur.rowcount, 0)
        return result


def _legacy_resolve_13f_managers_unused(limit: int | None = None) -> dict[str, int]:
    """Resolve manager names from all available sources.

    Kept only as a compatibility alias while older import paths settle.
    The canonical implementation below writes exclusively to dim_13f_manager.
    """
    return resolve_13f_managers(limit=limit)

    """
    Fixes three data quality issues:
      1. Backfills NULL manager_name in fact_13f_submission from COVERPAGE.
         The SEC's SUBMISSION.tsv stopped including FILINGMANAGER_NAME in
         recent quarters, but COVERPAGE.tsv always has it.
      2. Resolves CIK=name placeholders in the legacy manager table using names from
         fact_13f_submission (which now includes COVERPAGE-derived names).
      3. Updates legacy filing counts to reflect actual counts.
      4. Extracts secondary managers from OTHERMANAGER*.tsv and populates
         dim_13f_manager.
    """
    from xbrl_sec.sec.db.bulk import execute_values

    result: dict[str, int] = {}

    # ── Phase 1: Backfill fact_13f_submission from COVERPAGE ──────────────
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE fact_13f_submission f
               SET manager_name = c.raw_payload->>'FILINGMANAGER_NAME',
                   updated_at = now()
              FROM fact_13f_coverpage c
             WHERE f.accession_number = c.accession_number
               AND f.manager_name IS NULL
               AND c.raw_payload->>'FILINGMANAGER_NAME' IS NOT NULL
               AND c.raw_payload->>'FILINGMANAGER_NAME' != ''
        """)
        result["submission_backfilled_from_coverpage"] = cur.rowcount

    # ── Phase 2: Resolve CIK=name placeholders in legacy manager rows ─────
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH best_name AS (
                SELECT DISTINCT ON (f.manager_cik)
                       f.manager_cik,
                       f.manager_name
                  FROM fact_13f_submission f
                 WHERE f.manager_name IS NOT NULL
                   AND f.manager_name != f.manager_cik
                 ORDER BY f.manager_cik, f.filed_date DESC
            )
            UPDATE dim_13f_manager r
               SET manager_name = b.manager_name,
                   updated_at = now()
              FROM best_name b
             WHERE r.manager_cik = b.manager_cik
               AND r.manager_name = r.manager_cik
               AND b.manager_name != r.manager_cik
        """)
        result["ref_manager_names_resolved"] = cur.rowcount

    # ── Phase 3: Update filing_count_13f ─────────────────────────────────
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE dim_13f_manager r
               SET filing_count_13f = s.cnt,
                   first_quarter_filed = s.first_q,
                   last_quarter_filed = s.last_q,
                   updated_at = now()
              FROM (
                SELECT manager_cik,
                       COUNT(DISTINCT accession_number) AS cnt,
                       MIN(report_period) AS first_q,
                       MAX(report_period) AS last_q
                  FROM fact_13f_submission
                 GROUP BY manager_cik
              ) s
             WHERE r.manager_cik = s.manager_cik
        """)
        result["ref_manager_counts_updated"] = cur.rowcount

    # ── Phase 4: Populate dim_13f_manager from primary managers ──────────
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO dim_13f_manager (
                manager_cik, manager_name, name_source,
                crd_number, sec_file_number, form_13f_file_number, report_type,
                street1, street2, city, state, zip_code,
                filing_count_primary, filing_count_other, filing_count_total,
                first_quarter_filed, last_quarter_filed
            )
            SELECT
                r.manager_cik,
                r.manager_name,
                'submission',
                c.raw_payload->>'CRDNUMBER',
                c.raw_payload->>'SECFILENUMBER',
                c.raw_payload->>'FORM13FFILENUMBER',
                c.raw_payload->>'REPORTTYPE',
                c.raw_payload->>'FILINGMANAGER_STREET1',
                c.raw_payload->>'FILINGMANAGER_STREET2',
                c.raw_payload->>'FILINGMANAGER_CITY',
                c.raw_payload->>'FILINGMANAGER_STATEORCOUNTRY',
                c.raw_payload->>'FILINGMANAGER_ZIPCODE',
                r.filing_count_13f,
                0,
                r.filing_count_13f,
                r.first_quarter_filed,
                r.last_quarter_filed
            FROM dim_13f_manager r
            JOIN fact_13f_submission f ON r.manager_cik = f.manager_cik
            JOIN fact_13f_coverpage c ON f.accession_number = c.accession_number
            WHERE r.manager_name != r.manager_cik
            ORDER BY r.manager_cik, f.filed_date DESC
            ON CONFLICT (manager_cik) DO NOTHING
        """)
        result["dim_primary_populated"] = cur.rowcount

    # ── Phase 5: Merge secondary managers from OTHERMANAGER*.tsv ──────────
    limit_sql = "LIMIT %s" if limit else ""
    params = [limit] if limit else []

    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT dataset_key, local_path FROM source_13f_dataset_state WHERE downloaded AND local_path IS NOT NULL {limit_sql}", params)
        states = cur.fetchall()

    om_manager_rows: dict[str, dict] = {}
    total_om_rows = 0

    for dataset_key, local_path in states:
        path = Path(local_path)
        if not path.exists():
            continue

        for tsv_name in ("OTHERMANAGER.tsv", "OTHERMANAGER2.tsv"):
            try:
                if path.is_dir():
                    rows = _read_tsv_file(path / tsv_name)
                elif zipfile.is_zipfile(path):
                    with zipfile.ZipFile(path) as zf:
                        rows = _read_tsv(zf, _zip_member(zf, tsv_name))
                else:
                    continue
            except Exception:
                continue

            for row in rows:
                cik = normalize_cik(_value(row, "CIK"))
                name = _value(row, "NAME")
                crd = _value(row, "CRDNUMBER")
                sec_file = _value(row, "SECFILENUMBER")
                form_13f = _value(row, "FORM13FFILENUMBER")
                if not cik or not name:
                    continue
                total_om_rows += 1

                if cik not in om_manager_rows:
                    om_manager_rows[cik] = {"names": {}, "crd": crd, "sec_file": sec_file, "form_13f": form_13f, "count": 0}
                entry = om_manager_rows[cik]
                entry["names"][name] = entry["names"].get(name, 0) + 1
                entry["count"] += 1
                if not entry["crd"] and crd:
                    entry["crd"] = crd
                if not entry["sec_file"] and sec_file:
                    entry["sec_file"] = sec_file
                if not entry["form_13f"] and form_13f:
                    entry["form_13f"] = form_13f

    om_best_rows = []
    for cik, entry in om_manager_rows.items():
        best_name = max(entry["names"], key=lambda n: (entry["names"][n], len(n)))
        om_best_rows.append((
            cik, best_name, "other_manager",
            entry["crd"], entry["sec_file"], entry["form_13f"], None,
            None, None, None, None, None,
            0, entry["count"], entry["count"],
        ))

    dim_om_written = 0
    if om_best_rows:
        with connect() as conn, conn.cursor() as cur:
            dim_om_written = execute_values(cur, """
                INSERT INTO dim_13f_manager
                    (manager_cik, manager_name, name_source,
                     crd_number, sec_file_number, form_13f_file_number, report_type,
                     street1, street2, city, state, zip_code,
                     filing_count_primary, filing_count_other, filing_count_total)
                VALUES %s
                ON CONFLICT (manager_cik) DO UPDATE SET
                    name_source = CASE
                        WHEN dim_13f_manager.name_source = 'submission' THEN 'both'
                        ELSE 'other_manager'
                    END,
                    crd_number = COALESCE(dim_13f_manager.crd_number, EXCLUDED.crd_number),
                    sec_file_number = COALESCE(dim_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                    form_13f_file_number = COALESCE(dim_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                    filing_count_other = EXCLUDED.filing_count_other,
                    filing_count_total = dim_13f_manager.filing_count_primary + EXCLUDED.filing_count_other,
                    updated_at = now()
            """, om_best_rows)

    # ── Phase 5b: Backfill address for secondary managers from their primary filings ──
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH best_cover AS (
                SELECT DISTINCT ON (f.manager_cik)
                       f.manager_cik,
                       c.raw_payload->>'FILINGMANAGER_STREET1'  AS st1,
                       c.raw_payload->>'FILINGMANAGER_STREET2'  AS st2,
                       c.raw_payload->>'FILINGMANAGER_CITY'     AS city,
                       c.raw_payload->>'FILINGMANAGER_STATEORCOUNTRY' AS state,
                       c.raw_payload->>'FILINGMANAGER_ZIPCODE'  AS zip
                  FROM fact_13f_submission f
                  JOIN fact_13f_coverpage c ON f.accession_number = c.accession_number
                 WHERE c.raw_payload->>'FILINGMANAGER_STREET1' IS NOT NULL
                 ORDER BY f.manager_cik, f.filed_date DESC
            )
            UPDATE dim_13f_manager d
               SET street1  = bc.st1,
                   street2  = bc.st2,
                   city     = bc.city,
                   state    = bc.state,
                   zip_code = bc.zip,
                   updated_at = now()
              FROM best_cover bc
             WHERE d.manager_cik = bc.manager_cik
               AND d.street1 IS NULL
        """)
        result["dim_om_address_backfill"] = cur.rowcount

    result["other_manager_rows"] = total_om_rows
    result["dim_other_upserted"] = dim_om_written
    return result


def resolve_13f_managers(limit: int | None = None) -> dict[str, int]:
    """Build the canonical manager dimension from submissions and other-manager TSVs."""
    result: dict[str, int] = {}

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE fact_13f_submission f
               SET manager_name = c.raw_payload->>'FILINGMANAGER_NAME',
                   updated_at = now()
              FROM fact_13f_coverpage c
             WHERE f.accession_number = c.accession_number
               AND (f.manager_name IS NULL OR f.manager_name = f.manager_cik)
               AND c.raw_payload->>'FILINGMANAGER_NAME' IS NOT NULL
               AND c.raw_payload->>'FILINGMANAGER_NAME' <> ''
        """)
        result["submission_names_backfilled"] = cur.rowcount

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH primary_stats AS (
                SELECT manager_cik,
                       COUNT(DISTINCT accession_number)::integer AS filing_count_primary,
                       MIN(report_period) AS first_quarter_filed,
                       MAX(report_period) AS last_quarter_filed
                FROM fact_13f_submission
                GROUP BY manager_cik
            ),
            best_submission AS (
                SELECT DISTINCT ON (s.manager_cik)
                       s.manager_cik,
                       COALESCE(NULLIF(s.manager_name, ''), s.manager_cik) AS manager_name,
                       c.crd_number,
                       c.sec_file_number,
                       c.form_13f_file_number,
                       c.report_type,
                       c.raw_payload->>'FILINGMANAGER_STREET1' AS street1,
                       c.raw_payload->>'FILINGMANAGER_STREET2' AS street2,
                       c.raw_payload->>'FILINGMANAGER_CITY' AS city,
                       c.raw_payload->>'FILINGMANAGER_STATEORCOUNTRY' AS state,
                       c.raw_payload->>'FILINGMANAGER_ZIPCODE' AS zip_code
                FROM fact_13f_submission s
                LEFT JOIN fact_13f_coverpage c ON c.accession_number = s.accession_number
                ORDER BY s.manager_cik,
                         (COALESCE(NULLIF(s.manager_name, ''), s.manager_cik) <> s.manager_cik) DESC,
                         s.report_period DESC NULLS LAST,
                         s.filed_date DESC NULLS LAST
            )
            INSERT INTO dim_13f_manager (
                manager_cik, manager_name, name_source,
                crd_number, sec_file_number, form_13f_file_number, report_type,
                street1, street2, city, state, zip_code,
                filing_count_primary, filing_count_total,
                first_quarter_filed, last_quarter_filed, last_seen_at
            )
            SELECT
                p.manager_cik,
                b.manager_name,
                'submission',
                b.crd_number,
                b.sec_file_number,
                b.form_13f_file_number,
                b.report_type,
                b.street1,
                b.street2,
                b.city,
                b.state,
                b.zip_code,
                p.filing_count_primary,
                p.filing_count_primary,
                p.first_quarter_filed,
                p.last_quarter_filed,
                now()
            FROM primary_stats p
            JOIN best_submission b ON b.manager_cik = p.manager_cik
            ON CONFLICT (manager_cik) DO UPDATE SET
                manager_name = CASE
                    WHEN dim_13f_manager.manager_name = dim_13f_manager.manager_cik
                         AND EXCLUDED.manager_name <> EXCLUDED.manager_cik
                    THEN EXCLUDED.manager_name
                    ELSE COALESCE(NULLIF(dim_13f_manager.manager_name, ''), EXCLUDED.manager_name)
                END,
                name_source = CASE
                    WHEN dim_13f_manager.name_source IN ('both', 'other_manager') THEN 'both'
                    ELSE 'submission'
                END,
                crd_number = COALESCE(dim_13f_manager.crd_number, EXCLUDED.crd_number),
                sec_file_number = COALESCE(dim_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                form_13f_file_number = COALESCE(dim_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                report_type = COALESCE(dim_13f_manager.report_type, EXCLUDED.report_type),
                street1 = COALESCE(dim_13f_manager.street1, EXCLUDED.street1),
                street2 = COALESCE(dim_13f_manager.street2, EXCLUDED.street2),
                city = COALESCE(dim_13f_manager.city, EXCLUDED.city),
                state = COALESCE(dim_13f_manager.state, EXCLUDED.state),
                zip_code = COALESCE(dim_13f_manager.zip_code, EXCLUDED.zip_code),
                filing_count_primary = EXCLUDED.filing_count_primary,
                filing_count_total = EXCLUDED.filing_count_primary + dim_13f_manager.filing_count_other,
                first_quarter_filed = EXCLUDED.first_quarter_filed,
                last_quarter_filed = EXCLUDED.last_quarter_filed,
                last_seen_at = now(),
                updated_at = now()
        """)
        result["dim_primary_upserted"] = cur.rowcount

    limit_sql = "LIMIT %s" if limit else ""
    params = [limit] if limit else []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT dataset_key, local_path FROM source_13f_dataset_state WHERE downloaded AND local_path IS NOT NULL {limit_sql}", params)
        states = cur.fetchall()

    om_manager_rows: dict[str, dict] = {}
    total_om_rows = 0
    for _dataset_key, local_path in states:
        path = Path(local_path)
        if not path.exists():
            continue
        for tsv_name in ("OTHERMANAGER.tsv", "OTHERMANAGER2.tsv"):
            try:
                if path.is_dir():
                    rows = _read_tsv_file(path / tsv_name)
                elif zipfile.is_zipfile(path):
                    with zipfile.ZipFile(path) as zf:
                        rows = _read_tsv(zf, _zip_member(zf, tsv_name))
                else:
                    continue
            except Exception:
                continue
            for row in rows:
                cik = normalize_cik(_value(row, "CIK"))
                name = _value(row, "NAME")
                if not cik or not name:
                    continue
                total_om_rows += 1
                entry = om_manager_rows.setdefault(cik, {"names": {}, "crd": None, "sec_file": None, "form_13f": None, "count": 0})
                entry["names"][name] = entry["names"].get(name, 0) + 1
                entry["count"] += 1
                entry["crd"] = entry["crd"] or _value(row, "CRDNUMBER")
                entry["sec_file"] = entry["sec_file"] or _value(row, "SECFILENUMBER")
                entry["form_13f"] = entry["form_13f"] or _value(row, "FORM13FFILENUMBER")

    om_best_rows = []
    for cik, entry in om_manager_rows.items():
        best_name = max(entry["names"], key=lambda n: (entry["names"][n], len(n)))
        om_best_rows.append((cik, best_name, "other_manager", entry["crd"], entry["sec_file"], entry["form_13f"], 0, entry["count"], entry["count"]))

    dim_om_written = 0
    if om_best_rows:
        with connect() as conn, conn.cursor() as cur:
            dim_om_written = execute_values(cur, """
                INSERT INTO dim_13f_manager
                    (manager_cik, manager_name, name_source, crd_number, sec_file_number,
                     form_13f_file_number, filing_count_primary, filing_count_other, filing_count_total)
                VALUES %s
                ON CONFLICT (manager_cik) DO UPDATE SET
                    manager_name = CASE
                        WHEN dim_13f_manager.name_source = 'other_manager'
                          OR dim_13f_manager.manager_name = dim_13f_manager.manager_cik
                        THEN EXCLUDED.manager_name
                        ELSE dim_13f_manager.manager_name
                    END,
                    name_source = CASE
                        WHEN dim_13f_manager.name_source IN ('submission', 'both') THEN 'both'
                        ELSE 'other_manager'
                    END,
                    crd_number = COALESCE(dim_13f_manager.crd_number, EXCLUDED.crd_number),
                    sec_file_number = COALESCE(dim_13f_manager.sec_file_number, EXCLUDED.sec_file_number),
                    form_13f_file_number = COALESCE(dim_13f_manager.form_13f_file_number, EXCLUDED.form_13f_file_number),
                    filing_count_other = EXCLUDED.filing_count_other,
                    filing_count_total = dim_13f_manager.filing_count_primary + EXCLUDED.filing_count_other,
                    updated_at = now()
            """, om_best_rows)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH best_cover AS (
                SELECT DISTINCT ON (f.manager_cik)
                       f.manager_cik,
                       c.raw_payload->>'FILINGMANAGER_STREET1' AS st1,
                       c.raw_payload->>'FILINGMANAGER_STREET2' AS st2,
                       c.raw_payload->>'FILINGMANAGER_CITY' AS city,
                       c.raw_payload->>'FILINGMANAGER_STATEORCOUNTRY' AS state,
                       c.raw_payload->>'FILINGMANAGER_ZIPCODE' AS zip
                FROM fact_13f_submission f
                JOIN fact_13f_coverpage c ON f.accession_number = c.accession_number
                WHERE c.raw_payload->>'FILINGMANAGER_STREET1' IS NOT NULL
                ORDER BY f.manager_cik, f.report_period DESC NULLS LAST, f.filed_date DESC NULLS LAST
            )
            UPDATE dim_13f_manager d
               SET street1 = bc.st1,
                   street2 = bc.st2,
                   city = bc.city,
                   state = bc.state,
                   zip_code = bc.zip,
                   updated_at = now()
              FROM best_cover bc
             WHERE d.manager_cik = bc.manager_cik
               AND d.street1 IS NULL
        """)
        result["dim_address_backfilled"] = cur.rowcount

    result["other_manager_rows"] = total_om_rows
    result["dim_other_upserted"] = dim_om_written
    return result


def run_13f(quarter: str | None = None, from_year: int = 2013, force: bool = False, limit: int | None = None, row_limit: int | None = None) -> dict[str, int]:
    out = {}
    out |= {f"discover_{k}": v for k, v in discover_13f(from_year=from_year).items()}
    out |= {f"register_local_{k}": v for k, v in register_local_13f(quarter=quarter).items()}
    out |= {f"download_{k}": v for k, v in download_13f(quarter=quarter, force=force, limit=limit).items()}
    out |= {f"parse_{k}": v for k, v in parse_13f(quarter=quarter, limit=limit, row_limit=row_limit, force=force).items()}
    out |= {f"resolve_{k}": v for k, v in resolve_13f_issuers().items()}
    out |= {f"resolve_managers_{k}": v for k, v in resolve_13f_managers().items()}
    return out


def discover_13dg(cik: str | None = None) -> dict[str, int]:
    # Lean v1: register local 13D/G files if the directory exists. Network discovery
    # can be added without changing the downstream parser/table contract.
    root = _dg_root()
    rows = []
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".xml", ".html", ".htm", ".txt"}:
                continue
            acc = dashed_accession(path.stem.split("_", 1)[0])
            if cik and normalize_cik(cik) not in str(path):
                continue
            rows.append((acc or path.stem, None, None, None, None, str(path), True, False, sha256_file(path)))
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO source_13dg_filing_state
                (accession_number, reporting_person_cik, issuer_cik, form_type, filed_date,
                 local_path, downloaded, parsed, source_hash)
            VALUES %s
            ON CONFLICT (accession_number) DO UPDATE SET
                local_path = EXCLUDED.local_path,
                downloaded = EXCLUDED.downloaded,
                source_hash = EXCLUDED.source_hash,
                updated_at = now()
        """, rows)
    return {"discovered": written}


def _record_13dg_download_results(results: list[dict], issuer_cik: str | None = None, reporting_person_cik: str | None = None) -> int:
    rows = []
    for result in results:
        archive_cik = normalize_cik(result.get("cik") or reporting_person_cik or issuer_cik)
        for filing in result.get("filings", []) or []:
            path = filing.get("path")
            accession = dashed_accession(filing.get("accession"))
            if not path or not accession:
                continue
            source_url = None
            primary_document = filing.get("primary_document")
            if archive_cik and primary_document:
                source_url = f"https://www.sec.gov/Archives/edgar/data/{int(archive_cik)}/{accession.replace('-', '')}/{primary_document}"
            rows.append((
                accession,
                normalize_cik(reporting_person_cik) if reporting_person_cik else None,
                normalize_cik(issuer_cik) if issuer_cik else None,
                filing.get("form"),
                parse_date(filing.get("date")),
                path,
                source_url,
                True,
                False,
                sha256_file(Path(path)),
            ))
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, """
            INSERT INTO source_13dg_filing_state
                (accession_number, reporting_person_cik, issuer_cik, form_type, filed_date,
                 local_path, source_url, downloaded, parsed, source_hash)
            VALUES %s
            ON CONFLICT (accession_number) DO UPDATE SET
                reporting_person_cik = COALESCE(source_13dg_filing_state.reporting_person_cik, EXCLUDED.reporting_person_cik),
                issuer_cik = COALESCE(source_13dg_filing_state.issuer_cik, EXCLUDED.issuer_cik),
                form_type = COALESCE(EXCLUDED.form_type, source_13dg_filing_state.form_type),
                filed_date = COALESCE(EXCLUDED.filed_date, source_13dg_filing_state.filed_date),
                local_path = EXCLUDED.local_path,
                source_url = COALESCE(EXCLUDED.source_url, source_13dg_filing_state.source_url),
                downloaded = EXCLUDED.downloaded,
                source_hash = EXCLUDED.source_hash,
                updated_at = now()
        """, rows)


def _issuer_ciks_for_13dg(all_issuer_universe: bool = False, limit: int | None = None) -> list[str]:
    limit_sql = "LIMIT %s" if limit else ""
    params = [limit] if limit else []
    where = "WHERE cik IS NOT NULL"
    if not all_issuer_universe:
        where += " AND include_in_pipeline"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT cik FROM dim_company_us {where} ORDER BY cik {limit_sql}", params)
        return [normalize_cik(row[0]) for row in cur.fetchall()]


def download_13dg(
    cik: str | None = None,
    all_issuers: bool = False,
    all_issuer_universe: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    from xbrl_sec.sec.sources.institutional_download import download_13dg_filings_for_cik

    ciks = _issuer_ciks_for_13dg(all_issuer_universe=all_issuer_universe, limit=limit) if all_issuers else ([normalize_cik(cik)] if cik else [])
    if not ciks:
        return {"candidates": 0, "downloaded": 0, "registered": 0, "skipped": 0, "errors": 0}

    downloaded = errors = registered = 0
    for target_cik in ciks:
        result = download_13dg_filings_for_cik(target_cik)
        downloaded += int(result.get("downloaded") or 0)
        errors += int(result.get("failed") or (1 if result.get("error") else 0))
        registered += _record_13dg_download_results(
            [result],
            issuer_cik=target_cik if all_issuers else None,
            reporting_person_cik=None if all_issuers else target_cik,
        )
    return {
        "candidates": len(ciks),
        "downloaded": downloaded,
        "registered": registered,
        "skipped": 0,
        "errors": errors,
    }


def _strip_filing_text(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _regex_first(text: str, *patterns: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I | re.S)
        if m:
            return m.group(1).strip(" :;\t\r\n")
    return None


def _regex_before_label(text: str, label: str, max_chars: int = 240) -> str | None:
    pattern = rf"([A-Z0-9][A-Z0-9 .,&'/$\-]{{1,{max_chars}}})\s*\(\s*{label}\s*\)"
    matches = list(re.finditer(pattern, text, flags=re.I | re.S))
    if not matches:
        return None
    value = matches[0].group(1)
    parts = [p.strip(" :;\t\r\n") for p in re.split(r"\s{2,}", value) if p.strip()]
    return parts[-1] if parts else value.strip(" :;\t\r\n")


def _clean_13dg_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" :;\t\r\n")
    cleaned = re.sub(r"^(?:\(?\d+\)?\s+)+", "", cleaned).strip()
    alpha_count = len(re.findall(r"[A-Z]", cleaned, flags=re.I))
    if len(cleaned) < 3 or alpha_count < 3:
        return None
    return cleaned


def _parse_percent(value: str | None):
    if not value:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_int_text(value: str | None):
    if not value:
        return None
    m = re.search(r"-?\d[\d,]*", value)
    return int(m.group(0).replace(",", "")) if m else None


def _extract_13dg_row(
    accession: str,
    local_path: str,
    state_form_type: str | None = None,
    state_filed_date=None,
    state_issuer_cik: str | None = None,
    state_reporting_person_cik: str | None = None,
) -> tuple:
    raw = Path(local_path).read_text(encoding="utf-8", errors="ignore")
    text = _strip_filing_text(raw)
    form_type = state_form_type or _regex_first(text, r"FORM TYPE\s*([A-Z0-9 /-]+)", r"(SC 13[DG]/?A?)")
    issuer_name = (
        _regex_before_label(text, "Name of Issuer")
        or _regex_first(text, r"Name of Issuer\)?\s*([A-Z0-9 .,&'\-]+?)\s*(Title of Class|CUSIP|Item)")
    )
    issuer_name = _clean_13dg_text(issuer_name)
    title = (
        _regex_before_label(text, "Title of Class(?:\\s+of Securities)?")
        or _regex_first(text, r"Title of Class of Securities\)?\s*([A-Z0-9 .,&'\-]+?)\s*(CUSIP|Item)")
    )
    title = _clean_13dg_text(title)
    cusip = _regex_first(text, r"([A-Z0-9]{6,9})\s*\(\s*CUSIP(?: Number)?\s*\)", r"CUSIP(?: Number)?\)?\s*([A-Z0-9]{6,9})")
    reporting_person = _regex_first(
        text,
        r"Name of Reporting Person[s]?\s*([A-Z0-9 .,&'\-]+?)\s*(I\.R\.S\.|Check|Item)",
        r"Reporting Person\s*([A-Z0-9 .,&'\-]+?)\s*(Item|Citizenship)",
    )
    reporting_person = _clean_13dg_text(reporting_person)
    amount = _parse_int_text(_regex_first(text, r"Aggregate Amount Beneficially Owned.*?([0-9,]+)"))
    percent = _parse_percent(_regex_first(text, r"Percent of Class Represented.*?([0-9,.]+%)"))
    purpose = _regex_first(text, r"Purpose of Transaction\s*(.*?)(Item\s+5|Interest in Securities|Signature)")
    source = _regex_first(text, r"Source and Amount of Funds.*?\s*(.*?)(Item\s+4|Purpose of Transaction)")
    return (
        accession, 1, form_type, state_filed_date, None, state_issuer_cik, issuer_name, None, title, cusip,
        state_reporting_person_cik, reporting_person, None, False, None, amount, percent, None, None, None,
        None, amount, purpose, text[:2000], json.dumps({"source_path": local_path, "source_of_funds": source}),
    )


def parse_13dg(cik: str | None = None, limit: int | None = None) -> dict[str, int]:
    params: list = []
    where = "WHERE downloaded AND local_path IS NOT NULL"
    if cik:
        where += " AND (issuer_cik = %s OR reporting_person_cik = %s OR local_path LIKE %s)"
        params.extend([normalize_cik(cik), normalize_cik(cik), f"%{normalize_cik(cik)}%"])
    if limit:
        where += " LIMIT %s"
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT accession_number, local_path, form_type, filed_date, issuer_cik, reporting_person_cik
            FROM source_13dg_filing_state {where}
            """,
            params,
        )
        states = cur.fetchall()
    rows = []
    errors = []
    for ordinal, (accession, local_path, form_type, filed_date, issuer_cik, reporting_person_cik) in enumerate(states, start=1):
        try:
            rows.append(_extract_13dg_row(accession, local_path, form_type, filed_date, issuer_cik, reporting_person_cik))
        except Exception as exc:
            errors.append((str(exc), accession))
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO fact_13dg_ownership
                (accession_number, row_ordinal, form_type, filed_date, period_of_report,
                 issuer_cik, issuer_name, issuer_ticker, title_of_class, cusip,
                 reporting_person_cik, reporting_person_name, reporting_person_type,
                 is_group_member, group_name, amount_beneficially_owned, percent_of_class,
                 sole_voting_power, shared_voting_power, sole_dispositive_power,
                 shared_dispositive_power, aggregate_amount, purpose_of_transaction,
                 raw_text_excerpt, raw_payload)
            VALUES %s
            ON CONFLICT (accession_number, row_ordinal) DO UPDATE SET
                form_type = EXCLUDED.form_type,
                filed_date = EXCLUDED.filed_date,
                period_of_report = EXCLUDED.period_of_report,
                issuer_cik = EXCLUDED.issuer_cik,
                issuer_name = EXCLUDED.issuer_name,
                issuer_ticker = EXCLUDED.issuer_ticker,
                title_of_class = EXCLUDED.title_of_class,
                cusip = EXCLUDED.cusip,
                reporting_person_cik = EXCLUDED.reporting_person_cik,
                reporting_person_name = EXCLUDED.reporting_person_name,
                amount_beneficially_owned = EXCLUDED.amount_beneficially_owned,
                percent_of_class = EXCLUDED.percent_of_class,
                aggregate_amount = EXCLUDED.aggregate_amount,
                purpose_of_transaction = EXCLUDED.purpose_of_transaction,
                raw_text_excerpt = EXCLUDED.raw_text_excerpt,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = now()
        """, rows)
        if written:
            execute_values(cur, """
                UPDATE source_13dg_filing_state AS s
                   SET parsed = true, rows_parsed = 1, parse_error = NULL, updated_at = now()
                  FROM (VALUES %s) AS v(accession_number)
                 WHERE s.accession_number = v.accession_number
            """, [(r[0],) for r in rows])
        execute_values(cur, """
            UPDATE source_13dg_filing_state AS s
               SET parsed = false, parse_error = v.parse_error, updated_at = now()
              FROM (VALUES %s) AS v(parse_error, accession_number)
             WHERE s.accession_number = v.accession_number
        """, errors)
        cur.execute("""
            WITH matches AS (
                SELECT DISTINCT ON (o.accession_number, o.row_ordinal)
                       o.accession_number,
                       o.row_ordinal,
                       h.issuer_cik,
                       h.issuer_ticker
                  FROM fact_13dg_ownership o
                  JOIN fact_13f_holdings h
                    ON h.cusip = o.cusip
                   AND h.issuer_cik IS NOT NULL
                 WHERE o.cusip IS NOT NULL
                   AND (o.issuer_cik IS NULL OR o.issuer_ticker IS NULL)
                 ORDER BY o.accession_number, o.row_ordinal, h.report_period DESC NULLS LAST
            )
            UPDATE fact_13dg_ownership o
               SET issuer_cik = COALESCE(o.issuer_cik, m.issuer_cik),
                   issuer_ticker = COALESCE(o.issuer_ticker, m.issuer_ticker),
                   updated_at = now()
              FROM matches m
             WHERE o.accession_number = m.accession_number
               AND o.row_ordinal = m.row_ordinal
        """)
        issuer_ciks_resolved = cur.rowcount
        cur.execute("""
            WITH best AS (
                SELECT accession_number, MAX(issuer_cik) AS issuer_cik
                  FROM fact_13dg_ownership
                 WHERE issuer_cik IS NOT NULL
                 GROUP BY accession_number
            )
            UPDATE source_13dg_filing_state s
               SET issuer_cik = COALESCE(s.issuer_cik, best.issuer_cik),
                   updated_at = now()
              FROM best
             WHERE s.accession_number = best.accession_number
               AND s.issuer_cik IS NULL
        """)
    return {"filings": len(states), "ownership_rows": written, "issuer_ciks_resolved": issuer_ciks_resolved, "errors": len(errors)}


def _table_row_estimate(cur, table_name: str) -> int | None:
    cur.execute(
        """
        SELECT GREATEST(c.reltuples, 0)::bigint
        FROM pg_class c
        WHERE c.oid = to_regclass(%s)
        """,
        (table_name,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def status() -> dict[str, int | bool | None]:
    with connect() as conn, conn.cursor() as cur:
        out = {}
        cur.execute("SELECT COUNT(*) FROM source_13f_dataset_state")
        out["datasets"] = cur.fetchone()[0]
        out["holdings"] = _table_row_estimate(cur, "fact_13f_holdings")
        out["holdings_estimated"] = True
        cur.execute("SELECT COUNT(*) FROM dim_13f_manager")
        out["managers"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM dim_security_identifier_us")
        out["security_identifiers"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fact_security_identifier_evidence_us")
        out["security_identifier_evidence"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM source_13dg_filing_state")
        out["source_13dg"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fact_13dg_ownership")
        out["ownership_13dg"] = cur.fetchone()[0]
        return out
