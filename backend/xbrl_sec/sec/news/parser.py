"""Small dependency-free RSS/Atom parser."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import re
from urllib.parse import urljoin
from xml.etree import ElementTree


@dataclass(frozen=True)
class NewsArticle:
    feed_key: str
    title: str
    url: str
    summary: str
    author: str | None
    published_at: datetime | None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return " ".join(text.split())


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(xml_bytes: bytes, feed_key: str, feed_url: str) -> list[NewsArticle]:
    root = ElementTree.fromstring(xml_bytes)
    entries = [
        node for node in root.iter()
        if _local_name(node.tag) in {"item", "entry"}
    ]
    articles: list[NewsArticle] = []
    for entry in entries:
        title = _clean_html(_child_text(entry, "title"))
        summary = _clean_html(_child_text(entry, "summary", "description", "content", "encoded"))
        author = _clean_html(_child_text(entry, "author", "creator")) or None
        published = _parse_date(_child_text(entry, "published", "pubdate", "updated", "date"))
        link = _child_text(entry, "link", "guid")
        if not link:
            for child in list(entry):
                if _local_name(child.tag) == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        link = urljoin(feed_url, link.strip())
        if title and link:
            articles.append(NewsArticle(feed_key, title, link, summary, author, published))
    return articles
