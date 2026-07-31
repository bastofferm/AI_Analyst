-- Extend v_dim_company to UNION dim_company_intl as a third jurisdiction.
--
-- The base view (US + JP arms) is created out-of-band (not in this repo's sql/
-- migrations, per pg_get_viewdef), so this migration recreates the whole view
-- with the INTL arm added. Column shape and semantics are preserved for the
-- US/JP arms; INTL supplies cik=NULL, edinet_code=NULL, uid=intl_company_id::text.

SET search_path TO sec, public;

CREATE OR REPLACE VIEW v_dim_company AS
SELECT 'US'::text AS jurisdiction,
       d.cik AS uid,
       d.cik,
       NULL::text AS edinet_code,
       d.primary_ticker AS ticker,
       d.primary_ticker,
       d.name,
       d.exchange,
       COALESCE(d.country_code, 'US'::text) AS country_code,
       'EQUITY'::text AS quote_type,
       d.gics_sector_code,
       d.gics_sector_name,
       d.gics_industry_group_code,
       d.gics_industry_group_name,
       NULL::text AS gics_industry_name,
       NULL::text AS gics_sub_industry_name,
       d.mapping_sector
  FROM dim_company_us d
 WHERE d.primary_ticker IS NOT NULL AND COALESCE(d.include_in_pipeline, true)
UNION ALL
SELECT 'JP'::text AS jurisdiction,
       d.edinet_code AS uid,
       NULL::text AS cik,
       d.edinet_code,
       d.primary_ticker AS ticker,
       d.primary_ticker,
       COALESCE(d.name_en, d.name, d.primary_ticker) AS name,
       NULL::text AS exchange,
       COALESCE(d.country_code, 'JP'::text) AS country_code,
       'EQUITY'::text AS quote_type,
       d.gics_sector_code,
       d.gics_sector_name,
       d.gics_industry_group_code,
       d.gics_industry_group_name,
       NULL::text AS gics_industry_name,
       NULL::text AS gics_sub_industry_name,
       d.mapping_sector
  FROM dim_company_jp d
 WHERE d.primary_ticker IS NOT NULL AND COALESCE(d.include_in_pipeline, true)
UNION ALL
SELECT 'INTL'::text AS jurisdiction,
       d.intl_company_id::text AS uid,
       NULL::text AS cik,
       NULL::text AS edinet_code,
       d.primary_ticker AS ticker,
       d.primary_ticker,
       COALESCE(d.name_en, d.name, d.primary_ticker) AS name,
       d.exchange,
       d.country_code,
       COALESCE(d.quote_type, 'EQUITY'::text) AS quote_type,
       d.gics_sector_code,
       COALESCE(d.gics_sector_name, d.sector) AS gics_sector_name,
       d.gics_industry_group_code,
       COALESCE(d.gics_industry_group_name, d.industry) AS gics_industry_group_name,
       NULL::text AS gics_industry_name,
       NULL::text AS gics_sub_industry_name,
       d.mapping_sector
  FROM dim_company_intl d
 WHERE d.primary_ticker IS NOT NULL AND COALESCE(d.include_in_pipeline, true);

COMMENT ON VIEW v_dim_company IS
    'Cross-jurisdiction company dimension. UNION of dim_company_us + dim_company_jp + dim_company_intl. INTL companies come from Yahoo (dim_company_intl) with cik and edinet_code both NULL.';
