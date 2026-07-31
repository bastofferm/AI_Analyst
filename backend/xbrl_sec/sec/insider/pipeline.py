from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.filings.helpers import (
    clean_accession,
    dashed_accession,
    download_url,
    normalize_cik,
    parse_date,
    sha256_file,
    us_sec_root,
)
from xbrl_sec.sec.state.store import finish_run, start_run


def _insider_root() -> Path:
    return us_sec_root() / "insider"


def _insider_index_root() -> Path:
    return us_sec_root() / "insider_index"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(elem: ET.Element | None, *path: str) -> ET.Element | None:
    cur = elem
    for part in path:
        if cur is None:
            return None
        target = part.lower()
        cur = next((child for child in list(cur) if _local_name(child.tag) == target), None)
    return cur


def _text(elem: ET.Element | None, *path: str) -> str | None:
    target = _child(elem, *path) if path else elem
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value or None


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _num(value: str | None):
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except Exception:
        return None


def _footnotes(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    ids = []
    for node in elem.iter():
        fid = node.attrib.get("id") or node.attrib.get("footnoteId") or node.attrib.get("footnoteID")
        if fid:
            ids.append(fid)
    return " ".join(sorted(set(ids))) or None


def discover_local(cik: str | None = None, limit: int | None = None) -> dict[str, int]:
    root = _insider_root()
    candidates = []
    if root.exists():
        cik_filter = normalize_cik(cik) if cik else None
        for path in root.rglob("form4.xml"):
            parts = path.parts
            try:
                idx = parts.index("insider")
                raw_cik = parts[idx + 1]
                accession = parts[idx + 2]
            except Exception:
                raw_cik = path.parent.parent.name
                accession = path.parent.name
            norm_cik = normalize_cik(raw_cik)
            if cik_filter and norm_cik != cik_filter:
                continue
            candidates.append((dashed_accession(accession), norm_cik, None, True, False, str(path), sha256_file(path)))
            if limit and len(candidates) >= limit:
                break
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO source_insider_filing_state
                (accession_number, cik, filing_date, xml_downloaded, xml_parsed, disk_path, source_hash)
            VALUES %s
            ON CONFLICT (accession_number) DO UPDATE SET
                cik = EXCLUDED.cik,
                xml_downloaded = EXCLUDED.xml_downloaded,
                disk_path = EXCLUDED.disk_path,
                source_hash = EXCLUDED.source_hash,
                updated_at = now()
        """, candidates, page_size=5000)
    return {"discovered": written}


def index_sync(from_year: int | None = None, limit: int | None = None) -> dict[str, int]:
    rows = []
    files = sorted(_insider_index_root().glob("*.gz")) if _insider_index_root().exists() else []
    for gz_path in files:
        year_text = gz_path.name[:4]
        if from_year and year_text.isdigit() and int(year_text) < from_year:
            continue
        try:
            with gzip.open(gz_path, "rt", encoding="latin-1", errors="ignore") as fh:
                for line in fh:
                    if "|4|" not in line and "|4/A|" not in line:
                        continue
                    parts = line.strip().split("|")
                    if len(parts) < 5:
                        continue
                    cik_value, _name, form_type, filed, filename = parts[:5]
                    accession = dashed_accession(Path(filename).stem)
                    rows.append((accession, normalize_cik(cik_value), form_type, parse_date(filed), False, False, None, None))
                    if limit and len(rows) >= limit:
                        break
        except Exception:
            continue
        if limit and len(rows) >= limit:
            break
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO source_insider_filing_state
                (accession_number, cik, form_type, filing_date, xml_downloaded, xml_parsed, disk_path, source_hash)
            VALUES %s
            ON CONFLICT (accession_number) DO UPDATE SET
                cik = EXCLUDED.cik,
                form_type = EXCLUDED.form_type,
                filing_date = COALESCE(EXCLUDED.filing_date, source_insider_filing_state.filing_date),
                updated_at = now()
        """, rows, page_size=5000)
    return {"indexed": written}


def download(cik: str | None = None, limit: int | None = None, force: bool = False) -> dict[str, int]:
    params: list = []
    where = "WHERE TRUE"
    if cik:
        where += " AND cik = %s"
        params.append(normalize_cik(cik))
    if not force:
        where += " AND NOT xml_downloaded"
    if limit:
        where += " LIMIT %s"
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT accession_number, cik FROM source_insider_filing_state {where}", params)
        states = cur.fetchall()
    downloaded = skipped = errors = 0
    updates = []
    for accession, cik_value in states:
        clean = clean_accession(accession)
        dest = _insider_root() / str(int(cik_value)) / clean / "form4.xml"
        if dest.exists() and not force:
            skipped += 1
            updates.append((True, str(dest), None, sha256_file(dest), accession))
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_value)}/{clean}/{clean}.txt"
        ok, error = download_url(url, dest, force=force)
        downloaded += int(ok)
        errors += int(not ok)
        updates.append((ok or dest.exists(), str(dest), error, sha256_file(dest), accession))
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, """
            UPDATE source_insider_filing_state AS s
               SET xml_downloaded = v.downloaded,
                   xml_downloaded_at = CASE WHEN v.downloaded THEN now() ELSE s.xml_downloaded_at END,
                   disk_path = v.disk_path,
                   download_error = v.download_error,
                   source_hash = v.source_hash,
                   updated_at = now()
              FROM (VALUES %s) AS v(downloaded, disk_path, download_error, source_hash, accession_number)
             WHERE s.accession_number = v.accession_number
        """, updates)
    return {"candidates": len(states), "downloaded": downloaded, "skipped": skipped, "errors": errors}


def _parse_xml(path: Path, fallback_accession: str, fallback_cik: str) -> tuple[tuple, list[tuple], list[tuple]]:
    root = ET.parse(path).getroot()
    issuer = _child(root, "issuer")
    owner = _child(root, "reportingOwner")
    rel = _child(owner, "reportingOwnerRelationship")
    doc_type = _text(root, "documentType")
    cik = normalize_cik(_text(issuer, "issuerCik") or fallback_cik)
    filing_row = (
        fallback_accession,
        cik,
        normalize_cik(_text(owner, "reportingOwnerId", "rptOwnerCik")) or None,
        _text(owner, "reportingOwnerId", "rptOwnerName"),
        _bool(_text(rel, "isDirector")),
        _bool(_text(rel, "isOfficer")),
        _bool(_text(rel, "isTenPercentOwner")),
        _text(rel, "officerTitle"),
        _text(rel, "otherText"),
        _text(issuer, "issuerName"),
        _text(issuer, "issuerTradingSymbol"),
        parse_date(_text(root, "periodOfReport")),
        None,
        parse_date(_text(_child(root, "ownerSignature"), "signatureDate")),
        doc_type,
        bool(doc_type and doc_type.endswith("/A")),
        str(path),
        json.dumps({"source_hash": sha256_file(path)}),
    )
    non_derivative = []
    for ordinal, node in enumerate([n for n in root.iter() if _local_name(n.tag) == "nonderivativetransaction"], start=1):
        coding = _child(node, "transactionCoding")
        amounts = _child(node, "transactionAmounts")
        post = _child(node, "postTransactionAmounts")
        nature = _child(node, "ownershipNature")
        non_derivative.append((
            fallback_accession,
            ordinal,
            _text(node, "securityTitle", "value"),
            parse_date(_text(node, "transactionDate", "value")),
            _text(coding, "transactionCode"),
            _num(_text(amounts, "transactionShares", "value")),
            _num(_text(amounts, "transactionPricePerShare", "value")),
            _text(amounts, "transactionAcquiredDisposedCode", "value"),
            _num(_text(post, "sharesOwnedFollowingTransaction", "value")),
            _text(nature, "directOrIndirectOwnership", "value"),
            _bool(_text(coding, "equitySwapInvolved")) or False,
            _footnotes(node),
        ))
    derivative = []
    for ordinal, node in enumerate([n for n in root.iter() if _local_name(n.tag) == "derivativetransaction"], start=1):
        coding = _child(node, "transactionCoding")
        amounts = _child(node, "transactionAmounts")
        underlying = _child(node, "underlyingSecurity")
        nature = _child(node, "ownershipNature")
        derivative.append((
            fallback_accession,
            ordinal,
            _text(node, "securityTitle", "value"),
            _num(_text(node, "conversionOrExercisePrice", "value")),
            parse_date(_text(node, "transactionDate", "value")),
            _text(coding, "transactionCode"),
            _num(_text(amounts, "transactionShares", "value")),
            _text(amounts, "transactionAcquiredDisposedCode", "value"),
            parse_date(_text(node, "exerciseDate", "value")),
            parse_date(_text(node, "expirationDate", "value")),
            _text(underlying, "underlyingSecurityTitle", "value"),
            _num(_text(underlying, "underlyingSecurityShares", "value")),
            _text(nature, "directOrIndirectOwnership", "value"),
            _bool(_text(coding, "equitySwapInvolved")) or False,
            _footnotes(node),
        ))
    return filing_row, non_derivative, derivative


def parse(cik: str | None = None, limit: int | None = None, force: bool = False) -> dict[str, int]:
    params: list = []
    where = "WHERE xml_downloaded AND disk_path IS NOT NULL"
    if not force:
        where += " AND NOT xml_parsed"
    if cik:
        where += " AND cik = %s"
        params.append(normalize_cik(cik))
    if limit:
        where += " LIMIT %s"
        params.append(limit)
    ctx = start_run("US_INSIDER", "parse", "incremental")
    filings = non_derivative_count = derivative_count = errors = 0
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT accession_number, cik, disk_path FROM source_insider_filing_state {where}", params)
            states = cur.fetchall()
        updates = []
        for accession, cik_value, disk_path in states:
            path = Path(disk_path)
            try:
                filing_row, non_rows, der_rows = _parse_xml(path, accession, cik_value)
                with connect() as conn, conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO fact_insider_filing
                            (accession_number, cik, reporting_owner_cik, reporting_owner_name,
                             is_director, is_officer, is_ten_percent_owner, officer_title, other_text,
                             issuer_name, issuer_trading_symbol, period_of_report, filing_date,
                             signature_date, document_type, is_amendment, source_path, raw_payload)
                        VALUES %s
                        ON CONFLICT (accession_number) DO UPDATE SET
                            cik = EXCLUDED.cik,
                            reporting_owner_cik = EXCLUDED.reporting_owner_cik,
                            reporting_owner_name = EXCLUDED.reporting_owner_name,
                            is_director = EXCLUDED.is_director,
                            is_officer = EXCLUDED.is_officer,
                            is_ten_percent_owner = EXCLUDED.is_ten_percent_owner,
                            officer_title = EXCLUDED.officer_title,
                            other_text = EXCLUDED.other_text,
                            issuer_name = EXCLUDED.issuer_name,
                            issuer_trading_symbol = EXCLUDED.issuer_trading_symbol,
                            period_of_report = EXCLUDED.period_of_report,
                            signature_date = EXCLUDED.signature_date,
                            document_type = EXCLUDED.document_type,
                            is_amendment = EXCLUDED.is_amendment,
                            source_path = EXCLUDED.source_path,
                            raw_payload = EXCLUDED.raw_payload,
                            updated_at = now()
                    """, [filing_row])
                    cur.execute("DELETE FROM fact_insider_transaction_non_derivative WHERE accession_number = %s", (accession,))
                    cur.execute("DELETE FROM fact_insider_transaction_derivative WHERE accession_number = %s", (accession,))
                    non_derivative_count += execute_values(cur, """
                        INSERT INTO fact_insider_transaction_non_derivative
                            (accession_number, transaction_ordinal, security_title, transaction_date,
                             transaction_code, shares_amount, price_per_share, acquired_disposed_code,
                             shares_owned_following, direct_or_indirect, equity_swap_involved, footnote_ids)
                        VALUES %s
                    """, non_rows)
                    derivative_count += execute_values(cur, """
                        INSERT INTO fact_insider_transaction_derivative
                            (accession_number, transaction_ordinal, security_title, conversion_exercise_price,
                             transaction_date, transaction_code, shares_amount, acquired_disposed_code,
                             exercise_date, expiration_date, underlying_security_title, underlying_shares_amount,
                             direct_or_indirect, equity_swap_involved, footnote_ids)
                        VALUES %s
                    """, der_rows)
                filings += 1
                updates.append((True, None, accession))
            except Exception as exc:
                errors += 1
                updates.append((False, str(exc)[:2000], accession))
        with connect() as conn, conn.cursor() as cur:
            execute_values(cur, """
                UPDATE source_insider_filing_state AS s
                   SET xml_parsed = v.parsed,
                       xml_parsed_at = CASE WHEN v.parsed THEN now() ELSE s.xml_parsed_at END,
                       parse_error = v.parse_error,
                       updated_at = now()
                  FROM (VALUES %s) AS v(parsed, parse_error, accession_number)
                 WHERE s.accession_number = v.accession_number
            """, updates)
        finish_run(ctx, "succeeded", rows_in=len(states), rows_out=filings)
        return {
            "candidates": len(states),
            "filings": filings,
            "non_derivative": non_derivative_count,
            "derivative": derivative_count,
            "errors": errors,
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def run(cik: str | None = None, limit: int | None = None) -> dict[str, int]:
    out = {}
    out |= {f"discover_{k}": v for k, v in discover_local(cik=cik, limit=limit).items()}
    out |= {f"parse_{k}": v for k, v in parse(cik=cik, limit=limit).items()}
    return out


def status(cik: str | None = None) -> dict[str, int]:
    params = []
    where = ""
    if cik:
        where = "WHERE cik = %s"
        params.append(normalize_cik(cik))
    with connect() as conn, conn.cursor() as cur:
        out = {}
        cur.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE xml_parsed) FROM source_insider_filing_state {where}", params)
        total, parsed = cur.fetchone()
        out["source_filings"] = total
        out["parsed_filings"] = parsed
        cur.execute("SELECT COUNT(*) FROM fact_insider_transaction_non_derivative")
        out["non_derivative"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fact_insider_transaction_derivative")
        out["derivative"] = cur.fetchone()[0]
        return out
