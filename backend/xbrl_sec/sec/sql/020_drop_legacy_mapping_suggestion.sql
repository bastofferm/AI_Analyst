-- Remove the retired broad suggestion table.
--
-- The production mapping table is sec.map_concept_to_taxonomy_versioned.
-- The only review staging surface is sec.map_concept_to_taxonomy_review_queue.

SET search_path TO sec, public;

DROP TABLE IF EXISTS map_concept_to_taxonomy_suggestion;
