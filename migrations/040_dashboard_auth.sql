-- Architecture 13: dashboard auth (server-side sessions + login rate limiting).
-- Additive. Passwords are never stored here (only a scrypt hash lives in the environment).

CREATE TABLE dashboard_sessions(
  token_hash TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX idx_dashboard_sessions_expires ON dashboard_sessions(expires_at DESC);

CREATE TABLE dashboard_login_attempts(
  client_key TEXT PRIMARY KEY,
  failed_count INTEGER NOT NULL DEFAULT 0,
  last_failed_at TEXT,
  locked_until TEXT
);

-- Automated YouTube uploads stay PRIVATE by default (operator-configurable).
ALTER TABLE youtube_publish_jobs ADD COLUMN automated_privacy TEXT NOT NULL DEFAULT 'PRIVATE';
