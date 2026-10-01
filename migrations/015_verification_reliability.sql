-- Architecture 06F-R: resumable, checkpointed verification without changing
-- any factual evidence or claim-adjudication rule.

ALTER TABLE verification_runs ADD COLUMN current_phase TEXT
  CHECK(current_phase IS NULL OR current_phase IN (
    'PRIMARY_EVIDENCE_EXTRACTION','CORROBORATION_DISCOVERY','CORROBORATING_SOURCE_RETRIEVAL',
    'CLAIM_SOURCE_MATCHING','CONTRADICTION_ANALYSIS','FINAL_CLAIM_ADJUDICATION'
  ));
ALTER TABLE verification_runs ADD COLUMN last_completed_phase TEXT
  CHECK(last_completed_phase IS NULL OR last_completed_phase IN (
    'PRIMARY_EVIDENCE_EXTRACTION','CORROBORATION_DISCOVERY','CORROBORATING_SOURCE_RETRIEVAL',
    'CLAIM_SOURCE_MATCHING','CONTRADICTION_ANALYSIS','FINAL_CLAIM_ADJUDICATION'
  ));
ALTER TABLE verification_runs ADD COLUMN resume_state TEXT
  CHECK(resume_state IS NULL OR resume_state='PAUSED_TRANSIENT');
ALTER TABLE verification_runs ADD COLUMN recoverable INTEGER NOT NULL DEFAULT 0 CHECK(recoverable IN (0,1));
ALTER TABLE verification_runs ADD COLUMN resume_reason TEXT;
ALTER TABLE verification_runs ADD COLUMN last_checkpoint_at TEXT;
ALTER TABLE verification_runs ADD COLUMN resumed_at TEXT;
ALTER TABLE verification_runs ADD COLUMN search_timeout_seconds INTEGER;
ALTER TABLE verification_runs ADD COLUMN retrieval_timeout_seconds INTEGER;
ALTER TABLE verification_runs ADD COLUMN total_timeout_seconds INTEGER;

CREATE TABLE verification_attempts(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  phase TEXT NOT NULL CHECK(phase IN ('CORROBORATION_DISCOVERY','CORROBORATING_SOURCE_RETRIEVAL')),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED')),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  elapsed_seconds REAL,
  provider_request_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  actual_search_calls INTEGER,
  actual_open_calls INTEGER,
  actual_sources_returned INTEGER,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown','not_billed')),
  cost_usd REAL,
  cost_usd_ticks INTEGER,
  failure_category TEXT CHECK(failure_category IS NULL OR failure_category IN (
    'TRANSIENT_PROVIDER_TIMEOUT','TRANSIENT_PROVIDER_FAILURE','PROVIDER_FAILURE','AUTH_FAILURE',
    'INVALID_PROVIDER_OUTPUT','RETRIEVAL_TIMEOUT'
  )),
  failure_code TEXT,
  failure_message TEXT,
  retry_of_attempt_id TEXT REFERENCES verification_attempts(id),
  retry_after_seconds REAL,
  UNIQUE(verification_run_id,attempt_number)
);
CREATE INDEX idx_verification_attempts_run ON verification_attempts(verification_run_id,attempt_number);

-- Pre-06F-R runs only stored one aggregate attempt. Every current real row has
-- attempt_count=1, so its exact aggregate metadata can be backfilled without
-- inventing additional requests or zero billing.
INSERT INTO verification_attempts(
  id,verification_run_id,attempt_number,phase,provider,model,status,started_at,ended_at,elapsed_seconds,
  provider_request_id,input_tokens,output_tokens,total_tokens,actual_search_calls,actual_open_calls,
  actual_sources_returned,cost_status,cost_usd,cost_usd_ticks,failure_category,failure_code,failure_message
)
SELECT
  'VA-LEGACY-' || substr(replace(id,'VR-',''),1,12),id,1,'CORROBORATION_DISCOVERY',provider,model,
  CASE WHEN status IN ('COMPLETED','CACHED') THEN 'COMPLETED' ELSE 'FAILED' END,
  COALESCE(started_at,requested_at),completed_at,provider_elapsed_seconds,provider_request_id,
  input_tokens,output_tokens,total_tokens,actual_search_calls,actual_open_calls,actual_sources_returned,
  cost_status,cost_usd,cost_usd_ticks,
  CASE
    WHEN error_code IN ('connection_timeout','response_timeout') THEN 'TRANSIENT_PROVIDER_TIMEOUT'
    WHEN error_code='network_error' THEN 'TRANSIENT_PROVIDER_FAILURE'
    WHEN error_code IN ('missing_api_key','http_401','http_403') THEN 'AUTH_FAILURE'
    WHEN error_code='invalid_provider_response' THEN 'INVALID_PROVIDER_OUTPUT'
    WHEN status='FAILED' THEN 'PROVIDER_FAILURE'
    ELSE NULL
  END,
  error_code,error_message
FROM verification_runs
WHERE attempt_count=1;

CREATE TABLE verification_decision_revisions(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  superseded_decision_id TEXT NOT NULL REFERENCES verification_decisions(id),
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_verification_decision_revisions_run
  ON verification_decision_revisions(verification_run_id,claim_version_id,created_at);

CREATE TABLE verification_checkpoints(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  phase TEXT NOT NULL CHECK(phase IN (
    'PRIMARY_EVIDENCE_EXTRACTION','CORROBORATION_DISCOVERY','CORROBORATING_SOURCE_RETRIEVAL',
    'CLAIM_SOURCE_MATCHING','CONTRADICTION_ANALYSIS','FINAL_CLAIM_ADJUDICATION'
  )),
  status TEXT NOT NULL CHECK(status IN ('COMPLETED','PARTIAL')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_verification_checkpoints_run ON verification_checkpoints(verification_run_id,created_at);
CREATE UNIQUE INDEX idx_verification_checkpoint_completed
  ON verification_checkpoints(verification_run_id,phase) WHERE status='COMPLETED';

CREATE TABLE verification_source_family_assessments(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  left_snapshot_id TEXT NOT NULL REFERENCES verification_snapshots(id),
  right_snapshot_id TEXT NOT NULL REFERENCES verification_snapshots(id),
  relationship TEXT NOT NULL CHECK(relationship IN ('SAME_FAMILY','INDEPENDENT_FAMILY')),
  reason TEXT NOT NULL,
  text_similarity REAL NOT NULL,
  left_host TEXT NOT NULL,
  right_host TEXT NOT NULL,
  assessed_at TEXT NOT NULL,
  UNIQUE(verification_run_id,left_snapshot_id,right_snapshot_id)
);
CREATE INDEX idx_verification_family_run ON verification_source_family_assessments(verification_run_id,relationship);

-- Existing timeout runs become explicitly resumable, but remain failed and
-- REVIEW_REQUIRED until a future, explicitly requested resume completes.
UPDATE verification_runs
SET current_phase='CORROBORATION_DISCOVERY',
    resume_state='PAUSED_TRANSIENT',
    recoverable=1,
    resume_reason=error_message
WHERE status='FAILED' AND error_code IN ('connection_timeout','response_timeout','network_error');
