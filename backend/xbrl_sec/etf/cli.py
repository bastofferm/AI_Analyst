"""CLI for the ETF pipeline. Run: python -m xbrl_sec.etf.cli <command>.

Commands:
  firds   Discover + parse the latest ESMA FIRDS file, upsert DE/AT ETFs.
  xetra   Enrich dim_etf from the Xetra ETF CSV.
  prices  Fetch daily OHLCV for active ETFs from yfinance.
  justetf-metadata  Audit/fill justETF metadata and exchange tickers.
  etf-yahoo-resolve  Resolve missing ETF Yahoo symbols into a staging table.
  etf-yahoo-promote  Promote one staged Yahoo symbol into the ETF profile.
  providers  Seed/backfill canonical ETF providers and print coverage.
  holdings   Fetch official provider holdings where adapters are available.
  bond-ratings  Backfill bond ETF credit-quality ratings and rating snapshots.
  run     firds -> xetra -> prices.
  status  Print row counts across the ETF tables.
"""
from __future__ import annotations

import argparse

from . import pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="MZQA ETF data pipeline (ESMA FIRDS)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_firds = sub.add_parser("firds", help="Discover + parse latest FIRDS file")
    p_firds.add_argument("--file-type", default="FULINS", choices=["FULINS", "DLTINS"])
    p_firds.add_argument("--letter", default="C", help="FIRDS instrument letter (C = collective investment/ETFs)")
    p_firds.add_argument("--limit", type=int, default=None, help="Cap unique ISINs kept (smoke runs)")
    p_firds.add_argument("--max-scan", type=int, default=None, help="Cap records inspected (smoke runs)")

    sub.add_parser("xetra", help="Enrich dim_etf from the Xetra CSV")

    p_profile = sub.add_parser("profile", help="Enrich ETF holdings/sectors/asset-class via yfinance")
    p_profile.add_argument("--limit", type=int, default=None)
    p_profile.add_argument("--all", action="store_true", help="Re-fetch even already-complete profiles")
    p_profile.add_argument("--snapshot-date", default="2026-06-26", help="Snapshot as-of date (YYYY-MM-DD)")
    p_profile.add_argument("--no-snapshot", action="store_true", help="Only refresh current profile tables")

    p_bond_ratings = sub.add_parser("bond-ratings", help="Backfill bond ETF credit-quality ratings via yfinance")
    p_bond_ratings.add_argument("--limit", type=int, default=None)
    p_bond_ratings.add_argument("--all", action="store_true", help="Re-fetch even already-rated bond ETFs")
    p_bond_ratings.add_argument("--snapshot-date", default="2026-06-26", help="Snapshot as-of date (YYYY-MM-DD)")
    p_bond_ratings.add_argument("--no-snapshot", action="store_true", help="Only refresh current rating tables")

    p_factors = sub.add_parser("factors", help="Fit Fama-French FF5/FF6 regressions for equity ETFs")
    p_factors.add_argument("--limit", type=int, default=None)
    p_factors.add_argument("--all", action="store_true", help="Recompute even already-fitted ETFs")

    p_prices = sub.add_parser("prices", help="Fetch ETF prices from yfinance")
    p_prices.add_argument("--limit", type=int, default=None)
    p_prices.add_argument("--isin", default=None, help="Single ETF ISIN")
    p_prices.add_argument("--period", default="max")
    p_prices.add_argument("--only-missing", action="store_true", help="Fetch only active ETFs with no price history")
    p_prices.add_argument("--no-fallbacks", action="store_true", help="Disable exchange-ticker fallback candidates")
    p_prices.add_argument("--allow-licensed-justetf", action="store_true", help="Allow local licensed justETF/vendor CSV fallback")
    p_prices.add_argument("--licensed-justetf-dir", default=None, help="Directory containing licensed per-ISIN CSV histories")

    p_justetf = sub.add_parser("justetf-metadata", help="Audit/fill justETF metadata and exchange tickers")
    p_justetf.add_argument("--limit", type=int, default=None)
    p_justetf.add_argument("--isin", default=None, help="Single ETF ISIN")
    p_justetf.add_argument("--all", action="store_true", help="Audit all active ETFs, not only unpriced ETFs")
    p_justetf.add_argument("--apply", action="store_true", help="Write metadata and exchange tickers to DB")
    p_justetf.add_argument("--output-csv", default=None)
    p_justetf.add_argument("--sleep-seconds", type=float, default=0.8)

    p_yahoo = sub.add_parser("etf-yahoo-resolve", help="Resolve ETF ISINs to Yahoo Finance quote symbols")
    p_yahoo.add_argument("--limit", type=int, default=None)
    p_yahoo.add_argument("--apply", action="store_true", help="Write candidates to staging and auto-promote accepted rows")
    p_yahoo.add_argument("--dry-run", action="store_true", help="Resolve and score without writing")
    p_yahoo.add_argument("--min-score", type=float, default=85.0)
    p_yahoo.add_argument("--no-promote", action="store_true", help="Stage candidates but do not auto-promote")
    p_yahoo.add_argument("--no-selenium", action="store_true", help="Use Yahoo search API only")
    p_yahoo.add_argument("--headless", dest="headless", action="store_true", default=True)
    p_yahoo.add_argument("--no-headless", dest="headless", action="store_false")
    p_yahoo.add_argument("--edge-binary-path", default=None)
    p_yahoo.add_argument("--driver-path", default=None)
    p_yahoo.add_argument("--max-symbols-per-query", type=int, default=8)
    p_yahoo.add_argument("--wait-seconds", type=float, default=6.0)
    p_yahoo.add_argument("--sleep-seconds", type=float, default=0.5)
    p_yahoo.add_argument("--no-price-validation", action="store_true")
    p_yahoo.add_argument("--price-period", default="max")

    p_promote = sub.add_parser("etf-yahoo-promote", help="Promote one staged ETF Yahoo candidate")
    p_promote.add_argument("--isin", required=True)
    p_promote.add_argument("--symbol", required=True)
    p_promote.add_argument("--min-score", type=float, default=85.0)
    p_promote.add_argument("--allow-review", action="store_true", help="Allow promoting review rows above min-score")
    p_promote.add_argument("--force", action="store_true", help="Override score/profile safeguards")
    p_promote.add_argument("--dry-run", action="store_true")

    p_promote_best = sub.add_parser(
        "etf-yahoo-promote-best",
        help="Revalidate staged ETF Yahoo candidates and promote the longest price history per ISIN",
    )
    p_promote_best.add_argument("--limit", type=int, default=None)
    p_promote_best.add_argument("--min-score", type=float, default=85.0)
    p_promote_best.add_argument("--allow-review", action="store_true", help="Allow promoting review rows above min-score")
    p_promote_best.add_argument("--dry-run", action="store_true")
    p_promote_best.add_argument("--price-period", default="max")

    p_providers = sub.add_parser("providers", help="Seed/backfill ETF provider registry")
    p_providers.add_argument("--limit", type=int, default=None, help="Cap ETF rows for smoke backfills")

    p_holdings = sub.add_parser("holdings", help="Fetch official provider holdings")
    p_holdings.add_argument("--provider", default="all", help="Provider ID or 'all'")
    p_holdings.add_argument("--isin", default=None, help="Single ETF ISIN")
    p_holdings.add_argument("--limit", type=int, default=None, help="Cap candidates")
    p_holdings.add_argument("--all", action="store_true", help="Re-fetch ETFs even if a success state exists")
    p_holdings.add_argument("--random", action="store_true", help="Randomize candidate selection before applying --limit")
    p_holdings.add_argument("--dry-run", action="store_true", help="Only report candidate/provider coverage")
    p_holdings.add_argument("--snapshot-date", default=None, help="Snapshot as-of date (YYYY-MM-DD)")

    p_run = sub.add_parser("run", help="Run firds -> xetra -> prices")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--period", default="max")
    p_run.add_argument("--max-scan", type=int, default=None)

    sub.add_parser("status", help="Print ETF table counts")

    args = parser.parse_args()

    if args.cmd == "firds":
        letter = None if args.letter.lower() in {"", "all", "none"} else args.letter
        result = pipeline.run_firds(
            file_type=args.file_type, instrument_letter=letter,
            limit=args.limit, max_scan=args.max_scan,
        )
    elif args.cmd == "xetra":
        result = pipeline.run_xetra()
    elif args.cmd == "profile":
        from .profile import enrich_profiles
        result = enrich_profiles(
            limit=args.limit,
            only_missing=not args.all,
            write_snapshots=not args.no_snapshot,
            as_of_date=args.snapshot_date,
        )
    elif args.cmd == "bond-ratings":
        from .profile import backfill_bond_ratings
        result = backfill_bond_ratings(
            limit=args.limit,
            only_missing=not args.all,
            write_snapshots=not args.no_snapshot,
            as_of_date=args.snapshot_date,
        )
    elif args.cmd == "factors":
        from .factor_model import compute_etf_factors
        result = compute_etf_factors(limit=args.limit, only_missing=not args.all)
    elif args.cmd == "prices":
        result = pipeline.run_prices(
            limit=args.limit,
            period=args.period,
            only_missing=args.only_missing,
            isin=args.isin,
            use_fallbacks=not args.no_fallbacks,
            allow_licensed_justetf=args.allow_licensed_justetf,
            licensed_justetf_dir=args.licensed_justetf_dir,
        )
    elif args.cmd == "justetf-metadata":
        from .justetf import run_justetf_metadata_audit
        result = run_justetf_metadata_audit(
            limit=args.limit,
            only_unpriced=not args.all,
            isin=args.isin,
            apply=args.apply,
            output_csv=args.output_csv,
            sleep_seconds=args.sleep_seconds,
        )
    elif args.cmd == "etf-yahoo-resolve":
        from .yahoo_resolver import DEFAULT_EDGE_BINARY_PATH, run_yahoo_symbol_resolution
        result = run_yahoo_symbol_resolution(
            limit=args.limit,
            apply=bool(args.apply and not args.dry_run),
            min_score=args.min_score,
            auto_promote=not args.no_promote,
            use_selenium=not args.no_selenium,
            headless=args.headless,
            edge_binary_path=args.edge_binary_path or DEFAULT_EDGE_BINARY_PATH,
            driver_path=args.driver_path,
            max_symbols_per_query=args.max_symbols_per_query,
            wait_seconds=args.wait_seconds,
            sleep_seconds=args.sleep_seconds,
            validate_prices=not args.no_price_validation,
            price_period=args.price_period,
        )
    elif args.cmd == "etf-yahoo-promote":
        from .yahoo_resolver import promote_yahoo_candidate
        result = promote_yahoo_candidate(
            args.isin,
            args.symbol,
            min_score=args.min_score,
            allow_review=args.allow_review,
            dry_run=args.dry_run,
            force=args.force,
        )
    elif args.cmd == "etf-yahoo-promote-best":
        from .yahoo_resolver import promote_best_yahoo_candidates
        result = promote_best_yahoo_candidates(
            limit=args.limit,
            min_score=args.min_score,
            allow_review=args.allow_review,
            dry_run=args.dry_run,
            price_period=args.price_period,
        )
    elif args.cmd == "providers":
        from .providers import provider_facets, setup_provider_registry
        result = setup_provider_registry(limit=args.limit)
        facets = provider_facets()
        result.update({
            "provider_count": len(facets),
            "unknown_etfs": next((f["etf_count"] for f in facets if f["provider_id"] == "unknown_provider"), 0),
        })
    elif args.cmd == "holdings":
        from datetime import date
        from .holdings import run_holdings_fetch
        snapshot_date = date.fromisoformat(args.snapshot_date) if args.snapshot_date else None
        result = run_holdings_fetch(
            provider=args.provider,
            isin=args.isin,
            limit=args.limit,
            refresh_all=args.all,
            random_sample=args.random,
            dry_run=args.dry_run,
            as_of_date=snapshot_date,
        )
    elif args.cmd == "run":
        result = pipeline.run_all(limit=args.limit, period=args.period, max_scan=args.max_scan)
    else:
        result = pipeline.status()

    print(f"{args.cmd}: " + " ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
