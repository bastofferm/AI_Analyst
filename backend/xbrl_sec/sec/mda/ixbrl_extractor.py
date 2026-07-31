from __future__ import annotations

from html import unescape

from xbrl_sec.sec.mda.text import clean_html_to_text, soup_from_html


_CONCEPTS = {
    "item_7": (
        "managementdiscussionandanalysistextblock",
        "managementsdiscussionandanalysistextblock",
    ),
    "item_2": (
        "managementdiscussionandanalysistextblock",
        "managementsdiscussionandanalysistextblock",
    ),
    "item_7a": (
        "quantitativeandqualitativedisclosuresaboutmarketrisktextblock",
    ),
}


def extract_ixbrl_textblock(html: str, section_id: str) -> str | None:
    concepts = _CONCEPTS.get(section_id, ())
    if not concepts:
        return None
    soup = soup_from_html(html)
    matches = []
    for tag in soup.find_all(True):
        name = str(tag.get("name") or tag.get("contextref") or "")
        local = name.split(":")[-1].lower()
        if local in concepts:
            raw = tag.decode_contents() or tag.get_text(" ")
            text = clean_html_to_text(unescape(raw))
            if text:
                matches.append(text)
    if not matches:
        return None
    return max(matches, key=len)
