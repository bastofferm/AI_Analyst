-- Add explicit action for rows that should become reviewed mapping-change
-- candidates rather than remain vague needs_review rows.

SET search_path TO sec, public;

ALTER TABLE concept_target_display_policy
    DROP CONSTRAINT IF EXISTS concept_target_display_policy_policy_action_check;

ALTER TABLE concept_target_display_policy
    ADD CONSTRAINT concept_target_display_policy_policy_action_check
    CHECK (policy_action IN (
        'allow_main',
        'prefer_main',
        'fallback_only',
        'component_only',
        'supplemental_only',
        'audit_only',
        'deny_main',
        'mapping_change_candidate',
        'needs_review'
    ));
