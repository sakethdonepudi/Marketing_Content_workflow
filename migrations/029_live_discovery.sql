-- Architecture 09: continuous live discovery scheduler, source health, checkpoints, SLO.
-- Additive and forward-only. Signals here are leads, never verified evidence.

CREATE TABLE discovery_sources(
  id TEXT PRIMARY KEY,
  family TEXT NOT NULL,
  adapter TEXT NOT NULL,
  publisher TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  configured INTEGER NOT NULL DEFAULT 1 CHECK(configured IN (0,1)),
  poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
  max_concurrency INTEGER NOT NULL DEFAULT 1,
  request_budget_per_cycle INTEGER NOT NULL DEFAULT 20,
  last_polled_at TEXT,
  last_seen_published_at TEXT,
  cursor TEXT,
  last_success_at TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  average_latency_ms REAL,
  results_last_24h INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE discovery_fetch_runs(
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES discovery_sources(id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','RATE_LIMITED')),
  signals_fetched INTEGER NOT NULL DEFAULT 0,
  signals_deduped INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  latency_ms REAL
);
CREATE INDEX idx_discovery_fetch_runs_source ON discovery_fetch_runs(source_id,started_at DESC);

-- Raw discovery signals, separate from the evidence ledger.
CREATE TABLE discovery_signals(
  id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES discovery_sources(id),
  fetch_run_id TEXT REFERENCES discovery_fetch_runs(id),
  source_family TEXT NOT NULL,
  publisher TEXT,
  title TEXT NOT NULL,
  text TEXT,
  url TEXT,
  canonical_url TEXT,
  published_at TEXT,
  first_seen_at TEXT NOT NULL,
  entities_json TEXT NOT NULL DEFAULT '[]',
  location TEXT,
  language TEXT,
  engagement_metrics_json TEXT NOT NULL DEFAULT '{}',
  raw_metadata_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL,
  candidate_id TEXT REFERENCES event_candidates(id),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_discovery_signals_hash ON discovery_signals(content_hash);
CREATE INDEX idx_discovery_signals_created ON discovery_signals(created_at DESC);

-- SLO samples for discovery latency stages.
CREATE TABLE discovery_slo_samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT,
  fetch_latency_ms REAL,
  normalization_latency_ms REAL,
  cluster_latency_ms REAL,
  candidate_latency_seconds REAL,
  slo_met INTEGER CHECK(slo_met IN (0,1)),
  recorded_at TEXT NOT NULL
);
