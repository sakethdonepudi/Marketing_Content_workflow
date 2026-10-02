-- Architecture 12: publishing hardening + analytics.
-- Additive and forward-only. Aggregate video-level metrics only; no demographic profiling.

-- Current error-state fields are clearable on success; history lives in the append-only event table.
ALTER TABLE youtube_publish_jobs ADD COLUMN last_error_at TEXT;
ALTER TABLE youtube_publish_jobs ADD COLUMN public_capability TEXT NOT NULL DEFAULT 'UNKNOWN'
  CHECK(public_capability IN ('AVAILABLE','RESTRICTED','UNKNOWN'));

-- OAuth refresh health (separate from the connection status vocabulary).
ALTER TABLE youtube_oauth_state ADD COLUMN refresh_health TEXT NOT NULL DEFAULT 'UNKNOWN'
  CHECK(refresh_health IN ('HEALTHY','REAUTH_REQUIRED','ERROR','UNKNOWN'));
ALTER TABLE youtube_oauth_state ADD COLUMN last_refresh_check_at TEXT;
ALTER TABLE youtube_oauth_state ADD COLUMN refresh_failure_count INTEGER NOT NULL DEFAULT 0;

-- Analytics linked to reel/copy/topic context (aggregate content features only).
ALTER TABLE youtube_performance_snapshots ADD COLUMN event_id TEXT;
ALTER TABLE youtube_performance_snapshots ADD COLUMN reel_version TEXT;
ALTER TABLE youtube_performance_snapshots ADD COLUMN youtube_copy_version INTEGER;
ALTER TABLE youtube_performance_snapshots ADD COLUMN title TEXT;
ALTER TABLE youtube_performance_snapshots ADD COLUMN processing_state TEXT;
