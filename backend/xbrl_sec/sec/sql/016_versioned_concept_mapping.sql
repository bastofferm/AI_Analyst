-- Protected, effective-dated concept mapping layer.
--
-- map_concept_to_taxonomy remains the legacy/staging snapshot table. This
-- versioned table is governed production mapping data and must never be
-- truncated by reference sync or pipeline refresh commands.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS map_concept_to_taxonomy_versioned (
    mapping_id BIGSERIAL PRIMARY KEY,
    concept_id TEXT NOT NULL,
    target_variable TEXT NOT NULL,
    tier INTEGER,
    multiplier NUMERIC NOT NULL DEFAULT 1,
    reasoning TEXT,
    mapping_sector TEXT NOT NULL DEFAULT '',
    jurisdiction TEXT NOT NULL DEFAULT 'BOTH',
    effective_from_year SMALLINT NOT NULL DEFAULT 1900,
    effective_to_year SMALLINT,
    taxonomy_version TEXT,
    accounting_standard TEXT,
    review_status TEXT NOT NULL DEFAULT 'legacy_imported',
    mapping_source TEXT NOT NULL DEFAULT 'map_concept_to_taxonomy_snapshot',
    confidence NUMERIC,
    gics_sector TEXT,
    gics_industry_group TEXT,
    gics_industry TEXT,
    gics_sub_industry TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_mctv_jurisdiction
        CHECK (jurisdiction IN ('US', 'JP', 'BOTH')),
    CONSTRAINT chk_mctv_effective_years
        CHECK (effective_to_year IS NULL OR effective_to_year >= effective_from_year),
    CONSTRAINT chk_mctv_confidence
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

COMMENT ON TABLE map_concept_to_taxonomy_versioned IS
    'Protected production concept-to-taxonomy mappings. Do not TRUNCATE or bulk overwrite; use inserts or targeted effective-date closures.';
COMMENT ON TABLE map_concept_to_taxonomy IS
    'Legacy/staging concept-to-taxonomy snapshot. Safe target for reference sync; not the governed production mapping source.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.gics_sector IS
    'Optional GICS sector code constraint. NULL means the mapping is generic at this GICS level.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.gics_industry_group IS
    'Optional GICS industry group code constraint. NULL means the mapping is generic at this GICS level.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.gics_industry IS
    'Optional GICS industry code constraint. NULL means the mapping is generic at this GICS level.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.gics_sub_industry IS
    'Optional GICS sub-industry code constraint. NULL means the mapping is generic at this GICS level.';

CREATE INDEX IF NOT EXISTS idx_mctv_lookup
    ON map_concept_to_taxonomy_versioned
    (concept_id, jurisdiction, mapping_sector, effective_from_year, effective_to_year);

CREATE INDEX IF NOT EXISTS idx_mctv_target
    ON map_concept_to_taxonomy_versioned (target_variable);

CREATE INDEX IF NOT EXISTS idx_mctv_gics
    ON map_concept_to_taxonomy_versioned
    (gics_sector, gics_industry_group, gics_industry, gics_sub_industry);

CREATE OR REPLACE FUNCTION prevent_versioned_mapping_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    conflicting_mapping_id BIGINT;
BEGIN
    SELECT m.mapping_id
      INTO conflicting_mapping_id
      FROM map_concept_to_taxonomy_versioned m
     WHERE m.mapping_id <> COALESCE(NEW.mapping_id, -1)
       AND (m.jurisdiction = NEW.jurisdiction
            OR m.jurisdiction = 'BOTH'
            OR NEW.jurisdiction = 'BOTH')
       AND m.concept_id = NEW.concept_id
       AND m.mapping_sector IS NOT DISTINCT FROM NEW.mapping_sector
       AND m.gics_sector IS NOT DISTINCT FROM NEW.gics_sector
       AND m.gics_industry_group IS NOT DISTINCT FROM NEW.gics_industry_group
       AND m.gics_industry IS NOT DISTINCT FROM NEW.gics_industry
       AND m.gics_sub_industry IS NOT DISTINCT FROM NEW.gics_sub_industry
       AND m.effective_from_year <= COALESCE(NEW.effective_to_year, 9999)
       AND NEW.effective_from_year <= COALESCE(m.effective_to_year, 9999)
     LIMIT 1;

    IF conflicting_mapping_id IS NOT NULL THEN
        RAISE EXCEPTION
            'Overlapping versioned concept mapping % conflicts with concept %, jurisdiction %, sector %, years %-%',
            conflicting_mapping_id,
            NEW.concept_id,
            NEW.jurisdiction,
            NEW.mapping_sector,
            NEW.effective_from_year,
            COALESCE(NEW.effective_to_year::TEXT, 'open');
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mctv_prevent_overlap ON map_concept_to_taxonomy_versioned;
CREATE TRIGGER trg_mctv_prevent_overlap
BEFORE INSERT OR UPDATE ON map_concept_to_taxonomy_versioned
FOR EACH ROW
EXECUTE FUNCTION prevent_versioned_mapping_overlap();

INSERT INTO map_concept_to_taxonomy_versioned (
    concept_id,
    target_variable,
    tier,
    multiplier,
    reasoning,
    mapping_sector,
    jurisdiction,
    effective_from_year,
    effective_to_year,
    review_status,
    mapping_source,
    created_at
)
SELECT
    m.concept_id,
    m.target_variable,
    m.tier,
    COALESCE(m.multiplier, 1),
    m.reasoning,
    COALESCE(m.mapping_sector, ''),
    CASE
        WHEN m.concept_id LIKE 'us-gaap/%'
          OR m.concept_id LIKE 'us-gaap:%'
          OR m.concept_id LIKE 'srt/%'
          OR m.concept_id LIKE 'srt:%'
          OR m.concept_id LIKE 'dei/%'
          OR m.concept_id LIKE 'dei:%'
            THEN 'US'
        WHEN m.concept_id LIKE 'jp%'
          OR m.concept_id LIKE 'jppfs%'
          OR m.concept_id LIKE 'jpcrp%'
          OR m.concept_id LIKE 'jpdei%'
          OR m.concept_id LIKE 'jpigp%'
            THEN 'JP'
        ELSE 'BOTH'
    END AS jurisdiction,
    1900,
    NULL::SMALLINT,
    'legacy_imported',
    'map_concept_to_taxonomy_snapshot',
    COALESCE(m.created_at, now())
FROM map_concept_to_taxonomy m
WHERE NOT EXISTS (
    SELECT 1
      FROM map_concept_to_taxonomy_versioned v
     WHERE v.concept_id = m.concept_id
       AND v.target_variable = m.target_variable
       AND v.tier IS NOT DISTINCT FROM m.tier
       AND v.multiplier IS NOT DISTINCT FROM COALESCE(m.multiplier, 1)
       AND v.mapping_sector IS NOT DISTINCT FROM COALESCE(m.mapping_sector, '')
       AND v.effective_from_year = 1900
       AND v.effective_to_year IS NULL
       AND v.gics_sector IS NULL
       AND v.gics_industry_group IS NULL
       AND v.gics_industry IS NULL
       AND v.gics_sub_industry IS NULL
       AND v.mapping_source = 'map_concept_to_taxonomy_snapshot'
);

ALTER TABLE fact_fundamentals_std_us
    ADD COLUMN IF NOT EXISTS mapping_id BIGINT;

ALTER TABLE fact_fundamentals_std_jp
    ADD COLUMN IF NOT EXISTS mapping_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_ffstd_us_mapping_id
    ON fact_fundamentals_std_us (mapping_id);

CREATE INDEX IF NOT EXISTS idx_ffstd_jp_mapping_id
    ON fact_fundamentals_std_jp (mapping_id);
