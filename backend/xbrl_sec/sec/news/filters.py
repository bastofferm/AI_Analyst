"""Watchlist matching and fast-lane classification."""
from __future__ import annotations

from dataclasses import dataclass
import re

from xbrl_sec.sec.db.connection import connect


@dataclass(frozen=True)
class WatchItem:
    ticker: str
    market: str
    proxy_terms: tuple[str, ...]


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def load_watchlist() -> list[WatchItem]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, market, proxy_terms
            FROM news.watchlist
            WHERE enabled
            ORDER BY ticker
        """)
        return [WatchItem(row[0], row[1], tuple(row[2] or ())) for row in cur.fetchall()]


def load_urgency_triggers() -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT phrase
            FROM news.urgency_triggers
            WHERE enabled
            ORDER BY weight DESC, phrase
        """)
        return [row[0] for row in cur.fetchall()]


def matching_tickers(text: str, watchlist: list[WatchItem]) -> list[str]:
    haystack = normalize(text)
    matches: list[str] = []
    for item in watchlist:
        terms = (item.ticker, *item.proxy_terms)
        if any(normalize(term) in haystack for term in terms if normalize(term)):
            matches.append(item.ticker)
    return matches


def is_fast_lane(text: str, triggers: list[str]) -> bool:
    haystack = normalize(text)
    return any(normalize(trigger) in haystack for trigger in triggers)
