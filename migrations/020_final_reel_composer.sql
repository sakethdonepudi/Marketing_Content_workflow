-- Final Reel Composer: immutable, review-gated derivatives of approved generated video.
-- Additive and forward-only. Composition never mutates or replaces the source asset.

CREATE TABLE final_reel_assets(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  content_package_id TEXT NOT NULL REFERENCES content_packages(id),
  content_package_version INTEGER NOT NULL,
  source_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  source_asset_version INTEGER NOT NULL,
  source_asset_checksum_sha256 TEXT NOT NULL,
  source_render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  storage_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL CHECK(mime_type='video/mp4'),
  width INTEGER NOT NULL CHECK(width > 0),
  height INTEGER NOT NULL CHECK(height > 0),
  duration_seconds REAL NOT NULL CHECK(duration_seconds > 0),
  frame_rate REAL,
  codec TEXT,
  has_audio INTEGER NOT NULL CHECK(has_audio IN (0,1)),
  audio_codec TEXT,
  file_size INTEGER NOT NULL CHECK(file_size > 0),
  checksum_sha256 TEXT NOT NULL,
  narration_text TEXT NOT NULL,
  voice_provider TEXT NOT NULL,
  voice_model TEXT NOT NULL,
  subtitle_manifest_json TEXT NOT NULL,
  audio_manifest_json TEXT NOT NULL,
  transform_manifest_json TEXT NOT NULL,
  transform_hash TEXT NOT NULL,
  technical_qa_json TEXT NOT NULL,
  subtitle_qa_json TEXT NOT NULL,
  audio_qa_json TEXT NOT NULL,
  factual_qa_json TEXT NOT NULL,
  instagram_compatibility_json TEXT NOT NULL,
  facebook_compatibility_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('READY_FOR_REVIEW','BLOCKED')),
  human_review_status TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(human_review_status='REQUIRED'),
  cost_status TEXT NOT NULL CHECK(cost_status IN ('known','unknown','not_billed')),
  cost_usd REAL,
  currency TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(source_asset_id,transform_hash),
  UNIQUE(checksum_sha256)
);
CREATE INDEX idx_final_reel_assets_event ON final_reel_assets(event_id,created_at DESC);

CREATE TABLE final_reel_reviews(
  id TEXT PRIMARY KEY,
  final_reel_asset_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  action TEXT NOT NULL CHECK(action IN ('APPROVED','CHANGES_REQUIRED','REJECTED')),
  reviewer TEXT NOT NULL,
  comment TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_final_reel_reviews_asset ON final_reel_reviews(final_reel_asset_id,created_at DESC);

CREATE TRIGGER final_reel_assets_immutable_update BEFORE UPDATE ON final_reel_assets BEGIN
  SELECT RAISE(ABORT,'final reel assets are immutable');
END;
CREATE TRIGGER final_reel_assets_immutable_delete BEFORE DELETE ON final_reel_assets BEGIN
  SELECT RAISE(ABORT,'final reel assets are immutable');
END;
CREATE TRIGGER final_reel_reviews_immutable_update BEFORE UPDATE ON final_reel_reviews BEGIN
  SELECT RAISE(ABORT,'final reel reviews are immutable');
END;
CREATE TRIGGER final_reel_reviews_immutable_delete BEFORE DELETE ON final_reel_reviews BEGIN
  SELECT RAISE(ABORT,'final reel reviews are immutable');
END;
