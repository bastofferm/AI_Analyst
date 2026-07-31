"""GICS enrichment LLM fallback for EDINET filers.

Der existierende `enrich_jp_gics`-Workflow verlässt sich auf TSE33→GICS-CSV-
Mappings (siehe Migration 010). Wenn ein Filer keine Mapping-Zeile hat, fallen
wir hier auf einen DeepSeek-Agent zurück: der bekommt den Firmennamen +
japanische Branchen-Beschreibung und Tools, mit denen er ähnliche bereits
gemappte Firmen findet sowie die GICS-Taxonomie nachschlägt. Output ist eine
`GicsSuggestion`. Confidence < 0.7 → Approval-Queue.
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import GicsSuggestion
from xbrl_sec.sec.db.connection import connect


_AUTO_PROMOTE_THRESHOLD = 0.7


@tool
def query_similar_jp_filers(edinet_code: str, limit: int = 8) -> list[dict[str, Any]]:
    """Return EDINET filers whose JP industry-code is the same as the target,
    together with their already-resolved GICS codes."""
    sql = """
        WITH target AS (
            SELECT industry_code FROM dim_company_jp WHERE edinet_code = %s
        )
        SELECT c.edinet_code, c.name_en, c.industry_code,
               c.gics_sector_code, c.gics_industry_group_code,
               c.gics_industry_code, c.gics_sub_industry_code
        FROM dim_company_jp c, target t
        WHERE c.industry_code = t.industry_code
          AND c.gics_sector_code IS NOT NULL
          AND c.edinet_code <> %s
        ORDER BY c.edinet_code
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (edinet_code, edinet_code, limit))
            return [
                {
                    "edinet_code": row[0],
                    "name_en": row[1],
                    "industry_code": row[2],
                    "gics_sector": row[3],
                    "gics_industry_group": row[4],
                    "gics_industry": row[5],
                    "gics_sub_industry": row[6],
                }
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)[:200]}]


@tool
def lookup_gics_taxonomy(prefix: str = "", limit: int = 25) -> list[dict[str, Any]]:
    """List GICS taxonomy entries starting with the given sector/industry prefix."""
    sql = """
        SELECT sector_code, sector_name, industry_group_code, industry_group_name,
               industry_code, industry_name, sub_industry_code, sub_industry_name
        FROM ref_gics_taxonomy
        WHERE COALESCE(sector_code::text, '') LIKE %s
        ORDER BY sector_code, industry_group_code, industry_code, sub_industry_code
        LIMIT %s
    """
    pattern = f"{prefix}%" if prefix else "%"
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (pattern, limit))
            return [
                {
                    "sector_code": str(row[0]) if row[0] is not None else None,
                    "sector_name": row[1],
                    "industry_group_code": str(row[2]) if row[2] is not None else None,
                    "industry_group_name": row[3],
                    "industry_code": str(row[4]) if row[4] is not None else None,
                    "industry_name": row[5],
                    "sub_industry_code": str(row[6]) if row[6] is not None else None,
                    "sub_industry_name": row[7],
                }
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)[:200]}]


@tool
def lookup_translation_alias(name_jp: str) -> dict[str, Any]:
    """Return the English-name and industry blurb for a known EDINET filer."""
    sql = """
        SELECT edinet_code, name_en, industry_code, business_description
        FROM dim_company_jp
        WHERE name_jp = %s OR name_en = %s
        LIMIT 1
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (name_jp, name_jp))
            row = cur.fetchone()
            if not row:
                return {"found": False}
            return {
                "found": True,
                "edinet_code": row[0],
                "name_en": row[1],
                "industry_code": row[2],
                "business_description": row[3],
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def _fetch_filer_profile(edinet_code: str) -> dict[str, Any]:
    sql = """
        SELECT edinet_code, name_en, name_jp, industry_code, business_description, ticker_jp
        FROM dim_company_jp
        WHERE edinet_code = %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (edinet_code,))
            row = cur.fetchone()
            if not row:
                return {"edinet_code": edinet_code}
            return {
                "edinet_code": row[0],
                "name_en": row[1],
                "name_jp": row[2],
                "industry_code": row[3],
                "business_description": row[4],
                "ticker_jp": row[5],
            }
    except Exception:
        return {"edinet_code": edinet_code}


def _apply_suggestion(suggestion: GicsSuggestion) -> str:
    """Persist the suggestion. confidence >= threshold → write to dim_company_jp,
    otherwise queue an approval row via the caller; here we only update the
    dim_company_jp shadow columns."""
    if suggestion.confidence < _AUTO_PROMOTE_THRESHOLD:
        return "queued_for_review"
    sql = """
        UPDATE dim_company_jp
        SET gics_sector_code = COALESCE(%s, gics_sector_code),
            gics_industry_group_code = COALESCE(%s, gics_industry_group_code),
            gics_industry_code = COALESCE(%s, gics_industry_code),
            gics_sub_industry_code = COALESCE(%s, gics_sub_industry_code),
            gics_source = 'deepseek_gics_enrich_agent',
            updated_at = NOW()
        WHERE edinet_code = %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    suggestion.suggested_gics_sector,
                    suggestion.suggested_gics_industry_group,
                    suggestion.suggested_gics_industry,
                    suggestion.suggested_gics_sub_industry,
                    suggestion.edinet_code,
                ),
            )
        return "auto_promoted"
    except Exception:
        return "rejected"


_INSTRUCTIONS = (
    "You assign a GICS classification to a Japanese EDINET filer when the "
    "deterministic TSE33→GICS mapping has no row. Use the available tools to "
    "(a) find similar filers with the same JP industry_code and their GICS "
    "codes, (b) inspect the GICS taxonomy, (c) translate Japanese name aliases. "
    "Return one GicsSuggestion. Set confidence >= 0.7 only if at least two "
    "comparable filers agree on the sector. Otherwise set lower confidence."
)


def _build_agent(llm: ChatDeepSeek | None) -> ChatDeepSeek:
    llm = llm or ChatDeepSeek(model="deepseek-v4-flash", temperature=0.1, max_tokens=1200)
    llm.bind_tools([query_similar_jp_filers, lookup_gics_taxonomy, lookup_translation_alias])
    return llm


def suggest_gics_for_filer(
    edinet_code: str,
    *,
    llm: ChatDeepSeek | None = None,
) -> GicsSuggestion:
    """Run the agent for a single filer; persist the suggestion."""
    chat = _build_agent(llm)
    structured = chat.with_structured_output(GicsSuggestion)
    profile = _fetch_filer_profile(edinet_code)
    prompt = (
        f"{_INSTRUCTIONS}\n\n"
        f"edinet_code: {edinet_code}\n"
        f"profile: {profile}\n"
    )
    try:
        suggestion = structured.invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        suggestion = GicsSuggestion(
            edinet_code=edinet_code,
            suggested_gics_sector="UNKNOWN",
            rationale=f"LLM error: {exc}",
            similar_filers=[],
            confidence=0.0,
        )
    if suggestion.edinet_code != edinet_code:
        suggestion = suggestion.model_copy(update={"edinet_code": edinet_code})
    _apply_suggestion(suggestion)
    return suggestion


def fetch_unmapped_jp_filers(limit: int = 25) -> list[str]:
    sql = """
        SELECT edinet_code
        FROM dim_company_jp
        WHERE include_in_pipeline = TRUE
          AND gics_sector_code IS NULL
        ORDER BY edinet_code
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def enrich_unmapped_gics(
    *,
    limit: int = 25,
    llm: ChatDeepSeek | None = None,
) -> dict[str, int]:
    targets = fetch_unmapped_jp_filers(limit)
    auto = review = rejected = 0
    for edinet_code in targets:
        suggestion = suggest_gics_for_filer(edinet_code, llm=llm)
        if suggestion.confidence >= _AUTO_PROMOTE_THRESHOLD:
            auto += 1
        elif suggestion.confidence >= 0.3:
            review += 1
        else:
            rejected += 1
    return {
        "candidates": len(targets),
        "auto_promoted": auto,
        "queued_for_review": review,
        "rejected": rejected,
    }
