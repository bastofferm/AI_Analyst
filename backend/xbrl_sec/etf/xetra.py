"""Xetra ETF enrichment via live.deutsche-boerse.com search API.

Deutsche Börse retired their public CSV; the live site now hydrates the ETF
table from POST /v1/search/v2/etp_search. The endpoint serves the same dataset
(name, ISIN, WKN, TER, AUM, SFDR, replication, benchmark, issuer, fund
currency) and is reachable without auth as long as we send a browser-style
Origin/Referer.

Best-effort: a fetch or schema drift here must not fail the FIRDS/price
pipeline, so all exceptions are swallowed and surfaced in the return dict.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import httpx

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


XETRA_API_URL = "https://api.live.deutsche-boerse.com/v1/search/v2/etp_search"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Content-Type": "application/json",
    "Origin": "https://live.deutsche-boerse.com",
    "Referer": "https://live.deutsche-boerse.com/",
}
_PAGE_SIZE = 100

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _fetch_page(client: httpx.Client, offset: int, limit: int) -> dict[str, Any]:
    body = {
        "instrumentType": "etf",
        "lang": "en",
        "offset": offset,
        "limit": limit,
        "sorting": "ASSETS_UNDER_MANAGEMENT",
        "sortOrder": "DESC",
    }
    r = client.post(XETRA_API_URL, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_xetra_live(max_pages: int | None = None) -> list[dict[str, Any]]:
    """Fetch every ETF the live site exposes. Returns the raw `data` rows.

    ~2,400 instruments at the time of writing, paginated 100/page. Each page
    request adds ~150ms of latency; full run is well under 60 seconds.
    """
    rows: list[dict[str, Any]] = []
    with httpx.Client(headers=_HEADERS) as client:
        first = _fetch_page(client, 0, _PAGE_SIZE)
        total = int(first.get("recordsTotal") or 0)
        rows.extend(first.get("data") or [])
        pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        if max_pages is not None:
            pages = min(pages, max_pages)
        for page_idx in range(1, pages):
            time.sleep(0.4)  # courtesy delay to the public API
            page = _fetch_page(client, page_idx * _PAGE_SIZE, _PAGE_SIZE)
            rows.extend(page.get("data") or [])
    return rows


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_PHYSICAL = {"full replication", "optimised", "optimized", "sampling", "stratified sampling"}
_SYNTHETIC = {"swap-based", "swap based", "unfunded swap", "funded swap", "synthetic"}

# Heuristic from benchmark text -> asset class. Most rows fall through to Equity
# which matches the screener default. Order matters (bond before equity).
_ASSET_CLASS_RULES: list[tuple[str, str]] = [
    ("bond", "Fixed Income"),
    ("treasury", "Fixed Income"),
    ("gilt", "Fixed Income"),
    ("aggregate", "Fixed Income"),
    ("credit", "Fixed Income"),
    ("high yield", "Fixed Income"),
    ("hy ", "Fixed Income"),
    ("gold", "Commodity"),
    ("silver", "Commodity"),
    ("commodity", "Commodity"),
    ("commodities", "Commodity"),
    ("metal", "Commodity"),
    ("multi-asset", "Mixed"),
    ("balanced", "Mixed"),
]


def _normalize_replication(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in _PHYSICAL:
        return "Physical"
    if s in _SYNTHETIC:
        return "Synthetic"
    return raw.strip()


def _normalize_sfdr(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower().replace("art.", "").replace("article", "").strip()
    if s in {"6", "8", "9"}:
        return s
    return None


def _classify_asset_class(benchmark: str | None, name: str | None) -> str:
    text = " ".join(filter(None, (benchmark, name))).lower()
    for needle, cls in _ASSET_CLASS_RULES:
        if needle in text:
            return cls
    return "Equity"


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    isin = (raw.get("isin") or "").strip().upper()
    if not isin or len(isin) != 12:
        return None
    name_obj = raw.get("name") or {}
    overview = raw.get("overview") or {}
    keydata = raw.get("keyData") or {}
    benchmark_obj = keydata.get("benchmark") or {}
    rep_obj = keydata.get("replicationMethod") or {}
    ccy_obj = overview.get("currency") or {}

    benchmark = (benchmark_obj.get("originalValue") or "").strip() or None
    name = (name_obj.get("originalValue") or "").strip() or None

    ter = overview.get("totalExpenseRatio")
    aum = overview.get("assetsUnderManagement")

    return {
        "isin": isin,
        "ter_pct": float(ter) if isinstance(ter, (int, float)) else None,
        "aum_eur": float(aum) if isinstance(aum, (int, float)) else None,
        "issuer_name": (keydata.get("issuer") or "").strip() or None,
        "replication_method": _normalize_replication(rep_obj.get("originalValue")),
        "index_tracked": benchmark,
        "fund_currency": (ccy_obj.get("originalValue") or "").strip().upper() or None,
        "sfdr_article": _normalize_sfdr(overview.get("sfdr")),
        "asset_class": _classify_asset_class(benchmark, name),
    }


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------

_UPDATE_SQL = """
    UPDATE sec.dim_etf d SET
        ter_pct            = COALESCE(v.ter_pct, d.ter_pct),
        aum_eur            = COALESCE(v.aum_eur, d.aum_eur),
        issuer_name        = COALESCE(v.issuer_name, d.issuer_name),
        replication_method = COALESCE(v.replication_method, d.replication_method),
        index_tracked      = COALESCE(v.index_tracked, d.index_tracked),
        fund_currency      = COALESCE(v.fund_currency, d.fund_currency),
        sfdr_article       = COALESCE(v.sfdr_article, d.sfdr_article),
        asset_class        = COALESCE(d.asset_class, v.asset_class),
        updated_at         = NOW()
    FROM (VALUES %s) AS v(isin, ter_pct, aum_eur, issuer_name,
                          replication_method, index_tracked, fund_currency,
                          sfdr_article, asset_class)
    WHERE d.isin = v.isin
"""


def _payload(rows: Iterable[dict[str, Any]]) -> list[tuple]:
    return [(
        r["isin"], r["ter_pct"], r["aum_eur"], r["issuer_name"],
        r["replication_method"], r["index_tracked"], r["fund_currency"],
        r["sfdr_article"], r["asset_class"],
    ) for r in rows]


def enrich_from_xetra(max_pages: int | None = None) -> dict[str, Any]:
    """Pull live Xetra ETF metadata and merge onto sec.dim_etf.

    Returns a dict with counts; never raises. Designed to be called from the
    `etf run` orchestrator without aborting downstream steps on failure.
    """
    try:
        raw = fetch_xetra_live(max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("xetra.fetch failed: %s", exc)
        return {"fetched": 0, "updated": 0, "error": 1, "message": str(exc)[:200]}
    rows = [n for n in (_normalize_row(r) for r in raw) if n is not None]
    if not rows:
        return {"fetched": 0, "updated": 0}
    payload = _payload(rows)
    try:
        with connect() as conn, conn.cursor() as cur:
            # Single batch so cur.rowcount reflects the full UPDATE, not the
            # tail of execute_values' default pagination (default page_size=100
            # returns only the last batch's rowcount).
            execute_values(cur, _UPDATE_SQL, payload, page_size=len(payload) or 1)
            updated = max(cur.rowcount, 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("xetra.update failed: %s", exc)
        return {"fetched": len(rows), "updated": 0, "error": 1, "message": str(exc)[:200]}
    return {"fetched": len(rows), "updated": updated}
