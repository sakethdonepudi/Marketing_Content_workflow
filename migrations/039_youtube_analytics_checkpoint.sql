-- Architecture 12: allow a single canonical analytics checkpoint value.
-- SQLite cannot alter a CHECK in place: rebuild and copy rows.
CREATE TABLE youtube_performance_snapshots_new(
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL,
  reel_id TEXT REFERENCES final_reel_assets(id),
  youtube_publish_job_id TEXT REFERENCES youtube_publish_jobs(id),
  checkpoint TEXT NOT NULL CHECK(checkpoint IN ('latest','1h','6h','24h','3d','7d')),
  views INTEGER, likes INTEGER, comments INTEGER,
  watch_time_seconds REAL, average_view_duration_seconds REAL,
  average_percentage_viewed REAL, subscribers_gained INTEGER, shares INTEGER,
  source TEXT NOT NULL DEFAULT 'UNKNOWN',
  recorded_at TEXT NOT NULL,
  event_id TEXT, reel_version TEXT, youtube_copy_version INTEGER,
  title TEXT, processing_state TEXT
);
INSERT OR IGNORE INTO youtube_performance_snapshots_new(id,video_id,reel_id,youtube_publish_job_id,checkpoint,
  views,likes,comments,watch_time_seconds,average_view_duration_seconds,average_percentage_viewed,
  subscribers_gained,shares,source,recorded_at,event_id,reel_version,youtube_copy_version,title,processing_state)
SELECT id,video_id,reel_id,youtube_publish_job_id,checkpoint,views,likes,comments,watch_time_seconds,
  average_view_duration_seconds,average_percentage_viewed,subscribers_gained,shares,source,recorded_at,
  event_id,reel_version,youtube_copy_version,title,processing_state
FROM youtube_performance_snapshots;
DROP TABLE youtube_performance_snapshots;
ALTER TABLE youtube_performance_snapshots_new RENAME TO youtube_performance_snapshots;
CREATE INDEX idx_youtube_perf_video ON youtube_performance_snapshots(video_id,recorded_at);
