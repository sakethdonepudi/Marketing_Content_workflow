ALTER TABLE events ADD COLUMN production_status TEXT NOT NULL DEFAULT 'NO_JOB'
  CHECK(production_status IN ('NO_JOB','QUEUED','GENERATING','VALIDATING','READY_FOR_APPROVAL','HUMAN_REVIEW','BLOCKED','FAILED'));

ALTER TABLE claim_versions ADD COLUMN revoked_at TEXT;
ALTER TABLE claim_versions ADD COLUMN revocation_reason TEXT;
ALTER TABLE verification_snapshots ADD COLUMN invalidated_at TEXT;
ALTER TABLE verification_snapshots ADD COLUMN invalidation_reason TEXT;
ALTER TABLE media_assets ADD COLUMN rights_reviewed_at TEXT;

CREATE TABLE production_jobs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  content_decision_id TEXT NOT NULL REFERENCES content_decisions(id),
  approved_claim_set_id TEXT NOT NULL REFERENCES approved_claim_sets(id),
  approved_claim_set_version INTEGER NOT NULL,
  evidence_version TEXT NOT NULL,
  media_version TEXT NOT NULL,
  publishing_history_version TEXT NOT NULL,
  input_version TEXT NOT NULL,
  production_policy_version TEXT NOT NULL,
  prompt_schema_version TEXT NOT NULL,
  requested_format TEXT NOT NULL CHECK(requested_format IN ('REEL','STORY','CAROUSEL','IMAGE')),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_mode TEXT NOT NULL CHECK(provider_mode IN ('live','fixture')),
  status TEXT NOT NULL CHECK(status IN ('QUEUED','GENERATING','VALIDATING','READY_FOR_APPROVAL','HUMAN_REVIEW','BLOCKED','FAILED')),
  regeneration_number INTEGER NOT NULL DEFAULT 1 CHECK(regeneration_number >= 1),
  idempotency_key TEXT NOT NULL,
  fixture_only INTEGER NOT NULL DEFAULT 0 CHECK(fixture_only IN (0,1)),
  requested_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  provider_called INTEGER NOT NULL DEFAULT 0 CHECK(provider_called IN (0,1)),
  provider_request_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  total_tokens INTEGER,
  latency_ms INTEGER,
  cost_usd REAL,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown')),
  cost_policy_version TEXT,
  validation_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(validation_status IN ('PENDING','PASSED','FAILED')),
  validation_result_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(content_decision_id,regeneration_number),
  UNIQUE(content_decision_id,idempotency_key)
);

CREATE UNIQUE INDEX idx_production_one_active_decision
  ON production_jobs(content_decision_id)
  WHERE status IN ('QUEUED','GENERATING','VALIDATING');
CREATE INDEX idx_production_jobs_event ON production_jobs(event_id,requested_at DESC);

CREATE TABLE production_job_claims(
  job_id TEXT NOT NULL REFERENCES production_jobs(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  claim_id TEXT NOT NULL,
  version_number INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  assertion_scope TEXT NOT NULL,
  attribution TEXT,
  required_for_event INTEGER NOT NULL CHECK(required_for_event IN (0,1)),
  PRIMARY KEY(job_id,claim_version_id)
);

CREATE TABLE production_job_evidence(
  job_id TEXT NOT NULL REFERENCES production_jobs(id),
  snapshot_id TEXT NOT NULL REFERENCES verification_snapshots(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  canonical_url TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  evidence_family_id TEXT NOT NULL,
  PRIMARY KEY(job_id,snapshot_id,claim_version_id)
);

CREATE TABLE production_job_media(
  job_id TEXT NOT NULL REFERENCES production_jobs(id),
  media_asset_id TEXT NOT NULL REFERENCES media_assets(id),
  media_type TEXT NOT NULL,
  url TEXT NOT NULL,
  rights_status TEXT NOT NULL,
  availability_status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(job_id,media_asset_id)
);

CREATE TABLE production_job_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES production_jobs(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  changed_at TEXT NOT NULL
);

CREATE TABLE production_drafts(
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL UNIQUE REFERENCES production_jobs(id),
  structured_output_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE content_packages(
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL UNIQUE REFERENCES production_jobs(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  version_number INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status='READY_FOR_APPROVAL'),
  requested_format TEXT NOT NULL,
  story_angle TEXT NOT NULL,
  content_objective TEXT NOT NULL,
  package_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  approved_claim_set_id TEXT NOT NULL REFERENCES approved_claim_sets(id),
  approved_claim_set_version INTEGER NOT NULL,
  approved_claim_version_ids_json TEXT NOT NULL,
  evidence_snapshot_ids_json TEXT NOT NULL,
  evidence_version TEXT NOT NULL,
  media_version TEXT NOT NULL,
  production_policy_version TEXT NOT NULL,
  prompt_schema_version TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_mode TEXT NOT NULL,
  fixture_only INTEGER NOT NULL CHECK(fixture_only IN (0,1)),
  provider_request_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  total_tokens INTEGER,
  latency_ms INTEGER,
  cost_usd REAL,
  cost_status TEXT NOT NULL CHECK(cost_status IN ('known','unknown')),
  validation_result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(event_id,version_number)
);

CREATE INDEX idx_content_packages_event ON content_packages(event_id,version_number DESC);

CREATE TRIGGER production_jobs_identity_immutable
BEFORE UPDATE OF event_id,content_decision_id,approved_claim_set_id,approved_claim_set_version,
  evidence_version,media_version,publishing_history_version,input_version,production_policy_version,
  prompt_schema_version,requested_format,provider,model,provider_mode,regeneration_number,idempotency_key,
  fixture_only,requested_at,created_at ON production_jobs BEGIN
  SELECT RAISE(ABORT,'production job input snapshot is immutable');
END;

CREATE TRIGGER production_job_claims_immutable_update BEFORE UPDATE ON production_job_claims BEGIN
  SELECT RAISE(ABORT,'production job claims are immutable');
END;
CREATE TRIGGER production_job_claims_immutable_delete BEFORE DELETE ON production_job_claims BEGIN
  SELECT RAISE(ABORT,'production job claims are immutable');
END;
CREATE TRIGGER production_job_evidence_immutable_update BEFORE UPDATE ON production_job_evidence BEGIN
  SELECT RAISE(ABORT,'production job evidence is immutable');
END;
CREATE TRIGGER production_job_evidence_immutable_delete BEFORE DELETE ON production_job_evidence BEGIN
  SELECT RAISE(ABORT,'production job evidence is immutable');
END;
CREATE TRIGGER production_job_media_immutable_update BEFORE UPDATE ON production_job_media BEGIN
  SELECT RAISE(ABORT,'production job media is immutable');
END;
CREATE TRIGGER production_job_media_immutable_delete BEFORE DELETE ON production_job_media BEGIN
  SELECT RAISE(ABORT,'production job media is immutable');
END;
CREATE TRIGGER production_job_history_immutable_update BEFORE UPDATE ON production_job_status_history BEGIN
  SELECT RAISE(ABORT,'production job history is immutable');
END;
CREATE TRIGGER production_job_history_immutable_delete BEFORE DELETE ON production_job_status_history BEGIN
  SELECT RAISE(ABORT,'production job history is immutable');
END;
CREATE TRIGGER production_drafts_immutable_update BEFORE UPDATE ON production_drafts BEGIN
  SELECT RAISE(ABORT,'production drafts are immutable');
END;
CREATE TRIGGER production_drafts_immutable_delete BEFORE DELETE ON production_drafts BEGIN
  SELECT RAISE(ABORT,'production drafts are immutable');
END;
CREATE TRIGGER content_packages_immutable_update BEFORE UPDATE ON content_packages BEGIN
  SELECT RAISE(ABORT,'content packages are immutable');
END;
CREATE TRIGGER content_packages_immutable_delete BEFORE DELETE ON content_packages BEGIN
  SELECT RAISE(ABORT,'content packages are immutable');
END;
