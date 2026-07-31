from __future__ import annotations

from html import unescape
import re
import warnings


_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "table", "section", "article", "header",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6",
}


def soup_from_html(html: str):
    try:
        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 is required for MD&A extraction") from exc
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        return BeautifulSoup(html, "lxml")


def clean_html_to_text(html: str) -> str:
    html = unescape(html or "")
    soup = soup_from_html(html)
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.name and tag.name.lower() in _BLOCK_TAGS:
            tag.insert_before("\n")
            tag.insert_after("\n")
    text = soup.get_text(" ")
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_plain_text(text: str) -> str:
    text = unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
