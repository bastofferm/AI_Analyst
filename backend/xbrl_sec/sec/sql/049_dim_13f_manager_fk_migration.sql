-- Move 13F manager foreign keys from legacy ref_13f_manager to dim_13f_manager.

SET search_path TO sec, public;

DO $$
BEGIN
    IF to_regclass('sec.ref_13f_manager') IS NOT NULL THEN
        INSERT INTO dim_13f_manager (
            manager_cik, manager_name, manager_type, is_public_company, public_entity_cik,
            name_source, filing_count_primary, filing_count_total,
            first_quarter_filed, last_quarter_filed, last_seen_at, created_at, updated_at
        )
        SELECT
            r.manager_cik,
            r.manager_name,
            r.manager_type,
            r.is_public_company,
            r.public_entity_cik,
            'submission',
            r.filing_count_13f,
            r.filing_count_13f,
            r.first_quarter_filed,
            r.last_quarter_filed,
            r.last_seen_at,
            r.created_at,
            r.updated_at
        FROM ref_13f_manager r
        ON CONFLICT (manager_cik) DO UPDATE SET
            manager_name = CASE
                WHEN dim_13f_manager.manager_name = dim_13f_manager.manager_cik
                     AND EXCLUDED.manager_name <> EXCLUDED.manager_cik
                THEN EXCLUDED.manager_name
                ELSE dim_13f_manager.manager_name
            END,
            manager_type = COALESCE(NULLIF(dim_13f_manager.manager_type, 'unknown'), EXCLUDED.manager_type, 'unknown'),
            is_public_company = dim_13f_manager.is_public_company OR EXCLUDED.is_public_company,
            public_entity_cik = COALESCE(dim_13f_manager.public_entity_cik, EXCLUDED.public_entity_cik),
            name_source = COALESCE(dim_13f_manager.name_source, EXCLUDED.name_source),
            filing_count_primary = GREATEST(dim_13f_manager.filing_count_primary, EXCLUDED.filing_count_primary),
            filing_count_total = GREATEST(dim_13f_manager.filing_count_total, EXCLUDED.filing_count_total),
            first_quarter_filed = LEAST(
                COALESCE(dim_13f_manager.first_quarter_filed, EXCLUDED.first_quarter_filed),
                COALESCE(EXCLUDED.first_quarter_filed, dim_13f_manager.first_quarter_filed)
            ),
            last_quarter_filed = GREATEST(
                COALESCE(dim_13f_manager.last_quarter_filed, EXCLUDED.last_quarter_filed),
                COALESCE(EXCLUDED.last_quarter_filed, dim_13f_manager.last_quarter_filed)
            ),
            last_seen_at = GREATEST(
                COALESCE(dim_13f_manager.last_seen_at, EXCLUDED.last_seen_at),
                COALESCE(EXCLUDED.last_seen_at, dim_13f_manager.last_seen_at)
            ),
            updated_at = now();
    END IF;
END $$;

INSERT INTO dim_13f_manager (manager_cik, manager_name, name_source)
SELECT DISTINCT manager_cik, COALESCE(NULLIF(manager_name, ''), manager_cik), 'submission'
FROM fact_13f_submission
WHERE manager_cik IS NOT NULL
ON CONFLICT (manager_cik) DO NOTHING;

INSERT INTO dim_13f_manager (manager_cik, manager_name, name_source)
SELECT DISTINCT manager_cik, manager_cik, 'submission'
FROM source_13f_filing_state
WHERE manager_cik IS NOT NULL
ON CONFLICT (manager_cik) DO NOTHING;

INSERT INTO dim_13f_manager (manager_cik, manager_name, name_source)
SELECT DISTINCT manager_cik, manager_cik, 'submission'
FROM fact_13f_holdings
WHERE manager_cik IS NOT NULL
ON CONFLICT (manager_cik) DO NOTHING;

ALTER TABLE source_13f_filing_state DROP CONSTRAINT IF EXISTS source_13f_filing_state_manager_cik_fkey;
ALTER TABLE fact_13f_submission DROP CONSTRAINT IF EXISTS fact_13f_submission_manager_cik_fkey;
ALTER TABLE fact_13f_holdings DROP CONSTRAINT IF EXISTS fact_13f_holdings_manager_cik_fkey;

ALTER TABLE source_13f_filing_state
    ADD CONSTRAINT source_13f_filing_state_manager_cik_fkey
    FOREIGN KEY (manager_cik) REFERENCES dim_13f_manager(manager_cik) NOT VALID;

ALTER TABLE fact_13f_submission
    ADD CONSTRAINT fact_13f_submission_manager_cik_fkey
    FOREIGN KEY (manager_cik) REFERENCES dim_13f_manager(manager_cik) NOT VALID;

ALTER TABLE fact_13f_holdings
    ADD CONSTRAINT fact_13f_holdings_manager_cik_fkey
    FOREIGN KEY (manager_cik) REFERENCES dim_13f_manager(manager_cik) NOT VALID;

ALTER TABLE source_13f_filing_state VALIDATE CONSTRAINT source_13f_filing_state_manager_cik_fkey;
ALTER TABLE fact_13f_submission VALIDATE CONSTRAINT fact_13f_submission_manager_cik_fkey;
ALTER TABLE fact_13f_holdings VALIDATE CONSTRAINT fact_13f_holdings_manager_cik_fkey;

DROP TABLE IF EXISTS ref_13f_manager;
