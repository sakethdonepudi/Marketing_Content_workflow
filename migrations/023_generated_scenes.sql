-- Original neutral scenes generated with the configured live image renderer for
-- multi-visual Final Reels. Additive and forward-only. Rights are always
-- GENERATED_ORIGINAL; the prompt and provider request are stored for provenance.

CREATE TABLE generated_scenes(
  id TEXT PRIMARY KEY,
  scene_key TEXT NOT NULL,
  label TEXT NOT NULL,
  storage_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  file_size INTEGER NOT NULL CHECK(file_size > 0),
  checksum_sha256 TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL,
  model TEXT,
  provider_request_id TEXT,
  prompt TEXT NOT NULL,
  rights_status TEXT NOT NULL CHECK(rights_status IN ('GENERATED_ORIGINAL','WORKSPACE_VERIFIED')),
  cost_status TEXT NOT NULL CHECK(cost_status IN ('known','unknown','not_billed')),
  cost_usd REAL,
  currency TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(scene_key,checksum_sha256)
);
CREATE INDEX idx_generated_scenes_key ON generated_scenes(scene_key,created_at DESC);

ALTER TABLE final_reel_assets ADD COLUMN source_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN editorial_continuity_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN narration_naturalness_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN narration_job_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN continuous_narration_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN year_pronunciation_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN local_context_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN rights_provenance_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN rendered_frame_continuity_qa_json TEXT;

