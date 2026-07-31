from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.filings.helpers import normalize_cik


LABEL_ALTERNATIVE = "Asset Management: Alternative (Speculative/Trading)"
LABEL_TRADITIONAL = "Asset Management: Traditional (Long-Term Capital)"
LABEL_WEALTH = "Banking: Wealth & Trust (Investment)"
LABEL_BANK_TRADING = "Banking: Capital Markets & Trading (Speculative)"
LABEL_INSURANCE = "Insurance: General Account (Long-Term Capital)"
LABELS = {
    LABEL_ALTERNATIVE,
    LABEL_TRADITIONAL,
    LABEL_WEALTH,
    LABEL_BANK_TRADING,
    LABEL_INSURANCE,
}

PROMPT_VERSION = "13f_manager_classifier_v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_CHAT_MODEL = "deepseek-chat"
DEEPSEEK_REASONER_MODEL = "deepseek-reasoner"
FUZZY_THRESHOLD = 0.94

LEGAL_SUFFIX_RE = re.compile(
    r"\b("
    r"llc|l\.l\.c|lp|l\.p|llp|l\.l\.p|inc|incorporated|corp|corporation|co|company|"
    r"ltd|limited|plc|sa|s\.a|se|ag|nv|n\.v|bv|b\.v|sgr|spa|s\.p\.a|group|holdings?"
    r")\b",
    re.IGNORECASE,
)
GENERIC_MANAGER_TERMS_RE = re.compile(
    r"\b(asset|assets|management|manager|managers|advisors?|advisers?|investment|investments|"
    r"capital|fund|funds|partners?|global|international|financial|alternative|alternatives)\b",
    re.IGNORECASE,
)
PAREN_RE = re.compile(r"\([^)]*\)")
TOKEN_RE = re.compile(r"[^a-z0-9]+")

AMBIGUOUS_FUZZY_NAMES = {
    "man",
    "man group",
    "discovery capital",
    "legacy capital",
    "compass group",
    "capital group",
    "l1 capital",
    "guardian capital",
    "value partners",
    "sanitas",
}

ALT_KEYWORDS = re.compile(
    r"\b(hedge|alternative|private equity|private credit|distressed|long/short|long-short|"
    r"l/s|macro|event[- ]driven|activist|arbitrage|multi[- ]strategy|multi strat|"
    r"multi[- ]manager|pod|market neutral|absolute return|special situations|opportunistic credit|"
    r"convertible arbitrage|volatility|trading)\b",
    re.IGNORECASE,
)
WEALTH_KEYWORDS = re.compile(
    r"\b(private bank|private banking|private wealth|wealth management|trust company|"
    r"fiduciary trust|family office|multi-family office|multifamily office|ultra-high-net-worth|hnw)\b",
    re.IGNORECASE,
)
INSURANCE_NAME_RE = re.compile(
    r"\b(life|reinsurance|reinsur|insurance|assurance|assurances|annuit|mutual|"
    r"p&c|property casualty|casualty|underwriting)\b",
    re.IGNORECASE,
)
BANK_TRADING_RE = re.compile(
    r"\b(goldman|morgan stanley|jpmorgan|jp morgan|ubs|citigroup|citi|bank of america|"
    r"barclays|deutsche bank|credit suisse|nomura|jefferies|securities)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StyleReference:
    reference_id: int | None
    source_file: str
    source_category: str
    source_rank: int | None
    canonical_name: str
    normalized_name: str
    aliases: tuple[str, ...]
    domicile_or_headquarters: str | None
    strategy_or_profile: str | None
    target_label: str
    confidence_policy: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class ReferenceMatch:
    reference: StyleReference
    match_type: str
    score: float
    matched_name: str


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _de_mojibake(text: str) -> str:
    replacements = {
        "â€“": "-",
        "â€”": "-",
        "â€™": "'",
        "Ã©": "e",
        "Ã¨": "e",
        "Ã¼": "u",
        "Ã¤": "a",
        "Ã¶": "o",
        "Ã£": "a",
        "Ã±": "n",
        "Ã§": "c",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def normalize_name(value: str | None, *, strip_generic: bool = False) -> str:
    if not value:
        return ""
    text = _de_mojibake(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = PAREN_RE.sub(" ", text)
    text = text.replace("&", " and ")
    text = LEGAL_SUFFIX_RE.sub(" ", text)
    if strip_generic:
        text = GENERIC_MANAGER_TERMS_RE.sub(" ", text)
    return TOKEN_RE.sub(" ", text.lower()).strip()


def _alias_candidates(name: str) -> set[str]:
    raw = _de_mojibake(name).strip()
    without_parens = PAREN_RE.sub(" ", raw).strip()
    aliases = {raw, without_parens}
    aliases.add(re.sub(r"\bD\.\s*E\.\s*Shaw\b", "DE Shaw", raw, flags=re.I))
    aliases.add(re.sub(r"\bJ\.P\.\s*Morgan\b", "JP Morgan", raw, flags=re.I))
    aliases.add(re.sub(r"\bMgmt\b", "Management", raw, flags=re.I))
    aliases.add(re.sub(r"\bMgmt\b", "Mgmt", raw, flags=re.I))
    aliases.add(re.sub(r"\bInv(?:est)?ments?\b", "Investment", raw, flags=re.I))
    aliases.add(re.sub(r"\bInv(?:est)?ments?\b", "Investments", raw, flags=re.I))
    aliases.add(re.sub(r"\bAsset Mgmt\b", "Asset Management", raw, flags=re.I))
    aliases.add(re.sub(r"\bCapital Mgmt\b", "Capital Management", raw, flags=re.I))
    aliases.add(re.sub(r"\bAdvisors?\b", "Advisers", raw, flags=re.I))
    aliases.add(re.sub(r"\bAdvisers?\b", "Advisors", raw, flags=re.I))
    if "/" in raw:
        aliases.update(part.strip() for part in raw.split("/") if part.strip())
    return {normalize_name(alias) for alias in aliases if normalize_name(alias)}


def _label_for_asset_manager_entry(name: str, description: str) -> str:
    text = f"{name} {description}"
    if WEALTH_KEYWORDS.search(text):
        return LABEL_WEALTH
    if ALT_KEYWORDS.search(text):
        return LABEL_ALTERNATIVE
    return LABEL_TRADITIONAL


def _parse_tsv_reference(path: Path, source_category: str, label: str) -> list[StyleReference]:
    text = _read_text(path)
    rows = csv.DictReader(io.StringIO(text), delimiter="\t")
    refs: list[StyleReference] = []
    for row in rows:
        name = row.get("Firm Name") or row.get("Group / Corporate Parent") or ""
        if not name.strip():
            continue
        rank_raw = row.get("Rank")
        try:
            rank = int(rank_raw) if rank_raw else None
        except ValueError:
            rank = None
        domicile = row.get("Global Headquarters") or row.get("Domicile")
        profile = row.get("Primary Strategy Class") or row.get("Core Asset / Liability Profile")
        refs.append(StyleReference(
            reference_id=None,
            source_file=path.name,
            source_category=source_category,
            source_rank=rank,
            canonical_name=_de_mojibake(name).strip(),
            normalized_name=normalize_name(name),
            aliases=tuple(sorted(_alias_candidates(name))),
            domicile_or_headquarters=_de_mojibake(domicile).strip() if domicile else None,
            strategy_or_profile=_de_mojibake(profile).strip() if profile else None,
            target_label=label,
            confidence_policy="exact_or_high_confidence_fuzzy",
            raw_payload={k: _de_mojibake(v) if isinstance(v, str) else v for k, v in row.items()},
        ))
    return refs


def _parse_asset_mgt_reference(path: Path) -> list[StyleReference]:
    text = _read_text(path)
    refs: list[StyleReference] = []
    rank = 0
    for raw_line in text.splitlines():
        line = _de_mojibake(raw_line).strip()
        if not line or line.startswith("Chunk ") or re.match(r"^\d+\s*[-–]", line):
            continue
        match = re.match(r"^(?P<name>.+?)\s+(?:-|--|–)\s+(?P<desc>.+)$", line)
        if not match:
            continue
        name = match.group("name").strip(" \t-*")
        desc = match.group("desc").strip()
        if len(name) < 3 or name.lower().startswith("this "):
            continue
        rank += 1
        refs.append(StyleReference(
            reference_id=None,
            source_file=path.name,
            source_category="asset_management",
            source_rank=rank,
            canonical_name=name,
            normalized_name=normalize_name(name),
            aliases=tuple(sorted(_alias_candidates(name))),
            domicile_or_headquarters=None,
            strategy_or_profile=desc,
            target_label=_label_for_asset_manager_entry(name, desc),
            confidence_policy="exact_or_high_confidence_fuzzy",
            raw_payload={"line": line},
        ))
    return refs


def parse_reference_files(spec_dir: Path | None = None) -> list[StyleReference]:
    spec_dir = spec_dir or (_project_root() / "spec")
    refs: list[StyleReference] = []
    refs.extend(_parse_tsv_reference(spec_dir / "top250hedgefunds.txt", "hedge_fund", LABEL_ALTERNATIVE))
    refs.extend(_parse_tsv_reference(spec_dir / "top250insurance.txt", "insurance", LABEL_INSURANCE))
    refs.extend(_parse_asset_mgt_reference(spec_dir / "top500assetmgt.txt"))
    return refs


def import_style_references(spec_dir: Path | None = None) -> dict[str, int]:
    refs = parse_reference_files(spec_dir)
    rows = [
        (
            ref.source_file,
            ref.source_category,
            ref.source_rank,
            ref.canonical_name,
            ref.normalized_name,
            json.dumps(list(ref.aliases), ensure_ascii=False),
            ref.domicile_or_headquarters,
            ref.strategy_or_profile,
            ref.target_label,
            ref.confidence_policy,
            json.dumps(ref.raw_payload, ensure_ascii=False),
        )
        for ref in refs
    ]
    with connect() as conn, conn.cursor() as cur:
        written = execute_values(cur, """
            INSERT INTO ref_13f_manager_style_reference
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
        """, rows, page_size=1000)
    counts: dict[str, int] = {}
    for ref in refs:
        counts[ref.source_category] = counts.get(ref.source_category, 0) + 1
    return {"references": written, **counts}


def _load_style_references(cur) -> list[StyleReference]:
    cur.execute("""
        SELECT reference_id, source_file, source_category, source_rank, canonical_name, normalized_name,
               aliases, domicile_or_headquarters, strategy_or_profile, target_label,
               confidence_policy, raw_payload
        FROM ref_13f_manager_style_reference
        ORDER BY source_file, source_rank NULLS LAST, canonical_name
    """)
    refs = []
    for row in cur.fetchall():
        aliases = row[6] or []
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        refs.append(StyleReference(
            reference_id=row[0],
            source_file=row[1],
            source_category=row[2],
            source_rank=row[3],
            canonical_name=row[4],
            normalized_name=row[5],
            aliases=tuple(aliases),
            domicile_or_headquarters=row[7],
            strategy_or_profile=row[8],
            target_label=row[9],
            confidence_policy=row[10],
            raw_payload=row[11] or {},
        ))
    return refs


def _is_ambiguous_for_fuzzy(value: str) -> bool:
    compact = normalize_name(value)
    stripped = normalize_name(value, strip_generic=True)
    return compact in AMBIGUOUS_FUZZY_NAMES or stripped in {"", "group", "capital", "management", "asset"}


def _token_set_score(a: str, b: str) -> float:
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    common = a_tokens & b_tokens
    if not common:
        return 0.0
    precision = len(common) / len(a_tokens)
    recall = len(common) / len(b_tokens)
    harmonic = 2 * precision * recall / (precision + recall)
    seq = SequenceMatcher(None, " ".join(sorted(a_tokens)), " ".join(sorted(b_tokens))).ratio()
    return max(harmonic, seq)


def _reference_evidence(ref: StyleReference) -> str:
    if ref.reference_id is not None:
        return f"ref_13f_manager_style_reference:{ref.reference_id}"
    return f"{ref.source_file}:{ref.source_rank or ref.canonical_name}"


def match_reference(manager_name: str, refs: list[StyleReference]) -> tuple[ReferenceMatch | None, str | None]:
    norm = normalize_name(manager_name)
    aliases = _alias_candidates(manager_name)
    exact = [
        ReferenceMatch(ref, "exact", 1.0, ref.canonical_name)
        for ref in refs
        if norm == ref.normalized_name or norm in ref.aliases or aliases.intersection(set(ref.aliases) | {ref.normalized_name})
    ]
    exact_labels = {m.reference.target_label for m in exact}
    if len(exact_labels) == 1 and exact:
        return exact[0], None
    if len(exact_labels) > 1:
        return None, "reference_conflict_exact"

    if _is_ambiguous_for_fuzzy(manager_name):
        return None, None

    scores: list[ReferenceMatch] = []
    stripped_norm = normalize_name(manager_name, strip_generic=True)
    for ref in refs:
        candidates = [ref.normalized_name, *ref.aliases]
        scored = []
        for candidate in candidates:
            if not candidate:
                continue
            if norm and candidate and norm[0] != candidate[0]:
                continue
            if abs(len(norm) - len(candidate)) > max(8, int(max(len(norm), len(candidate)) * 0.25)):
                continue
            seq_score = SequenceMatcher(None, norm, candidate).ratio()
            token_score = _token_set_score(norm, candidate)
            stripped_candidate = normalize_name(candidate, strip_generic=True)
            stripped_score = _token_set_score(stripped_norm, stripped_candidate)
            scored.append((max(seq_score, token_score, stripped_score), candidate))
        best = max(scored) if scored else (0.0, "")
        if best[0] >= FUZZY_THRESHOLD:
            scores.append(ReferenceMatch(ref, "fuzzy", best[0], best[1]))
    scores.sort(key=lambda m: m.score, reverse=True)
    if not scores:
        return None, None
    if len(scores) > 1 and (scores[0].score - scores[1].score) < 0.03:
        return None, "reference_conflict_fuzzy"
    return scores[0], None


def _safe_div(num: float, den: float) -> float | None:
    return num / den if den else None


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _mean(values: list[float]) -> float | None:
    return float(statistics.mean(values)) if values else None


def _latest_price_map(cur, tickers: list[str], periods: list[date]) -> dict[tuple[str, date], float]:
    if not tickers or not periods:
        return {}
    values_sql = ", ".join(["(%s::date)"] * len(periods))
    cur.execute(
        f"""
        WITH target(period) AS (VALUES {values_sql})
        SELECT DISTINCT ON (p.ticker, target.period)
               p.ticker, target.period, COALESCE(p.adj_close, p.close) AS price
        FROM fact_prices_us p
        JOIN target ON p.date <= target.period
        WHERE p.ticker = ANY(%s)
          AND COALESCE(p.adj_close, p.close) IS NOT NULL
        ORDER BY p.ticker, target.period, p.date DESC
        """,
        [*periods, tickers],
    )
    return {(row[0], row[1]): float(row[2]) for row in cur.fetchall()}


def build_feature_payload(cur, manager_cik: str) -> dict[str, Any] | None:
    manager_cik = normalize_cik(manager_cik)
    cur.execute(
        """
        SELECT manager_cik, manager_name
        FROM dim_13f_manager
        WHERE manager_cik = %s
        """,
        (manager_cik,),
    )
    manager = cur.fetchone()
    if not manager:
        return None
    cur.execute(
        """
        SELECT DISTINCT report_period
        FROM fact_13f_holdings
        WHERE manager_cik = %s AND is_latest_amendment
        ORDER BY report_period DESC
        LIMIT 8
        """,
        (manager_cik,),
    )
    periods = [row[0] for row in cur.fetchall()]
    if not periods:
        return None
    periods = sorted(periods)
    cur.execute(
        """
        SELECT report_period, issuer_ticker, cusip, value_x1000, put_call, sh_prn_flag,
               voting_authority_sole, voting_authority_shared, voting_authority_none
        FROM fact_13f_holdings
        WHERE manager_cik = %s
          AND is_latest_amendment
          AND report_period = ANY(%s)
        """,
        (manager_cik, periods),
    )
    by_period: dict[date, list[dict[str, Any]]] = {p: [] for p in periods}
    tickers: set[str] = set()
    for row in cur.fetchall():
        item = {
            "period": row[0],
            "ticker": row[1],
            "cusip": row[2],
            "value": float(row[3] or 0.0),
            "put_call": (row[4] or "").strip().upper(),
            "sh_prn_flag": (row[5] or "SH").strip().upper(),
            "vote_sole": float(row[6] or 0.0),
            "vote_shared": float(row[7] or 0.0),
            "vote_none": float(row[8] or 0.0),
        }
        by_period[item["period"]].append(item)
        if item["ticker"]:
            tickers.add(item["ticker"])

    price_map = _latest_price_map(cur, sorted(tickers), periods)
    quarterly: dict[date, dict[str, Any]] = {}
    price_weight_covered = []
    for period in periods:
        rows = by_period[period]
        long_rows = [r for r in rows if not r["put_call"] and r["sh_prn_flag"] == "SH" and r["value"] > 0]
        option_rows = [r for r in rows if r["put_call"] in {"PUT", "CALL"} and r["value"] > 0]
        total_long = sum(r["value"] for r in long_rows)
        total_all = sum(r["value"] for r in rows if r["value"] > 0)
        weights: dict[str, float] = {}
        covered_value = 0.0
        for r in long_rows:
            key = r["ticker"] or r["cusip"]
            if not key or not total_long:
                continue
            weights[key] = weights.get(key, 0.0) + r["value"] / total_long
            if r["ticker"] and (r["ticker"], period) in price_map:
                covered_value += r["value"]
        sorted_weights = sorted(weights.values(), reverse=True)
        votes_total = sum(r["vote_sole"] + r["vote_shared"] + r["vote_none"] for r in rows)
        quarterly[period] = {
            "long_value": total_long,
            "total_value": total_all,
            "options_value": sum(r["value"] for r in option_rows),
            "weights": weights,
            "top5": sum(sorted_weights[:5]) if sorted_weights else None,
            "position_count": len(weights),
            "vote_sole_pct": _safe_div(sum(r["vote_sole"] for r in rows), votes_total),
        }
        if total_long:
            price_weight_covered.append(covered_value / total_long)

    turnovers: list[float] = []
    coverage_for_turnover: list[float] = []
    for prev_period, cur_period in zip(periods, periods[1:]):
        prev = quarterly[prev_period]
        cur_q = quarterly[cur_period]
        prev_weights = prev["weights"]
        cur_weights = cur_q["weights"]
        returns: dict[str, float] = {}
        covered_prev_weight = 0.0
        for key, prev_weight in prev_weights.items():
            ticker = key if (key, prev_period) in price_map else None
            if not ticker:
                returns[key] = 0.0
                continue
            p0 = price_map.get((ticker, prev_period))
            p1 = price_map.get((ticker, cur_period))
            if p0 and p1 and p0 > 0:
                returns[key] = (p1 / p0) - 1.0
                covered_prev_weight += prev_weight
            else:
                returns[key] = 0.0
        portfolio_return = sum(prev_weights.get(k, 0.0) * returns.get(k, 0.0) for k in prev_weights)
        adjusted_prev = {
            k: w * ((1.0 + returns.get(k, 0.0)) / (1.0 + portfolio_return))
            for k, w in prev_weights.items()
            if (1.0 + portfolio_return) != 0
        }
        keys = set(cur_weights) | set(adjusted_prev)
        turnovers.append(0.5 * sum(abs(cur_weights.get(k, 0.0) - adjusted_prev.get(k, 0.0)) for k in keys))
        coverage_for_turnover.append(covered_prev_weight)

    latest = quarterly[periods[-1]]
    payload = {
        "cik": manager_cik,
        "legal_name": manager[1],
        "current_aum": latest["long_value"],
        "rolling_2y_metrics": {
            "median_turnover_rate": _median(turnovers),
            "max_turnover_rate": max(turnovers) if turnovers else None,
            "median_options_ratio": _median([
                _safe_div(q["options_value"], q["total_value"]) or 0.0 for q in quarterly.values()
            ]),
            "mean_position_count": int(round(_mean([q["position_count"] for q in quarterly.values()]) or 0)),
            "top_5_concentration": latest["top5"],
        },
        "filing_history": {
            "consecutive_quarters": len(periods),
            "shares_voting_sole_pct": _median([
                q["vote_sole_pct"] for q in quarterly.values() if q["vote_sole_pct"] is not None
            ]),
        },
        "feature_quality": {
            "price_coverage_weight": _mean(coverage_for_turnover) if coverage_for_turnover else _mean(price_weight_covered),
            "periods": [str(p) for p in periods],
        },
    }
    payload["input_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    payload["report_period"] = periods[-1]
    return payload


def _metric(payload: dict[str, Any], key: str) -> float | None:
    value = (payload.get("rolling_2y_metrics") or {}).get(key)
    return float(value) if value is not None and not (isinstance(value, float) and math.isnan(value)) else None


def _history_metric(payload: dict[str, Any], key: str) -> float | None:
    value = (payload.get("filing_history") or {}).get(key)
    return float(value) if value is not None else None


def _feature_quality(payload: dict[str, Any], key: str) -> float | None:
    value = (payload.get("feature_quality") or {}).get(key)
    return float(value) if value is not None else None


def _classification(label: str, confidence: float, trigger: str, tier: str, reason: str, evidence: str | None = None) -> dict[str, Any]:
    return {
        "classification_status": "classified",
        "primary_label": label,
        "confidence_score": confidence,
        "quantitative_trigger_metric": trigger,
        "route_tier": tier,
        "route_reason": reason,
        "evidence_source": evidence,
        "model": "deterministic",
        "response_json": {},
        "error_type": None,
        "error_message": None,
    }


def deterministic_classify(payload: dict[str, Any], refs: list[StyleReference]) -> tuple[dict[str, Any] | None, str | None]:
    name = payload.get("legal_name") or ""
    median_turnover = _metric(payload, "median_turnover_rate") or 0.0
    max_turnover = _metric(payload, "max_turnover_rate") or 0.0
    options = _metric(payload, "median_options_ratio") or 0.0
    positions = _metric(payload, "mean_position_count") or 0.0

    match, conflict = match_reference(name, refs)
    if match:
        ref = match.reference
        if ref.target_label == LABEL_INSURANCE and (max_turnover >= 0.75 or options >= 0.25):
            return None, "insurance_reference_extreme_behavior_conflict"
        confidence = 0.98 if match.match_type == "exact" else min(0.95, match.score)
        return _classification(
            ref.target_label,
            confidence,
            f"reference_match={_reference_evidence(ref)}",
            f"tier1_reference_{match.match_type}",
            f"Matched curated {ref.source_category} reference: {ref.canonical_name}",
            _reference_evidence(ref),
        ), None
    if conflict:
        return None, conflict

    if re.search(r"\b(VANGUARD|BLACKROCK|STATE STREET|FIDELITY|T\.?\s*ROWE)\b", name, re.I):
        return _classification(LABEL_TRADITIONAL, 0.96, "major_traditional_manager_name", "tier1_rule", "Major traditional asset manager name match."), None
    if WEALTH_KEYWORDS.search(name) and median_turnover < 0.08:
        return _classification(LABEL_WEALTH, 0.92, f"median_turnover_rate={median_turnover:.4f}", "tier1_rule", "Wealth/trust name with low turnover."), None
    if INSURANCE_NAME_RE.search(name) and median_turnover < 0.15 and options < 0.05:
        return _classification(LABEL_INSURANCE, 0.90, f"median_turnover_rate={median_turnover:.4f}; options_ratio={options:.4f}", "tier1_rule", "Insurance-like name with stable low-options portfolio."), None
    if BANK_TRADING_RE.search(name) and (max_turnover > 0.60 or options > 0.25) and positions > 200:
        return _classification(LABEL_BANK_TRADING, 0.88, f"max_turnover_rate={max_turnover:.4f}; options_ratio={options:.4f}", "tier1_rule", "Bank/securities name with trading-style 13F behavior."), None
    if median_turnover > 0.25 or (max_turnover > 0.40 and options > 0.10) or options > 0.20:
        return _classification(LABEL_ALTERNATIVE, 0.86, f"median_turnover_rate={median_turnover:.4f}; max_turnover_rate={max_turnover:.4f}; options_ratio={options:.4f}", "tier1_rule", "High turnover/options behavior."), None
    if median_turnover < 0.15 and options < 0.02 and positions >= 50:
        return _classification(LABEL_TRADITIONAL, 0.82, f"median_turnover_rate={median_turnover:.4f}; options_ratio={options:.4f}", "tier1_rule", "Stable diversified low-options portfolio."), None
    return None, None


SYSTEM_PROMPT = """You are an execution node in an institutional quantitative financial data pipeline. Your sole objective is to ingest historical 13F metadata and output an exact, deterministic classification label for the manager CIK.

You must assign the manager to exactly one of the following five schemas:
1. "Asset Management: Alternative (Speculative/Trading)"
2. "Asset Management: Traditional (Long-Term Capital)"
3. "Banking: Wealth & Trust (Investment)"
4. "Banking: Capital Markets & Trading (Speculative)"
5. "Insurance: General Account (Long-Term Capital)"

Return only JSON with keys: primary_label, confidence_score, quantitative_trigger_metric."""


def _validate_llm_json(data: dict[str, Any]) -> dict[str, Any]:
    label = data.get("primary_label")
    confidence = data.get("confidence_score")
    trigger = data.get("quantitative_trigger_metric")
    if label not in LABELS:
        raise ValueError(f"Invalid primary_label: {label!r}")
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"Invalid confidence_score: {confidence!r}")
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("quantitative_trigger_metric is required")
    return {
        "primary_label": label,
        "confidence_score": float(confidence),
        "quantitative_trigger_metric": trigger.strip(),
    }


def _call_deepseek(payload: dict[str, Any], *, escalated: bool = False) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")
    model = DEEPSEEK_REASONER_MODEL if escalated else DEEPSEEK_CHAT_MODEL
    req_body: dict[str, Any] = {
        "model": model,
        "temperature": 1.0 if escalated else 0.0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, default=str, sort_keys=True)},
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
        data = json.loads(response.read().decode("utf-8"))
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return _validate_llm_json(json.loads(text))


def llm_classify(payload: dict[str, Any], route_reason: str | None = None) -> dict[str, Any]:
    try:
        data = _call_deepseek(payload, escalated=False)
        tier = "tier2_deepseek_v3"
        model = DEEPSEEK_CHAT_MODEL
        if data["confidence_score"] < 0.85 or route_reason:
            data = _call_deepseek(payload, escalated=True)
            tier = "tier3_deepseek_r1"
            model = DEEPSEEK_REASONER_MODEL
        return {
            "classification_status": "classified",
            **data,
            "route_tier": tier,
            "route_reason": route_reason or "LLM fallback for unresolved manager.",
            "evidence_source": None,
            "model": model,
            "response_json": data,
            "error_type": None,
            "error_message": None,
        }
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        fallback, _ = deterministic_classify(payload, [])
        if fallback:
            fallback = dict(fallback)
            fallback["route_tier"] = "tier_error_fallback_rule"
            fallback["route_reason"] = f"LLM failed; deterministic numeric fallback used. {exc}"
            fallback["error_type"] = type(exc).__name__
            fallback["error_message"] = str(exc)
            return fallback
        return {
            "classification_status": "error",
            "primary_label": None,
            "confidence_score": None,
            "quantitative_trigger_metric": "UNCATEGORIZED_LLM_ERROR",
            "route_tier": "tier_error",
            "route_reason": route_reason or "LLM fallback failed.",
            "evidence_source": None,
            "model": None,
            "response_json": {},
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }


def _upsert_feature_snapshot(cur, payload: dict[str, Any]) -> None:
    metrics = payload.get("rolling_2y_metrics") or {}
    history = payload.get("filing_history") or {}
    quality = payload.get("feature_quality") or {}
    cur.execute(
        """
        INSERT INTO fact_13f_manager_feature_snapshot
            (manager_cik, report_period, input_hash, legal_name, current_aum,
             median_turnover_rate, max_turnover_rate, median_options_ratio,
             mean_position_count, top_5_concentration, consecutive_quarters,
             shares_voting_sole_pct, price_coverage_weight, feature_payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (manager_cik, report_period, input_hash) DO NOTHING
        """,
        (
            payload["cik"],
            payload["report_period"],
            payload["input_hash"],
            payload["legal_name"],
            payload.get("current_aum"),
            metrics.get("median_turnover_rate"),
            metrics.get("max_turnover_rate"),
            metrics.get("median_options_ratio"),
            metrics.get("mean_position_count"),
            metrics.get("top_5_concentration"),
            history.get("consecutive_quarters"),
            history.get("shares_voting_sole_pct"),
            quality.get("price_coverage_weight"),
            json.dumps(payload, default=str),
        ),
    )


def _upsert_classification(cur, payload: dict[str, Any], result: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO fact_13f_manager_classification
            (manager_cik, report_period, input_hash, prompt_version, classification_status,
             primary_label, confidence_score, quantitative_trigger_metric, route_tier,
             route_reason, evidence_source, model, response_json, error_type, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (manager_cik, report_period, input_hash, prompt_version) DO UPDATE SET
             classification_status = EXCLUDED.classification_status,
             primary_label = EXCLUDED.primary_label,
             confidence_score = EXCLUDED.confidence_score,
             quantitative_trigger_metric = EXCLUDED.quantitative_trigger_metric,
             route_tier = EXCLUDED.route_tier,
             route_reason = EXCLUDED.route_reason,
             evidence_source = EXCLUDED.evidence_source,
             model = EXCLUDED.model,
             response_json = EXCLUDED.response_json,
             error_type = EXCLUDED.error_type,
             error_message = EXCLUDED.error_message,
             created_at = now()
        """,
        (
            payload["cik"],
            payload["report_period"],
            payload["input_hash"],
            PROMPT_VERSION,
            result["classification_status"],
            result["primary_label"],
            result["confidence_score"],
            result["quantitative_trigger_metric"],
            result["route_tier"],
            result["route_reason"],
            result["evidence_source"],
            result["model"],
            json.dumps(result.get("response_json") or {}, default=str),
            result.get("error_type"),
            result.get("error_message"),
        ),
    )


def _latest_hash(cur, manager_cik: str, report_period: date) -> str | None:
    cur.execute(
        """
        SELECT input_hash
        FROM fact_13f_manager_classification
        WHERE manager_cik = %s AND report_period = %s AND prompt_version = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (manager_cik, report_period, PROMPT_VERSION),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _latest_manager_report_period(cur, manager_cik: str) -> date | None:
    cur.execute(
        """
        SELECT MAX(report_period)
        FROM fact_13f_holdings
        WHERE manager_cik = %s AND is_latest_amendment
        """,
        (manager_cik,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _upsert_entity_match(
    cur,
    manager_cik: str,
    manager_name: str,
    match: ReferenceMatch | None,
    conflict: str | None,
) -> str:
    if match:
        ref = match.reference
        status = "matched"
        values = (
            manager_cik,
            ref.reference_id,
            manager_name,
            ref.canonical_name,
            ref.target_label,
            match.match_type,
            match.score,
            match.matched_name,
            _reference_evidence(ref),
            None,
            status,
        )
    else:
        status = "conflict" if conflict else "unmatched"
        values = (
            manager_cik,
            None,
            manager_name,
            None,
            None,
            "none",
            None,
            None,
            None,
            conflict,
            status,
        )
    cur.execute(
        """
        INSERT INTO ref_13f_manager_entity_match
            (manager_cik, reference_id, manager_name, reference_name, target_label,
             match_type, match_score, matched_name, evidence_source, conflict_reason, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (manager_cik) DO UPDATE SET
             reference_id = EXCLUDED.reference_id,
             manager_name = EXCLUDED.manager_name,
             reference_name = EXCLUDED.reference_name,
             target_label = EXCLUDED.target_label,
             match_type = EXCLUDED.match_type,
             match_score = EXCLUDED.match_score,
             matched_name = EXCLUDED.matched_name,
             evidence_source = EXCLUDED.evidence_source,
             conflict_reason = EXCLUDED.conflict_reason,
             status = EXCLUDED.status,
             updated_at = now()
        """,
        values,
    )
    return status


def _backfill_reference_classification(
    cur,
    manager_cik: str,
    manager_name: str,
    match: ReferenceMatch,
    force: bool,
) -> bool:
    period = _latest_manager_report_period(cur, manager_cik)
    if not period:
        return False
    if not force:
        cur.execute(
            """
            SELECT 1
            FROM fact_13f_manager_classification
            WHERE manager_cik = %s
              AND classification_status = 'classified'
            LIMIT 1
            """,
            (manager_cik,),
        )
        if cur.fetchone():
            return False
    ref = match.reference
    input_hash = hashlib.sha256(
        json.dumps(
            {
                "manager_cik": manager_cik,
                "manager_name": manager_name,
                "reference": _reference_evidence(ref),
                "match_type": match.match_type,
                "match_score": round(match.score, 5),
                "report_period": str(period),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    confidence = 0.985 if match.match_type == "exact" else min(0.955, max(0.94, match.score))
    result = _classification(
        ref.target_label,
        confidence,
        f"reference_entity_match={_reference_evidence(ref)}",
        f"tier1_reference_link_{match.match_type}",
        f"Linked dim_13f_manager to curated {ref.source_category} reference: {ref.canonical_name}",
        _reference_evidence(ref),
    )
    result["response_json"] = {
        "reference_id": ref.reference_id,
        "reference_name": ref.canonical_name,
        "match_type": match.match_type,
        "match_score": match.score,
        "strategy_or_profile": ref.strategy_or_profile,
    }
    _upsert_classification(
        cur,
        {"cik": manager_cik, "report_period": period, "input_hash": input_hash},
        result,
    )
    return True


def link_manager_style_references(
    manager: str | None = None,
    limit: int | None = None,
    force: bool = False,
    backfill_classifications: bool = True,
) -> dict[str, int]:
    counts = {
        "candidates": 0,
        "matched": 0,
        "exact": 0,
        "fuzzy": 0,
        "conflict": 0,
        "unmatched": 0,
        "classifications_backfilled": 0,
    }
    with connect() as conn, conn.cursor() as cur:
        refs = _load_style_references(cur)
        params: list[Any] = []
        where = ""
        if manager:
            where = "WHERE manager_cik = %s"
            params.append(normalize_cik(manager))
        limit_sql = "LIMIT %s" if limit else ""
        if limit:
            params.append(limit)
        cur.execute(
            f"""
            SELECT manager_cik, manager_name
            FROM dim_13f_manager
            {where}
            ORDER BY manager_cik
            {limit_sql}
            """,
            params,
        )
        rows = cur.fetchall()
        counts["candidates"] = len(rows)
        for manager_cik, manager_name in rows:
            match, conflict = match_reference(manager_name or manager_cik, refs)
            status = _upsert_entity_match(cur, manager_cik, manager_name or manager_cik, match, conflict)
            counts[status] += 1
            if match:
                counts[match.match_type] += 1
                if backfill_classifications and _backfill_reference_classification(cur, manager_cik, manager_name or manager_cik, match, force):
                    counts["classifications_backfilled"] += 1
    return counts


def _manager_candidates(cur, manager: str | None = None, limit: int | None = None) -> list[str]:
    params: list[Any] = []
    where = ""
    if manager:
        where = "WHERE m.manager_cik = %s"
        params.append(normalize_cik(manager))
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    cur.execute(
        f"""
        WITH latest AS (
            SELECT manager_cik, MAX(report_period) AS report_period
            FROM fact_13f_holdings
            WHERE is_latest_amendment
            GROUP BY manager_cik
        )
        SELECT m.manager_cik
        FROM dim_13f_manager m
        JOIN latest l ON l.manager_cik = m.manager_cik
        {where}
        ORDER BY l.report_period DESC, m.manager_cik
        {limit_sql}
        """,
        params,
    )
    return [row[0] for row in cur.fetchall()]


def classify_13f_managers(
    manager: str | None = None,
    limit: int | None = None,
    force: bool = False,
    deterministic_only: bool = False,
    reference_only: bool = False,
) -> dict[str, int]:
    counts = {
        "candidates": 0,
        "classified": 0,
        "skipped": 0,
        "features_missing": 0,
        "errors": 0,
        "unresolved": 0,
        "reference_candidates": 0,
        "tier1_reference": 0,
        "tier1_rule": 0,
        "llm": 0,
    }
    with connect() as conn, conn.cursor() as cur:
        refs = _load_style_references(cur)
        managers = _manager_candidates(cur, manager=manager, limit=limit)
        counts["candidates"] = len(managers)
        for manager_cik in managers:
            if reference_only:
                cur.execute("SELECT manager_name FROM dim_13f_manager WHERE manager_cik = %s", (manager_cik,))
                manager_row = cur.fetchone()
                ref_match, _ref_conflict = match_reference(manager_row[0] if manager_row else "", refs)
                if not ref_match:
                    counts["unresolved"] += 1
                    continue
                counts["reference_candidates"] += 1
            payload = build_feature_payload(cur, manager_cik)
            if not payload:
                counts["features_missing"] += 1
                continue
            if not force and _latest_hash(cur, payload["cik"], payload["report_period"]) == payload["input_hash"]:
                counts["skipped"] += 1
                continue
            _upsert_feature_snapshot(cur, payload)
            cur.execute(
                """
                SELECT primary_label, confidence_score, quantitative_trigger_metric, route_reason
                FROM ref_13f_manager_classification_override
                WHERE manager_cik = %s
                """,
                (payload["cik"],),
            )
            override = cur.fetchone()
            if override:
                result = _classification(
                    override[0],
                    float(override[1]),
                    override[2] or "manual_override",
                    "tier0_manual_override",
                    override[3] or "Manual CIK override.",
                    "ref_13f_manager_classification_override",
                )
            else:
                result, conflict = deterministic_classify(payload, refs)
                if not result:
                    if deterministic_only:
                        counts["unresolved"] += 1
                        continue
                    result = llm_classify(payload, conflict)
            _upsert_classification(cur, payload, result)
            if result["classification_status"] == "classified":
                counts["classified"] += 1
            else:
                counts["errors"] += 1
            route = result.get("route_tier") or ""
            if route.startswith("tier1_reference"):
                counts["tier1_reference"] += 1
            elif route == "tier1_rule" or route == "tier0_manual_override":
                counts["tier1_rule"] += 1
            elif "deepseek" in route:
                counts["llm"] += 1
    return counts
