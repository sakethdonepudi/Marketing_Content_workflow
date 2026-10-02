-- Architecture 11: OAuth state expiry (additive). Legacy rows keep expires_at NULL and are
-- treated as invalid for callback consumption (never backfilled with a future time).
ALTER TABLE youtube_oauth_states ADD COLUMN expires_at TEXT;
