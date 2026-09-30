ALTER TABLE render_jobs ADD COLUMN provider_job_id TEXT;
ALTER TABLE render_jobs ADD COLUMN provider_status TEXT;
ALTER TABLE render_jobs ADD COLUMN submitted_at TEXT;
ALTER TABLE render_jobs ADD COLUMN last_polled_at TEXT;
ALTER TABLE render_jobs ADD COLUMN poll_count INTEGER NOT NULL DEFAULT 0 CHECK(poll_count >= 0);
ALTER TABLE render_jobs ADD COLUMN provider_started_at TEXT;
ALTER TABLE render_jobs ADD COLUMN provider_completed_at TEXT;
ALTER TABLE render_jobs ADD COLUMN provider_failure_code TEXT;
ALTER TABLE render_jobs ADD COLUMN provider_failure_reason TEXT;
ALTER TABLE render_jobs ADD COLUMN technical_validation_status TEXT NOT NULL DEFAULT 'PENDING'
  CHECK(technical_validation_status IN ('PENDING','PASSED','FAILED'));
ALTER TABLE render_jobs ADD COLUMN text_validation_status TEXT NOT NULL DEFAULT 'NOT_PERFORMED'
  CHECK(text_validation_status IN ('PASSED','FAILED','NOT_PERFORMED'));
ALTER TABLE render_jobs ADD COLUMN semantic_qa_status TEXT NOT NULL DEFAULT 'NOT_PERFORMED'
  CHECK(semantic_qa_status IN ('PASSED','FLAGGED','NOT_PERFORMED'));
ALTER TABLE render_jobs ADD COLUMN human_review_status TEXT NOT NULL DEFAULT 'REQUIRED'
  CHECK(human_review_status='REQUIRED');
ALTER TABLE render_jobs ADD COLUMN currency TEXT;
ALTER TABLE render_jobs ADD COLUMN input_units REAL;
ALTER TABLE render_jobs ADD COLUMN output_units REAL;

ALTER TABLE generated_assets ADD COLUMN usable_for_review INTEGER NOT NULL DEFAULT 0 CHECK(usable_for_review IN (0,1));
ALTER TABLE generated_assets ADD COLUMN stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1));
ALTER TABLE generated_assets ADD COLUMN stale_reason TEXT;
ALTER TABLE generated_assets ADD COLUMN technical_validation_status TEXT NOT NULL DEFAULT 'PENDING'
  CHECK(technical_validation_status IN ('PENDING','PASSED','FAILED'));
ALTER TABLE generated_assets ADD COLUMN text_validation_status TEXT NOT NULL DEFAULT 'NOT_PERFORMED'
  CHECK(text_validation_status IN ('PASSED','FAILED','NOT_PERFORMED'));
ALTER TABLE generated_assets ADD COLUMN semantic_qa_status TEXT NOT NULL DEFAULT 'NOT_PERFORMED'
  CHECK(semantic_qa_status IN ('PASSED','FLAGGED','NOT_PERFORMED'));
ALTER TABLE generated_assets ADD COLUMN human_review_status TEXT NOT NULL DEFAULT 'REQUIRED'
  CHECK(human_review_status='REQUIRED');

CREATE TABLE render_provider_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  attempt_number INTEGER,
  event_type TEXT NOT NULL CHECK(event_type IN ('SUBMITTED','POLLED','RATE_LIMITED','COMPLETED','FAILED','DOWNLOADED')),
  provider_status TEXT,
  provider_job_id TEXT,
  safe_metadata_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_render_provider_events_job ON render_provider_events(render_job_id,id);

CREATE TABLE media_qa_results(
  id TEXT PRIMARY KEY,
  render_job_id TEXT NOT NULL REFERENCES render_jobs(id),
  generated_asset_id TEXT REFERENCES generated_assets(id),
  qa_type TEXT NOT NULL CHECK(qa_type IN ('TECHNICAL','TEXT_OVERLAY','SEMANTIC_VISUAL')),
  status TEXT NOT NULL CHECK(status IN ('PASSED','FAILED','FLAGGED','NOT_PERFORMED')),
  provider TEXT,
  model TEXT,
  confidence REAL,
  flags_json TEXT NOT NULL DEFAULT '[]',
  details_json TEXT NOT NULL DEFAULT '{}',
  policy_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(render_job_id,qa_type)
);
CREATE INDEX idx_media_qa_asset ON media_qa_results(generated_asset_id,qa_type);

CREATE TABLE cost_ledger(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  stage TEXT NOT NULL CHECK(stage IN ('VERIFICATION','CONTENT_CEO','CONTENT_PRODUCTION','MEDIA_RENDERING')),
  reference_type TEXT NOT NULL,
  reference_id TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  cost_status TEXT NOT NULL CHECK(cost_status IN ('known','unknown')),
  currency TEXT,
  provider_reported_cost REAL,
  locally_calculated_cost REAL,
  units_json TEXT NOT NULL DEFAULT '{}',
  pricing_version TEXT,
  recorded_at TEXT NOT NULL,
  UNIQUE(stage,reference_type,reference_id)
);
CREATE INDEX idx_cost_ledger_event ON cost_ledger(event_id,recorded_at);

CREATE TRIGGER render_provider_events_immutable_update BEFORE UPDATE ON render_provider_events BEGIN
  SELECT RAISE(ABORT,'render provider events are immutable');
END;
CREATE TRIGGER render_provider_events_immutable_delete BEFORE DELETE ON render_provider_events BEGIN
  SELECT RAISE(ABORT,'render provider events are immutable');
END;
CREATE TRIGGER media_qa_results_immutable_update BEFORE UPDATE ON media_qa_results BEGIN
  SELECT RAISE(ABORT,'media QA results are immutable');
END;
CREATE TRIGGER media_qa_results_immutable_delete BEFORE DELETE ON media_qa_results BEGIN
  SELECT RAISE(ABORT,'media QA results are immutable');
END;
CREATE TRIGGER cost_ledger_immutable_update BEFORE UPDATE ON cost_ledger BEGIN
  SELECT RAISE(ABORT,'cost ledger records are immutable');
END;
CREATE TRIGGER cost_ledger_immutable_delete BEFORE DELETE ON cost_ledger BEGIN
  SELECT RAISE(ABORT,'cost ledger records are immutable');
END;
