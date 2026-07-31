-- Document the filing-year semantics of fact_fundamentals_*.fiscal_year and
-- expose a period-aligned fiscal year for consumers that need the fact's own
-- period year rather than the filing context year.
--
-- BACKGROUND (the SEC companyfacts footgun)
-- -----------------------------------------------------------------------------
-- For US facts, fiscal_year is populated from the SEC companyfacts `fy` field
-- (see parsers/sec_companyfacts.py: "fiscal_year": fact.get("fy")). That field
-- describes the FILING that reported the fact, not the period the fact covers.
-- A 10-K restates two prior comparative years, and every comparative fact in
-- that filing inherits the filing's `fy`/`fp`.
--
-- Example (WMT, us-gaap/CostOfRevenue, fiscal_period='FY'):
--   period_end=2022-01-31 may be reported in the FY2024 10-K as a comparative
--   carrying fiscal_year=2024. Filtering fiscal_year=2024 therefore yields a
--   two-year-old fact, and the real FY2024 figure (period_end=2024-01-31) is NOT
--   in that bucket. This bites every filer with an early-calendar fiscal year
--   end (WMT, HD, DELL, ...).
--
-- The JP path (parsers/edinet_xbrl.py) derives fiscal_year from period_end.year,
-- so JP fiscal_year is already period-aligned; the view below is a no-op for the
-- FY case there and harmless for interim periods.
--
-- CANONICAL PERIOD-ALIGNED FISCAL YEAR
-- -----------------------------------------------------------------------------
-- period_fiscal_year mirrors std/us_standardize.py:_fact_fiscal_year(): for
-- annual periods use period_end.year; otherwise fall back to the stored
-- fiscal_year (interim quarters keep their reported label).
--
-- NOTE: v_entity_mapping_gap (period-aligned + latest-vintage) is defined in
-- migration 113, which supersedes the migration-102 definition.

SET search_path TO sec, public;

COMMENT ON COLUMN fact_fundamentals_us.fiscal_year IS
    'FILING fiscal year (SEC companyfacts `fy`), NOT the fact''s period year. '
    'Comparative facts from a 10-K are binned under the filing''s year, so a row '
    'with period_end two years before the filing still carries the filing''s '
    'fiscal_year. Do NOT filter/group by this as if it were the fact period year '
    '- derive the period year from period_end (see view v_fact_fundamentals_us, '
    'column period_fiscal_year, or std/us_standardize.py:_fact_fiscal_year).';

COMMENT ON COLUMN fact_fundamentals_jp.fiscal_year IS
    'Fiscal year. For JP this is derived from period_end.year at ingest '
    '(parsers/edinet_xbrl.py) and is period-aligned, unlike the US column which '
    'carries the filing context year. v_fact_fundamentals_jp.period_fiscal_year '
    'exposes the canonical period year for cross-jurisdiction parity.';

-- Shared period-aligned wrappers. Consumers that need the fact's own period year
-- should read period_fiscal_year from these views instead of the raw column.
-- (These expose ALL vintages; for one-row-per-period "current best knowledge"
-- use v_fact_fundamentals_us_latest from migration 113.)
CREATE OR REPLACE VIEW v_fact_fundamentals_us AS
SELECT f.*,
       CASE
           WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL
               THEN EXTRACT(YEAR FROM f.period_end)::int
           ELSE f.fiscal_year
       END AS period_fiscal_year
FROM fact_fundamentals_us f;

COMMENT ON VIEW v_fact_fundamentals_us IS
    'fact_fundamentals_us plus period_fiscal_year, the period-aligned fiscal '
    'year (period_end.year for annual periods). Use period_fiscal_year when you '
    'mean the fact''s own period; the underlying fiscal_year is the filing year.';

CREATE OR REPLACE VIEW v_fact_fundamentals_jp AS
SELECT f.*,
       CASE
           WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL
               THEN EXTRACT(YEAR FROM f.period_end)::int
           ELSE f.fiscal_year
       END AS period_fiscal_year
FROM fact_fundamentals_jp f;

COMMENT ON VIEW v_fact_fundamentals_jp IS
    'fact_fundamentals_jp plus period_fiscal_year (period-aligned fiscal year). '
    'JP fiscal_year is already period-aligned at ingest; this view exists for '
    'parity with v_fact_fundamentals_us.';
