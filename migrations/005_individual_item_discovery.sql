ALTER TABLE sources ADD COLUMN source_class TEXT NOT NULL DEFAULT 'official_primary'
  CHECK(source_class IN ('official_primary', 'independent_reporting'));
ALTER TABLE sources ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'verified'
  CHECK(identity_status IN ('verified', 'review', 'rejected'));
ALTER TABLE sources ADD COLUMN identity_evidence_url TEXT;
ALTER TABLE sources ADD COLUMN rate_limit_seconds REAL NOT NULL DEFAULT 1.0;
ALTER TABLE sources ADD COLUMN poll_interval_seconds INTEGER NOT NULL DEFAULT 900;

CREATE TABLE source_poll_state(
  source_id TEXT PRIMARY KEY REFERENCES sources(id),
  last_polled_at TEXT,
  next_poll_at TEXT,
  cursor_url TEXT,
  pages_fetched INTEGER NOT NULL DEFAULT 0,
  last_status TEXT NOT NULL DEFAULT 'never_polled',
  last_error TEXT
);

DROP TRIGGER IF EXISTS signals_are_immutable_update;
DROP TRIGGER IF EXISTS signals_are_immutable_delete;

ALTER TABLE signals RENAME TO signals_legacy;
DROP INDEX IF EXISTS idx_signals_event_id;
DROP INDEX IF EXISTS idx_signals_publication_time;
DROP INDEX IF EXISTS idx_signals_event_time;
DROP INDEX IF EXISTS idx_signals_item_kind;
DROP INDEX IF EXISTS idx_signals_workspace_key;

CREATE TABLE signals(
  id TEXT PRIMARY KEY,
  event_id TEXT REFERENCES events(id),
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
  cluster_method TEXT NOT NULL DEFAULT 'not_clustered',
  workspace_key TEXT NOT NULL,
  item_kind TEXT NOT NULL DEFAULT 'unclassified'
    CHECK(item_kind IN ('unclassified', 'reference', 'review', 'rejected', 'event')),
  item_type TEXT,
  event_time TEXT,
  classification_reason TEXT NOT NULL DEFAULT 'awaiting_classification',
  author TEXT,
  source_class TEXT NOT NULL DEFAULT 'official_primary'
    CHECK(source_class IN ('official_primary', 'independent_reporting')),
  event_time_basis TEXT CHECK(event_time_basis IN ('stated_event_time', 'publication_time'))
);

INSERT INTO signals(
  id,event_id,source_id,url,canonical_url,publication_time,detected_at,title,text,
  source_name,source_type,source_metadata_json,content_hash,cluster_score,cluster_method,
  workspace_key,item_kind,item_type,event_time,classification_reason
)
SELECT
  id,event_id,source_id,url,canonical_url,publication_time,detected_at,title,text,
  source_name,source_type,source_metadata_json,content_hash,cluster_score,cluster_method,
  workspace_key,item_kind,item_type,event_time,classification_reason
FROM signals_legacy;

DROP TABLE signals_legacy;

CREATE INDEX idx_signals_event_id ON signals(event_id);
CREATE INDEX idx_signals_publication_time ON signals(publication_time);
CREATE INDEX idx_signals_event_time ON signals(event_time);
CREATE INDEX idx_signals_item_kind ON signals(item_kind);
CREATE INDEX idx_signals_workspace_key ON signals(workspace_key);
CREATE INDEX idx_sources_source_class ON sources(source_class);

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
