"""Canonical ETF provider registry and DB backfill helpers.

The ETF universe arrives from FIRDS/Xetra with mixed issuer spellings and many
blank issuer fields. This module is the single Python source for mapping those
strings to stable provider IDs used by the API, UI facets, and holdings
scraper queue.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


UNKNOWN_PROVIDER_ID = "unknown_provider"


@dataclass(frozen=True)
class ProviderSeed:
    provider_id: str
    label: str
    domain: str | None
    aliases: tuple[str, ...]
    source_status: str = "fallback_only"


@dataclass(frozen=True)
class ProviderMatch:
    provider_id: str
    label: str
    domain: str | None
    source_status: str
    confidence: float
    matched_by: str


OFFICIAL_ADAPTER_STATUS = "official_adapter"
GENERIC_DISCOVERY_STATUS = "generic_discovery"
FALLBACK_ONLY_STATUS = "fallback_only"
UNSUPPORTED_STATUS = "unsupported"


PROVIDER_SEEDS: tuple[ProviderSeed, ...] = (
    ProviderSeed("amundi", "Amundi", "amundi.com", ("Amundi", "Lyxor", "AIS-Amundi", "AIS-Am", "MUF Amundi", "MUFAmundi", "MUL Amundi", "MULAmundi"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("ark", "Ark ETF", "ark-funds.com", ("Ark ETF", "ARK", "ARK ETF")),
    ProviderSeed("axxion", "Axxion", "axxion.lu", ("Axxion",)),
    ProviderSeed("deka", "Deka ETF", "deka-etf.de", ("Deka ETF", "Deka"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("easyetf", "EasyETF", "bnpparibas-am.com", ("EasyETF", "BNP Paribas Easy", "BNPP", "BNPPEASY", "BNPEASY", "BNP Easy"), FALLBACK_ONLY_STATUS),
    ProviderSeed("expat", "Expat", "expat.bg", ("Expat",)),
    ProviderSeed("fidelity", "Fidelity ETF", "fidelity.com", ("Fidelity ETF", "Fidelity", "Fid2", "Fid2Glbl", "Fid2USD"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("first_trust", "First Trust", "ftportfolios.com", ("First Trust", "First", "FT")),
    ProviderSeed("franklin_templeton", "Franklin Templeton", "franklintempleton.com", ("Franklin Templeton", "Franklin", "Templeton")),
    ProviderSeed("global_x", "Global X", "globalxetfs.com", ("Global X", "Global", "GLX"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("goldman_sachs", "Goldman Sachs ETF", "gsam.com", ("Goldman Sachs ETF", "Goldman Sachs", "GS", "GS ETF")),
    ProviderSeed("hanetf", "HANetf", "hanetf.com", ("HANetf", "HANETF")),
    ProviderSeed("hsbc", "HSBC ETF", "hsbc.com", ("HSBC ETF", "HSBC"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("invesco", "Invesco", "invesco.com", ("Invesco", "InvescoMI", "InvSP", "InvDWA", "InvDynamic", "InvescoM2")),
    ProviderSeed("ishares", "iShares", "ishares.com", ("iShares", "iShs", "IShs", "iShsII", "iShsIII", "iShsIV", "iShsV", "iShsVII", "ISHS", "BlackRock"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("jpmorgan", "J.P. Morgan ETF", "jpmorgan.com", ("J.P. Morgan ETF", "J.P. Morgan", "JP Morgan", "JPM", "JPMorgan"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("janus_henderson", "Janus Henderson", "janushenderson.com", ("Janus Henderson",)),
    ProviderSeed("kraneshares", "KraneShares", "kraneshares.eu", ("KraneShares", "Krane")),
    ProviderSeed("lg", "L&G ETF", "lgim.com", ("L&G ETF", "L&G", "LGIM", "Legal & General", "Legal General"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("lunate", "Lunate", "lunate.com", ("Lunate",)),
    ProviderSeed("market_access", "Market Access", None, ("Market Access",)),
    ProviderSeed("melanion", "Melanion", "melanion.com", ("Melanion",)),
    ProviderSeed("northern_trust", "Northern Trust", "northerntrust.com", ("Northern Trust", "FlexShares")),
    ProviderSeed("ossiam", "Ossiam", "ossiam.com", ("Ossiam",), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("palmer_square", "Palmer Square", "palmersquarecap.com", ("Palmer Square",)),
    ProviderSeed("pimco", "PIMCO", "pimco.com", ("PIMCO",)),
    ProviderSeed("proshares", "ProShares", "proshares.com", ("ProShares", "PROSHS", "ProShs", "ProSh."), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("spdr", "SPDR", "ssga.com", ("SPDR", "State Street", "SSGA", "StStrSPDR", "StStr SPDR"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("teq_capital", "TEQ Capital", None, ("TEQ Capital",)),
    ProviderSeed("ubs", "UBS ETF", "ubs.com", ("UBS ETF", "UBS", "UBS Irl ETF")),
    ProviderSeed("unicredit", "UniCredit", "unicreditgroup.eu", ("UniCredit",)),
    ProviderSeed("vaneck", "VanEck", "vaneck.com", ("VanEck", "VANECK"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("vanguard", "Vanguard", "vanguard.com", ("Vanguard", "Vanguardr"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed("wisdomtree", "WisdomTree", "wisdomtree.eu", ("WisdomTree",)),
    ProviderSeed("xtrackers", "Xtrackers", "xtrackers.com", ("Xtrackers", "xtrackers", "Xtr", "XtrII", "XtrIE", "DWS"), OFFICIAL_ADAPTER_STATUS),
    ProviderSeed(UNKNOWN_PROVIDER_ID, "Unknown Provider", None, ("unknown_provider",), UNSUPPORTED_STATUS),
)


_BY_ID = {seed.provider_id: seed for seed in PROVIDER_SEEDS}


def _key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def provider_id_from_label(label: str | None) -> str:
    """Create a stable provider_id for labels not yet in the seed registry."""
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return UNKNOWN_PROVIDER_ID
    if slug[0].isdigit():
        slug = f"provider_{slug}"
    return slug[:80]


_EXACT_ALIAS_TO_ID: dict[str, str] = {}
for _seed in PROVIDER_SEEDS:
    for _alias in (_seed.label, *_seed.aliases):
        _EXACT_ALIAS_TO_ID[_key(_alias)] = _seed.provider_id


def _seed_match(provider_id: str, confidence: float, matched_by: str) -> ProviderMatch:
    seed = _BY_ID[provider_id]
    return ProviderMatch(
        provider_id=seed.provider_id,
        label=seed.label,
        domain=seed.domain,
        source_status=seed.source_status,
        confidence=confidence,
        matched_by=matched_by,
    )


def _heuristic_provider_id(raw_text: str) -> tuple[str, str] | None:
    raw = (raw_text or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    compact = _key(raw)

    checks: tuple[tuple[bool, str, str], ...] = (
        (compact.startswith("ishs") or "ishares" in compact or "blackrock" in compact, "ishares", "name_prefix"),
        (compact.startswith("aisam") or compact.startswith("mufamundi") or compact.startswith("mulamundi") or "lyxor" in compact or "amundi" in compact, "amundi", "name_prefix"),
        (compact.startswith("xtr") or "xtrackers" in compact or re.search(r"\bdws\b", lower) is not None, "xtrackers", "name_prefix"),
        (compact.startswith("vanguardr") or "vanguard" in compact, "vanguard", "name_prefix"),
        (compact.startswith("ststrspdr") or "spdr" in compact or "statestreet" in compact or "ssga" in compact, "spdr", "name_prefix"),
        (compact.startswith("jpm") or "jpmorgan" in compact or "jpmorgan" in _key(lower), "jpmorgan", "name_prefix"),
        (compact.startswith("bnpp") or compact.startswith("bnpeasy") or "bnpparibaseasy" in compact or "easyetf" in compact, "easyetf", "name_prefix"),
        (compact.startswith("ubs") or "ubsetf" in compact, "ubs", "name_prefix"),
        (compact.startswith("hsbc"), "hsbc", "name_prefix"),
        (compact.startswith("wisdomtree"), "wisdomtree", "name_prefix"),
        (compact.startswith("vaneck") or compact.startswith("vaneck"), "vaneck", "name_prefix"),
        (compact.startswith("invesco") or compact.startswith("invsp") or compact.startswith("invdwa") or compact.startswith("invdynamic"), "invesco", "name_prefix"),
        (compact.startswith("globalx") or compact == "global" or compact.startswith("glx"), "global_x", "name_prefix"),
        (compact.startswith("firsttrust") or compact.startswith("ft"), "first_trust", "name_prefix"),
        (compact.startswith("fidelity") or compact.startswith("fid2"), "fidelity", "name_prefix"),
        (compact.startswith("hanetf"), "hanetf", "name_prefix"),
        (compact.startswith("lg") or "legalgeneral" in compact or "lgim" in compact, "lg", "name_prefix"),
        (compact.startswith("gs") or "goldmansachs" in compact, "goldman_sachs", "name_prefix"),
        (compact.startswith("ark"), "ark", "name_prefix"),
        (compact.startswith("krane"), "kraneshares", "name_prefix"),
        (compact.startswith("pimco"), "pimco", "name_prefix"),
        (compact.startswith("deka"), "deka", "name_prefix"),
        (compact.startswith("ossiam"), "ossiam", "name_prefix"),
        (compact.startswith("janushenderson"), "janus_henderson", "name_prefix"),
        (compact.startswith("northerntrust") or compact.startswith("flexshares"), "northern_trust", "name_prefix"),
        (compact.startswith("axxion"), "axxion", "name_prefix"),
        (compact.startswith("expat"), "expat", "name_prefix"),
        (compact.startswith("lunate"), "lunate", "name_prefix"),
        (compact.startswith("melanion"), "melanion", "name_prefix"),
        (compact.startswith("unicredit"), "unicredit", "name_prefix"),
        (compact.startswith("marketaccess"), "market_access", "name_prefix"),
        (compact.startswith("palmersquare"), "palmer_square", "name_prefix"),
        (compact.startswith("teqcapital"), "teq_capital", "name_prefix"),
    )
    for ok, provider_id, matched_by in checks:
        if ok:
            return provider_id, matched_by
    return None


def canonicalize_provider(
    issuer_name: str | None = None,
    fund_family: str | None = None,
    full_name: str | None = None,
    short_name: str | None = None,
) -> ProviderMatch | None:
    """Map mixed ETF provider strings to a stable provider ID."""
    parts = [p for p in (issuer_name, fund_family, full_name, short_name) if p and str(p).strip()]
    if not parts:
        return None

    for part in parts:
        provider_id = _EXACT_ALIAS_TO_ID.get(_key(part))
        if provider_id and provider_id != UNKNOWN_PROVIDER_ID:
            return _seed_match(provider_id, 1.0, "exact_alias")

    guess = _heuristic_provider_id(" ".join(parts))
    if guess:
        provider_id, matched_by = guess
        return _seed_match(provider_id, 0.9, matched_by)

    return None


def seed_provider_registry() -> dict[str, int]:
    rows = [
        (s.provider_id, s.label, s.domain, list(s.aliases), s.source_status)
        for s in PROVIDER_SEEDS
    ]
    sql = """
        INSERT INTO sec.dim_etf_provider
            (provider_id, label, domain, aliases, source_status)
        VALUES %s
        ON CONFLICT (provider_id) DO UPDATE SET
            label = EXCLUDED.label,
            domain = COALESCE(EXCLUDED.domain, sec.dim_etf_provider.domain),
            aliases = (
                SELECT ARRAY(
                    SELECT DISTINCT alias
                    FROM unnest(sec.dim_etf_provider.aliases || EXCLUDED.aliases) AS alias
                    WHERE alias IS NOT NULL AND btrim(alias) <> ''
                    ORDER BY alias
                )
            ),
            source_status = EXCLUDED.source_status,
            updated_at = NOW()
    """
    with connect() as conn, conn.cursor() as cur:
        inserted = execute_values(cur, sql, rows)
    return {"providers_seeded": inserted}


def upsert_dynamic_providers(rows: Iterable[tuple[str, str, str | None, list[str] | tuple[str, ...] | None, str | None]]) -> int:
    """Insert/update providers discovered after the static seed list.

    Existing seeded providers are preserved except for additive aliases/domain
    where the incoming value is non-empty.
    """
    payload = []
    for provider_id, label, domain, aliases, source_status in rows:
        normalized_id = provider_id_from_label(provider_id or label)
        clean_label = (label or "").strip()
        if not normalized_id or normalized_id == UNKNOWN_PROVIDER_ID or not clean_label:
            continue
        clean_aliases = []
        for alias in (aliases or (clean_label,)):
            alias_text = str(alias or "").strip()
            if alias_text and alias_text not in clean_aliases:
                clean_aliases.append(alias_text)
        if clean_label not in clean_aliases:
            clean_aliases.insert(0, clean_label)
        payload.append(
            (
                normalized_id,
                clean_label,
                domain,
                clean_aliases,
                source_status or FALLBACK_ONLY_STATUS,
            )
        )
    if not payload:
        return 0
    with connect() as conn, conn.cursor() as cur:
        return execute_values(
            cur,
            """
            INSERT INTO sec.dim_etf_provider
                (provider_id, label, domain, aliases, source_status)
            VALUES %s
            ON CONFLICT (provider_id) DO UPDATE SET
                label = COALESCE(NULLIF(EXCLUDED.label, ''), sec.dim_etf_provider.label),
                domain = COALESCE(EXCLUDED.domain, sec.dim_etf_provider.domain),
                aliases = (
                    SELECT ARRAY(
                        SELECT DISTINCT alias
                        FROM unnest(sec.dim_etf_provider.aliases || EXCLUDED.aliases) AS alias
                        WHERE alias IS NOT NULL AND btrim(alias) <> ''
                        ORDER BY alias
                    )
                ),
                source_status = CASE
                    WHEN sec.dim_etf_provider.source_status IN ('official_adapter', 'generic_discovery')
                    THEN sec.dim_etf_provider.source_status
                    ELSE COALESCE(EXCLUDED.source_status, sec.dim_etf_provider.source_status)
                END,
                updated_at = NOW()
            """,
            payload,
        )


def _load_provider_aliases() -> dict[str, ProviderMatch]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT provider_id, label, domain, aliases, source_status
            FROM sec.dim_etf_provider
            WHERE provider_id <> %s
            """,
            (UNKNOWN_PROVIDER_ID,),
        )
        rows = cur.fetchall()

    aliases: dict[str, ProviderMatch] = {}
    for provider_id, label, domain, alias_values, source_status in rows:
        match = ProviderMatch(
            provider_id=provider_id,
            label=label,
            domain=domain,
            source_status=source_status,
            confidence=1.0,
            matched_by="provider_registry_alias",
        )
        for alias in (label, *(alias_values or [])):
            key = _key(alias)
            if key:
                aliases[key] = match
    return aliases


def _canonicalize_from_registry(
    registry_aliases: dict[str, ProviderMatch],
    issuer_name: str | None = None,
    fund_family: str | None = None,
    full_name: str | None = None,
    short_name: str | None = None,
) -> ProviderMatch | None:
    parts = [p for p in (issuer_name, fund_family, full_name, short_name) if p and str(p).strip()]
    for part in parts:
        match = registry_aliases.get(_key(part))
        if match:
            return match
    return canonicalize_provider(issuer_name, fund_family, full_name, short_name)


def backfill_etf_provider_ids(limit: int | None = None, include_unknown: bool = True) -> dict[str, int]:
    sql = """
        SELECT d.isin, d.provider_id, d.issuer_name, p.fund_family, d.full_name, d.short_name
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        ORDER BY d.isin
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    registry_aliases = _load_provider_aliases()
    rows: list[tuple[str, str, str | None, float]] = []
    known = unknown = preserved = 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for isin, current_provider_id, issuer_name, fund_family, full_name, short_name in cur.fetchall():
                current_known = bool(current_provider_id and current_provider_id != UNKNOWN_PROVIDER_ID)
                match = _canonicalize_from_registry(
                    registry_aliases,
                    issuer_name,
                    fund_family,
                    full_name,
                    short_name,
                )
                if match:
                    known += 1
                    if not current_known or current_provider_id == match.provider_id:
                        rows.append((isin, match.provider_id, match.label, match.confidence))
                    else:
                        preserved += 1
                elif current_known:
                    preserved += 1
                elif include_unknown:
                    if not current_provider_id:
                        rows.append((isin, UNKNOWN_PROVIDER_ID, None, 0.0))
                    unknown += 1
                else:
                    unknown += 1
        if rows:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    UPDATE sec.dim_etf d SET
                        provider_id = v.provider_id,
                        issuer_name = CASE
                            WHEN (d.issuer_name IS NULL OR btrim(d.issuer_name) = '')
                                 AND v.provider_id <> 'unknown_provider'
                                 AND v.confidence >= 0.85
                            THEN v.label
                            ELSE d.issuer_name
                        END,
                        updated_at = NOW()
                    FROM (VALUES %s) AS v(isin, provider_id, label, confidence)
                    WHERE d.isin = v.isin
                      AND (
                        d.provider_id IS DISTINCT FROM v.provider_id
                        OR (
                            (d.issuer_name IS NULL OR btrim(d.issuer_name) = '')
                            AND v.provider_id <> 'unknown_provider'
                            AND v.confidence >= 0.85
                        )
                      )
                    """,
                    rows,
                )
    return {
        "scanned": known + unknown + preserved,
        "known": known,
        "unknown": unknown,
        "preserved_existing_provider": preserved,
        "updates_attempted": len(rows),
    }


def setup_provider_registry(limit: int | None = None) -> dict[str, int]:
    out = seed_provider_registry()
    out.update(backfill_etf_provider_ids(limit=limit))
    return out


def provider_facets() -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pv.provider_id, pv.label, pv.domain, pv.aliases, pv.source_status,
                   COUNT(DISTINCT d.isin) AS etf_count,
                   COUNT(DISTINCT h.isin) AS holdings_isins,
                   COUNT(DISTINCT d.isin) FILTER (WHERE COALESCE(d.is_active, TRUE)) AS active_etfs,
                   COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'success') AS official_success,
                   COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'unsupported') AS unsupported,
                   COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'failed') AS failed
            FROM sec.dim_etf_provider pv
            LEFT JOIN sec.dim_etf d ON d.provider_id = pv.provider_id
            LEFT JOIN sec.etf_holding h ON h.isin = d.isin
            LEFT JOIN sec.etf_holdings_fetch_state fs ON fs.isin = d.isin
            GROUP BY pv.provider_id, pv.label, pv.domain, pv.aliases, pv.source_status
            HAVING COUNT(DISTINCT d.isin) > 0 OR pv.provider_id = 'unknown_provider'
            ORDER BY COUNT(DISTINCT d.isin) DESC, pv.label
            """
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for provider_id, label, domain, aliases, source_status, etf_count, holdings_isins, active_etfs, official_success, unsupported, failed in rows:
        count = int(etf_count or 0)
        holdings = int(holdings_isins or 0)
        out.append({
            "provider_id": provider_id,
            "label": label,
            "domain": domain,
            "aliases": list(aliases or []),
            "source_status": source_status,
            "etf_count": count,
            "active_etfs": int(active_etfs or 0),
            "holdings_isins": holdings,
            "holdings_coverage": (holdings / count) if count else 0.0,
            "official_success": int(official_success or 0),
            "unsupported": int(unsupported or 0),
            "failed": int(failed or 0),
        })
    return out


def official_adapter_provider_ids() -> set[str]:
    return {seed.provider_id for seed in PROVIDER_SEEDS if seed.source_status == OFFICIAL_ADAPTER_STATUS}


def provider_seed_by_id(provider_id: str) -> ProviderSeed | None:
    return _BY_ID.get(provider_id)


def seed_rows() -> Iterable[ProviderSeed]:
    return PROVIDER_SEEDS
