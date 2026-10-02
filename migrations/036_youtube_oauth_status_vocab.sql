-- Architecture 11: widen youtube_oauth_state.token_status vocabulary.
-- SQLite cannot alter a CHECK constraint in place: rebuild the table, copying existing rows.
-- Additive/forward-only semantics preserved (single row id='youtube').

CREATE TABLE youtube_oauth_state_new(
  id TEXT PRIMARY KEY,
  channel_id TEXT,
  channel_title TEXT,
  scope TEXT,
  token_status TEXT NOT NULL DEFAULT 'UNCONFIGURED'
    CHECK(token_status IN ('CONNECTED','UNCONFIGURED','TOKEN_EXPIRED','ERROR','DEGRADED','NOT_CONNECTED')),
  connected_account TEXT,
  last_authorized_call_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  token_expires_at TEXT,
  refresh_token_present INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO youtube_oauth_state_new(id,channel_id,channel_title,scope,token_status,
  connected_account,last_authorized_call_at,last_error,created_at,updated_at,token_expires_at,
  refresh_token_present)
SELECT id,channel_id,channel_title,scope,token_status,connected_account,last_authorized_call_at,
  last_error,created_at,updated_at,token_expires_at,refresh_token_present
FROM youtube_oauth_state;

DROP TABLE youtube_oauth_state;
ALTER TABLE youtube_oauth_state_new RENAME TO youtube_oauth_state;
