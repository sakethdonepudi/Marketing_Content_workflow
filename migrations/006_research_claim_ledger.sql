ALTER TABLE events ADD COLUMN research_status TEXT NOT NULL DEFAULT 'NOT_RESEARCHED'
    CHECK (research_status IN ('NOT_RESEARCHED', 'QUEUED', 'RUNNING', 'REVIEW_REQUIRED', 'COMPLETE', 'FAILED'));

CREATE TABLE research_runs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('test', 'live')),
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CACHED')),
    evidence_version TEXT NOT NULL,
    cache_source_run_id TEXT REFERENCES research_runs(id),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    progress_message TEXT NOT NULL DEFAULT 'Queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    search_limit INTEGER NOT NULL,
    token_limit INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    search_count INTEGER,
    cost_usd REAL,
    cost_status TEXT NOT NULL DEFAULT 'unknown' CHECK (cost_status IN ('known', 'unknown')),
    provider_request_id TEXT,
    relevance_assessment TEXT,
    occurrence_kind TEXT,
    summary_json TEXT,
    verification_explanation TEXT,
    error_code TEXT,
    error_message TEXT
);

CREATE UNIQUE INDEX idx_research_one_active_per_event
    ON research_runs(event_id)
    WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX idx_research_runs_event ON research_runs(event_id, requested_at DESC);
CREATE INDEX idx_research_runs_cache ON research_runs(event_id, provider, model, evidence_version, status);

CREATE TABLE research_run_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES research_runs(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    message TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE evidence_snapshots (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_runs(id),
    signal_id TEXT NOT NULL REFERENCES signals(id),
    source_id TEXT REFERENCES sources(id),
    source_name TEXT NOT NULL,
    source_class TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    publication_time TEXT,
    stated_event_time TEXT,
    author TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (run_id, signal_id)
);

CREATE INDEX idx_evidence_snapshots_run ON evidence_snapshots(run_id);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    research_run_id TEXT NOT NULL REFERENCES research_runs(id),
    text TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('factual_assertion', 'quotation', 'opinion', 'promise', 'allegation')),
    assertion_scope TEXT NOT NULL CHECK (assertion_scope IN ('occurrence', 'announcement', 'approval', 'funding_allocation', 'completed_work', 'quotation', 'opinion', 'promise', 'allegation')),
    attribution TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('UNVERIFIED', 'SUPPORTED', 'CONFLICTED', 'INSUFFICIENT_EVIDENCE')),
    reviewer_notes TEXT,
    required_for_event INTEGER NOT NULL DEFAULT 0 CHECK (required_for_event IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_claims_event ON claims(event_id, created_at);
CREATE INDEX idx_claims_run ON claims(research_run_id);

CREATE TABLE claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    snapshot_id TEXT REFERENCES evidence_snapshots(id),
    source_url TEXT NOT NULL,
    supporting_excerpt TEXT,
    support_kind TEXT NOT NULL CHECK (support_kind IN ('supports', 'conflicts')),
    validation_status TEXT NOT NULL CHECK (validation_status IN ('VALID', 'URL_NOT_IN_EVIDENCE', 'EXCERPT_NOT_FOUND', 'MISSING_EXCERPT')),
    validation_note TEXT,
    UNIQUE (claim_id, source_url, supporting_excerpt, support_kind)
);

CREATE INDEX idx_claim_evidence_claim ON claim_evidence(claim_id);

CREATE TABLE claim_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

