-- Architecture 11 follow-up: OAuth connection hardening.
-- Non-secret metadata only; tokens live in the local .env, never in the DB or API.

ALTER TABLE youtube_oauth_state ADD COLUMN token_expires_at TEXT;
ALTER TABLE youtube_oauth_state ADD COLUMN refresh_token_present INTEGER NOT NULL DEFAULT 0;

-- Short-lived CSRF state values for the OAuth authorization flow.
CREATE TABLE youtube_oauth_states(
  state TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE INDEX idx_youtube_oauth_states_created ON youtube_oauth_states(created_at DESC);
