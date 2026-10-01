-- Content CEO decisions distinguish evidence media from an original-generation
-- production strategy. Historical decisions remain immutable and linked.

ALTER TABLE content_decisions ADD COLUMN media_source_strategy TEXT NOT NULL DEFAULT 'NONE'
  CHECK(media_source_strategy IN (
    'GENERATE_ORIGINAL','USE_APPROVED_OWNED_MEDIA','USE_APPROVED_LICENSED_MEDIA','NONE'
  ));

ALTER TABLE content_decisions ADD COLUMN previous_decision_id TEXT REFERENCES content_decisions(id);
CREATE INDEX idx_content_decisions_previous ON content_decisions(previous_decision_id);
