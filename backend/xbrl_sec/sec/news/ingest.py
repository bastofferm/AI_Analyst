"""Producer/consumer orchestration for RSS articles."""
from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.request import Request, urlopen

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
from psycopg2.extras import Json

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.news.extractor import extract_article_text
from xbrl_sec.sec.news.feeds import enabled_feeds
from xbrl_sec.sec.news.filters import (
    is_fast_lane,
    load_urgency_triggers,
    load_watchlist,
    matching_tickers,
)
from xbrl_sec.sec.news.parser import NewsArticle, parse_feed
from xbrl_sec.sec.news.sentiment import score_article
from xbrl_sec.sec.settings import load_settings


def _fetch_feed(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MZQA-News/1.0)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )
    with urlopen(request, timeout=load_settings().news_fetch_timeout_seconds) as response:
        return response.read(5_000_000)


def _start_run() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO news.ingestion_runs (started_at) VALUES (%s) RETURNING run_id",
            (datetime.now(timezone.utc),),
        )
        return int(cur.fetchone()[0])


def _finish_run(run_id: int, *, feeds: int, new: int, scored: int, errors: list[dict[str, str]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE news.ingestion_runs
            SET finished_at = %s, feeds_polled = %s, articles_new = %s,
                articles_scored = %s, errors = %s
            WHERE run_id = %s
        """, (
            datetime.now(timezone.utc),
            feeds,
            new,
            scored,
            Json(errors) if errors else None,
            run_id,
        ))


def _store_article(article: NewsArticle, *, full_content: str, fast_lane: bool) -> tuple[int, bool]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO news.articles
                (feed_key, url, title, summary, full_content, author, published_at, fast_lane)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
            RETURNING article_id
        """, (
            article.feed_key,
            article.url,
            article.title,
            article.summary,
            full_content,
            article.author,
            article.published_at,
            fast_lane,
        ))
        inserted = cur.fetchone()
        if inserted:
            return int(inserted[0]), True
        cur.execute("""
            UPDATE news.articles
            SET title = %s,
                summary = COALESCE(NULLIF(%s, ''), summary),
                full_content = CASE
                    WHEN length(COALESCE(%s, '')) > length(COALESCE(full_content, '')) THEN %s
                    ELSE full_content
                END,
                author = COALESCE(%s, author),
                published_at = COALESCE(%s, published_at),
                fast_lane = fast_lane OR %s
            WHERE url = %s
            RETURNING article_id
        """, (
            article.title,
            article.summary,
            full_content,
            full_content,
            article.author,
            article.published_at,
            fast_lane,
            article.url,
        ))
        return int(cur.fetchone()[0]), False


def _write_scores(
    article_id: int,
    ticker: str,
    title: str,
    text: str,
    fast_lane: bool,
    backend: str,
) -> int:
    results = score_article(
        ticker=ticker,
        title=title,
        text=text,
        fast_lane=fast_lane,
        backend_name=backend,
    )
    with connect() as conn, conn.cursor() as cur:
        for model, result in results:
            cur.execute("""
                INSERT INTO news.sentiment_scores
                    (article_id, ticker, model, label, score, rationale)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id, ticker, model) DO UPDATE SET
                    label = EXCLUDED.label,
                    score = EXCLUDED.score,
                    rationale = EXCLUDED.rationale,
                    scored_at = NOW()
            """, (
                article_id,
                ticker,
                model,
                result["label"],
                result["score"],
                result["rationale"],
            ))
        cur.execute("UPDATE news.articles SET processed = TRUE WHERE article_id = %s", (article_id,))
    return len(results)


def ingest_feeds(
    *,
    feed_key: str | None = None,
    backend: str | None = None,
    score: bool = True,
    limit: int | None = None,
) -> dict[str, object]:
    run_id = _start_run()
    feeds = enabled_feeds(feed_key)
    watchlist = load_watchlist()
    triggers = load_urgency_triggers()
    backend = backend or load_settings().news_reasoning_backend
    articles_new = 0
    scores_written = 0
    errors: list[dict[str, str]] = []

    try:
        for feed in feeds:
            try:
                parsed = parse_feed(_fetch_feed(feed.url), feed.feed_key, feed.url)
                selected = parsed if limit is None else parsed[:limit]
                for article in selected:
                    teaser = f"{article.title}\n{article.summary}"
                    tickers = matching_tickers(teaser, watchlist)
                    fast_lane = is_fast_lane(teaser, triggers)
                    full_content = (
                        extract_article_text(article.url, article.summary)
                        if tickers or fast_lane
                        else article.summary
                    )
                    article_id, inserted = _store_article(
                        article,
                        full_content=full_content,
                        fast_lane=fast_lane,
                    )
                    articles_new += int(inserted)
                    if score and tickers:
                        for ticker in tickers:
                            scores_written += _write_scores(
                                article_id,
                                ticker,
                                article.title,
                                full_content or article.summary,
                                fast_lane,
                                backend,
                            )
            except Exception as exc:
                errors.append({"feed": feed.feed_key, "error": str(exc)})
        return {
            "run_id": run_id,
            "feeds_polled": len(feeds),
            "articles_new": articles_new,
            "scores_written": scores_written,
            "errors": errors,
        }
    finally:
        _finish_run(
            run_id,
            feeds=len(feeds),
            new=articles_new,
            scored=scores_written,
            errors=errors,
        )


def score_pending_articles(
    *,
    since: date | None = None,
    backend: str | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    watchlist = load_watchlist()
    backend = backend or load_settings().news_reasoning_backend
    params: list[object] = []
    where = ["NOT processed"]
    if since:
        where.append("COALESCE(published_at, discovered_at)::date >= %s")
        params.append(since)
    sql = f"""
        SELECT article_id, title, summary, full_content, fast_lane
        FROM news.articles
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(published_at, discovered_at) DESC
    """
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    articles_scored = 0
    scores_written = 0
    errors: list[dict[str, str]] = []
    for article_id, title, summary, full_content, fast_lane in rows:
        text = full_content or summary or ""
        tickers = matching_tickers(f"{title}\n{text}", watchlist)
        if not tickers:
            continue
        try:
            for ticker in tickers:
                scores_written += _write_scores(
                    article_id,
                    ticker,
                    title,
                    text,
                    bool(fast_lane),
                    backend,
                )
            articles_scored += 1
        except Exception as exc:
            errors.append({"article_id": str(article_id), "error": str(exc)})
    return {
        "articles_considered": len(rows),
        "articles_scored": articles_scored,
        "scores_written": scores_written,
        "errors": errors,
    }
