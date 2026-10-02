-- Real-world media discovery: candidates found and rights-checked, never auto-used.
-- Additive and forward-only. Only APPROVED_FOR_USE candidates may enter production.

CREATE TABLE media_candidates(
  id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL UNIQUE,
  publisher TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  title TEXT NOT NULL,
  license_status TEXT NOT NULL CHECK(license_status IN
    ('VERIFIED_REUSE','ATTRIBUTION_REQUIRED','USER_PROVIDED','UNKNOWN','REJECTED')),
  license_text TEXT,
  attribution_required INTEGER NOT NULL DEFAULT 0 CHECK(attribution_required IN (0,1)),
  ap_specific TEXT NOT NULL DEFAULT 'unknown' CHECK(ap_specific IN ('yes','no','unknown')),
  real_footage INTEGER NOT NULL DEFAULT 1 CHECK(real_footage IN (0,1)),
  recommended_scene TEXT,
  state TEXT,
  district TEXT,
  location_confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
  usage_scope TEXT NOT NULL DEFAULT 'contextual',
  verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  lifecycle_state TEXT NOT NULL DEFAULT 'DISCOVERED' CHECK(lifecycle_state IN
    ('DISCOVERED','RIGHTS_CHECK','APPROVED_FOR_USE','INGESTED','REJECTED')),
  content_hash TEXT,
  downloaded_at TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_media_candidates_state ON media_candidates(lifecycle_state,license_status);
