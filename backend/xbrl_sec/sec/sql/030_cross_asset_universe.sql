SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_cross_asset (
    ticker      TEXT        NOT NULL,
    name        TEXT,
    asset_class TEXT,
    PRIMARY KEY (ticker)
);

CREATE INDEX IF NOT EXISTS idx_dim_cross_asset_class ON dim_cross_asset (asset_class);
