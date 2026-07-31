"""justETF metadata acquisition for ETF symbol/listing fallback.

The adapter is intentionally limited to profile metadata and symbol mapping.
It does not automate justETF price-history downloads; licensed price exports
are handled by the explicit CSV adapter in ``price_sources.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
import csv
import html
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings


JUSTETF_PROFILE_URL = "https://www.justetf.com/en/etf-profile.html?isin={isin}"
LEGAL_PRICE_SOURCE = "metadata_only_unlicensed"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}


@dataclass(frozen=True)
class JustEtfProfile:
    isin: str
    provider_id: str | None = None
    justetf_found: bool = False
    primary_ticker: str | None = None
    wkn: str | None = None
    xetra_symbol: str | None = None
    ric: str | None = None
    clean_name: str | None = None
    fund_family: str | None = None
    ter_pct: float | None = None
    aum_eur: float | None = None
    inception_date: date | None = None
    distribution_policy: str | None = None
    replication_method: str | None = None
    fund_domicile: str | None = None
    fund_currency: str | None = None
    factsheet_url: str | None = None
    kid_url: str | None = None
    documents: dict[str, str] | None = None
    history_available_hint: bool = False
    legal_price_source: str = LEGAL_PRICE_SOURCE
    raw_payload: dict[str, Any] | None = None


def parse_justetf_profile_html(html_text: str, isin: str, *, provider_id: str | None = None) -> JustEtfProfile:
    """Parse justETF profile HTML/embedded JSON into metadata fields."""
    isin = isin.strip().upper()
    raw = html.unescape(html_text or "")
    flat = _flat_text(raw)
    found = isin in raw.upper() or "ETF PROFILE" in flat.upper()
    payload = _embedded_payload(raw)

    primary_ticker = _first_clean(
        _json_field(payload, "ticker"),
        _json_field(payload, "tickerSymbol"),
        _json_field(payload, "symbol"),
        _ticker_from_meta_description(raw, isin),
        _label_value(flat, "Ticker"),
    )
    if primary_ticker and _looks_like_isin(primary_ticker):
        primary_ticker = None
    primary_ticker = _clean_ticker(primary_ticker)

    wkn = _first_clean(_json_field(payload, "wkn"), _json_field(payload, "WKN"), _label_value(flat, "WKN"))
    clean_name = _first_clean(
        _json_field(payload, "name"),
        _json_field(payload, "fundName"),
        _json_field(payload, "etfName"),
        _meta_content(raw, "og:title"),
    )
    clean_name = _clean_name(clean_name)
    fund_family = _clean_provider(_first_clean(
        _json_field(payload, "fundProvider"),
        _json_field(payload, "provider"),
        _json_field(payload, "issuer"),
        _label_value(flat, "Fund Provider"),
        _label_value(flat, "ETF provider"),
    ))
    ter_pct = _parse_percent(_first_clean(_json_field(payload, "ter"), _json_field(payload, "totalExpenseRatio"), _label_value(flat, "TER")))
    aum_eur = _parse_money(_first_clean(_json_field(payload, "aum"), _json_field(payload, "assetsUnderManagement"), _label_value(flat, "Fund size")))
    inception = _parse_date(
        _first_clean(
            _json_field(payload, "inceptionDate"),
            _json_field(payload, "launchDate"),
            _label_value(flat, "Inception"),
            _label_value(flat, "Fund launch"),
        )
    )
    distribution = _distribution_from_name(clean_name) or _clean_distribution(_first_clean(
        _json_field(payload, "distributionPolicy"),
        _json_field(payload, "distribution"),
        _label_value(flat, "Distribution policy"),
        _label_value(flat, "Use of profits"),
    ))
    replication = _clean_replication(_first_clean(
        _json_field(payload, "replicationMethod"),
        _json_field(payload, "replication"),
        _label_value(flat, "Replication method"),
    ))
    domicile = _first_clean(_json_field(payload, "domicile"), _json_field(payload, "fundDomicile"), _label_value(flat, "Fund domicile"))
    currency = _currency(_first_clean(_json_field(payload, "fundCurrency"), _json_field(payload, "currency"), _label_value(flat, "Fund currency")))
    ric = _first_clean(_json_field(payload, "ric"), _json_field(payload, "reutersCode"), _json_field(payload, "reutersInstrumentCode"))
    ric = _clean_symbol(ric)
    xetra_symbol = _first_clean(_json_field(payload, "xetraSymbol"), _json_field(payload, "xetraTicker"))
    xetra_symbol = _clean_symbol(xetra_symbol)
    if not xetra_symbol and primary_ticker:
        xetra_symbol = f"{primary_ticker}.DE"
    if not ric and xetra_symbol:
        ric = xetra_symbol

    documents = _document_links(raw)
    factsheet_url = _first_clean(documents.get("factsheet"), _json_field(payload, "factsheetUrl"))
    kid_url = _first_clean(documents.get("kid"), _json_field(payload, "kidUrl"), _json_field(payload, "kiidUrl"))
    history_hint = any(token in flat.lower() for token in ("performance", "chart", "1 year", "ytd", "return"))

    return JustEtfProfile(
        isin=isin,
        provider_id=provider_id,
        justetf_found=found,
        primary_ticker=primary_ticker,
        wkn=_clean_symbol(wkn),
        xetra_symbol=xetra_symbol,
        ric=ric,
        clean_name=clean_name,
        fund_family=fund_family,
        ter_pct=ter_pct,
        aum_eur=aum_eur,
        inception_date=inception,
        distribution_policy=distribution,
        replication_method=replication,
        fund_domicile=domicile,
        fund_currency=currency,
        factsheet_url=factsheet_url,
        kid_url=kid_url,
        documents=documents,
        history_available_hint=history_hint,
        raw_payload=payload,
    )


def fetch_justetf_profile(isin: str, *, provider_id: str | None = None, client: httpx.Client | None = None) -> JustEtfProfile:
    url = JUSTETF_PROFILE_URL.format(isin=isin.strip().upper())
    owns_client = client is None
    http = client or httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True)
    try:
        response = http.get(url)
        response.raise_for_status()
        return parse_justetf_profile_html(response.text, isin, provider_id=provider_id)
    finally:
        if owns_client:
            http.close()


def select_justetf_metadata_targets(
    limit: int | None = None,
    *,
    only_unpriced: bool = True,
    isin: str | None = None,
) -> list[tuple[str, str | None]]:
    where = ["COALESCE(d.is_active, TRUE)"]
    params: list[object] = []
    if isin:
        where.append("d.isin = %s")
        params.append(isin.strip().upper())
    if only_unpriced:
        where.append("NOT EXISTS (SELECT 1 FROM sec.fact_prices_etf fp WHERE fp.isin = d.isin)")
    sql = f"""
        SELECT d.isin, d.provider_id
        FROM sec.dim_etf d
        WHERE {" AND ".join(where)}
        ORDER BY d.provider_id NULLS LAST, d.isin
    """
    if limit is not None:
        sql += f"\n        LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        return [(row[0], row[1]) for row in cur.fetchall()]


def upsert_justetf_profiles(profiles: Iterable[JustEtfProfile]) -> int:
    rows = []
    for profile in profiles:
        documents = profile.documents or {}
        raw_payload = profile.raw_payload or {}
        rows.append(
            (
                profile.isin,
                profile.provider_id,
                profile.justetf_found,
                profile.primary_ticker,
                profile.wkn,
                profile.xetra_symbol,
                profile.ric,
                profile.clean_name,
                profile.fund_family,
                profile.ter_pct,
                profile.aum_eur,
                profile.inception_date,
                profile.distribution_policy,
                profile.replication_method,
                profile.fund_domicile,
                profile.fund_currency,
                profile.factsheet_url,
                profile.kid_url,
                json.dumps(documents, ensure_ascii=False, sort_keys=True),
                profile.history_available_hint,
                profile.legal_price_source,
                json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
            )
        )
    if not rows:
        return 0
    with connect() as conn, conn.cursor() as cur:
        inserted = execute_values(
            cur,
            """
            INSERT INTO sec.etf_justetf_profile
                (isin, provider_id, justetf_found, primary_ticker, wkn, xetra_symbol,
                 ric, clean_name, fund_family, ter_pct, aum_eur, inception_date,
                 distribution_policy, replication_method, fund_domicile, fund_currency,
                 factsheet_url, kid_url, documents, history_available_hint,
                 legal_price_source, raw_payload)
            VALUES %s
            ON CONFLICT (isin) DO UPDATE SET
                provider_id = COALESCE(EXCLUDED.provider_id, sec.etf_justetf_profile.provider_id),
                justetf_found = EXCLUDED.justetf_found,
                primary_ticker = COALESCE(EXCLUDED.primary_ticker, sec.etf_justetf_profile.primary_ticker),
                wkn = COALESCE(EXCLUDED.wkn, sec.etf_justetf_profile.wkn),
                xetra_symbol = COALESCE(EXCLUDED.xetra_symbol, sec.etf_justetf_profile.xetra_symbol),
                ric = COALESCE(EXCLUDED.ric, sec.etf_justetf_profile.ric),
                clean_name = COALESCE(EXCLUDED.clean_name, sec.etf_justetf_profile.clean_name),
                fund_family = COALESCE(EXCLUDED.fund_family, sec.etf_justetf_profile.fund_family),
                ter_pct = COALESCE(EXCLUDED.ter_pct, sec.etf_justetf_profile.ter_pct),
                aum_eur = COALESCE(EXCLUDED.aum_eur, sec.etf_justetf_profile.aum_eur),
                inception_date = COALESCE(EXCLUDED.inception_date, sec.etf_justetf_profile.inception_date),
                distribution_policy = COALESCE(EXCLUDED.distribution_policy, sec.etf_justetf_profile.distribution_policy),
                replication_method = COALESCE(EXCLUDED.replication_method, sec.etf_justetf_profile.replication_method),
                fund_domicile = COALESCE(EXCLUDED.fund_domicile, sec.etf_justetf_profile.fund_domicile),
                fund_currency = COALESCE(EXCLUDED.fund_currency, sec.etf_justetf_profile.fund_currency),
                factsheet_url = COALESCE(EXCLUDED.factsheet_url, sec.etf_justetf_profile.factsheet_url),
                kid_url = COALESCE(EXCLUDED.kid_url, sec.etf_justetf_profile.kid_url),
                documents = CASE
                    WHEN EXCLUDED.documents = '{}'::jsonb THEN sec.etf_justetf_profile.documents
                    ELSE sec.etf_justetf_profile.documents || EXCLUDED.documents
                END,
                history_available_hint = EXCLUDED.history_available_hint,
                legal_price_source = EXCLUDED.legal_price_source,
                raw_payload = EXCLUDED.raw_payload,
                fetched_at = NOW(),
                updated_at = NOW()
            """,
            rows,
            page_size=500,
        )
        _merge_justetf_master_data(cur)
    return inserted


def _merge_justetf_master_data(cur: Any) -> None:
    cur.execute(
        """
        UPDATE sec.dim_etf d SET
            wkn = COALESCE(d.wkn, j.wkn),
            ter_pct = COALESCE(d.ter_pct, j.ter_pct),
            aum_eur = COALESCE(d.aum_eur, j.aum_eur),
            inception_date = COALESCE(d.inception_date, j.inception_date),
            replication_method = COALESCE(d.replication_method, LEFT(j.replication_method, 50)),
            fund_currency = COALESCE(d.fund_currency, j.fund_currency),
            distribution_policy = COALESCE(d.distribution_policy, j.distribution_policy),
            fund_domicile = COALESCE(d.fund_domicile, j.fund_domicile),
            metadata_sources = COALESCE(d.metadata_sources, '{}'::jsonb)
                || jsonb_build_object('justetf_metadata', jsonb_build_object(
                    'legal_price_source', j.legal_price_source,
                    'history_available_hint', j.history_available_hint,
                    'xetra_symbol', j.xetra_symbol,
                    'ric', j.ric
                )),
            updated_at = NOW()
        FROM sec.etf_justetf_profile j
        WHERE d.isin = j.isin
          AND j.justetf_found
        """
    )
    cur.execute(
        """
        INSERT INTO sec.dim_etf_profile
            (isin, clean_name, fund_family, factsheet_url, kid_url, profile_status, updated_at)
        SELECT j.isin, j.clean_name, j.fund_family, j.factsheet_url, j.kid_url, 'pending', NOW()
        FROM sec.etf_justetf_profile j
        WHERE j.justetf_found
        ON CONFLICT (isin) DO UPDATE SET
            clean_name = COALESCE(sec.dim_etf_profile.clean_name, EXCLUDED.clean_name),
            fund_family = COALESCE(sec.dim_etf_profile.fund_family, EXCLUDED.fund_family),
            factsheet_url = COALESCE(sec.dim_etf_profile.factsheet_url, EXCLUDED.factsheet_url),
            kid_url = COALESCE(sec.dim_etf_profile.kid_url, EXCLUDED.kid_url),
            profile_status = CASE
                WHEN sec.dim_etf_profile.profile_status IN ('pending', 'empty', 'failed')
                THEN 'pending'
                ELSE sec.dim_etf_profile.profile_status
            END,
            updated_at = NOW()
        """
    )
    cur.execute(
        """
        UPDATE sec.dim_etf_listing l SET
            exchange_ticker = LEFT(j.primary_ticker, 20)
        FROM sec.etf_justetf_profile j
        WHERE l.isin = j.isin
          AND j.justetf_found
          AND j.primary_ticker IS NOT NULL
          AND l.mic IN ('XETR', 'GETT', 'XFRA', 'XSTU', 'XMUN', 'XDUS', 'XHAM', 'XHAN', 'XBER', 'TGAT')
          AND (l.exchange_ticker IS NULL OR BTRIM(l.exchange_ticker) = '')
        """
    )


def run_justetf_metadata_audit(
    *,
    limit: int | None = None,
    only_unpriced: bool = True,
    isin: str | None = None,
    apply: bool = False,
    output_csv: str | Path | None = None,
    sleep_seconds: float = 0.8,
) -> dict[str, int | str]:
    targets = select_justetf_metadata_targets(limit=limit, only_unpriced=only_unpriced, isin=isin)
    out_path = Path(output_csv) if output_csv else load_settings().project_root / "artifacts" / "etf_justetf_metadata_audit.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    profiles: list[JustEtfProfile] = []
    errors = 0
    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as client:
        for idx, (isin, provider_id) in enumerate(targets, start=1):
            try:
                profiles.append(fetch_justetf_profile(isin, provider_id=provider_id, client=client))
            except Exception as exc:  # noqa: BLE001 - batch audit should continue
                errors += 1
                profiles.append(
                    JustEtfProfile(
                        isin=isin,
                        provider_id=provider_id,
                        justetf_found=False,
                        raw_payload={"error": str(exc)[:300]},
                    )
                )
            if sleep_seconds > 0 and idx < len(targets):
                time.sleep(sleep_seconds)

    _write_audit_csv(out_path, profiles)
    upserted = upsert_justetf_profiles(profiles) if apply else 0
    return {
        "targets": len(targets),
        "found": sum(1 for p in profiles if p.justetf_found),
        "with_ticker": sum(1 for p in profiles if p.primary_ticker),
        "with_history_hint": sum(1 for p in profiles if p.history_available_hint),
        "errors": errors,
        "upserted": upserted,
        "csv": str(out_path),
    }


def _write_audit_csv(path: Path, profiles: Iterable[JustEtfProfile]) -> None:
    fields = [
        "isin",
        "provider_id",
        "justetf_found",
        "primary_ticker",
        "wkn",
        "xetra_symbol",
        "ric",
        "clean_name",
        "fund_family",
        "ter_pct",
        "aum_eur",
        "inception_date",
        "distribution_policy",
        "replication_method",
        "fund_domicile",
        "fund_currency",
        "factsheet_url",
        "kid_url",
        "history_available_hint",
        "legal_price_source",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for profile in profiles:
            row = asdict(profile)
            if profile.inception_date:
                row["inception_date"] = profile.inception_date.isoformat()
            writer.writerow({field: row.get(field) for field in fields})


def _embedded_payload(raw: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', raw, re.I | re.S):
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        payload.update(_flatten_json(data))
    payload.update(_regex_json_fields(raw))
    return payload


def _flatten_json(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            text_key = str(key)
            out[text_key] = item
            out.update(_flatten_json(item, text_key))
    elif isinstance(value, list):
        for item in value:
            out.update(_flatten_json(item, prefix))
    return out


def _regex_json_fields(raw: str) -> dict[str, str]:
    fields = (
        "isin",
        "ticker",
        "tickerSymbol",
        "symbol",
        "wkn",
        "name",
        "fundName",
        "etfName",
        "fundProvider",
        "provider",
        "issuer",
        "ter",
        "totalExpenseRatio",
        "aum",
        "assetsUnderManagement",
        "inceptionDate",
        "launchDate",
        "distributionPolicy",
        "distribution",
        "replicationMethod",
        "replication",
        "domicile",
        "fundDomicile",
        "fundCurrency",
        "currency",
        "ric",
        "reutersCode",
        "reutersInstrumentCode",
        "xetraSymbol",
        "xetraTicker",
        "factsheetUrl",
        "kidUrl",
        "kiidUrl",
    )
    out: dict[str, str] = {}
    for key in fields:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"(.*?)"', raw, re.I | re.S)
        if match:
            out[key] = html.unescape(match.group(1))
    return out


def _json_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        for item_key, item_value in payload.items():
            if item_key.lower() == key.lower():
                value = item_value
                break
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _flat_text(raw: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _label_value(flat: str, label: str) -> str | None:
    pattern = rf"\b{re.escape(label)}\b\s*[:\-]?\s+([^|]{{1,120}})"
    match = re.search(pattern, flat, flags=re.I)
    if not match:
        return None
    value = match.group(1)
    value = re.split(
        r"\b(ISIN|WKN|Ticker|TER|Fund size|Inception|Fund launch|Fund Provider|ETF provider|Replication method|Distribution policy|Fund domicile|Fund currency)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return value.strip()


def _meta_content(raw: str, prop: str) -> str | None:
    match = re.search(rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']', raw, re.I | re.S)
    return html.unescape(match.group(1)) if match else None


def _meta_name_content(raw: str, name: str) -> str | None:
    match = re.search(rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']', raw, re.I | re.S)
    return html.unescape(match.group(1)) if match else None


def _ticker_from_meta_description(raw: str, isin: str) -> str | None:
    description = _first_clean(_meta_name_content(raw, "description"), _meta_content(raw, "og:description"))
    if not description:
        return None
    match = re.search(r"\(([A-Z0-9.]{2,20})\s*\|\s*" + re.escape(isin.upper()) + r"\)", description, flags=re.I)
    return match.group(1) if match else None


def _document_links(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for href in re.findall(r'href=["\'](.*?)["\']', raw, flags=re.I | re.S):
        clean = html.unescape(href)
        lower = clean.lower()
        if "factsheet" in lower and "factsheet" not in out:
            out["factsheet"] = _absolute_justetf_url(clean)
        elif any(token in lower for token in ("kid", "kiid", "key-information")) and "kid" not in out:
            out["kid"] = _absolute_justetf_url(clean)
        elif "prospectus" in lower and "prospectus" not in out:
            out["prospectus"] = _absolute_justetf_url(clean)
    return out


def _absolute_justetf_url(value: str) -> str:
    if value.startswith("http"):
        return value
    if value.startswith("/"):
        return "https://www.justetf.com" + value
    return value


def _first_clean(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text:
            return text
    return None


def _clean_symbol(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    text = re.sub(r"[^A-Za-z0-9.]+", "", text).upper()
    return text or None


def _clean_ticker(value: str | None) -> str | None:
    text = _clean_symbol(value)
    if not text:
        return None
    text = text.split(".", 1)[0]
    if len(text) > 20 or _looks_like_isin(text):
        return None
    return text


def _clean_name(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    text = re.sub(r"\s*\|\s*justETF.*$", "", text, flags=re.I).strip()
    text = re.sub(r"\s*\|\s*[A-Z0-9]{5,}\s*\|\s*[A-Z]{2}[A-Z0-9]{9}[0-9]\s*$", "", text).strip()
    return text or None


def _clean_provider(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    text = re.split(r"\b(Legal structure|Fund Structure|UCITS compliance|Administrator)\b", text, maxsplit=1, flags=re.I)[0]
    return text.strip()[:120] or None


def _clean_distribution(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    lower = text.lower()
    if len(text) > 120 and ("article" in lower or "question" in lower):
        return None
    if "accumulat" in lower:
        return "Accumulating"
    if "distribut" in lower:
        return "Distributing"
    return text[:80]


def _distribution_from_name(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    if re.search(r"\b(dist|distributing|income|inc)\b", text, flags=re.I):
        return "Distributing"
    if re.search(r"\b(acc|accumulating)\b", text, flags=re.I):
        return "Accumulating"
    return None


def _clean_replication(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    lower = text.lower()
    if "physical" in lower or "physically" in lower or "full replication" in lower:
        return "Physical"
    if "synthetic" in lower or "swap" in lower:
        return "Synthetic"
    if "sampling" in lower:
        return "Sampling"
    return text[:50]


def _looks_like_isin(value: str | None) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", str(value or "").strip().upper()))


def _parse_percent(value: str | None) -> float | None:
    text = _first_clean(value)
    if not text:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    parsed = float(match.group(0).replace(",", "."))
    return parsed / 100.0 if "%" in text or parsed > 1 else parsed


def _parse_money(value: str | None) -> float | None:
    text = _first_clean(value)
    if not text:
        return None
    multiplier = 1.0
    lower = text.lower()
    if "bn" in lower or "billion" in lower:
        multiplier = 1_000_000_000.0
    elif "m" in lower or "million" in lower:
        multiplier = 1_000_000.0
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    return float(match.group(0).replace(",", ".")) * multiplier


def _parse_date(value: str | None) -> date | None:
    text = _first_clean(value)
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text[:20], pattern).date()
        except ValueError:
            continue
    return None


def _currency(value: str | None) -> str | None:
    text = _first_clean(value)
    if not text:
        return None
    match = re.search(r"\b[A-Z]{3}\b", text.upper())
    return match.group(0) if match else None
