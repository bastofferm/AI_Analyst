-- Phase 2: Normalized XBRL linkbase relationship storage.
--
-- One row per (filing, linkbase type, parent concept, child concept) edge.
-- Replaces the implicit calculation/presentation/definition evidence that
-- today is denormalized onto raw fact rows. The shared M:1 resolver and
-- mapping review tools query this table for aggregation safety and
-- dimension compatibility.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_xbrl_relationship_edge (
    edge_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id TEXT,
    filing_id TEXT,
    taxonomy TEXT,
    role_uri TEXT,
    linkbase_type TEXT NOT NULL CHECK (linkbase_type IN ('calculation', 'presentation', 'definition')),
    parent_concept_id TEXT,
    child_concept_id TEXT NOT NULL,
    weight NUMERIC,
    order_index NUMERIC,
    arcrole TEXT,
    preferred_label TEXT,
    dimension_axis TEXT,
    dimension_member TEXT,
    usable BOOLEAN,
    source_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_xbrl_relationship_edge IS
    'Normalized XBRL linkbase arcs (calculation, presentation, definition). Queryable evidence for aggregation safety, dimension compatibility, and mapping review.';
COMMENT ON COLUMN ref_xbrl_relationship_edge.linkbase_type IS
    'Source linkbase: calculation (cal), presentation (pre), or definition (def).';
COMMENT ON COLUMN ref_xbrl_relationship_edge.weight IS
    'Calculation arc weight (typically +1 or -1). NULL for non-calculation arcs.';
COMMENT ON COLUMN ref_xbrl_relationship_edge.dimension_axis IS
    'Definition arc axis/dimension. NULL for non-definition arcs.';
COMMENT ON COLUMN ref_xbrl_relationship_edge.dimension_member IS
    'Definition arc member of the axis. NULL for non-definition arcs.';
COMMENT ON COLUMN ref_xbrl_relationship_edge.usable IS
    'Definition arc usable flag for dimensional facts. NULL for non-definition arcs.';
COMMENT ON COLUMN ref_xbrl_relationship_edge.source_path IS
    'Absolute or relative path to the linkbase file the edge was read from; for traceability.';

CREATE INDEX IF NOT EXISTS idx_rxre_filing_child
    ON ref_xbrl_relationship_edge (filing_id, child_concept_id);
CREATE INDEX IF NOT EXISTS idx_rxre_filing_parent
    ON ref_xbrl_relationship_edge (filing_id, parent_concept_id)
    WHERE parent_concept_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rxre_linkbase_role
    ON ref_xbrl_relationship_edge (linkbase_type, role_uri);
CREATE INDEX IF NOT EXISTS idx_rxre_jurisdiction_filing
    ON ref_xbrl_relationship_edge (jurisdiction, filing_id);

-- Uniqueness avoids duplicate edges if backfill runs more than once.
-- COALESCE keeps NULLs distinct across the natural key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rxre_natural_key
    ON ref_xbrl_relationship_edge (
        jurisdiction,
        COALESCE(filing_id, ''),
        linkbase_type,
        COALESCE(role_uri, ''),
        COALESCE(parent_concept_id, ''),
        child_concept_id,
        COALESCE(dimension_axis, ''),
        COALESCE(dimension_member, '')
    );
