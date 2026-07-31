from __future__ import annotations

import re
from typing import Any

from ._db import read_sql


MDA_EXCERPT_CHARS = 3600
MDA_MAX_RECENT_FILINGS = 3
MAX_PEERS = 10

MDA_SYSTEM_PROMPT = (
    "You are a buy-side analyst scoring management's qualitative MD&A guidance. "
    "Use only the supplied XBRL-HTML-derived MD&A excerpts, weighting the most recent filing "
    "most heavily and older filings as context. Return JSON with a `companies` array. Each item must have: "
    "`ticker`, `tone_score` (-1 to 1), `guidance` (positive, neutral, or negative), "
    "`summary` (one sentence), `buzzword_headlines` (3 to 5 short headline-style phrases), "
    "and `risk_flags` (0 to 3 short strings). Keep summaries and flags brief. "
    "Do not invent facts outside the excerpts."
)


def fetch_latest_mda_texts(items: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Backward-compatible wrapper for the recency-weighted MD&A fetch."""
    return fetch_recent_mda_texts(items, max_filings=MDA_MAX_RECENT_FILINGS)


def fetch_recent_mda_texts(
    items: list[dict[str, str]],
    *,
    max_filings: int = MDA_MAX_RECENT_FILINGS,
) -> dict[str, dict[str, Any]]:
    """Fetch recent MD&A snippets for ticker/jurisdiction pairs using local XBRL HTML extracts."""
    out: dict[str, dict[str, Any]] = {}
    by_jurisdiction: dict[str, list[str]] = {}
    for item in items:
        ticker = str(item.get("ticker") or "").upper().strip()
        jurisdiction = str(item.get("jurisdiction") or "US").upper().strip()
        if ticker:
            by_jurisdiction.setdefault(jurisdiction, []).append(ticker)

    for jurisdiction, tickers in by_jurisdiction.items():
        if jurisdiction == "JP":
            sql = """
                WITH ranked AS (
                    SELECT d.primary_ticker AS ticker, m.filing_id, m.section_id,
                           m.doc_type_code AS form_type, m.section_text, m.filed_date, m.char_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY UPPER(d.primary_ticker)
                               ORDER BY m.filed_date DESC NULLS LAST, m.char_count DESC NULLS LAST
                           ) AS rn
                    FROM fact_mda_sections_jp m
                    JOIN dim_company_jp d ON d.edinet_code = m.edinet_code
                    WHERE UPPER(d.primary_ticker) = ANY(%(tickers)s)
                      AND m.section_text IS NOT NULL
                )
                SELECT * FROM ranked
                WHERE rn <= %(max_filings)s
                ORDER BY UPPER(ticker), rn
            """
        else:
            sql = """
                WITH ranked AS (
                    SELECT d.primary_ticker AS ticker, m.filing_id, m.section_id, m.form_type,
                           m.section_text, m.filed_date, m.char_count,
                           ROW_NUMBER() OVER (
                               PARTITION BY UPPER(d.primary_ticker)
                               ORDER BY m.filed_date DESC NULLS LAST,
                                        CASE m.section_id
                                            WHEN 'item_2' THEN 0
                                            WHEN 'item_7' THEN 1
                                            ELSE 2
                                        END,
                                        m.char_count DESC NULLS LAST
                           ) AS rn
                    FROM fact_mda_sections_us m
                    JOIN dim_company_us d ON d.cik = m.cik
                    WHERE UPPER(d.primary_ticker) = ANY(%(tickers)s)
                      AND m.section_id IN ('item_2', 'item_7')
                      AND m.section_text IS NOT NULL
                )
                SELECT * FROM ranked
                WHERE rn <= %(max_filings)s
                ORDER BY UPPER(ticker), rn
            """
        try:
            df = read_sql(sql, {"tickers": [t.upper() for t in tickers], "max_filings": max(1, max_filings)})
        except Exception:
            continue
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in _records(df):
            ticker = str(row.get("ticker") or "").upper()
            if not ticker:
                continue
            grouped.setdefault(ticker, []).append(row)
        for ticker, rows in grouped.items():
            text = _combine_recent_filings(rows, jurisdiction=jurisdiction)
            out[ticker] = {
                "ticker": ticker,
                "jurisdiction": jurisdiction,
                "text": text,
                "filed_date": _date_text(rows[0].get("filed_date")),
                "filings": [_filing_meta(row, jurisdiction=jurisdiction) for row in rows],
            }
    return out


def build_mda_user_prompt(records: list[dict[str, Any]]) -> str:
    chunks = []
    for record in records:
        ticker = str(record.get("ticker") or "").upper()
        role = record.get("role") or "peer"
        text = clean_text(record.get("text") or "")[:MDA_EXCERPT_CHARS]
        if not ticker or not text:
            continue
        chunks.append(
            f"### {ticker} ({role})\n"
            "Source: local XBRL HTML MD&A extraction; highest recency weight appears first.\n"
            f"{text}"
        )
    return "\n\n".join(chunks)


def no_key_analysis(ticker: str, jurisdiction: str, mda_text: str | None, peer_count: int) -> dict[str, Any]:
    warnings = ["No DeepSeek key - MD&A guidance score was not generated."]
    if not clean_text(mda_text):
        warnings.append("No MD&A text found for analyzed company.")
    return base_analysis(ticker, jurisdiction, mda_text, warnings=warnings, peer_count=peer_count)


def base_analysis(
    ticker: str,
    jurisdiction: str,
    mda_text: str | None,
    *,
    warnings: list[str] | None = None,
    peer_count: int = 0,
) -> dict[str, Any]:
    excerpt = clean_text(mda_text or "")
    return {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "tone_score": None,
        "guidance": None,
        "peer_percentile": None,
        "peer_rank": None,
        "peer_count": peer_count,
        "summary": None,
        "buzzword_headlines": [],
        "risk_flags": [],
        "raw_excerpt": excerpt[:900] if excerpt else None,
        "warnings": list(warnings or []),
    }


def analysis_from_llm_response(
    *,
    ticker: str,
    jurisdiction: str,
    llm_data: dict[str, Any],
    peer_tickers: list[str],
    mda_text: str | None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    rows = _company_rows(llm_data)
    by_ticker = {str(row.get("ticker") or "").upper(): row for row in rows}
    target_row = by_ticker.get(ticker.upper())
    if not target_row:
        fallback = base_analysis(
            ticker,
            jurisdiction,
            mda_text,
            warnings=[*(warnings or []), "Model response did not include analyzed company MD&A score."],
            peer_count=len(peer_tickers),
        )
        return fallback

    target_score = clamp_score(target_row.get("tone_score", target_row.get("tone")))
    peer_scores = [
        clamp_score((by_ticker.get(peer.upper()) or {}).get("tone_score", (by_ticker.get(peer.upper()) or {}).get("tone")))
        for peer in peer_tickers
    ]
    peer_scores = [score for score in peer_scores if score is not None]
    rank, percentile = peer_rank_and_percentile(target_score, peer_scores)

    return {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "tone_score": target_score,
        "guidance": _guidance(target_row.get("guidance"), target_score),
        "peer_percentile": percentile,
        "peer_rank": rank,
        "peer_count": len(peer_scores),
        "summary": _short_text(target_row.get("summary") or target_row.get("note"), 260),
        "buzzword_headlines": _string_list(target_row.get("buzzword_headlines") or target_row.get("headlines"), 5, 80),
        "risk_flags": _string_list(target_row.get("risk_flags"), 3, 90),
        "raw_excerpt": clean_text(mda_text or "")[:900] if mda_text else None,
        "warnings": list(warnings or []),
    }


def clamp_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:
        return None
    return max(-1.0, min(1.0, round(score, 3)))


def peer_rank_and_percentile(target_score: float | None, peer_scores: list[float]) -> tuple[int | None, float | None]:
    if target_score is None or not peer_scores:
        return None, None
    all_scores = sorted([target_score, *peer_scores], reverse=True)
    rank = all_scores.index(target_score) + 1
    below = sum(1 for score in peer_scores if score < target_score)
    equal = sum(1 for score in peer_scores if score == target_score)
    percentile = round((below + 0.5 * equal) / len(peer_scores) * 100.0, 1)
    return rank, percentile


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _combine_recent_filings(rows: list[dict[str, Any]], *, jurisdiction: str) -> str:
    if not rows:
        return ""
    budgets = _recency_budgets(len(rows), MDA_EXCERPT_CHARS)
    chunks: list[str] = []
    for index, row in enumerate(rows):
        text = clean_text(row.get("section_text") or "")
        if not text:
            continue
        weight = _recency_weight(index)
        header = (
            f"[{_date_text(row.get('filed_date')) or 'undated'} "
            f"{clean_text(row.get('form_type') or '')} "
            f"{clean_text(row.get('section_id') or '')}; "
            f"filing {clean_text(row.get('filing_id') or '')}; "
            f"recency_weight={weight:.0%}; "
            f"source=sec.fact_mda_sections_{jurisdiction.lower()}]"
        )
        chunks.append(f"{header} {text[:budgets[index]].rstrip()}")
    return clean_text(" ".join(chunks))[:MDA_EXCERPT_CHARS]


def _recency_budgets(count: int, total_chars: int) -> list[int]:
    weights = [_recency_weight(index) for index in range(count)]
    total_weight = sum(weights) or 1.0
    budgets = [max(400, int(total_chars * weight / total_weight)) for weight in weights]
    overflow = sum(budgets) - total_chars
    if overflow > 0:
        budgets[-1] = max(250, budgets[-1] - overflow)
    return budgets


def _recency_weight(index: int) -> float:
    weights = (0.55, 0.30, 0.15)
    if index < len(weights):
        return weights[index]
    return 0.05


def _filing_meta(row: dict[str, Any], *, jurisdiction: str) -> dict[str, Any]:
    return {
        "filing_id": clean_text(row.get("filing_id") or "") or None,
        "filed_date": _date_text(row.get("filed_date")),
        "form_type": clean_text(row.get("form_type") or "") or None,
        "section_id": clean_text(row.get("section_id") or "") or None,
        "char_count": row.get("char_count"),
        "source_path": f"sec.fact_mda_sections_{jurisdiction.lower()}",
    }


def _company_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("companies")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if data.get("ticker"):
        return [data]
    return []


def _guidance(value: Any, score: float | None) -> str | None:
    text = clean_text(value).lower()
    if text in {"positive", "neutral", "negative"}:
        return text
    if score is None:
        return None
    if score >= 0.2:
        return "positive"
    if score <= -0.2:
        return "negative"
    return "neutral"


def _string_list(value: Any, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _short_text(item, max_chars)
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _short_text(value: Any, max_chars: int) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    return text[:max_chars].rstrip()


def _date_text(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = clean_text(value)
    return text or None


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return df.to_dict(orient="records")
    return []
