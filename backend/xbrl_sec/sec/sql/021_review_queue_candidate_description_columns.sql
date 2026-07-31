-- Make the review queue inspectable without opening candidate_targets JSON.

SET search_path TO sec, public;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD COLUMN IF NOT EXISTS top_candidate_label TEXT,
    ADD COLUMN IF NOT EXISTS top_candidate_description TEXT,
    ADD COLUMN IF NOT EXISTS top_candidate_category TEXT,
    ADD COLUMN IF NOT EXISTS top_candidate_unit_type TEXT;
