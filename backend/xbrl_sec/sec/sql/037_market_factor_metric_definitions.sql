SET search_path TO sec, public;

INSERT INTO ref_metric_definitions
    (metric_id, category, name, importance, formula, required_line_items, note,
     unit_type, metric_type, formula_symbolic, formula_sql)
VALUES
    (
        'post_earnings_announcement_drift_raw',
        'market_and_sentiment',
        'PEAD Raw Return',
        1,
        'Cumulative adjusted stock return from trading day +1 through +5 after the filing date.',
        ARRAY['stock_price'],
        'Post-filing drift measured directly from adjusted daily equity returns.',
        'PCT',
        'MARKET_DERIVED',
        '\prod_{d=1}^{5}(1+r_d)-1',
        'EXP(SUM(LN(1+daily_return))) - 1 over trading days filed_date+1..+5'
    ),
    (
        'post_earnings_announcement_drift_residual_ff3',
        'market_and_sentiment',
        'PEAD Residual Return (FF3)',
        1,
        'Cumulative idiosyncratic return from trading day +1 through +5 after the filing date using FF3 implied returns.',
        ARRAY['stock_price'],
        'Daily residual is stock log return minus FF3 model-implied return.',
        'PCT',
        'MARKET_DERIVED',
        '\exp\left(\sum_{d=1}^{5}(r_d-\hat r^{FF3}_d)\right)-1',
        'EXP(SUM(log_return - implied_return_ff3)) - 1 over trading days filed_date+1..+5'
    ),
    (
        'post_earnings_announcement_drift_residual_ff4',
        'market_and_sentiment',
        'PEAD Residual Return (FF4)',
        1,
        'Cumulative idiosyncratic return from trading day +1 through +5 after the filing date using FF4 implied returns.',
        ARRAY['stock_price'],
        'Daily residual is stock log return minus FF4 model-implied return.',
        'PCT',
        'MARKET_DERIVED',
        '\exp\left(\sum_{d=1}^{5}(r_d-\hat r^{FF4}_d)\right)-1',
        'EXP(SUM(log_return - implied_return_ff4)) - 1 over trading days filed_date+1..+5'
    ),
    (
        'post_earnings_announcement_drift_residual_ff5',
        'market_and_sentiment',
        'PEAD Residual Return (FF5)',
        1,
        'Cumulative idiosyncratic return from trading day +1 through +5 after the filing date using FF5 implied returns.',
        ARRAY['stock_price'],
        'Daily residual is stock log return minus FF5 model-implied return.',
        'PCT',
        'MARKET_DERIVED',
        '\exp\left(\sum_{d=1}^{5}(r_d-\hat r^{FF5}_d)\right)-1',
        'EXP(SUM(log_return - implied_return_ff5)) - 1 over trading days filed_date+1..+5'
    ),
    (
        'post_earnings_announcement_drift_residual_ff6',
        'market_and_sentiment',
        'PEAD Residual Return (FF6)',
        1,
        'Cumulative idiosyncratic return from trading day +1 through +5 after the filing date using FF6 implied returns.',
        ARRAY['stock_price'],
        'Daily residual is stock log return minus FF6 model-implied return.',
        'PCT',
        'MARKET_DERIVED',
        '\exp\left(\sum_{d=1}^{5}(r_d-\hat r^{FF6}_d)\right)-1',
        'EXP(SUM(log_return - implied_return_ff6)) - 1 over trading days filed_date+1..+5'
    ),
    (
        'idiosyncratic_volatility_ff3',
        'market_and_sentiment',
        'Idiosyncratic Volatility (FF3)',
        2,
        'Annualized residual volatility from the most recent FF3 rolling factor regression window.',
        ARRAY['stock_price'],
        'Firm-specific volatility not explained by market, size, and value factors.',
        'PCT',
        'MARKET_DERIVED',
        '\sigma(\epsilon^{FF3})\sqrt{252}',
        'residual_vol from fact_factor_reg_meta where model = FF3'
    ),
    (
        'idiosyncratic_volatility_ff4',
        'market_and_sentiment',
        'Idiosyncratic Volatility (FF4)',
        2,
        'Annualized residual volatility from the most recent FF4 rolling factor regression window.',
        ARRAY['stock_price'],
        'Firm-specific volatility not explained by market, size, value, and momentum factors.',
        'PCT',
        'MARKET_DERIVED',
        '\sigma(\epsilon^{FF4})\sqrt{252}',
        'residual_vol from fact_factor_reg_meta where model = FF4'
    ),
    (
        'idiosyncratic_volatility_ff5',
        'market_and_sentiment',
        'Idiosyncratic Volatility (FF5)',
        2,
        'Annualized residual volatility from the most recent FF5 rolling factor regression window.',
        ARRAY['stock_price'],
        'Firm-specific volatility not explained by market, size, value, profitability, and investment factors.',
        'PCT',
        'MARKET_DERIVED',
        '\sigma(\epsilon^{FF5})\sqrt{252}',
        'residual_vol from fact_factor_reg_meta where model = FF5'
    ),
    (
        'idiosyncratic_volatility_ff6',
        'market_and_sentiment',
        'Idiosyncratic Volatility (FF6)',
        2,
        'Annualized residual volatility from the most recent FF6 rolling factor regression window.',
        ARRAY['stock_price'],
        'Firm-specific volatility not explained by market, size, value, profitability, investment, and momentum factors.',
        'PCT',
        'MARKET_DERIVED',
        '\sigma(\epsilon^{FF6})\sqrt{252}',
        'residual_vol from fact_factor_reg_meta where model = FF6'
    )
ON CONFLICT (metric_id) DO UPDATE SET
    category = EXCLUDED.category,
    name = EXCLUDED.name,
    importance = EXCLUDED.importance,
    formula = EXCLUDED.formula,
    required_line_items = EXCLUDED.required_line_items,
    note = EXCLUDED.note,
    unit_type = EXCLUDED.unit_type,
    metric_type = EXCLUDED.metric_type,
    formula_symbolic = EXCLUDED.formula_symbolic,
    formula_sql = EXCLUDED.formula_sql;

UPDATE ref_metric_definitions
SET unit_type = 'PCT',
    formula = 'Annualized standard deviation of adjusted daily stock returns over the trailing 252 trading days.',
    formula_symbolic = '\sigma(r_{t-251:t})\sqrt{252}',
    formula_sql = 'STDDEV(daily_return) * SQRT(252) over trailing 252 trading days'
WHERE metric_id = 'total_volatility_252_day';

UPDATE ref_metric_definitions
SET unit_type = 'PCT',
    formula = 'Annualized residual volatility from the most recent FF6 rolling factor regression window when available, otherwise FF5.',
    formula_symbolic = '\sigma(\epsilon^{FF6})\sqrt{252}',
    formula_sql = 'residual_vol from latest fact_factor_reg_meta where model = FF6, fallback FF5'
WHERE metric_id = 'idiosyncratic_volatility';

UPDATE ref_metric_definitions
SET unit_type = 'RATIO',
    formula = 'Most recent FF6 market-factor loading when available, otherwise FF5.',
    formula_symbolic = '\beta_{MKT}',
    formula_sql = 'beta_mkt from latest fact_factor_loadings where model = FF6, fallback FF5'
WHERE metric_id = 'market_beta';

UPDATE ref_metric_definitions
SET unit_type = 'PCT',
    name = 'PEAD Raw Return',
    formula = 'Cumulative adjusted stock return from trading day +1 through +5 after the filing date.',
    formula_symbolic = '\prod_{d=1}^{5}(1+r_d)-1',
    formula_sql = 'EXP(SUM(LN(1+daily_return))) - 1 over trading days filed_date+1..+5',
    required_line_items = ARRAY['stock_price']
WHERE metric_id = 'post_earnings_announcement_drift';
