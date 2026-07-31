"""Article download and main-text extraction."""
from __future__ import annotations

from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from xbrl_sec.sec.settings import load_settings


def _download(url: str) -> bytes:
    settings = load_settings()
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MZQA-News/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=settings.news_fetch_timeout_seconds) as response:
        return response.read(2_000_000)


def extract_article_text(url: str, fallback: str = "") -> str:
    try:
        raw = _download(url)
    except Exception:
        return " ".join((fallback or "").split())[:20_000]

    try:
        import trafilatura
    except ImportError:
        trafilatura = None

    if trafilatura is not None:
        extracted = trafilatura.extract(
            raw.decode("utf-8", errors="replace"),
            include_comments=False,
            include_tables=False,
            favor_recall=True,
            url=url,
        )
        if extracted and len(extracted.strip()) >= 300:
            return " ".join(extracted.split())[:20_000]

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
        tag.decompose()
    article = soup.find("article") or soup.find("main") or soup.body
    text = article.get_text(" ", strip=True) if article else ""
    return " ".join((text or fallback or "").split())[:20_000]
