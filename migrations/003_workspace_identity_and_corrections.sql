ALTER TABLE sources ADD COLUMN workspace_key TEXT;
ALTER TABLE sources ADD COLUMN validated_at TEXT;
ALTER TABLE signals ADD COLUMN workspace_key TEXT NOT NULL DEFAULT 'n-chandrababu-naidu-andhra-pradesh';
ALTER TABLE events ADD COLUMN workspace_key TEXT NOT NULL DEFAULT 'n-chandrababu-naidu-andhra-pradesh';

CREATE TABLE data_corrections(
  id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  details_json TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE correction_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  correction_id TEXT NOT NULL REFERENCES data_corrections(id),
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  corrected_at TEXT NOT NULL,
  UNIQUE(correction_id, entity_type, entity_id, action)
);

CREATE INDEX idx_sources_workspace_key ON sources(workspace_key);
CREATE INDEX idx_signals_workspace_key ON signals(workspace_key);
CREATE INDEX idx_events_workspace_key ON events(workspace_key);
CREATE INDEX idx_correction_audit_correction_id ON correction_audit(correction_id);
