"""ETF profile enrichment via yfinance funds_data.

Pulls the data that characterises an ETF beyond price/TER/AUM:
  - clean display name (yfinance longName, much tidier than the FIRDS name)
  - fund family / issuer
  - asset-class split (stock/bond/cash/other)
  - top-10 holdings (symbol, name, weight) with optional company-logo mapping
  - sector, industry and credit-quality weightings when yfinance exposes them
  - portfolio valuation ratios (P/E, P/B)

Writes the current read-optimized tables and, by default, a dated snapshot layer.
Best-effort per ISIN: one failure never aborts the batch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import warnings
from datetime import date
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore")

SNAPSHOT_START_DATE = date(2026, 6, 26)

# yfinance sector keys -> display labels.
_SECTOR_LABELS = {
    "realestate": "Real Estate",
    "consumer_cyclical": "Consumer Cyclical",
    "basic_materials": "Basic Materials",
    "consumer_defensive": "Consumer Defensive",
    "technology": "Technology",
    "communication_services": "Communication Services",
    "financial_services": "Financial Services",
    "utilities": "Utilities",
    "industrials": "Industrials",
    "energy": "Energy",
    "healthcare": "Healthcare",
}

_WEIGHT_KEYS = (
    "Holding Percent",
    "Weight",
    "Weighting",
    "Percent",
    "Portfolio %",
    "Portfolio Percent",
)

_NAME_KEYS = ("Name", "Holding", "Holding Name", "Company", "Security", "Long Name")
_ISIN_KEYS = ("ISIN", "Isin", "isin")
_SYMBOL_KEYS = ("Symbol", "Ticker", "Ticker Symbol")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    return s


def _valid_isin(value: str | None) -> bool:
    """Return True when value passes the standard ISIN Luhn check digit."""
    s = (value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", s):
        return False
    expanded = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in s)
    total = 0
    parity = len(expanded) % 2
    for idx, ch in enumerate(expanded):
        digit = int(ch)
        if idx % 2 == parity:
            digit *= 2
            digit = digit // 10 + digit % 10
        total += digit
    return total % 10 == 0


def _first(row: Any, keys: tuple[str, ...]) -> Any:
    if not hasattr(row, "get"):
        return None
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and value != value:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            pass
    return str(value)


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _weight_from_row(row: Any) -> float | None:
    raw = _first(row, _WEIGHT_KEYS)
    value = _num(raw)
    if value is None:
        return None
    # yfinance usually returns decimals, but a few tables use 0..100.
    return value / 100.0 if value > 1.5 else value


def _weight_pairs(raw: Any, label_map: dict[str, str] | None = None) -> list[tuple[str, float | None]]:
    out: list[tuple[str, float | None]] = []
    if raw is None:
        return out
    label_map = label_map or {}
    if isinstance(raw, dict):
        iterator = raw.items()
        for key, value in iterator:
            weight = _num(value)
            if weight is None or weight <= 0:
                continue
            label = label_map.get(str(key), str(key).replace("_", " ").title())
            out.append((label, weight / 100.0 if weight > 1.5 else weight))
        return out
    if hasattr(raw, "iterrows"):
        for idx, row in raw.iterrows():
            label = _text(_first(row, ("Rating", "Credit Quality", "Industry", "Sector", "Name"))) or _text(idx)
            weight = _weight_from_row(row)
            if label and weight is not None and weight > 0:
                out.append((label_map.get(label, label.replace("_", " ").title()), weight))
    return out


def _holding_rows(raw: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if raw is None or not hasattr(raw, "iterrows"):
        return rows
    for rank, (idx, row) in enumerate(raw.iterrows(), start=1):
        symbol = _text(_first(row, _SYMBOL_KEYS)) or _text(idx)
        name = _text(_first(row, _NAME_KEYS))
        holding_isin = _text(_first(row, _ISIN_KEYS))
        weight = _weight_from_row(row)
        rows.append({
            "rank": rank,
            "symbol": symbol,
            "holding_isin": holding_isin,
            "name": name,
            "weight": weight,
            "cik": None,
            "edinet_code": None,
            "logo_url": None,
            "resolved_company_id": None,
            "resolution_source": None,
        })
    return rows


def _funds_attr(funds: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            value = getattr(funds, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _symbol_candidates(symbol: str | None) -> list[str]:
    if not symbol:
        return []
    raw = symbol.strip().upper()
    base = re.split(r"[.\s]", raw, maxsplit=1)[0]
    variants = [raw, base, raw.replace("-", "."), raw.replace(".", "-")]
    out: list[str] = []
    for item in variants:
        if item and item not in out:
            out.append(item)
    return out


def _resolve_holding_metadata(conn, holdings: list[dict[str, Any]]) -> None:
    """Mutates holdings in place with CIK/EDINET/logo fields when resolvable."""
    symbols = sorted({candidate for h in holdings for candidate in _symbol_candidates(h.get("symbol"))})
    isins = sorted({_text(h.get("holding_isin")).upper() for h in holdings if _text(h.get("holding_isin"))})
    if not symbols and not isins:
        return

    us_by_symbol: dict[str, dict[str, Any]] = {}
    us_by_isin: dict[str, dict[str, Any]] = {}
    jp_by_symbol: dict[str, dict[str, Any]] = {}
    jp_by_isin: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT upper(COALESCE(primary_ticker, '')) AS ticker,
                       upper(COALESCE(isin, '')) AS isin,
                       LPAD(cik::text, 10, '0') AS cik_padded
                FROM sec.dim_company_us
                WHERE upper(COALESCE(primary_ticker, '')) = ANY(%s)
                   OR upper(COALESCE(isin, '')) = ANY(%s)
                """,
                (symbols, isins),
            )
            for ticker, isin, cik_padded in cur.fetchall():
                row = {"cik": cik_padded, "resolved_company_id": cik_padded}
                if ticker:
                    us_by_symbol[ticker] = row
                if isin:
                    us_by_isin[isin] = row
        except Exception as exc:
            logger.debug("US company holding resolution skipped: %s", exc)
        try:
            cur.execute(
                """
                SELECT upper(COALESCE(primary_ticker, '')) AS ticker,
                       upper(COALESCE(isin, '')) AS isin,
                       edinet_code
                FROM sec.dim_company_jp
                WHERE upper(COALESCE(primary_ticker, '')) = ANY(%s)
                   OR upper(COALESCE(isin, '')) = ANY(%s)
                """,
                (symbols, isins),
            )
            for ticker, isin, edinet_code in cur.fetchall():
                row = {"edinet_code": edinet_code, "resolved_company_id": edinet_code}
                if ticker:
                    jp_by_symbol[ticker] = row
                if isin:
                    jp_by_isin[isin] = row
        except Exception as exc:
            logger.debug("JP company holding resolution skipped: %s", exc)

    for holding in holdings:
        symbol_keys = _symbol_candidates(holding.get("symbol"))
        holding_isin = _text(holding.get("holding_isin"))
        isin_key = holding_isin.upper() if holding_isin else None
        match = None
        source = None
        if isin_key and isin_key in us_by_isin:
            match, source = us_by_isin[isin_key], "dim_company_us:isin"
        elif isin_key and isin_key in jp_by_isin:
            match, source = jp_by_isin[isin_key], "dim_company_jp:isin"
        else:
            for key in symbol_keys:
                if key in us_by_symbol:
                    match, source = us_by_symbol[key], "dim_company_us:primary_ticker"
                    break
                if key in jp_by_symbol:
                    match, source = jp_by_symbol[key], "dim_company_jp:primary_ticker"
                    break
        if not match:
            continue
        holding.update(match)
        holding["resolution_source"] = source
        logo_id = match.get("cik") or match.get("edinet_code")
        if logo_id:
            holding["logo_url"] = f"/logos/{logo_id}.png"


def _profile_symbol(isin: str, yf_ticker: str | None = None) -> str:
    symbol = str(yf_ticker or "").strip().upper()
    return symbol if symbol and symbol != isin.upper() else isin


def fetch_etf_profile(isin: str, yf_ticker: str | None = None) -> dict[str, Any] | None:
    """Fetch a single ETF's profile via yfinance. Returns a dict or None."""
    import yfinance as yf

    symbol = _profile_symbol(isin, yf_ticker)
    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    funds = getattr(t, "funds_data", None)

    holdings: list[dict[str, Any]] = []
    sectors: list[tuple[str, float | None]] = []
    industries: list[tuple[str, float | None]] = []
    credit_quality: list[tuple[str, float | None]] = []
    stock_pct = bond_pct = cash_pct = other_pct = None
    pe = pb = None
    raw_payload: dict[str, Any] = {"info": info}

    if funds is not None:
        try:
            top_holdings = _funds_attr(funds, ("top_holdings",))
            raw_payload["top_holdings"] = _json_safe(top_holdings)
            holdings = _holding_rows(top_holdings)
        except Exception as exc:
            logger.debug("holdings fail %s: %s", isin, exc)
        try:
            sector_weightings = _funds_attr(funds, ("sector_weightings",))
            raw_payload["sector_weightings"] = _json_safe(sector_weightings)
            sectors = _weight_pairs(sector_weightings, _SECTOR_LABELS)
        except Exception as exc:
            logger.debug("sector fail %s: %s", isin, exc)
        try:
            industry_weightings = _funds_attr(funds, ("industry_weightings", "industry_weighting", "industries"))
            raw_payload["industry_weightings"] = _json_safe(industry_weightings)
            industries = _weight_pairs(industry_weightings)
        except Exception as exc:
            logger.debug("industry fail %s: %s", isin, exc)
        try:
            ratings = _funds_attr(funds, ("bond_ratings", "credit_quality", "credit_quality_weightings"))
            raw_payload["credit_quality"] = _json_safe(ratings)
            credit_quality = _weight_pairs(ratings)
        except Exception as exc:
            logger.debug("credit quality fail %s: %s", isin, exc)
        try:
            ac = funds.asset_classes or {}
            raw_payload["asset_classes"] = _json_safe(ac)
            stock_pct = _num(ac.get("stockPosition"))
            bond_pct = _num(ac.get("bondPosition"))
            cash_pct = _num(ac.get("cashPosition"))
            other_pct = _num(ac.get("otherPosition"))
        except Exception:
            pass
        try:
            eh = funds.equity_holdings
            raw_payload["equity_holdings"] = _json_safe(eh)
            if eh is not None and hasattr(eh, "loc"):
                col = eh.columns[0]
                pe = _num(eh.loc["Price/Earnings", col]) if "Price/Earnings" in eh.index else None
                pb = _num(eh.loc["Price/Book", col]) if "Price/Book" in eh.index else None
        except Exception:
            pass
        try:
            raw_payload["bond_holdings"] = _json_safe(_funds_attr(funds, ("bond_holdings",)))
        except Exception:
            pass

    family = None
    if funds is not None:
        try:
            overview = funds.fund_overview or {}
            raw_payload["fund_overview"] = _json_safe(overview)
            family = overview.get("family")
        except Exception:
            family = None

    missing_flags = {
        "missing_holdings": not bool(holdings),
        "missing_sector": not bool(sectors),
        "missing_industry": not bool(industries),
        "missing_credit_quality": not bool(credit_quality),
        "fixed_income_limit": bool((bond_pct or 0) >= 0.2 and not credit_quality),
        "mixed_fund_limit": bool((other_pct or 0) >= 0.2 or ((stock_pct is not None) and stock_pct < 0.6)),
    }

    profile = {
        "isin": isin,
        "clean_name": info.get("longName") or None,
        "fund_family": family,
        "category": info.get("category") or None,
        "yf_ticker": symbol,
        "stock_pct": stock_pct,
        "bond_pct": bond_pct,
        "cash_pct": cash_pct,
        "other_pct": other_pct,
        "pe_ratio": pe,
        "pb_ratio": pb,
        "holdings_count": info.get("holdings") if isinstance(info.get("holdings"), int) else None,
        "holdings": holdings,
        "sectors": sectors,
        "industries": industries,
        "credit_quality": credit_quality,
        "missing_flags": missing_flags,
        "raw_payload": raw_payload,
        "source_payload_hash": _payload_hash(raw_payload),
    }
    has_data = bool(holdings or sectors or industries or credit_quality or profile["clean_name"])
    profile["_status"] = "complete" if has_data else "empty"
    return profile


def _write_profile(
    conn,
    p: dict[str, Any],
    *,
    as_of_date: date,
    write_snapshots: bool,
) -> None:
    _resolve_holding_metadata(conn, p["holdings"])
    missing_json = json.dumps(_json_safe(p["missing_flags"]), ensure_ascii=False, sort_keys=True)
    raw_json = json.dumps(_json_safe(p["raw_payload"]), ensure_ascii=False, sort_keys=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sec.dim_etf_profile
                (isin, clean_name, fund_family, category, yf_ticker, stock_pct, bond_pct,
                 cash_pct, other_pct, pe_ratio, pb_ratio, holdings_count, profile_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (isin) DO UPDATE SET
                clean_name=EXCLUDED.clean_name, fund_family=EXCLUDED.fund_family,
                category=EXCLUDED.category, yf_ticker=EXCLUDED.yf_ticker,
                stock_pct=EXCLUDED.stock_pct, bond_pct=EXCLUDED.bond_pct,
                cash_pct=EXCLUDED.cash_pct, other_pct=EXCLUDED.other_pct,
                pe_ratio=EXCLUDED.pe_ratio, pb_ratio=EXCLUDED.pb_ratio,
                holdings_count=EXCLUDED.holdings_count, profile_status=EXCLUDED.profile_status,
                updated_at=NOW()
            """,
            (p["isin"], p["clean_name"], p["fund_family"], p["category"], p["yf_ticker"],
             p["stock_pct"], p["bond_pct"], p["cash_pct"], p["other_pct"],
             p["pe_ratio"], p["pb_ratio"], p["holdings_count"], p["_status"]),
        )
        if p["holdings"]:
            cur.execute("DELETE FROM sec.etf_holding WHERE isin=%s", (p["isin"],))
            execute_values(
                cur,
                """
                INSERT INTO sec.etf_holding
                    (isin, rank, symbol, holding_isin, name, weight, cik, edinet_code,
                     logo_url, resolved_company_id, resolution_source)
                VALUES %s
                """,
                [
                    (
                        p["isin"], h["rank"], h["symbol"], h["holding_isin"], h["name"], h["weight"],
                        h["cik"], h["edinet_code"], h["logo_url"], h["resolved_company_id"], h["resolution_source"],
                    )
                    for h in p["holdings"]
                ],
            )
        if p["sectors"]:
            cur.execute("DELETE FROM sec.etf_sector_weight WHERE isin=%s", (p["isin"],))
            execute_values(
                cur,
                "INSERT INTO sec.etf_sector_weight (isin,sector,weight) VALUES %s",
                [(p["isin"], sec, w) for (sec, w) in p["sectors"]],
            )
        if p["industries"]:
            cur.execute("DELETE FROM sec.etf_industry_weight WHERE isin=%s", (p["isin"],))
            execute_values(
                cur,
                "INSERT INTO sec.etf_industry_weight (isin,industry,weight) VALUES %s",
                [(p["isin"], industry, w) for (industry, w) in p["industries"]],
            )
        if p["credit_quality"]:
            cur.execute("DELETE FROM sec.etf_credit_quality_weight WHERE isin=%s", (p["isin"],))
            execute_values(
                cur,
                "INSERT INTO sec.etf_credit_quality_weight (isin,rating,weight) VALUES %s",
                [(p["isin"], rating, w) for (rating, w) in p["credit_quality"]],
            )
        if not write_snapshots:
            return
        cur.execute(
            """
            INSERT INTO sec.etf_profile_snapshot
                (isin, as_of_date, source, source_payload_hash, clean_name, fund_family,
                 category, yf_ticker, stock_pct, bond_pct, cash_pct, other_pct,
                 pe_ratio, pb_ratio, holdings_count, profile_status, missing_flags, raw_payload)
            VALUES (%s,%s,'yfinance',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT (isin, as_of_date, source) DO UPDATE SET
                fetched_at=NOW(),
                source_payload_hash=EXCLUDED.source_payload_hash,
                clean_name=EXCLUDED.clean_name,
                fund_family=EXCLUDED.fund_family,
                category=EXCLUDED.category,
                yf_ticker=EXCLUDED.yf_ticker,
                stock_pct=EXCLUDED.stock_pct,
                bond_pct=EXCLUDED.bond_pct,
                cash_pct=EXCLUDED.cash_pct,
                other_pct=EXCLUDED.other_pct,
                pe_ratio=EXCLUDED.pe_ratio,
                pb_ratio=EXCLUDED.pb_ratio,
                holdings_count=EXCLUDED.holdings_count,
                profile_status=EXCLUDED.profile_status,
                missing_flags=EXCLUDED.missing_flags,
                raw_payload=EXCLUDED.raw_payload
            """,
            (
                p["isin"], as_of_date, p["source_payload_hash"], p["clean_name"], p["fund_family"],
                p["category"], p["yf_ticker"], p["stock_pct"], p["bond_pct"], p["cash_pct"],
                p["other_pct"], p["pe_ratio"], p["pb_ratio"], p["holdings_count"], p["_status"],
                missing_json, raw_json,
            ),
        )
        if p["holdings"]:
            execute_values(
                cur,
                """
                INSERT INTO sec.etf_holding_snapshot
                    (isin, as_of_date, source, rank, symbol, holding_isin, name, weight,
                     cik, edinet_code, logo_url, resolved_company_id, resolution_source)
                VALUES %s
                ON CONFLICT (isin, as_of_date, source, rank) DO UPDATE SET
                    symbol=EXCLUDED.symbol,
                    holding_isin=EXCLUDED.holding_isin,
                    name=EXCLUDED.name,
                    weight=EXCLUDED.weight,
                    cik=EXCLUDED.cik,
                    edinet_code=EXCLUDED.edinet_code,
                    logo_url=EXCLUDED.logo_url,
                    resolved_company_id=EXCLUDED.resolved_company_id,
                    resolution_source=EXCLUDED.resolution_source,
                    fetched_at=NOW()
                """,
                [
                    (
                        p["isin"], as_of_date, "yfinance", h["rank"], h["symbol"], h["holding_isin"],
                        h["name"], h["weight"], h["cik"], h["edinet_code"], h["logo_url"],
                        h["resolved_company_id"], h["resolution_source"],
                    )
                    for h in p["holdings"]
                ],
            )
        if p["sectors"]:
            execute_values(
                cur,
                """
                INSERT INTO sec.etf_sector_weight_snapshot (isin, as_of_date, source, sector, weight)
                VALUES %s
                ON CONFLICT (isin, as_of_date, source, sector) DO UPDATE SET
                    weight=EXCLUDED.weight, fetched_at=NOW()
                """,
                [(p["isin"], as_of_date, "yfinance", sec, w) for (sec, w) in p["sectors"]],
            )
        if p["industries"]:
            execute_values(
                cur,
                """
                INSERT INTO sec.etf_industry_weight_snapshot (isin, as_of_date, source, industry, weight)
                VALUES %s
                ON CONFLICT (isin, as_of_date, source, industry) DO UPDATE SET
                    weight=EXCLUDED.weight, fetched_at=NOW()
                """,
                [(p["isin"], as_of_date, "yfinance", industry, w) for (industry, w) in p["industries"]],
            )
        if p["credit_quality"]:
            execute_values(
                cur,
                """
                INSERT INTO sec.etf_credit_quality_weight_snapshot (isin, as_of_date, source, rating, weight)
                VALUES %s
                ON CONFLICT (isin, as_of_date, source, rating) DO UPDATE SET
                    weight=EXCLUDED.weight, fetched_at=NOW()
                """,
                [(p["isin"], as_of_date, "yfinance", rating, w) for (rating, w) in p["credit_quality"]],
            )


def enrich_profiles(
    limit: int | None = None,
    only_missing: bool = True,
    *,
    write_snapshots: bool = True,
    as_of_date: date | str | None = None,
) -> dict[str, int]:
    """Fetch + store profiles for active ETFs. Returns summary counts."""
    snapshot_date = date.fromisoformat(as_of_date) if isinstance(as_of_date, str) else (as_of_date or SNAPSHOT_START_DATE)
    where = "WHERE COALESCE(d.is_active,TRUE)=TRUE"
    if only_missing:
        where += (" AND NOT EXISTS (SELECT 1 FROM sec.dim_etf_profile p "
                  "WHERE p.isin=d.isin AND p.profile_status='complete')")
    sql = f"""
        SELECT d.isin, p.yf_ticker
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        {where}
        ORDER BY d.aum_eur DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = [(r[0], r[1]) for r in cur.fetchall()]

    ok = empty = failed = 0
    for isin, yf_ticker in rows:
        try:
            p = fetch_etf_profile(isin, yf_ticker)
            if p is None:
                failed += 1
                continue
            with connect() as conn:
                _write_profile(conn, p, as_of_date=snapshot_date, write_snapshots=write_snapshots)
            if p["_status"] == "complete":
                ok += 1
            else:
                empty += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("profile %s failed: %s", isin, str(exc)[:160])
    return {"requested": len(rows), "ok": ok, "empty": empty, "failed": failed}


def backfill_bond_ratings(
    limit: int | None = None,
    only_missing: bool = True,
    *,
    write_snapshots: bool = True,
    as_of_date: date | str | None = None,
) -> dict[str, int]:
    """Fetch credit-quality ratings for bond-like ETFs and write current/snapshot rows."""
    snapshot_date = date.fromisoformat(as_of_date) if isinstance(as_of_date, str) else (as_of_date or SNAPSHOT_START_DATE)
    bond_like = """
        (
            COALESCE(d.asset_class, '') = 'Fixed Income'
            OR COALESCE(p.bond_pct, 0) >= 0.20
            OR LOWER(
                COALESCE(d.full_name, '') || ' ' ||
                COALESCE(d.short_name, '') || ' ' ||
                COALESCE(d.index_tracked, '') || ' ' ||
                COALESCE(p.category, '')
            ) ~ '(bond|treasury|government|aggregate|credit|high yield|gilt|fixed income)'
        )
    """
    where = ["""
        COALESCE(d.is_active, TRUE)
        AND {bond_like}
    """.format(bond_like=bond_like)]
    params: list[Any] = []
    if only_missing:
        missing = [
            "("
            "NOT EXISTS (SELECT 1 FROM sec.etf_credit_quality_weight q WHERE q.isin = d.isin) "
            "AND NOT EXISTS (SELECT 1 FROM sec.etf_profile_snapshot ps "
            "WHERE ps.isin = d.isin AND ps.as_of_date = %s AND ps.source = 'yfinance')"
            ")",
        ]
        params.append(snapshot_date)
        if write_snapshots:
            missing.append(
                "("
                "EXISTS (SELECT 1 FROM sec.etf_credit_quality_weight q WHERE q.isin = d.isin) "
                "AND NOT EXISTS (SELECT 1 FROM sec.etf_credit_quality_weight_snapshot qs "
                "WHERE qs.isin = d.isin AND qs.as_of_date = %s AND qs.source = 'yfinance')"
                ")"
            )
            params.append(snapshot_date)
        where.append("(" + " OR ".join(missing) + ")")

    sql = f"""
        SELECT d.isin, p.yf_ticker
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE {' AND '.join(where)}
        ORDER BY d.aum_eur DESC NULLS LAST, d.isin
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        raw_rows = [(r[0], r[1]) for r in cur.fetchall()]

    rows = [(isin, yf_ticker) for isin, yf_ticker in raw_rows if _valid_isin(isin)]
    skipped_invalid = len(raw_rows) - len(rows)

    ok = empty = failed = rated = unrated = 0
    for isin, yf_ticker in rows:
        try:
            p = fetch_etf_profile(isin, yf_ticker)
            if p is None:
                failed += 1
                continue
            with connect() as conn:
                _write_profile(conn, p, as_of_date=snapshot_date, write_snapshots=write_snapshots)
            if p["_status"] == "complete":
                ok += 1
            else:
                empty += 1
            if p["credit_quality"]:
                rated += 1
            else:
                unrated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("bond rating profile %s failed: %s", isin, str(exc)[:160])
    return {
        "requested": len(rows),
        "ok": ok,
        "empty": empty,
        "rated": rated,
        "unrated": unrated,
        "skipped_invalid": skipped_invalid,
        "failed": failed,
    }
