-- Architecture 08: production control plane (queue, approvals, revisions, scheduling,
-- notifications, cost). Additive and forward-only.

-- Immutable approvals: valid only for one exact reel version; regeneration invalidates.
CREATE TABLE reel_approvals(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  reel_version TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  production_standard_version TEXT NOT NULL,
  claim_set_version TEXT,
  media_manifest_hash TEXT,
  render_hash TEXT NOT NULL,
  revoked_at TEXT,
  revoked_reason TEXT,
  UNIQUE(reel_id,render_hash)
);
CREATE INDEX idx_reel_approvals_reel ON reel_approvals(reel_id,approved_at DESC);

-- Change-request loop: a revision request spawns a NEW immutable reel version.
CREATE TABLE reel_revision_requests(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  categories_json TEXT NOT NULL DEFAULT '[]',
  comment TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','IN_PROGRESS','RESOLVED','CANCELLED')),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX idx_reel_revision_reel ON reel_revision_requests(reel_id,created_at DESC);

-- Scheduler: idempotent scheduled posts (one active per reel+platform).
CREATE TABLE scheduled_posts(
  id TEXT PRIMARY KEY,
  reel_id TEXT NOT NULL REFERENCES final_reel_assets(id),
  platform TEXT NOT NULL CHECK(platform IN ('INSTAGRAM_REELS','FACEBOOK_REELS')),
  scheduled_at TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  status TEXT NOT NULL CHECK(status IN ('SCHEDULED','PROCESSING','PUBLISHED','FAILED','CANCELLED')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  last_error TEXT,
  published_post_id TEXT,
  published_at TEXT,
  approval_id TEXT NOT NULL REFERENCES reel_approvals(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_sched_one_active ON scheduled_posts(reel_id,platform)
  WHERE status IN ('SCHEDULED','PROCESSING');

-- Internal notification center. No noisy notifications for successful internal stages.
CREATE TABLE notifications(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','ERROR')),
  message TEXT NOT NULL,
  link_json TEXT NOT NULL DEFAULT '{}',
  dedupe_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX idx_notifications_created ON notifications(created_at DESC);

-- Per-run cost tracking; unknown stays unknown, never zero.
ALTER TABLE reel_pipeline_runs ADD COLUMN research_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN llm_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN tts_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN image_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN video_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN total_known_cost_usd REAL;
ALTER TABLE reel_pipeline_runs ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE reel_pipeline_runs ADD COLUMN completed_at TEXT;
