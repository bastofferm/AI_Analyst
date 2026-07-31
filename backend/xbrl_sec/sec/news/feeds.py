"""Feed-source persistence helpers."""
from __future__ import annotations

from dataclasses import dataclass

from xbrl_sec.sec.db.connection import connect


@dataclass(frozen=True)
class FeedSource:
    feed_key: str
    label: str
    url: str


def enabled_feeds(feed_key: str | None = None) -> list[FeedSource]:
    sql = """
        SELECT feed_key, label, url
        FROM news.feed_sources
        WHERE enabled
    """
    params: tuple[object, ...] = ()
    if feed_key:
        sql += " AND feed_key = %s"
        params = (feed_key,)
    sql += " ORDER BY feed_key"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [FeedSource(*row) for row in cur.fetchall()]
