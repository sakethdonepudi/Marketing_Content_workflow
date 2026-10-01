-- Focused verification fix: preserve the legacy relationship used by downstream
-- gates while recording the more precise claim-to-passage classification.

ALTER TABLE verification_runs ADD COLUMN evidence_classifier_version TEXT;

ALTER TABLE verification_decision_evidence ADD COLUMN classification TEXT
  CHECK(classification IS NULL OR classification IN (
    'DIRECT_SUPPORT','PARTIAL_SUPPORT','MENTIONS_ONLY','CONTRADICTS','IRRELEVANT'
  ));
