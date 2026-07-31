SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_factor_loadings (
    jurisdiction  TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    window_end    DATE NOT NULL,
    model         TEXT NOT NULL,
    window_start  DATE NOT NULL,
    n_obs         INTEGER,
    ff_region     TEXT,
    alpha         DOUBLE PRECISION,
    beta_mkt      DOUBLE PRECISION,
    beta_smb      DOUBLE PRECISION,
    beta_hml      DOUBLE PRECISION,
    beta_mom      DOUBLE PRECISION,
    beta_rmw      DOUBLE PRECISION,
    beta_cma      DOUBLE PRECISION,
    t_alpha       DOUBLE PRECISION,
    t_mkt         DOUBLE PRECISION,
    t_smb         DOUBLE PRECISION,
    t_hml         DOUBLE PRECISION,
    t_mom         DOUBLE PRECISION,
    t_rmw         DOUBLE PRECISION,
    t_cma         DOUBLE PRECISION,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, ticker, window_end, model)
);

CREATE TABLE IF NOT EXISTS fact_factor_reg_meta (
    jurisdiction     TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    window_end       DATE NOT NULL,
    model            TEXT NOT NULL,
    n_obs            INTEGER,
    r2               DOUBLE PRECISION,
    adj_r2           DOUBLE PRECISION,
    f_stat           DOUBLE PRECISION,
    f_pvalue         DOUBLE PRECISION,
    rmse             DOUBLE PRECISION,
    residual_vol     DOUBLE PRECISION,
    durbin_watson    DOUBLE PRECISION,
    condition_number DOUBLE PRECISION,
    p_alpha          DOUBLE PRECISION,
    p_mkt            DOUBLE PRECISION,
    p_smb            DOUBLE PRECISION,
    p_hml            DOUBLE PRECISION,
    p_mom            DOUBLE PRECISION,
    p_rmw            DOUBLE PRECISION,
    p_cma            DOUBLE PRECISION,
    se_alpha         DOUBLE PRECISION,
    se_mkt           DOUBLE PRECISION,
    se_smb           DOUBLE PRECISION,
    se_hml           DOUBLE PRECISION,
    se_mom           DOUBLE PRECISION,
    se_rmw           DOUBLE PRECISION,
    se_cma           DOUBLE PRECISION,
    quality_score    SMALLINT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, ticker, window_end, model)
);

CREATE TABLE IF NOT EXISTS fact_factor_implied_returns (
    jurisdiction    TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    date            DATE NOT NULL,
    model           TEXT NOT NULL,
    implied_return  DOUBLE PRECISION,
    window_end      DATE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, ticker, date, model)
);

CREATE INDEX IF NOT EXISTS idx_factor_loadings_ticker
    ON fact_factor_loadings (jurisdiction, ticker, window_end DESC);
CREATE INDEX IF NOT EXISTS idx_factor_loadings_model
    ON fact_factor_loadings (model, window_end DESC);
CREATE INDEX IF NOT EXISTS idx_factor_reg_meta_ticker
    ON fact_factor_reg_meta (jurisdiction, ticker, window_end DESC);
CREATE INDEX IF NOT EXISTS idx_factor_reg_meta_quality
    ON fact_factor_reg_meta (model, quality_score, window_end DESC);
CREATE INDEX IF NOT EXISTS idx_factor_implied_ticker
    ON fact_factor_implied_returns (jurisdiction, ticker, date DESC);

CREATE OR REPLACE VIEW v_factor_quality AS
SELECT
    m.jurisdiction,
    m.ticker,
    m.model,
    m.window_end,
    m.n_obs,
    m.adj_r2,
    m.f_pvalue,
    m.condition_number,
    m.durbin_watson,
    m.rmse,
    m.residual_vol,
    CASE WHEN m.adj_r2           >  0.20 THEN 1 ELSE 0 END AS flag_adj_r2,
    CASE WHEN m.f_pvalue         <  0.05 THEN 1 ELSE 0 END AS flag_f_pvalue,
    CASE WHEN m.n_obs            >= 200  THEN 1 ELSE 0 END AS flag_n_obs,
    CASE WHEN m.condition_number <  30   THEN 1 ELSE 0 END AS flag_condition,
    CASE WHEN m.durbin_watson BETWEEN 1.5 AND 2.5 THEN 1 ELSE 0 END AS flag_dw,
    (
        CASE WHEN m.adj_r2           >  0.20 THEN 1 ELSE 0 END +
        CASE WHEN m.f_pvalue         <  0.05 THEN 1 ELSE 0 END +
        CASE WHEN m.n_obs            >= 200  THEN 1 ELSE 0 END +
        CASE WHEN m.condition_number <  30   THEN 1 ELSE 0 END +
        CASE WHEN m.durbin_watson BETWEEN 1.5 AND 2.5 THEN 1 ELSE 0 END
    )::smallint AS computed_quality_score
FROM fact_factor_reg_meta m;

COMMENT ON TABLE fact_factor_loadings IS
    'Rolling ticker-level Fama-French/Carhart factor loadings estimated from daily returns.';
COMMENT ON TABLE fact_factor_reg_meta IS
    'Diagnostics for rolling factor regressions, including quality score and residual risk.';
COMMENT ON TABLE fact_factor_implied_returns IS
    'Out-of-sample factor-implied daily returns using the latest completed rolling beta window.';
