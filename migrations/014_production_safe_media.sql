-- Architecture 06E: resumable provider jobs, paid-call idempotency, derived assets,
-- versioned QA runs, and per-version human review. Additive and forward-only.

ALTER TABLE render_jobs ADD COLUMN resume_state TEXT
  CHECK(resume_state IS NULL OR resume_state IN ('PROVIDER_PENDING','NEEDS_INTERVENTION','INTERRUPTED'));
ALTER TABLE render_jobs ADD COLUMN resume_reason TEXT;
ALTER TABLE render_jobs ADD COLUMN last_resume_check_at TEXT;
ALTER TABLE render_jobs ADD COLUMN resume_check_count INTEGER NOT NULL DEFAULT 0 CHECK(resume_check_count >= 0);
ALTER TABLE render_jobs ADD COLUMN request_fingerprint TEXT;
ALTER TABLE render_jobs ADD COLUMN client_request_id TEXT;
ALTER TABLE render_jobs ADD COLUMN source_derivative_id TEXT;
ALTER TABLE production_jobs ADD COLUMN client_request_id TEXT;

-- The fingerprint deliberately excludes the regeneration counter. Only one equivalent
-- paid render may be active, while an explicit regeneration is still possible after
-- the prior job reaches a terminal state.
CREATE UNIQUE INDEX idx_render_one_active_fingerprint
  ON render_jobs(request_fingerprint)
  WHERE request_fingerprint IS NOT NULL
    AND status IN ('QUEUED','PREPARING','RENDERING','VALIDATING');
CREATE UNIQUE INDEX idx_production_client_request
  ON production_jobs(client_request_id)
  WHERE client_request_id IS NOT NULL;

-- One row per client-supplied paid-request key: replays return the original job, never a new paid call.
CREATE TABLE paid_request_keys(
  request_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('RENDER','PRODUCTION')),
  job_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE derived_assets(
  id TEXT PRIMARY KEY,
  source_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  source_checksum_sha256 TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('VIDEO_SOURCE','QA_FRAME')),
  storage_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  file_size INTEGER NOT NULL CHECK(file_size > 0),
  checksum_sha256 TEXT NOT NULL,
  frame_time_seconds REAL,
  transform_json TEXT NOT NULL,
  transform_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(source_asset_id,purpose,transform_hash)
);
CREATE INDEX idx_derived_assets_source ON derived_assets(source_asset_id,purpose);

CREATE TABLE media_qa_runs(
  id TEXT PRIMARY KEY,
  generated_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  qa_kind TEXT NOT NULL CHECK(qa_kind IN ('TECHNICAL','OCR','VISUAL')),
  run_number INTEGER NOT NULL CHECK(run_number >= 1),
  trigger TEXT NOT NULL CHECK(trigger IN ('AUTOMATIC','MANUAL')),
  status TEXT NOT NULL CHECK(status IN ('PASS','FLAG','UNKNOWN')),
  provider TEXT,
  model TEXT,
  prompt_version TEXT,
  content_package_id TEXT,
  content_package_version INTEGER,
  checks_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  explanation TEXT,
  provider_request_id TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown','not_billed')),
  cost_usd REAL,
  created_at TEXT NOT NULL,
  UNIQUE(generated_asset_id,qa_kind,run_number)
);
CREATE INDEX idx_media_qa_runs_asset ON media_qa_runs(generated_asset_id,qa_kind,run_number DESC);

CREATE TABLE media_reviews(
  id TEXT PRIMARY KEY,
  generated_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  asset_version INTEGER NOT NULL,
  content_package_id TEXT NOT NULL,
  content_package_version INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('APPROVED','CHANGES_REQUIRED','REJECTED')),
  reviewer TEXT NOT NULL,
  comment TEXT,
  qa_run_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_media_reviews_asset ON media_reviews(generated_asset_id,created_at DESC);

CREATE TRIGGER paid_request_keys_immutable_update BEFORE UPDATE ON paid_request_keys BEGIN
  SELECT RAISE(ABORT,'paid request keys are immutable');
END;
CREATE TRIGGER paid_request_keys_immutable_delete BEFORE DELETE ON paid_request_keys BEGIN
  SELECT RAISE(ABORT,'paid request keys are immutable');
END;
CREATE TRIGGER derived_assets_immutable_update BEFORE UPDATE ON derived_assets BEGIN
  SELECT RAISE(ABORT,'derived assets are immutable');
END;
CREATE TRIGGER derived_assets_immutable_delete BEFORE DELETE ON derived_assets BEGIN
  SELECT RAISE(ABORT,'derived assets are immutable');
END;
CREATE TRIGGER media_qa_runs_immutable_update BEFORE UPDATE ON media_qa_runs BEGIN
  SELECT RAISE(ABORT,'media QA runs are immutable');
END;
CREATE TRIGGER media_qa_runs_immutable_delete BEFORE DELETE ON media_qa_runs BEGIN
  SELECT RAISE(ABORT,'media QA runs are immutable');
END;
CREATE TRIGGER media_reviews_immutable_update BEFORE UPDATE ON media_reviews BEGIN
  SELECT RAISE(ABORT,'media reviews are immutable');
END;
CREATE TRIGGER media_reviews_immutable_delete BEFORE DELETE ON media_reviews BEGIN
  SELECT RAISE(ABORT,'media reviews are immutable');
END;
