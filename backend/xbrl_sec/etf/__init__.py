"""ESMA FIRDS ETF data layer (DOC WA0006).

Self-contained pipeline that discovers/downloads ESMA FIRDS reference files,
filters DE/AT-listed UCITS ETFs, enriches via Xetra, fetches daily prices from
yfinance, and persists into the sec.dim_etf / sec.dim_etf_listing /
sec.fact_prices_etf tables (migration 117_etf_tables.sql).

Mirrors the xbrl_sec.sec conventions: shared db.connect, db.bulk.execute_values,
explicit pipeline state tables. Invoke via `python -m xbrl_sec.etf.cli`.
"""
