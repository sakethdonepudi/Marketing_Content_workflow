ALTER TABLE events ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'NOT_VERIFIED'
  CHECK(verification_status IN ('NOT_VERIFIED','QUEUED','RUNNING','REVIEW_REQUIRED','VERIFIED','FAILED','TEST_ONLY'));

UPDATE research_runs
SET cost_usd = cost_usd_ticks / 10000000000.0,
    cost_status = 'known'
WHERE cost_usd_ticks IS NOT NULL AND cost_usd IS NULL;

CREATE TABLE verification_runs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  research_run_id TEXT NOT NULL REFERENCES research_runs(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('live','test')),
  status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CACHED')),
  initial_evidence_version TEXT NOT NULL,
  final_evidence_version TEXT,
  claim_set_version TEXT NOT NULL,
  cache_source_run_id TEXT REFERENCES verification_runs(id),
  requested_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  progress_message TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  search_turn_limit INTEGER NOT NULL,
  token_limit INTEGER NOT NULL,
  actual_search_calls INTEGER,
  actual_open_calls INTEGER,
  actual_sources_returned INTEGER,
  limit_guaranteed INTEGER NOT NULL DEFAULT 0 CHECK(limit_guaranteed IN (0,1)),
  limit_notes TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  cost_usd REAL,
  cost_usd_ticks INTEGER,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown')),
  provider_request_id TEXT,
  provider_elapsed_seconds REAL,
  decision_explanation TEXT,
  summary_json TEXT,
  error_code TEXT,
  error_message TEXT
);

CREATE UNIQUE INDEX idx_verification_one_active
  ON verification_runs(research_run_id) WHERE status IN ('QUEUED','RUNNING');
CREATE INDEX idx_verification_event ON verification_runs(event_id,requested_at DESC);
CREATE INDEX idx_verification_cache ON verification_runs(research_run_id,provider,model,initial_evidence_version,claim_set_version,status);

CREATE TABLE verification_run_status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES verification_runs(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  message TEXT NOT NULL,
  changed_at TEXT NOT NULL
);

CREATE TABLE verification_leads(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  url TEXT NOT NULL,
  canonical_url TEXT,
  title TEXT,
  snippet TEXT,
  target_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  source_priority TEXT NOT NULL CHECK(source_priority IN ('official_primary','independent_reporting','unknown')),
  status TEXT NOT NULL CHECK(status IN ('DISCOVERED','INGESTED','REVIEW','REJECTED')),
  status_reason TEXT NOT NULL,
  signal_id TEXT REFERENCES signals(id),
  discovered_at TEXT NOT NULL,
  inspected_at TEXT,
  UNIQUE(verification_run_id,url)
);

CREATE TABLE claim_versions(
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(id),
  version_number INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  assertion_scope TEXT NOT NULL,
  attribution TEXT,
  required_for_event INTEGER NOT NULL CHECK(required_for_event IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(claim_id,content_hash),
  UNIQUE(claim_id,version_number)
);

CREATE TABLE verification_run_claims(
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  required_for_event INTEGER NOT NULL CHECK(required_for_event IN (0,1)),
  PRIMARY KEY(verification_run_id,claim_version_id)
);

CREATE TABLE verification_snapshots(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  signal_id TEXT REFERENCES signals(id),
  origin_kind TEXT NOT NULL CHECK(origin_kind IN ('research','corroboration')),
  origin_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_class TEXT NOT NULL CHECK(source_class IN ('official_primary','independent_reporting')),
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  text_family_hash TEXT NOT NULL,
  evidence_family_id TEXT NOT NULL,
  publication_time TEXT,
  stated_event_time TEXT,
  author TEXT,
  retrieved_at TEXT NOT NULL,
  UNIQUE(verification_run_id,canonical_url)
);

CREATE INDEX idx_verification_snapshots_run ON verification_snapshots(verification_run_id);
CREATE INDEX idx_verification_snapshots_family ON verification_snapshots(evidence_family_id);

CREATE TABLE verification_decisions(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  decision TEXT NOT NULL CHECK(decision IN ('SUPPORTED','CONFLICTED','INSUFFICIENT_EVIDENCE','EXCLUDED')),
  approved INTEGER NOT NULL CHECK(approved IN (0,1)),
  required_for_event INTEGER NOT NULL CHECK(required_for_event IN (0,1)),
  independent_family_count INTEGER NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL,
  missing_information_json TEXT NOT NULL DEFAULT '[]',
  decided_at TEXT NOT NULL,
  UNIQUE(verification_run_id,claim_version_id)
);

CREATE TABLE verification_decision_evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id TEXT NOT NULL REFERENCES verification_decisions(id),
  snapshot_id TEXT NOT NULL REFERENCES verification_snapshots(id),
  relationship TEXT NOT NULL CHECK(relationship IN ('supports','conflicts','mentions_only')),
  excerpt TEXT,
  excerpt_valid INTEGER NOT NULL CHECK(excerpt_valid IN (0,1)),
  directness TEXT NOT NULL CHECK(directness IN ('direct_primary','independent_report','syndicated_report')),
  evidence_family_id TEXT NOT NULL,
  rationale TEXT NOT NULL,
  UNIQUE(decision_id,snapshot_id,relationship)
);

CREATE TABLE approved_claim_sets(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  version_number INTEGER NOT NULL,
  evidence_version TEXT NOT NULL,
  claim_set_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('APPROVED','REVIEW_REQUIRED','TEST_ONLY')),
  created_at TEXT NOT NULL,
  UNIQUE(event_id,version_number)
);

CREATE TABLE approved_claim_set_items(
  claim_set_id TEXT NOT NULL REFERENCES approved_claim_sets(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  verification_decision_id TEXT NOT NULL REFERENCES verification_decisions(id),
  PRIMARY KEY(claim_set_id,claim_version_id)
);

