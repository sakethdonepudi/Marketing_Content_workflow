-- Architecture 07: Meta (Instagram Reels / Facebook Reels) distribution.
-- Additive and forward-only. Platform packages are immutable versions bound to one
-- approved media asset; approvals, publish jobs, and provider events are append-only
-- except for publish-job lifecycle fields.

CREATE TABLE distribution_packages(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  platform TEXT NOT NULL CHECK(platform IN ('INSTAGRAM_REELS','FACEBOOK_REELS')),
  version_number INTEGER NOT NULL CHECK(version_number >= 1),
  generated_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  asset_version INTEGER NOT NULL,
  asset_checksum_sha256 TEXT NOT NULL,
  media_review_id TEXT NOT NULL REFERENCES media_reviews(id),
  content_package_id TEXT NOT NULL REFERENCES content_packages(id),
  content_package_version INTEGER NOT NULL,
  content_package_hash TEXT NOT NULL,
  approved_claim_set_id TEXT NOT NULL REFERENCES approved_claim_sets(id),
  approved_claim_set_version INTEGER NOT NULL,
  caption TEXT NOT NULL,
  title TEXT,
  hashtags_json TEXT NOT NULL DEFAULT '[]',
  accessibility_text TEXT,
  cover_json TEXT NOT NULL DEFAULT '{}',
  platform_metadata_json TEXT NOT NULL DEFAULT '{}',
  copy_provenance_json TEXT NOT NULL DEFAULT '{}',
  copy_validation_json TEXT NOT NULL DEFAULT '{}',
  compliance_json TEXT NOT NULL DEFAULT '{}',
  compliant INTEGER NOT NULL CHECK(compliant IN (0,1)),
  copy_policy_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(platform,generated_asset_id,version_number)
);
CREATE INDEX idx_distribution_packages_event ON distribution_packages(event_id,created_at DESC);

CREATE TABLE distribution_reviews(
  id TEXT PRIMARY KEY,
  distribution_package_id TEXT NOT NULL REFERENCES distribution_packages(id),
  action TEXT NOT NULL CHECK(action IN ('APPROVED','CHANGES_REQUIRED','REJECTED')),
  reviewer TEXT NOT NULL,
  comment TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_distribution_reviews_package ON distribution_reviews(distribution_package_id,created_at DESC);

CREATE TABLE publish_jobs(
  id TEXT PRIMARY KEY,
  distribution_package_id TEXT NOT NULL REFERENCES distribution_packages(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  platform TEXT NOT NULL CHECK(platform IN ('INSTAGRAM_REELS','FACEBOOK_REELS')),
  generated_asset_id TEXT NOT NULL REFERENCES generated_assets(id),
  asset_checksum_sha256 TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('NOW','SCHEDULED')),
  status TEXT NOT NULL CHECK(status IN (
    'SCHEDULED','QUEUED','UPLOADING','PROCESSING','PUBLISHING','PUBLISHED',
    'FAILED','CANCELLED','BLOCKED','NEEDS_INTERVENTION'
  )),
  scheduled_for TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  client_request_id TEXT,
  requested_by TEXT,
  api_version TEXT,
  provider_container_id TEXT,
  provider_post_id TEXT,
  permalink TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1),
  last_error_code TEXT,
  last_error_message TEXT,
  gate_snapshot_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  published_at TEXT,
  cancelled_at TEXT,
  cancel_reason TEXT
);
-- Duplicate-post protection: one in-flight job per platform+asset, one successful post per platform+asset.
CREATE UNIQUE INDEX idx_publish_one_active_per_asset ON publish_jobs(platform,asset_checksum_sha256)
  WHERE status IN ('SCHEDULED','QUEUED','UPLOADING','PROCESSING','PUBLISHING','NEEDS_INTERVENTION');
CREATE UNIQUE INDEX idx_publish_one_published_per_asset ON publish_jobs(platform,asset_checksum_sha256)
  WHERE status='PUBLISHED';
CREATE INDEX idx_publish_jobs_due ON publish_jobs(status,scheduled_for);

CREATE TABLE publish_request_keys(
  request_key TEXT PRIMARY KEY,
  publish_job_id TEXT NOT NULL REFERENCES publish_jobs(id),
  created_at TEXT NOT NULL
);

CREATE TABLE publish_job_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publish_job_id TEXT NOT NULL REFERENCES publish_jobs(id),
  event_type TEXT NOT NULL,
  status TEXT,
  safe_metadata_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_publish_job_events_job ON publish_job_events(publish_job_id,id);

CREATE TRIGGER distribution_packages_immutable_update BEFORE UPDATE ON distribution_packages BEGIN
  SELECT RAISE(ABORT,'distribution packages are immutable');
END;
CREATE TRIGGER distribution_packages_immutable_delete BEFORE DELETE ON distribution_packages BEGIN
  SELECT RAISE(ABORT,'distribution packages are immutable');
END;
CREATE TRIGGER distribution_reviews_immutable_update BEFORE UPDATE ON distribution_reviews BEGIN
  SELECT RAISE(ABORT,'distribution reviews are immutable');
END;
CREATE TRIGGER distribution_reviews_immutable_delete BEFORE DELETE ON distribution_reviews BEGIN
  SELECT RAISE(ABORT,'distribution reviews are immutable');
END;
CREATE TRIGGER publish_request_keys_immutable_update BEFORE UPDATE ON publish_request_keys BEGIN
  SELECT RAISE(ABORT,'publish request keys are immutable');
END;
CREATE TRIGGER publish_request_keys_immutable_delete BEFORE DELETE ON publish_request_keys BEGIN
  SELECT RAISE(ABORT,'publish request keys are immutable');
END;
CREATE TRIGGER publish_job_events_immutable_update BEFORE UPDATE ON publish_job_events BEGIN
  SELECT RAISE(ABORT,'publish job events are immutable');
END;
CREATE TRIGGER publish_job_events_immutable_delete BEFORE DELETE ON publish_job_events BEGIN
  SELECT RAISE(ABORT,'publish job events are immutable');
END;
CREATE TRIGGER publish_jobs_no_delete BEFORE DELETE ON publish_jobs BEGIN
  SELECT RAISE(ABORT,'publish jobs are never deleted');
END;
