-- Core US fundamentals exclude 6-K, 8-K, proxy, registration, fee, and exhibit-only forms.

SET search_path TO sec, public;

DELETE FROM fact_fundamentals_std_us
WHERE filing_form IS NOT NULL
  AND filing_form NOT IN ('10-K','10-K/A','10-Q','10-Q/A','20-F','20-F/A','40-F','40-F/A');

DELETE FROM fact_fundamentals_us
WHERE filing_type IS NOT NULL
  AND filing_type NOT IN ('10-K','10-K/A','10-Q','10-Q/A','20-F','20-F/A','40-F','40-F/A');

DELETE FROM source_filing_state
WHERE jurisdiction = 'US'
  AND source_kind = 'companyfacts'
  AND filing_type IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM regexp_split_to_table(filing_type, ',') AS form(form_name)
      WHERE btrim(form.form_name) IN ('10-K','10-K/A','10-Q','10-Q/A','20-F','20-F/A','40-F','40-F/A')
  );
