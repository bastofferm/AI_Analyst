"""SEC filing text-extraction subgraph (opt-in).

Activated by `extract_sections=True` in the SEC daily graph. Reads the
filing HTML, chunks by Item heading (1A Risk Factors, 7 MD&A, 9A Controls),
runs ChatDeepSeek with the FilingSectionExtract schema for each chunk, and
writes the structured result to sec.filing_section_extract.

The HTTP fetch uses the existing SEC.gov polite-rate pattern (User-Agent
header) — heavy parsing libraries are intentionally avoided; we rely on a
regex item-splitter that is robust enough for 10-K/10-Q layouts.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterable

import httpx

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import FilingSectionExtract
from xbrl_sec.sec.db.connection import connect


_USER_AGENT = os.environ.get("SEC_HTTP_USER_AGENT") or "MZQA Research mzqa@example.com"
_ITEM_HEADERS = ("1A", "7", "7A", "9A")
_SECTION_MODEL_VERSION = "deepseek-v4-flash:1"
_ITEM_HEADING_RE = re.compile(
    r"(?im)\bItem\s+(1A|7A|7|9A)\.?\s+([A-Z][A-Za-z' ,\-/]+)"
)
_MAX_CHUNK_CHARS = 18000


def _filing_html_url(cik: str, accession: str) -> str:
    accession_compact = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_compact}"
    return f"{base}/{accession}-index.htm"


def _download_filing_text(url: str, *, timeout: float = 30.0) -> str:
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"network error fetching {url}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"http {response.status_code} for {url}: {response.text[:200]}")
    text = response.text or ""
    return re.sub(r"<[^>]+>", " ", text)


def _chunk_by_item_heading(text: str) -> dict[str, str]:
    matches = list(_ITEM_HEADING_RE.finditer(text))
    if not matches:
        return {}
    chunks: dict[str, str] = {}
    for index, match in enumerate(matches):
        item = match.group(1).upper()
        if item not in _ITEM_HEADERS:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        snippet = text[start:end].strip()
        if not snippet:
            continue
        chunks[item] = snippet[:_MAX_CHUNK_CHARS]
    return chunks


_EXTRACT_INSTRUCTIONS = (
    "Read the filing item below and emit a FilingSectionExtract. "
    "summary should be 3-5 sentences. key_risks is 3-7 short bullet labels. "
    "sentiment classifies the overall tone."
)


def _extract_section(
    filing_id: str,
    cik: str | None,
    accession: str | None,
    item: str,
    text_excerpt: str,
    llm: ChatDeepSeek,
) -> FilingSectionExtract | None:
    structured = llm.with_structured_output(FilingSectionExtract)
    prompt = (
        f"{_EXTRACT_INSTRUCTIONS}\n\n"
        f"filing_id: {filing_id}\n"
        f"item: {item}\n\n"
        f"text_excerpt:\n{text_excerpt}"
    )
    try:
        result = structured.invoke(prompt)
    except Exception:
        return None
    return result.model_copy(
        update={
            "filing_id": filing_id,
            "item": item,
            "text_excerpt": text_excerpt[:_MAX_CHUNK_CHARS],
            "model_version": _SECTION_MODEL_VERSION,
        }
    )


def _persist_extract(
    extract: FilingSectionExtract,
    *,
    cik: str | None,
    accession: str | None,
) -> None:
    sql = """
        INSERT INTO sec.filing_section_extract
            (filing_id, cik, accession, item, text_excerpt, summary,
             key_risks, sentiment, model_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (filing_id, item, model_version) DO UPDATE SET
            text_excerpt = EXCLUDED.text_excerpt,
            summary = EXCLUDED.summary,
            key_risks = EXCLUDED.key_risks,
            sentiment = EXCLUDED.sentiment,
            extracted_at = NOW()
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            sql,
            (
                extract.filing_id,
                cik,
                accession,
                extract.item,
                extract.text_excerpt,
                extract.summary,
                json.dumps(extract.key_risks),
                extract.sentiment,
                extract.model_version,
            ),
        )


def extract_filing_sections(
    filings: Iterable[tuple[str, str, str]],
    *,
    llm: ChatDeepSeek | None = None,
) -> dict[str, int]:
    """Extract sections for each `(filing_id, cik, accession)` triple.

    Returns aggregate counts. Network and parsing errors per filing are
    swallowed and counted in `errors` — the daily graph keeps moving.
    """
    llm = llm or ChatDeepSeek(model="deepseek-v4-flash", temperature=0.1, max_tokens=1800)
    extracted = empty = errors = 0
    for filing_id, cik, accession in filings:
        try:
            text = _download_filing_text(_filing_html_url(cik, accession))
        except Exception:
            errors += 1
            continue
        chunks = _chunk_by_item_heading(text)
        if not chunks:
            empty += 1
            continue
        for item, snippet in chunks.items():
            extract = _extract_section(filing_id, cik, accession, item, snippet, llm)
            if extract is None:
                errors += 1
                continue
            try:
                _persist_extract(extract, cik=cik, accession=accession)
                extracted += 1
            except Exception:
                errors += 1
    return {"extracted": extracted, "empty_filings": empty, "errors": errors}


def fetch_recent_unparsed_filings(jurisdiction: str = "US", limit: int = 10) -> list[tuple[str, str, str]]:
    """Return (filing_id, cik, accession) for filings without a section extract."""
    sql = """
        SELECT s.filing_id, s.entity_id, s.filing_id
        FROM source_filing_state s
        LEFT JOIN sec.filing_section_extract e ON e.filing_id = s.filing_id
        WHERE s.jurisdiction = %s
          AND s.parsed = TRUE
          AND e.filing_id IS NULL
        ORDER BY s.updated_at DESC
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (jurisdiction, limit))
            return [(row[0], row[1], row[2]) for row in cur.fetchall()]
    except Exception:
        return []
