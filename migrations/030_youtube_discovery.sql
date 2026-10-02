-- Architecture 09C: YouTube live discovery (YOUTUBE_DISCOVERY_V1).
-- Additive and forward-only. YouTube signals are discovery leads, never evidence;
-- media rights stay UNKNOWN so YouTube can never auto-become production B-roll.

CREATE TABLE youtube_discovery_state(
  id TEXT PRIMARY KEY,
  bucket_cursor INTEGER NOT NULL DEFAULT 0,
  query_offset INTEGER NOT NULL DEFAULT 0,
  last_bucket TEXT,
  last_queries TEXT NOT NULL DEFAULT '[]',
  quota_date TEXT,
  quota_used INTEGER NOT NULL DEFAULT 0,
  searches_today INTEGER NOT NULL DEFAULT 0,
  queries_today INTEGER NOT NULL DEFAULT 0,
  videos_today INTEGER NOT NULL DEFAULT 0,
  errors_today INTEGER NOT NULL DEFAULT 0,
  recent_video_ids TEXT NOT NULL DEFAULT '[]',
  last_success_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL
);
