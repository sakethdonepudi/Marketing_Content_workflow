-- Architecture 08 addendum: fast same-day discovery (entity-cluster event candidates
-- that hand off to strict verification). Additive and forward-only.

CREATE TABLE event_candidates(
  id TEXT PRIMARY KEY,
  headline TEXT NOT NULL,
  location TEXT,
  jurisdiction TEXT,
  state TEXT NOT NULL DEFAULT 'CANDIDATE' CHECK(state IN (
    'CANDIDATE','HANDED_OFF','UNVERIFIED','NEEDS_ATTENTION','PROMOTED','REJECTED')),
  confidence TEXT NOT NULL CHECK(confidence IN ('VERY_HIGH','HIGH','NORMAL','LOW')),
  discovery_confidence REAL NOT NULL DEFAULT 0,
  entities_json TEXT NOT NULL DEFAULT '[]',
  source_families_json TEXT NOT NULL DEFAULT '[]',
  source_count INTEGER NOT NULL DEFAULT 0,
  independent_source_count INTEGER NOT NULL DEFAULT 0,
  event_id TEXT REFERENCES events(id),
  first_seen_at TEXT NOT NULL,
  candidate_created_at TEXT NOT NULL,
  discovery_latency_seconds REAL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_event_candidates_state ON event_candidates(state,first_seen_at DESC);

CREATE TABLE candidate_signals(
  id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES event_candidates(id),
  source_family TEXT NOT NULL,
  url TEXT,
  title TEXT NOT NULL,
  text TEXT,
  published_at TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
  entities_json TEXT NOT NULL DEFAULT '[]',
  location TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_candidate_signals_candidate ON candidate_signals(candidate_id,created_at);
