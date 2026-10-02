-- Architecture 06F-S: additive source acquisition. Candidate records are not
-- verification decisions and cannot directly approve a claim or event.

CREATE TABLE official_source_authorities(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL UNIQUE,
  authority_type TEXT NOT NULL,
  priority INTEGER NOT NULL CHECK(priority >= 1),
  document_types_json TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  registry_version TEXT NOT NULL,
  synced_at TEXT NOT NULL
);

CREATE TABLE source_acquisition_runs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  verification_run_id TEXT REFERENCES verification_runs(id),
  trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('PLANNED_DISCOVERY','MANUAL_URL','VERIFICATION_PREP')),
  status TEXT NOT NULL CHECK(status IN ('PLANNED','RUNNING','COMPLETED','PARTIAL','FAILED')),
  provider TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  search_provider_calls INTEGER,
  direct_http_retrievals INTEGER NOT NULL DEFAULT 0,
  llm_adjudication_calls INTEGER NOT NULL DEFAULT 0,
  search_cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(search_cost_status IN ('known','unknown','not_billed')),
  search_cost_usd REAL,
  retrieval_cost_status TEXT NOT NULL DEFAULT 'not_billed' CHECK(retrieval_cost_status IN ('known','unknown','not_billed')),
  retrieval_cost_usd REAL,
  llm_cost_status TEXT NOT NULL DEFAULT 'not_billed' CHECK(llm_cost_status IN ('known','unknown','not_billed')),
  llm_cost_usd REAL,
  error_message TEXT
);
CREATE INDEX idx_source_acquisition_event ON source_acquisition_runs(event_id,started_at DESC);
CREATE INDEX idx_source_acquisition_verification ON source_acquisition_runs(verification_run_id,started_at DESC);

CREATE TABLE source_discovery_attempts(
  id TEXT PRIMARY KEY,
  acquisition_run_id TEXT NOT NULL REFERENCES source_acquisition_runs(id),
  strategy TEXT NOT NULL CHECK(strategy IN (
    'A_AUTHORITATIVE_DOMAIN','B_EXACT_PHRASE','C_TITLE_NOTIFICATION_FRAGMENT',
    'D_ENTITY_DATE_RANGE','E_SECONDARY_CORROBORATION','F_DIRECT_DOCUMENT_LINK','MANUAL_URL'
  )),
  query_text TEXT NOT NULL,
  domains_json TEXT NOT NULL DEFAULT '[]',
  target_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PLANNED','COMPLETED','FAILED')),
  provider_request_id TEXT,
  result_count INTEGER,
  cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_status IN ('known','unknown','not_billed')),
  cost_usd REAL,
  attempted_at TEXT NOT NULL,
  completed_at TEXT,
  error_message TEXT
);
CREATE INDEX idx_source_discovery_run ON source_discovery_attempts(acquisition_run_id,strategy);

CREATE TABLE source_candidates(
  id TEXT PRIMARY KEY,
  acquisition_run_id TEXT NOT NULL REFERENCES source_acquisition_runs(id),
  discovery_attempt_id TEXT REFERENCES source_discovery_attempts(id),
  original_url TEXT NOT NULL,
  final_url TEXT,
  canonical_url TEXT,
  http_status INTEGER,
  content_type TEXT,
  title TEXT,
  publication_date TEXT,
  publisher TEXT,
  retrieved_at TEXT,
  checksum_sha256 TEXT,
  extracted_text TEXT,
  document_type TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  source_class TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(source_class IN (
    'OFFICIAL_PRIMARY','INDEPENDENT_REPORTING','SYNDICATED_REPORTING','AGGREGATOR',
    'PRESS_RELEASE_REPRINT','UNKNOWN'
  )),
  classification_reason TEXT NOT NULL,
  authority_id TEXT REFERENCES official_source_authorities(id),
  evidence_family_id TEXT,
  family_reason TEXT,
  state TEXT NOT NULL CHECK(state IN ('DISCOVERED','RETRIEVED','UNAVAILABLE','REJECTED')),
  state_reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(acquisition_run_id,original_url)
);
CREATE INDEX idx_source_candidates_run ON source_candidates(acquisition_run_id,state);
CREATE INDEX idx_source_candidates_family ON source_candidates(evidence_family_id);

CREATE TABLE source_candidate_pages(
  candidate_id TEXT NOT NULL REFERENCES source_candidates(id),
  page_number INTEGER NOT NULL CHECK(page_number >= 1),
  text TEXT NOT NULL,
  text_checksum_sha256 TEXT NOT NULL,
  PRIMARY KEY(candidate_id,page_number)
);

CREATE TABLE source_candidate_family_assessments(
  id TEXT PRIMARY KEY,
  acquisition_run_id TEXT NOT NULL REFERENCES source_acquisition_runs(id),
  candidate_id TEXT NOT NULL REFERENCES source_candidates(id),
  compared_candidate_id TEXT REFERENCES source_candidates(id),
  relationship TEXT NOT NULL CHECK(relationship IN ('NEW_FAMILY','SAME_FAMILY','INDEPENDENT_FAMILY')),
  reason TEXT NOT NULL,
  text_similarity REAL NOT NULL,
  assessed_at TEXT NOT NULL
);

CREATE TABLE claim_source_candidates(
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  candidate_id TEXT NOT NULL REFERENCES source_candidates(id),
  relationship TEXT NOT NULL CHECK(relationship IN ('CANDIDATE','TEXT_ABSENT','CONTRADICTS')),
  match_score REAL NOT NULL,
  matched_passage TEXT,
  page_number INTEGER,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(claim_version_id,candidate_id),
  FOREIGN KEY(candidate_id,page_number) REFERENCES source_candidate_pages(candidate_id,page_number)
);
CREATE INDEX idx_claim_source_candidates_claim ON claim_source_candidates(claim_version_id,relationship);

CREATE TABLE acquisition_evidence_packets(
  id TEXT PRIMARY KEY,
  acquisition_run_id TEXT NOT NULL REFERENCES source_acquisition_runs(id),
  claim_version_id TEXT NOT NULL REFERENCES claim_versions(id),
  packet_json TEXT NOT NULL,
  packet_hash TEXT NOT NULL,
  official_primary_found INTEGER NOT NULL CHECK(official_primary_found IN (0,1)),
  independent_family_count INTEGER NOT NULL DEFAULT 0,
  deterministically_sufficient INTEGER NOT NULL CHECK(deterministically_sufficient IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(acquisition_run_id,claim_version_id)
);
CREATE INDEX idx_acquisition_packets_claim ON acquisition_evidence_packets(claim_version_id,created_at DESC);
