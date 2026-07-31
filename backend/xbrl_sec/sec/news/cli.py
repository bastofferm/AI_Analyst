"""CLI wiring for the MZQA news pipeline."""
from __future__ import annotations

import argparse
from datetime import date
import json

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.news.ingest import ingest_feeds, score_pending_articles


def configure_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="news_action", required=True)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--feed", default=None)
    ingest.add_argument("--backend", choices=["qwen_ollama", "deepseek"], default=None)
    ingest.add_argument("--no-score", action="store_true")
    ingest.add_argument("--limit", type=int, default=None)

    score = sub.add_parser("score")
    score.add_argument("--since", default=None)
    score.add_argument("--backend", choices=["qwen_ollama", "deepseek"], default=None)
    score.add_argument("--limit", type=int, default=None)

    watchlist = sub.add_parser("watchlist")
    watch_sub = watchlist.add_subparsers(dest="watchlist_action", required=True)
    add = watch_sub.add_parser("add")
    add.add_argument("ticker")
    add.add_argument("--market", choices=["US", "JP"], default="US")
    add.add_argument("--proxy", default="")
    watch_sub.add_parser("list")
    remove = watch_sub.add_parser("remove")
    remove.add_argument("ticker")


def _watchlist(args: argparse.Namespace) -> dict[str, object]:
    ticker = getattr(args, "ticker", "").strip().upper()
    if args.watchlist_action == "add":
        proxy_terms = [term.strip() for term in args.proxy.split(",") if term.strip()]
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO news.watchlist (ticker, market, proxy_terms, enabled)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (ticker) DO UPDATE SET
                    market = EXCLUDED.market,
                    proxy_terms = EXCLUDED.proxy_terms,
                    enabled = TRUE
            """, (ticker, args.market, proxy_terms))
        return {"ticker": ticker, "market": args.market, "proxy_terms": proxy_terms}
    if args.watchlist_action == "remove":
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM news.watchlist WHERE ticker = %s", (ticker,))
            removed = cur.rowcount
        return {"ticker": ticker, "removed": removed}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, market, proxy_terms, enabled
            FROM news.watchlist
            ORDER BY ticker
        """)
        rows = [
            {
                "ticker": row[0],
                "market": row[1],
                "proxy_terms": row[2] or [],
                "enabled": row[3],
            }
            for row in cur.fetchall()
        ]
    return {"watchlist": rows}


def run(args: argparse.Namespace) -> int:
    if args.news_action == "ingest":
        result = ingest_feeds(
            feed_key=args.feed,
            backend=args.backend,
            score=not args.no_score,
            limit=args.limit,
        )
    elif args.news_action == "score":
        result = score_pending_articles(
            since=date.fromisoformat(args.since) if args.since else None,
            backend=args.backend,
            limit=args.limit,
        )
    else:
        result = _watchlist(args)
    print(json.dumps(result, indent=2, default=str))
    return 0
