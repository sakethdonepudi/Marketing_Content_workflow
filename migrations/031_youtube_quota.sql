-- Architecture 09D: YouTube quota optimization.
-- Additive and forward-only. Adds query cache, video-metadata cache, poll state,
-- and budget/mode telemetry so YouTube discovery is quota-efficient.

CREATE TABLE youtube_query_cache(
  query_text TEXT PRIMARY KEY,
  query_key TEXT NOT NULL,
  executed_at TEXT NOT NULL,
  result_video_ids TEXT NOT NULL DEFAULT '[]',
  result_count INTEGER NOT NULL DEFAULT 0,
  cycle_reason TEXT
);
CREATE INDEX idx_youtube_query_cache_executed ON youtube_query_cache(executed_at DESC);

CREATE TABLE youtube_video_cache(
  video_id TEXT PRIMARY KEY,
  title TEXT,
  description TEXT,
  channel_id TEXT,
  channel_title TEXT,
  published_at TEXT,
  tags_json TEXT NOT NULL DEFAULT '[]',
  duration TEXT,
  live_broadcast TEXT,
  was_live INTEGER NOT NULL DEFAULT 0,
  view_count INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  fetched_at TEXT NOT NULL
);
CREATE INDEX idx_youtube_video_cache_fetched ON youtube_video_cache(fetched_at DESC);

-- Polling mode / adaptive-poll telemetry on the existing singleton state row.
ALTER TABLE youtube_discovery_state ADD COLUMN mode TEXT;
ALTER TABLE youtube_discovery_state ADD COLUMN next_poll_at TEXT;
ALTER TABLE youtube_discovery_state ADD COLUMN last_polled_at TEXT;
ALTER TABLE youtube_discovery_state ADD COLUMN paused_reason TEXT;
ALTER TABLE youtube_discovery_state ADD COLUMN pending_trigger TEXT;

