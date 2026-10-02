-- Architecture 11: YouTube-only publishing + discoverability intelligence.
-- Additive and forward-only. YouTube upload requires OAuth 2.0 (an API key can never write).
-- Tokens are stored server-side only and are never returned by any API/UI.

-- OAuth connection state (secret values live in .env; this holds only non-secret metadata).
CREATE TABLE youtube_oauth_state(
  id TEXT PRIMARY KEY,
  channel_id TEXT,
  channel_title TEXT,
  scope TEXT,
  token_status TEXT NOT NULL DEFAULT 'UNCONFIGURED'
    CHECK(token_status IN ('CONNECTED','UNCONFIGURED','TOKEN_EXPIRED','ERROR')),
  connected_account TEXT,
  last_authorized_call_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- YouTube content intelligence research snapshots (aggregate content metadata only).
CREATE TABLE youtube_research_snapshots(
  id TEXT PRIMARY KEY,
  event_id TEXT REFERENCES events(id),
  queries_json TEXT NOT NULL DEFAULT '[]',
  sample_count INTEGER NOT NULL DEFAULT 0,
  recurring_terms_json TEXT NOT NULL DEFAULT '[]',
  recurring_hashtags_json TEXT NOT NULL DEFAULT '[]',
  title_structures_json TEXT NOT NULL DEFAULT '[]',
  language_mix TEXT NOT NULL DEFAULT 'BILINGUAL',
  date_range_json TEXT NOT NULL DEFAULT '{}',
  videos_json TEXT NOT NULL DEFAULT '[]',
  framework TEXT NOT NULL DEFAULT 'YOUTUBE_CONTENT_INTELLIGENCE_V1',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_youtube_research_event ON youtube_research_snapshots(event_id,created_at DESC);

-- YouTube title/description/hashtag/tag package, versioned independently of reel approval.
CREATE TABLE youtube_packages(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  version_number INTEGER NOT NULL DEFAULT 1,
  title_primary TEXT NOT NULL,
  title_alt_1 TEXT,
  title_alt_2 TEXT,
  description TEXT NOT NULL,
  hashtags_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  language_mix TEXT NOT NULL DEFAULT 'BILINGUAL' CHECK(language_mix IN ('TELUGU','ENGLISH','BILINGUAL')),
  youtube_format TEXT NOT NULL DEFAULT 'VIDEO' CHECK(youtube_format IN ('VIDEO','SHORT')),
  claim_map_json TEXT NOT NULL DEFAULT '[]',
  qa_json TEXT NOT NULL DEFAULT '{}',
  research_snapshot_id TEXT REFERENCES youtube_research_snapshots(id),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','YOUTUBE_COPY_APPROVED','SUPERSEDED')),
  created_at TEXT NOT NULL,
  edited_by TEXT,
  edited_at TEXT
);
CREATE INDEX idx_youtube_packages_reel ON youtube_packages(reel_id,version_number DESC);

CREATE TABLE youtube_package_revisions(
  id TEXT PRIMARY KEY,
  youtube_package_id TEXT NOT NULL REFERENCES youtube_packages(id),
  version_number INTEGER NOT NULL,
  title_primary TEXT NOT NULL,
  description TEXT NOT NULL,
  hashtags_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  edited_by TEXT,
  edited_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_youtube_revisions_package ON youtube_package_revisions(youtube_package_id,version_number DESC);

-- Durable YouTube publish jobs (idempotent; private by default).
CREATE TABLE youtube_publish_jobs(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  youtube_package_id TEXT NOT NULL REFERENCES youtube_packages(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  reel_version TEXT NOT NULL,
  youtube_copy_version INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL CHECK(mode IN ('NOW','SCHEDULED')),
  status TEXT NOT NULL CHECK(status IN (
    'READY','UPLOADING','PROCESSING','SCHEDULED','PUBLISHED','FAILED','CANCELLED','NEEDS_INTERVENTION')),
  privacy_status TEXT NOT NULL DEFAULT 'PRIVATE' CHECK(privacy_status IN ('PRIVATE','UNLISTED','PUBLIC')),
  scheduled_at TEXT,
  timezone TEXT,
  video_id TEXT,
  permalink TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  last_error_message TEXT,
  project_audit_status TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK(project_audit_status IN ('UNKNOWN','PRIVATE_ONLY','PUBLIC_VERIFIED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  uploaded_at TEXT
);
CREATE UNIQUE INDEX idx_youtube_jobs_idem ON youtube_publish_jobs(idempotency_key);
CREATE INDEX idx_youtube_jobs_status ON youtube_publish_jobs(status,scheduled_at);

CREATE TABLE youtube_publish_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  youtube_publish_job_id TEXT NOT NULL REFERENCES youtube_publish_jobs(id),
  event_type TEXT NOT NULL,
  status TEXT,
  safe_metadata_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_youtube_publish_events_job ON youtube_publish_events(youtube_publish_job_id,id);

-- Append-only performance snapshots (UNKNOWN stays UNKNOWN).
CREATE TABLE youtube_performance_snapshots(
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL,
  reel_id TEXT REFERENCES final_reel_assets(id),
  youtube_publish_job_id TEXT REFERENCES youtube_publish_jobs(id),
  checkpoint TEXT NOT NULL CHECK(checkpoint IN ('1h','6h','24h','3d','7d')),
  views INTEGER, likes INTEGER, comments INTEGER,
  watch_time_seconds REAL, average_view_duration_seconds REAL,
  average_percentage_viewed REAL, subscribers_gained INTEGER, shares INTEGER,
  source TEXT NOT NULL DEFAULT 'UNKNOWN',
  recorded_at TEXT NOT NULL
);
CREATE INDEX idx_youtube_perf_video ON youtube_performance_snapshots(video_id,recorded_at);
