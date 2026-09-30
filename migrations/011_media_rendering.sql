ALTER TABLE events ADD COLUMN render_status TEXT NOT NULL DEFAULT 'NO_RENDER'
  CHECK(render_status IN ('NO_RENDER','QUEUED','PREPARING','RENDERING','VALIDATING','READY_FOR_REVIEW','HUMAN_REVIEW','BLOCKED','FAILED','CANCELLED'));

CREATE TABLE render_jobs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  content_decision_id TEXT NOT NULL REFERENCES content_decisions(id),
  production_job_id TEXT NOT NULL REFERENCES production_jobs(id),
  content_package_id TEXT NOT NULL REFERENCES content_packages(id),
  content_package_version INTEGER NOT NULL,
  media_type TEXT NOT NULL CHECK(media_type IN ('IMAGE','VIDEO','AUDIO','THUMBNAIL','CAROUSEL_SLIDE','VOICEOVER','SHORT_FORM_VIDEO','LONG_FORM_VIDEO')),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_mode TEXT NOT NULL CHECK(provider_mode IN ('live','fixture')),
  prompt_version TEXT NOT NULL,
  generation_config_version TEXT NOT NULL,
  input_version TEXT NOT NULL,
  input_media_asset_ids_json TEXT NOT NULL DEFAULT '[]',
  output_media_asset_ids_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT NOT NULL,
  regeneration_number INTEGER NOT NULL DEFAULT 1 CHECK(regeneration_number >= 1),
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
  max_retries INTEGER NOT NULL DEFAULT 0 CHECK(max_retries >= 0),
  status TEXT NOT NULL CHECK(status IN ('QUEUED','PREPARING','RENDERING','VALIDATING','READY_FOR_REVIEW','HUMAN_REVIEW','BLOCKED','FAILED','CANCELLED')),
  fixture_only INTEGER NOT NULL DEFAULT 0 CHECK(fixture_only IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  provider_called INTEGER NOT NULL DEFAULT 0 CHECK(provider_called IN (0,1)),
  provider_request_id TEXT,
  latency_ms INTEGER,
  request_count INTEGER,
  credits_consumed REAL,
  provider_units REAL,
  generation_seconds REAL,
  frame_count INTEGER,
  image_count INTEGER,
  provider_cost_usd REAL,
  calculated_cost_usd REAL,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown')),
  pricing_version TEXT,
  validation_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(validation_status IN ('PENDING','PASSED','FAILED')),
  validation_result_json TEXT,
  failure_code TEXT,
  failure_reason TEXT,
  UNIQUE(content_package_id,media_type,regeneration_number),
  UNIQUE(content_package_id,media_type,idempotency_key)
);

CREATE UNIQUE INDEX idx_render_one_active_target
  ON render_jobs(content_package_id,media_type)
  WHERE status IN ('QUEUED','PREPARING','RENDERING','VALIDATING');
CREATE INDEX idx_render_jobs_event ON render_jobs(event_id,created_at DESC);

CREATE TABLE render_prompt_snapshots(
  id TEXT PRIMARY KEY,
  render_job_id TEXT NOT NULL UNIQUE REFERENCES render_jobs(id),
  content_package_id TEXT NOT NULL REFERENCES content_packages(id),
  content_package_version INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  generation_config_version TEXT NOT NULL,
  request_json TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  reference_asset_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE render_job_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  attempt_number INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','FAILED')),
  provider_request_id TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  latency_ms INTEGER,
  retryable INTEGER CHECK(retryable IN (0,1)),
  error_code TEXT,
  error_message TEXT,
  usage_json TEXT,
  UNIQUE(render_job_id,attempt_number,status)
);

CREATE TABLE render_job_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  changed_at TEXT NOT NULL
);

CREATE TABLE generated_assets(
  id TEXT PRIMARY KEY,
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  content_package_id TEXT NOT NULL REFERENCES content_packages(id),
  content_package_version INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK(version_number >= 1),
  status TEXT NOT NULL CHECK(status IN ('GENERATED','VALIDATED','BLOCKED','INVALID')),
  executable INTEGER NOT NULL DEFAULT 0 CHECK(executable IN (0,1)),
  fixture_only INTEGER NOT NULL DEFAULT 0 CHECK(fixture_only IN (0,1)),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_asset_id TEXT,
  provider_request_id TEXT,
  original_provider_url TEXT,
  storage_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  duration_seconds REAL,
  frame_rate REAL,
  file_size INTEGER NOT NULL,
  checksum_sha256 TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  generation_config_version TEXT NOT NULL,
  source_asset_ids_json TEXT NOT NULL DEFAULT '[]',
  provenance_json TEXT NOT NULL,
  provider_metadata_json TEXT NOT NULL DEFAULT '{}',
  detected_text_json TEXT NOT NULL DEFAULT '[]',
  validation_status TEXT NOT NULL CHECK(validation_status IN ('PENDING','PASSED','FAILED')),
  validation_result_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(content_package_id,media_type,version_number),
  UNIQUE(content_package_id,media_type,checksum_sha256)
);

CREATE INDEX idx_generated_assets_event ON generated_assets(event_id,created_at DESC);
CREATE INDEX idx_generated_assets_package ON generated_assets(content_package_id,media_type,version_number DESC);

CREATE TABLE render_job_outputs(
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  generated_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  reused_identical_binary INTEGER NOT NULL DEFAULT 0 CHECK(reused_identical_binary IN (0,1)),
  PRIMARY KEY(render_job_id,generated_asset_id)
);

CREATE TRIGGER render_job_identity_immutable
BEFORE UPDATE OF event_id,content_decision_id,production_job_id,content_package_id,content_package_version,
  media_type,provider,model,provider_mode,prompt_version,generation_config_version,input_version,
  input_media_asset_ids_json,idempotency_key,regeneration_number,max_retries,fixture_only,created_at
ON render_jobs BEGIN
  SELECT RAISE(ABORT,'render job input snapshot is immutable');
END;

CREATE TRIGGER render_prompt_immutable_update BEFORE UPDATE ON render_prompt_snapshots BEGIN
  SELECT RAISE(ABORT,'render prompt snapshots are immutable');
END;
CREATE TRIGGER render_prompt_immutable_delete BEFORE DELETE ON render_prompt_snapshots BEGIN
  SELECT RAISE(ABORT,'render prompt snapshots are immutable');
END;
CREATE TRIGGER render_attempts_immutable_update BEFORE UPDATE ON render_job_attempts BEGIN
  SELECT RAISE(ABORT,'render attempts are immutable');
END;
CREATE TRIGGER render_attempts_immutable_delete BEFORE DELETE ON render_job_attempts BEGIN
  SELECT RAISE(ABORT,'render attempts are immutable');
END;
CREATE TRIGGER render_history_immutable_update BEFORE UPDATE ON render_job_status_history BEGIN
  SELECT RAISE(ABORT,'render history is immutable');
END;
CREATE TRIGGER render_history_immutable_delete BEFORE DELETE ON render_job_status_history BEGIN
  SELECT RAISE(ABORT,'render history is immutable');
END;
CREATE TRIGGER generated_assets_immutable_update BEFORE UPDATE ON generated_assets BEGIN
  SELECT RAISE(ABORT,'generated assets are immutable');
END;
CREATE TRIGGER generated_assets_immutable_delete BEFORE DELETE ON generated_assets BEGIN
  SELECT RAISE(ABORT,'generated assets are immutable');
END;
CREATE TRIGGER render_outputs_immutable_update BEFORE UPDATE ON render_job_outputs BEGIN
  SELECT RAISE(ABORT,'render output lineage is immutable');
END;
CREATE TRIGGER render_outputs_immutable_delete BEFORE DELETE ON render_job_outputs BEGIN
  SELECT RAISE(ABORT,'render output lineage is immutable');
END;
