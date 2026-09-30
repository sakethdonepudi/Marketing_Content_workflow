ALTER TABLE events ADD COLUMN first_seen_at TEXT;
ALTER TABLE events ADD COLUMN last_seen_at TEXT;

UPDATE events
SET first_seen_at = created_at,
    last_seen_at = updated_at
WHERE first_seen_at IS NULL OR last_seen_at IS NULL;

CREATE TABLE sources(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('rss', 'webpage', 'manual')),
  url TEXT NOT NULL UNIQUE,
  official INTEGER NOT NULL DEFAULT 0 CHECK(official IN (0, 1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE signals(
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  source_id TEXT REFERENCES sources(id),
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  publication_time TEXT,
  detected_at TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL,
  cluster_score REAL NOT NULL DEFAULT 0,
  cluster_method TEXT NOT NULL DEFAULT 'new_event'
);

CREATE INDEX idx_signals_event_id ON signals(event_id);
CREATE INDEX idx_signals_publication_time ON signals(publication_time);
CREATE INDEX idx_events_first_seen_at ON events(first_seen_at);

CREATE TRIGGER signals_are_immutable_update
BEFORE UPDATE ON signals
BEGIN
  SELECT RAISE(ABORT, 'signals are immutable');
END;

CREATE TRIGGER signals_are_immutable_delete
BEFORE DELETE ON signals
BEGIN
  SELECT RAISE(ABORT, 'signals are immutable');
END;
