-- Ticker-health rollup: per-company success/fail counts sourced from
-- market_source_item_state, aggregated so consistently-dead wholesale
-- tickers can be pruned from include_in_pipeline.
--
-- Refresh weekly:  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_yahoo_ticker_health;

SET search_path TO sec, public;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_yahoo_ticker_health AS
SELECT
    d.intl_company_id,
    d.primary_ticker,
    d.country_code,
    d.exchange_suffix,
    d.pipeline_sample_group,
    COUNT(*) FILTER (WHERE ms.status = 'failed') AS failed_runs,
    COUNT(*) FILTER (WHERE ms.status = 'done')   AS ok_runs,
    COUNT(*) FILTER (WHERE ms.status = 'skipped') AS skipped_runs,
    MAX(ms.finished_at)                          AS last_run_at,
    MAX(ms.finished_at) FILTER (WHERE ms.status = 'done') AS last_ok_at
FROM dim_company_intl d
LEFT JOIN market_source_item_state ms
    ON ms.source IN ('yahoo_global_fundamentals','yahoo_global_prices')
   AND ms.source_key = d.primary_ticker
GROUP BY d.intl_company_id, d.primary_ticker, d.country_code,
         d.exchange_suffix, d.pipeline_sample_group;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_yahoo_ticker_health_company
    ON mv_yahoo_ticker_health (intl_company_id);

CREATE INDEX IF NOT EXISTS idx_mv_yahoo_ticker_health_group
    ON mv_yahoo_ticker_health (pipeline_sample_group, failed_runs DESC);

CREATE INDEX IF NOT EXISTS idx_mv_yahoo_ticker_health_country
    ON mv_yahoo_ticker_health (country_code, failed_runs DESC);

COMMENT ON MATERIALIZED VIEW mv_yahoo_ticker_health IS
    'Per-ticker health for the Yahoo global fundamentals/prices pipelines. '
    'Rows with failed_runs >= 5 and ok_runs = 0 are candidates for '
    'include_in_pipeline=FALSE; deactivation stays manual for now.';
