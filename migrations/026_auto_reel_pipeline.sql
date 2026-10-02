-- AUTO_REEL_PIPELINE_V1: durable, resumable orchestration of the reel factory.
-- Additive and forward-only. Stages are checkpointed so a restart never duplicates paid work.

CREATE TABLE reel_pipeline_runs(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
  current_stage TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','WAITING','READY_FOR_REVIEW','NEEDS_ATTENTION','COMPLETE','HALTED')),
  production_standard_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  failure_reason TEXT,
  failure_stage TEXT,
  retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
  recommended_action TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
  reel_id TEXT REFERENCES final_reel_assets(id),
  checkpoint_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_reel_pipeline_status ON reel_pipeline_runs(status,updated_at DESC);

CREATE TABLE reel_pipeline_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pipeline_run_id TEXT NOT NULL REFERENCES reel_pipeline_runs(id),
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX idx_reel_pipeline_events_run ON reel_pipeline_events(pipeline_run_id,id);
