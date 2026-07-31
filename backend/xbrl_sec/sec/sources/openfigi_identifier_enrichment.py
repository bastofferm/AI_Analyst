"""OpenFIGI CUSIP→ticker enrichment for 13F securities.

Mirrors the surface of `yahoo_identifier_enrichment.py` but resolves via the
OpenFIGI v3 mapping API, which is authoritative for US-listed instruments.

Two-phase flow:
  1. `run_enrichment(...)` — load unresolved candidates from
     `dim_13f_security_us`, POST batches to OpenFIGI, upsert evidence into
     `fact_13f_openfigi_identifier_enrichment`. Optionally `apply` immediately.
  2. `apply_accepted_evidence(...)` — promote accepted evidence to
     `dim_13f_security_us` (primary_ticker + resolution_status='resolved' +
     evidence_payload JSON blob).

The first run naturally backfills the historical unresolved CUSIPs; subsequent
quarterly runs catch newly-filed ETFs and reorganized funds.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.request import Request, urlopen

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()

SOURCE_NAME = "openfigi.cusip_mapping"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# OpenFIGI exchange codes for US composite / primary US listings.
US_EXCH_CODES = {"US", "UN", "UP", "UQ", "UA", "UR", "UV", "UW", "UF"}

# Security types we trust enough to auto-apply (one-to-one CUSIP→ticker).
ACCEPTED_SECURITY_TYPES = {
    "Common Stock",
    "ADR",
    "REIT",
    "ETP",
    "Mutual Fund",
    "Closed-End Fund",
    "Open-End Fund",
    "Preferred Stock",
    "Unit",
}

# 13F security titles that indicate options/warrants — never resolvable via FIGI.
OPTION_TITLES = ("CALL", "PUT", "WRNT", "WTS", "OPTION")

_EVIDENCE_TABLE_READY = False

# --- HTTP / rate-limit config (env-tunable; module-level so callers can monkey-patch) ---
HTTP_RETRY_ATTEMPTS = int(os.environ.get("OPENFIGI_HTTP_RETRIES", "4"))
HTTP_RETRY_BACKOFF_SECONDS = float(os.environ.get("OPENFIGI_HTTP_BACKOFF_SECONDS", "0.75"))
OPENFIGI_API_KEY = (
    os.environ.get("OPENFIGI_API_KEY", "").strip()
    or os.environ.get("OPEN_FIGI_API_KEY", "").strip()  # alias: user-env convention
    or None
)

# OpenFIGI batch caps: anonymous = 10 jobs/request × 25 req/min = 250/min.
# Keyed = 100 jobs/request × 250 req/min = 25,000/min.
_DEFAULT_BATCH = 100 if OPENFIGI_API_KEY else 10
_DEFAULT_SLEEP = 0.25 if OPENFIGI_API_KEY else 2.5
OPENFIGI_BATCH_SIZE = int(os.environ.get("OPENFIGI_BATCH_SIZE", str(_DEFAULT_BATCH)))
OPENFIGI_RATE_LIMIT_SLEEP_S = float(os.environ.get("OPENFIGI_RATE_LIMIT_SLEEP_S", str(_DEFAULT_SLEEP)))


@dataclass(frozen=True)
class SecurityCandidate:
    cusip: str
    issuer_name: str | None
    security_title: str | None
    primary_ticker: str | None
    asset_bucket: str | None
    resolution_status: str | None
    row_count: int
    value_observed: float | None


@dataclass(frozen=True)
class MatchDecision:
    status: str
    status_reason: str
    confidence_score: float


@dataclass(frozen=True)
class EnrichmentEvidence:
    cusip: str
    issuer_name: str | None
    security_title: str | None
    asset_bucket: str | None
    openfigi_ticker: str | None
    openfigi_name: str | None
    openfigi_exch_code: str | None
    openfigi_security_type: str | None
    openfigi_security_type2: str | None
    openfigi_market_sector: str | None
    openfigi_figi: str | None
    openfigi_share_class_figi: str | None
    openfigi_composite_figi: str | None
    openfigi_listings_returned: int
    confidence_score: float
    status: str
    status_reason: str
    error_type: str | None
    error_message: str | None
    raw_payload: list[dict[str, Any]] | None


def normalize_cusip(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return text if re.fullmatch(r"[A-Z0-9]{9}", text) else None


def candidate_where_sql(alias: str | None = None) -> str:
    """Rows in dim_13f_security_us that we want to send to OpenFIGI."""
    prefix = f"{alias}." if alias else ""
    excluded = ", ".join(f"'{title}'" for title in OPTION_TITLES)
    return (
        f"{prefix}primary_ticker IS NULL\n"
        f"  AND {prefix}cusip ~ '^[A-Z0-9]{{9}}$'\n"
        f"  AND ({prefix}security_title IS NULL OR {prefix}security_title NOT IN ({excluded}))\n"
        f"  AND ({prefix}asset_bucket IS NULL OR {prefix}asset_bucket NOT IN ('derivatives', 'fixed_income'))\n"
        f"  AND {prefix}resolution_status IN ('unresolved', 'fund_etf', 'non_company_security', 'ambiguous')"
    )


# ---------------------------------------------------------------------------
# HTTP layer — mirrors yahoo_identifier_enrichment._read_url_with_retries
# ---------------------------------------------------------------------------

def _read_url_with_retries(req: Request, timeout: float = 15.0) -> bytes:
    attempts = max(1, HTTP_RETRY_ATTEMPTS)
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def _query_openfigi_batch(cusips: list[str]) -> list[dict[str, Any]]:
    """POST a batch of CUSIPs to OpenFIGI. Returns a list 1-to-1 with input order.

    Each element is one of:
      - {"data": [{ticker, name, exchCode, securityType, securityType2, marketSector,
                   figi, shareClassFIGI, compositeFIGI, ...}, ...]}
      - {"error": "No identifier found"}
      - {"warning": "..."}
    """
    body = [{"idType": "ID_CUSIP", "idValue": c} for c in cusips]
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_API_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_API_KEY
    req = Request(OPENFIGI_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    raw = _read_url_with_retries(req, timeout=30.0)
    parsed = json.loads(raw.decode("utf-8", errors="ignore"))
    if not isinstance(parsed, list) or len(parsed) != len(cusips):
        raise RuntimeError(
            f"OpenFIGI returned malformed response: expected list of {len(cusips)}, got {type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _pick_best_listing(data: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    """From OpenFIGI 'data' list, prefer US composite. Return (best, total_count)."""
    if not data:
        return None, 0
    us_only = [d for d in data if (d.get("exchCode") or "").upper() in US_EXCH_CODES]
    if us_only:
        return us_only[0], len(data)
    return data[0], len(data)


def classify_match(
    cusip: str,
    response_element: dict[str, Any],
) -> tuple[MatchDecision, dict[str, Any] | None, int]:
    """Decide outcome status and pick the best listing.

    Returns (decision, best_listing_or_None, total_listings_returned).
    """
    if "error" in response_element:
        msg = str(response_element.get("error") or "unknown")
        msg_lower = msg.lower()
        # OpenFIGI's per-element rejections — permanent, not transient. Treat as not_found
        # so --resume skips them and they don't get retried indefinitely.
        if (
            msg_lower.startswith("no identifier")
            or "invalid idvalue" in msg_lower
            or "invalid id" in msg_lower
        ):
            return MatchDecision("not_found", msg, 0.0), None, 0
        return MatchDecision("error", msg, 0.0), None, 0

    data = response_element.get("data") or []
    best, total = _pick_best_listing(data)
    if best is None:
        return MatchDecision("not_found", "OpenFIGI returned empty data.", 0.0), None, 0

    ticker = (best.get("ticker") or "").strip()
    exch = (best.get("exchCode") or "").upper()
    sec_type = (best.get("securityType") or "").strip()

    if not ticker:
        return MatchDecision("error", "OpenFIGI returned listing without ticker.", 0.0), best, total

    # Only US listings get applied; multi-listed foreign-only -> log but don't apply.
    if exch not in US_EXCH_CODES:
        return (
            MatchDecision(
                "multi_listing",
                f"OpenFIGI returned only non-US listings (best exchCode={exch}).",
                40.0,
            ),
            best,
            total,
        )

    if sec_type not in ACCEPTED_SECURITY_TYPES:
        return (
            MatchDecision(
                "multi_listing",
                f"US listing found but securityType={sec_type!r} is not in the accepted whitelist.",
                50.0,
            ),
            best,
            total,
        )

    if total == 1:
        return MatchDecision("accepted", "Single US-listed match.", 100.0), best, total
    return MatchDecision("accepted", f"Selected US composite from {total} listings.", 85.0), best, total


def _build_evidence(candidate: SecurityCandidate, response_element: dict[str, Any]) -> EnrichmentEvidence:
    decision, best, total = classify_match(candidate.cusip, response_element)
    raw_data = response_element.get("data") if isinstance(response_element, dict) else None
    if best is not None:
        return EnrichmentEvidence(
            cusip=candidate.cusip,
            issuer_name=candidate.issuer_name,
            security_title=candidate.security_title,
            asset_bucket=candidate.asset_bucket,
            openfigi_ticker=(best.get("ticker") or None),
            openfigi_name=(best.get("name") or None),
            openfigi_exch_code=(best.get("exchCode") or None),
            openfigi_security_type=(best.get("securityType") or None),
            openfigi_security_type2=(best.get("securityType2") or None),
            openfigi_market_sector=(best.get("marketSector") or None),
            openfigi_figi=(best.get("figi") or None),
            openfigi_share_class_figi=(best.get("shareClassFIGI") or None),
            openfigi_composite_figi=(best.get("compositeFIGI") or None),
            openfigi_listings_returned=total,
            confidence_score=decision.confidence_score,
            status=decision.status,
            status_reason=decision.status_reason,
            error_type=None,
            error_message=None,
            raw_payload=raw_data if isinstance(raw_data, list) else None,
        )
    return EnrichmentEvidence(
        cusip=candidate.cusip,
        issuer_name=candidate.issuer_name,
        security_title=candidate.security_title,
        asset_bucket=candidate.asset_bucket,
        openfigi_ticker=None,
        openfigi_name=None,
        openfigi_exch_code=None,
        openfigi_security_type=None,
        openfigi_security_type2=None,
        openfigi_market_sector=None,
        openfigi_figi=None,
        openfigi_share_class_figi=None,
        openfigi_composite_figi=None,
        openfigi_listings_returned=total,
        confidence_score=decision.confidence_score,
        status=decision.status,
        status_reason=decision.status_reason,
        error_type=None if decision.status != "error" else "openfigi_response",
        error_message=None if decision.status != "error" else decision.status_reason,
        raw_payload=None,
    )


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

def ensure_evidence_table() -> None:
    from xbrl_sec.sec.db.connection import connect

    global _EVIDENCE_TABLE_READY
    if _EVIDENCE_TABLE_READY:
        return
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "068_13f_openfigi_identifier_enrichment.sql"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql_path.read_text(encoding="utf-8-sig"))
    _EVIDENCE_TABLE_READY = True


def select_candidates(limit: int | None = None, resume: bool = False) -> list[SecurityCandidate]:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    resume_sql = ""
    if resume:
        resume_sql = """
          AND NOT EXISTS (
              SELECT 1
              FROM fact_13f_openfigi_identifier_enrichment e
              WHERE e.cusip = d.cusip
          )
        """
    limit_sql = "LIMIT %s" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.cusip, d.issuer_name, d.security_title, d.primary_ticker,
                   d.asset_bucket, d.resolution_status, d.row_count, d.value_observed
            FROM dim_13f_security_us d
            WHERE {candidate_where_sql("d")}
            {resume_sql}
            ORDER BY COALESCE(d.value_observed, 0) DESC, d.row_count DESC, d.cusip
            {limit_sql}
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        SecurityCandidate(
            row[0], row[1], row[2], row[3], row[4], row[5],
            int(row[6] or 0),
            float(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]


_UPSERT_EVIDENCE_SQL = """
INSERT INTO fact_13f_openfigi_identifier_enrichment
    (cusip, issuer_name, security_title, asset_bucket,
     openfigi_ticker, openfigi_name, openfigi_exch_code,
     openfigi_security_type, openfigi_security_type2, openfigi_market_sector,
     openfigi_figi, openfigi_share_class_figi, openfigi_composite_figi,
     openfigi_listings_returned,
     confidence_score, status, status_reason,
     error_type, error_message, raw_payload)
VALUES
    (%s, %s, %s, %s,
     %s, %s, %s,
     %s, %s, %s,
     %s, %s, %s,
     %s,
     %s, %s, %s,
     %s, %s, %s)
ON CONFLICT (cusip) DO UPDATE SET
    issuer_name = EXCLUDED.issuer_name,
    security_title = EXCLUDED.security_title,
    asset_bucket = EXCLUDED.asset_bucket,
    openfigi_ticker = EXCLUDED.openfigi_ticker,
    openfigi_name = EXCLUDED.openfigi_name,
    openfigi_exch_code = EXCLUDED.openfigi_exch_code,
    openfigi_security_type = EXCLUDED.openfigi_security_type,
    openfigi_security_type2 = EXCLUDED.openfigi_security_type2,
    openfigi_market_sector = EXCLUDED.openfigi_market_sector,
    openfigi_figi = EXCLUDED.openfigi_figi,
    openfigi_share_class_figi = EXCLUDED.openfigi_share_class_figi,
    openfigi_composite_figi = EXCLUDED.openfigi_composite_figi,
    openfigi_listings_returned = EXCLUDED.openfigi_listings_returned,
    confidence_score = EXCLUDED.confidence_score,
    status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason,
    error_type = EXCLUDED.error_type,
    error_message = EXCLUDED.error_message,
    raw_payload = EXCLUDED.raw_payload,
    updated_at = now()
"""


def _evidence_params(evidence: EnrichmentEvidence) -> tuple[Any, ...]:
    from psycopg2.extras import Json

    return (
        evidence.cusip,
        evidence.issuer_name,
        evidence.security_title,
        evidence.asset_bucket,
        evidence.openfigi_ticker,
        evidence.openfigi_name,
        evidence.openfigi_exch_code,
        evidence.openfigi_security_type,
        evidence.openfigi_security_type2,
        evidence.openfigi_market_sector,
        evidence.openfigi_figi,
        evidence.openfigi_share_class_figi,
        evidence.openfigi_composite_figi,
        evidence.openfigi_listings_returned,
        evidence.confidence_score,
        evidence.status,
        evidence.status_reason,
        evidence.error_type,
        evidence.error_message,
        Json(evidence.raw_payload) if evidence.raw_payload is not None else None,
    )


def upsert_evidence_batch(evidences: list[EnrichmentEvidence]) -> int:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    if not evidences:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(_UPSERT_EVIDENCE_SQL, [_evidence_params(e) for e in evidences])
    return len(evidences)


# ---------------------------------------------------------------------------
# Apply: promote accepted evidence to dim_13f_security_us
# ---------------------------------------------------------------------------

def apply_accepted_evidence(limit: int | None = None) -> dict[str, int]:
    """Promote accepted evidence to dim_13f_security_us.

    Guards: only overwrites rows where primary_ticker IS NULL (never trample a
    previously-resolved ticker). Stamps source_name='openfigi.cusip_mapping' and
    merges OpenFIGI metadata into evidence_payload.
    """
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when provided")
    limit_sql = "LIMIT %s" if limit is not None else ""
    apply_params: tuple[Any, ...] = (
        (SOURCE_NAME, SOURCE_NAME) if limit is None else (limit, SOURCE_NAME, SOURCE_NAME)
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fact_13f_openfigi_identifier_enrichment WHERE status = 'accepted'")
        accepted_total = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM fact_13f_openfigi_identifier_enrichment WHERE status = 'accepted' AND applied = true")
        already_applied = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(*)
            FROM fact_13f_openfigi_identifier_enrichment e
            JOIN dim_13f_security_us d ON d.cusip = e.cusip
            WHERE e.status = 'accepted'
              AND e.applied = false
              AND e.openfigi_ticker IS NOT NULL
              AND d.primary_ticker IS NULL
            """
        )
        eligible_unapplied = int(cur.fetchone()[0])
        cur.execute(
            f"""
            WITH eligible AS (
                SELECT e.cusip, e.openfigi_ticker, e.openfigi_name, e.openfigi_exch_code,
                       e.openfigi_security_type, e.openfigi_market_sector,
                       e.openfigi_figi, e.openfigi_share_class_figi, e.openfigi_composite_figi,
                       e.confidence_score, e.status_reason, e.updated_at
                FROM fact_13f_openfigi_identifier_enrichment e
                JOIN dim_13f_security_us d ON d.cusip = e.cusip
                WHERE e.status = 'accepted'
                  AND e.applied = false
                  AND e.openfigi_ticker IS NOT NULL
                  AND d.primary_ticker IS NULL
                ORDER BY e.confidence_score DESC, e.updated_at DESC, e.cusip
                {limit_sql}
            ),
            updated_dim AS (
                UPDATE dim_13f_security_us d
                SET primary_ticker = e.openfigi_ticker,
                    resolution_status = 'resolved',
                    source_name = %s,
                    confidence_score = e.confidence_score,
                    evidence_payload = COALESCE(d.evidence_payload, '{{}}'::jsonb)
                        || jsonb_build_object(
                            'openfigi',
                            jsonb_build_object(
                                'source_name', %s,
                                'openfigi_ticker', e.openfigi_ticker,
                                'openfigi_name', e.openfigi_name,
                                'openfigi_exch_code', e.openfigi_exch_code,
                                'openfigi_security_type', e.openfigi_security_type,
                                'openfigi_market_sector', e.openfigi_market_sector,
                                'openfigi_figi', e.openfigi_figi,
                                'openfigi_share_class_figi', e.openfigi_share_class_figi,
                                'openfigi_composite_figi', e.openfigi_composite_figi,
                                'confidence_score', e.confidence_score,
                                'status_reason', e.status_reason
                            )
                        ),
                    updated_at = now()
                FROM eligible e
                WHERE d.cusip = e.cusip AND d.primary_ticker IS NULL
                RETURNING e.cusip
            ),
            marked_evidence AS (
                UPDATE fact_13f_openfigi_identifier_enrichment e
                SET applied = true, applied_at = now(), updated_at = now()
                FROM updated_dim u
                WHERE e.cusip = u.cusip
                RETURNING e.cusip
            )
            SELECT COUNT(*) FROM marked_evidence
            """,
            apply_params,
        )
        applied = int(cur.fetchone()[0])
    return {
        "accepted_total": accepted_total,
        "already_applied": already_applied,
        "eligible_unapplied": eligible_unapplied,
        "applied": applied,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_enrichment(
    limit: int | None = None,
    apply: bool = False,
    resume: bool = True,
    batch_size: int | None = None,
    rate_limit_sleep_s: float | None = None,
) -> dict[str, int]:
    """Load unresolved candidates, query OpenFIGI in batches, upsert evidence.

    Args:
      limit: max candidates to fetch from `dim_13f_security_us`.
      apply: if True, run `apply_accepted_evidence()` after the enrichment pass.
      resume: if True (default), skip CUSIPs already present in the evidence table.
      batch_size: CUSIPs per OpenFIGI request (default: 1000 keyed / 100 anon).
      rate_limit_sleep_s: sleep between requests (default: 0.25 keyed / 2.5 anon).

    Returns counts: {candidates, batches, accepted, multi_listing, not_found, error, applied}.
    """
    batch_size = int(batch_size) if batch_size is not None else OPENFIGI_BATCH_SIZE
    sleep_s = float(rate_limit_sleep_s) if rate_limit_sleep_s is not None else OPENFIGI_RATE_LIMIT_SLEEP_S
    if batch_size < 1:
        raise ValueError("batch_size must be ≥ 1")

    candidates = select_candidates(limit=limit, resume=resume)
    counts = {
        "candidates": len(candidates),
        "batches": 0,
        "accepted": 0,
        "multi_listing": 0,
        "not_found": 0,
        "error": 0,
        "upserted": 0,
        "applied": 0,
    }
    if not candidates:
        if apply:
            counts.update({f"apply_{k}": v for k, v in apply_accepted_evidence().items()})
        return counts

    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        cusips = [c.cusip for c in chunk]
        try:
            response = _query_openfigi_batch(cusips)
        except Exception as exc:
            err_evidences = [
                EnrichmentEvidence(
                    cusip=c.cusip,
                    issuer_name=c.issuer_name,
                    security_title=c.security_title,
                    asset_bucket=c.asset_bucket,
                    openfigi_ticker=None,
                    openfigi_name=None,
                    openfigi_exch_code=None,
                    openfigi_security_type=None,
                    openfigi_security_type2=None,
                    openfigi_market_sector=None,
                    openfigi_figi=None,
                    openfigi_share_class_figi=None,
                    openfigi_composite_figi=None,
                    openfigi_listings_returned=0,
                    confidence_score=0.0,
                    status="error",
                    status_reason=f"HTTP batch failure: {type(exc).__name__}",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                    raw_payload=None,
                )
                for c in chunk
            ]
            counts["upserted"] += upsert_evidence_batch(err_evidences)
            counts["error"] += len(chunk)
            counts["batches"] += 1
            time.sleep(sleep_s)
            continue

        evidences = [_build_evidence(c, response[i]) for i, c in enumerate(chunk)]
        counts["upserted"] += upsert_evidence_batch(evidences)
        for ev in evidences:
            counts[ev.status] = counts.get(ev.status, 0) + 1
        counts["batches"] += 1
        time.sleep(sleep_s)

    if apply:
        applied_summary = apply_accepted_evidence()
        counts["applied"] = applied_summary["applied"]
        counts.update({f"apply_{k}": v for k, v in applied_summary.items()})
    return counts
