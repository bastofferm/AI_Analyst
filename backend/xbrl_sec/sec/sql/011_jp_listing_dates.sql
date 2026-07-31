-- Listing-status audit fields for JP company master.

SET search_path TO sec, public;

ALTER TABLE dim_company_jp ADD COLUMN IF NOT EXISTS listing_date DATE;
ALTER TABLE dim_company_jp ADD COLUMN IF NOT EXISTS delisting_date DATE;
