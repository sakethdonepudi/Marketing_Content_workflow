ALTER TABLE events ADD COLUMN content_decision_status TEXT NOT NULL DEFAULT 'NO_DECISION'
  CHECK(content_decision_status IN ('NO_DECISION','QUEUED','RUNNING','CREATE','HOLD','MONITOR','SKIP','HUMAN_REVIEW','FAILED'));

CREATE TABLE media_assets(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  mode TEXT NOT NULL CHECK(mode IN ('live','test')),
  media_type TEXT NOT NULL CHECK(media_type IN ('image','video','audio','document')),
  url TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_url TEXT,
  rights_status TEXT NOT NULL CHECK(rights_status IN ('verified','restricted','unknown')),
  availability_status TEXT NOT NULL CHECK(availability_status IN ('available','missing','expired','rejected')),
  content_hash TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(event_id,mode,url)
);

CREATE INDEX idx_media_assets_event ON media_assets(event_id,mode,availability_status);

CREATE TABLE publishing_history(
  id TEXT PRIMARY KEY,
  event_id TEXT REFERENCES events(id),
  mode TEXT NOT NULL CHECK(mode IN ('live','test')),
  claim_set_id TEXT REFERENCES approved_claim_sets(id),
  format TEXT NOT NULL CHECK(format IN ('REEL','STORY','CAROUSEL','IMAGE')),
  language TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PLANNED','PUBLISHED','CANCELLED')),
  content_fingerprint TEXT NOT NULL,
  title TEXT,
  published_at TEXT,
  recorded_at TEXT NOT NULL
);

CREATE INDEX idx_publishing_history_recent ON publishing_history(mode,recorded_at DESC);
CREATE INDEX idx_publishing_history_event ON publishing_history(event_id,mode,recorded_at DESC);

CREATE TABLE content_decision_runs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  claim_set_id TEXT REFERENCES approved_claim_sets(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('live','test')),
  status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CACHED')),
  eligibility_status TEXT NOT NULL CHECK(eligibility_status IN ('PRODUCTION_APPROVED','TEST_ONLY','BLOCKED')),
  input_version TEXT NOT NULL,
  cache_source_run_id TEXT REFERENCES content_decision_runs(id),
  requested_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  progress_message TEXT NOT NULL,
  provider_called INTEGER NOT NULL DEFAULT 0 CHECK(provider_called IN (0,1)),
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  cost_usd REAL,
  cost_usd_ticks INTEGER,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown')),
  provider_request_id TEXT,
  provider_elapsed_seconds REAL,
  error_code TEXT,
  error_message TEXT
);

CREATE UNIQUE INDEX idx_content_decision_one_active
  ON content_decision_runs(event_id,mode) WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX idx_content_decision_cache
  ON content_decision_runs(event_id,provider,model,mode,input_version,status);

CREATE TABLE content_decision_run_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES content_decision_runs(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  message TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE TABLE content_decisions(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE REFERENCES content_decision_runs(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  decision TEXT NOT NULL CHECK(decision IN ('CREATE','HOLD','MONITOR','SKIP','HUMAN_REVIEW')),
  recommended_format TEXT NOT NULL CHECK(recommended_format IN ('REEL','STORY','CAROUSEL','IMAGE')),
  language TEXT NOT NULL,
  proposed_duration_seconds INTEGER NOT NULL CHECK(proposed_duration_seconds BETWEEN 1 AND 300),
  priority TEXT NOT NULL CHECK(priority IN ('BREAKING','HIGH','NORMAL','LOW')),
  factual_rationale TEXT NOT NULL,
  approved_claim_set_id TEXT REFERENCES approved_claim_sets(id),
  approved_claim_set_version INTEGER,
  evidence_version TEXT,
  missing_evidence_or_media_json TEXT NOT NULL DEFAULT '[]',
  executable INTEGER NOT NULL CHECK(executable IN (0,1)),
  test_only INTEGER NOT NULL CHECK(test_only IN (0,1)),
  decided_at TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  input_version TEXT NOT NULL
);

CREATE INDEX idx_content_decisions_event ON content_decisions(event_id,decided_at DESC);

CREATE TRIGGER content_decisions_immutable_update
BEFORE UPDATE ON content_decisions BEGIN
  SELECT RAISE(ABORT,'content decisions are immutable');
END;

CREATE TRIGGER content_decisions_immutable_delete
BEFORE DELETE ON content_decisions BEGIN
  SELECT RAISE(ABORT,'content decisions are immutable');
END;
