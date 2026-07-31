from __future__ import annotations

import re

from xbrl_sec.sec.mda.text import clean_html_to_text, normalize_plain_text


_START_PATTERNS = {
    "item_7": re.compile(r"(?is)(?:^|\n)\s*item\s+7[\.\)\s:-]+.{0,200}?management(?:['’]s)?\s+discussion"),
    "item_2": re.compile(r"(?is)(?:^|\n)\s*item\s+2[\.\)\s:-]+.{0,200}?management(?:['’]s)?\s+discussion"),
    "item_7a": re.compile(
        r"(?is)(?:^|\n)\s*item\s+7a[\.\)\s:-]+quantitative\s+and\s+qualitative\s+disclosures?\s+about\s+market\s+risk"
    ),
}
_PART_OR_SIG = re.compile(r"(?im)^\s*(part\s+[ivx]+|signatures|exhibit\s+index)\b")


def _stop_pos(text: str, section_id: str, start: int) -> int:
    search_from = start + 40
    candidates = []
    if section_id == "item_7":
        for pattern in (
            re.compile(r"(?im)^\s*item\s+7a[\.\)\s:-]+"),
            re.compile(r"(?im)^\s*item\s+8[\.\)\s:-]+"),
        ):
            match = pattern.search(text, search_from)
            if match:
                candidates.append(match.start())
    elif section_id == "item_7a":
        match = re.search(r"(?im)^\s*item\s+8[\.\)\s:-]+", text[search_from:])
        if match:
            candidates.append(search_from + match.start())
    elif section_id == "item_2":
        for pattern in (
            re.compile(r"(?im)^\s*item\s+3[\.\)\s:-]+"),
            re.compile(r"(?im)^\s*part\s+ii\b"),
        ):
            match = pattern.search(text, search_from)
            if match:
                candidates.append(match.start())
    for pattern in (_PART_OR_SIG,):
        match = pattern.search(text, search_from)
        if match:
            candidates.append(match.start())
    return min(candidates) if candidates else len(text)


def extract_html_section(html: str, section_id: str) -> str | None:
    pattern = _START_PATTERNS.get(section_id)
    if not pattern:
        return None
    text = normalize_plain_text(clean_html_to_text(html))
    if not text:
        return None
    candidates = []
    for match in pattern.finditer(text):
        end = _stop_pos(text, section_id, match.start())
        candidate = normalize_plain_text(text[match.start():end])
        lowered = candidate[:800].lower()
        if "table of contents" in lowered and len(candidate) < 2000:
            continue
        if candidate:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=len)
