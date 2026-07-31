-- Simplify review queue triage from many brittle buckets to three lanes:
-- map_candidate, special_case_review, likely_exclude.
-- This changes only review staging metadata, not protected production mappings.

SET search_path TO sec, public;

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS map_concept_to_taxonomy_review_queue_review_class_check;

UPDATE map_concept_to_taxonomy_review_queue
SET review_class = CASE
    WHEN review_class = 'core_fundamental' THEN 'map_candidate'
    WHEN review_class IN ('supplemental_numeric', 'rate_or_ratio', 'rollforward_component') THEN 'special_case_review'
    ELSE 'likely_exclude'
END
WHERE review_class IN (
    'core_fundamental',
    'supplemental_numeric',
    'nonfundamental_disclosure',
    'rollforward_component',
    'rate_or_ratio',
    'table_member_noise',
    'unmappable_noise'
);

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT map_concept_to_taxonomy_review_queue_review_class_check
    CHECK (review_class IN (
        'map_candidate',
        'special_case_review',
        'likely_exclude'
    ));
