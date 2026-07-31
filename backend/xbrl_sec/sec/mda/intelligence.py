from __future__ import annotations

import json
import os
import re
from urllib.request import Request, urlopen

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


def _snippets(text: str, pattern: re.Pattern, max_snippets: int = 5) -> list[str]:
    out = []
    for match in pattern.finditer(text):
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 100)
        out.append(re.sub(r"\s+", " ", text[start:end]).strip())
        if len(out) >= max_snippets:
            break
    return out


def _sections(jurisdiction: str, entity_id: str | None = None, filing_id: str | None = None, limit: int | None = None):
    jurisdiction = jurisdiction.upper()
    params: list = []
    where = "WHERE jurisdiction = %s AND section_text IS NOT NULL"
    params.append(jurisdiction)
    if entity_id:
        where += " AND entity_id = %s"
        params.append(entity_id.zfill(10) if jurisdiction == "US" else entity_id)
    if filing_id:
        where += " AND filing_id = %s"
        params.append(filing_id)
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT entity_id, filing_id, section_id, section_text
            FROM vw_mda_sections
            {where}
            ORDER BY filed_date DESC NULLS LAST, filing_id
            {limit_sql}
            """,
            params,
        )
        return cur.fetchall()


def keywords(jurisdiction: str = "US", entity_id: str | None = None, filing_id: str | None = None, limit: int | None = None) -> dict[str, int]:
    jurisdiction = jurisdiction.upper()
    ctx = start_run(jurisdiction, "mda_keywords", "incremental")
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT keyword_id, regex_pattern, case_insensitive
                FROM ref_mda_keyword_lexicon
                WHERE enabled
                ORDER BY keyword_id
                """
            )
            lexicon = cur.fetchall()
        sections = _sections(jurisdiction, entity_id=entity_id, filing_id=filing_id, limit=limit)
        rows = []
        for eid, fid, section_id, text in sections:
            for keyword_id, regex_pattern, case_insensitive in lexicon:
                flags = re.I if case_insensitive else 0
                try:
                    pattern = re.compile(regex_pattern, flags)
                except re.error:
                    continue
                matches = list(pattern.finditer(text or ""))
                if not matches:
                    continue
                rows.append((jurisdiction, eid, fid, section_id, keyword_id, len(matches), _snippets(text, pattern)))
        written = 0
        if rows:
            with connect() as conn, conn.cursor() as cur:
                written = execute_values(
                    cur,
                    """
                    INSERT INTO fact_mda_keyword_hits
                        (jurisdiction, entity_id, filing_id, section_id, keyword_id, match_count, context_snippets)
                    VALUES %s
                    ON CONFLICT (jurisdiction, entity_id, filing_id, section_id, keyword_id)
                    DO UPDATE SET
                        match_count = EXCLUDED.match_count,
                        context_snippets = EXCLUDED.context_snippets,
                        scored_at = now()
                    """,
                    rows,
                    page_size=5000,
                )
        finish_run(ctx, "succeeded", rows_in=len(sections), rows_out=written)
        return {"sections": len(sections), "keyword_hits": written}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def _deepseek_summary(text: str, model: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a financial analyst extracting concise structured insights from MD&A text. Return valid JSON only.",
            },
            {
                "role": "user",
                "content": (
                    "Analyze this MD&A section. Return JSON with keys: summary, risk_factors, "
                    "opportunities, outlook. Do not invent facts.\n\n" + text[:60000]
                ),
            },
        ],
        "temperature": 0.1,
    }
    req = Request(
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=120) as response:
        raw = json.loads(response.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def summarize(
    jurisdiction: str = "US",
    entity_id: str | None = None,
    filing_id: str | None = None,
    section_id: str | None = None,
    model: str = "deepseek-chat",
    limit: int = 1,
) -> dict[str, int]:
    jurisdiction = jurisdiction.upper()
    sections = _sections(jurisdiction, entity_id=entity_id, filing_id=filing_id, limit=limit)
    rows = []
    for eid, fid, sid, text in sections:
        if section_id and sid != section_id:
            continue
        result = _deepseek_summary(text or "", model)
        rows.append((
            jurisdiction,
            eid,
            fid,
            sid,
            model,
            result.get("summary") or "",
            result.get("risk_factors") or [],
            result.get("opportunities") or [],
            result.get("outlook"),
            len(text or ""),
        ))
    written = 0
    if rows:
        with connect() as conn, conn.cursor() as cur:
            written = execute_values(cur, """
                INSERT INTO fact_mda_summary_cache
                    (jurisdiction, entity_id, filing_id, section_id, model, summary_text,
                     risk_factors, opportunities, outlook, source_chars)
                VALUES %s
                ON CONFLICT (jurisdiction, entity_id, filing_id, section_id, model)
                DO UPDATE SET
                    summary_text = EXCLUDED.summary_text,
                    risk_factors = EXCLUDED.risk_factors,
                    opportunities = EXCLUDED.opportunities,
                    outlook = EXCLUDED.outlook,
                    source_chars = EXCLUDED.source_chars,
                    updated_at = now()
            """, rows)
    return {"summaries": written}
