-- Architecture 10: fast verified content factory + caption/hashtag intelligence.
-- Additive and forward-only. Captions/hashtags are factual, non-partisan metadata only;
-- no demographic or political-personality profiling.

-- PART A — bounded, claim-directed evidence acquisition (EVIDENCE_ACQUISITION_V1).
CREATE TABLE evidence_acquisition_runs(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  pass_number INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','EXHAUSTED','FAILED')),
  queries_json TEXT NOT NULL DEFAULT '[]',
  urls_discovered INTEGER NOT NULL DEFAULT 0,
  urls_fetched INTEGER NOT NULL DEFAULT 0,
  parallel BOOLEAN NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  duration_ms REAL
);
CREATE INDEX idx_evidence_acq_run ON evidence_acquisition_runs(verification_run_id,pass_number);

-- Claim-source matrix (PART A.4) — the structured support evidence verification uses.
CREATE TABLE claim_source_matrix(
  id TEXT PRIMARY KEY,
  verification_run_id TEXT NOT NULL REFERENCES verification_runs(id),
  claim_id TEXT NOT NULL,
  claim_version_id TEXT,
  source_id TEXT,
  source_url TEXT,
  source_name TEXT,
  source_family TEXT,
  primary_or_independent TEXT NOT NULL CHECK(primary_or_independent IN ('primary','independent')),
  support_type TEXT NOT NULL CHECK(support_type IN (
    'DIRECT_SUPPORT','PARTIAL_SUPPORT','MENTIONS_ONLY','CONTRADICTS','IRRELEVANT')),
  support_span TEXT,
  published_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_claim_source_matrix_run ON claim_source_matrix(verification_run_id,claim_id);

-- PART B — pipeline stage timing (stage_started_at, stage_completed_at, duration_ms, wait_ms, provider_ms).
CREATE TABLE reel_pipeline_stage_timings(
  id TEXT PRIMARY KEY,
  pipeline_run_id TEXT NOT NULL REFERENCES reel_pipeline_runs(id),
  stage TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  stage_started_at TEXT NOT NULL,
  stage_completed_at TEXT,
  duration_ms REAL,
  wait_ms REAL,
  provider_ms REAL,
  parallel_group TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_pipeline_timings_run ON reel_pipeline_stage_timings(pipeline_run_id,stage);

-- PART C — caption/hashtag package (CAPTION_PACKAGE_V1), versioned independently of reel approval.
CREATE TABLE post_packages(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  version_number INTEGER NOT NULL DEFAULT 1,
  platform TEXT NOT NULL CHECK(platform IN ('INSTAGRAM','FACEBOOK')),
  caption_primary TEXT NOT NULL,
  caption_short TEXT,
  headline TEXT,
  hashtags_json TEXT NOT NULL DEFAULT '[]',
  search_keywords_json TEXT NOT NULL DEFAULT '[]',
  topic_tags_json TEXT NOT NULL DEFAULT '[]',
  source_attribution TEXT,
  language_mix TEXT NOT NULL DEFAULT 'BILINGUAL' CHECK(language_mix IN ('TELUGU','ENGLISH','BILINGUAL')),
  qa_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','POST_COPY_APPROVED','SUPERSEDED')),
  copied_label TEXT,
  created_at TEXT NOT NULL,
  edited_by TEXT,
  edited_at TEXT
);
CREATE INDEX idx_post_packages_reel ON post_packages(reel_id,platform,version_number DESC);

-- Copy revision history (PART C.21) — separate from reel approval.
CREATE TABLE post_package_revisions(
  id TEXT PRIMARY KEY,
  post_package_id TEXT NOT NULL REFERENCES post_packages(id),
  version_number INTEGER NOT NULL,
  caption_primary TEXT NOT NULL,
  caption_short TEXT,
  headline TEXT,
  hashtags_json TEXT NOT NULL DEFAULT '[]',
  search_keywords_json TEXT NOT NULL DEFAULT '[]',
  topic_tags_json TEXT NOT NULL DEFAULT '[]',
  edited_by TEXT,
  edited_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_post_revisions_package ON post_package_revisions(post_package_id,version_number DESC);

-- PART D — aggregate content discovery intelligence (CONTENT_DISCOVERY_INTELLIGENCE_V1).
-- Aggregate CONTENT signals only; never viewer demographic/political profiles.
CREATE TABLE content_topic_signals(
  id TEXT PRIMARY KEY,
  term TEXT NOT NULL,
  term_kind TEXT NOT NULL CHECK(term_kind IN ('hashtag','keyword','entity','location','event')),
  language TEXT NOT NULL DEFAULT 'ENGLISH',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  frequency_24h INTEGER NOT NULL DEFAULT 0,
  frequency_7d INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  source_families_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_content_topic_term ON content_topic_signals(term,term_kind,language);

-- Story-specific research output (PART D.23) — content/topic/public-interest clusters only.
CREATE TABLE story_topic_research(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  recurring_keywords_json TEXT NOT NULL DEFAULT '[]',
  relevant_hashtags_json TEXT NOT NULL DEFAULT '[]',
  entity_tags_json TEXT NOT NULL DEFAULT '[]',
  location_tags_json TEXT NOT NULL DEFAULT '[]',
  language_pattern TEXT NOT NULL DEFAULT 'BILINGUAL',
  caption_structure_json TEXT NOT NULL DEFAULT '[]',
  clusters_json TEXT NOT NULL DEFAULT '[]',
  sources_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_story_topic_event ON story_topic_research(event_id,created_at DESC);

-- PART D.24 — schema for future performance feedback (not activated; no microtargeting).
CREATE TABLE post_performance(
  id TEXT PRIMARY KEY,
  post_id TEXT,
  reel_id TEXT,
  platform TEXT,
  caption_version INTEGER,
  hashtags_used_json TEXT NOT NULL DEFAULT '[]',
  views INTEGER, reach INTEGER, shares INTEGER, comments INTEGER, saves INTEGER,
  watch_time_seconds REAL, completion_rate REAL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX idx_post_performance_reel ON post_performance(reel_id,platform);
