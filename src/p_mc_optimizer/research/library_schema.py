"""Version 3 storage for independent research documents and refresh runs."""

LIBRARY_SCHEMA = """
CREATE TABLE library_settings (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE retrieval_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE library_source_configs (
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(source_id, revision)
);
CREATE TABLE library_documents (
    document_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE
);
CREATE TABLE library_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES library_documents(document_id),
    bytes_sha256 TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX library_document_versions ON library_versions(document_id);
CREATE TABLE library_generations (
    generation INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    origin_id TEXT
);
CREATE TABLE source_sync_runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    owner_job_id TEXT REFERENCES jobs(job_id),
    status TEXT NOT NULL CHECK (status IN (
        'queued','running','completed','partial','failed','cancelled','interrupted'
    )),
    version INTEGER NOT NULL DEFAULT 1,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    worker_id TEXT,
    lease_until TEXT,
    generation INTEGER REFERENCES library_generations(generation),
    active_seconds REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    summary TEXT
);
CREATE INDEX source_sync_queue ON source_sync_runs(status, created_at, run_id);
CREATE TABLE source_sync_locks (
    source_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES source_sync_runs(run_id)
);
CREATE TABLE source_sync_invocations (
    run_id TEXT NOT NULL REFERENCES source_sync_runs(run_id),
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','completed','failed','unknown_outcome')),
    started_at TEXT NOT NULL,
    reserved_bytes INTEGER NOT NULL,
    reserved_seconds REAL NOT NULL,
    expanded_bytes INTEGER NOT NULL DEFAULT 0,
    unknown_bytes INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    http_status INTEGER,
    reason_code TEXT,
    PRIMARY KEY(run_id, url)
);
CREATE INDEX source_sync_source_ledger ON source_sync_invocations(run_id, source_id);
CREATE TABLE library_staging (
    run_id TEXT NOT NULL REFERENCES source_sync_runs(run_id),
    source_id TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(run_id, source_id, requested_url)
);
CREATE TABLE library_observations (
    observation_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES library_documents(document_id),
    version_id TEXT REFERENCES library_versions(version_id),
    sync_run_id TEXT REFERENCES source_sync_runs(run_id),
    owner_job_id TEXT REFERENCES jobs(job_id),
    generation INTEGER NOT NULL REFERENCES library_generations(generation),
    payload TEXT NOT NULL
);
CREATE INDEX library_document_observations ON library_observations(document_id, generation);
CREATE INDEX library_source_observations ON library_observations(source_id, generation);
CREATE TABLE library_memberships (
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES library_documents(document_id),
    observation_id TEXT REFERENCES library_observations(observation_id),
    last_attempt_observation_id TEXT NOT NULL REFERENCES library_observations(observation_id),
    last_attempt_at TEXT NOT NULL,
    last_successful_validation_at TEXT,
    remote_unavailable INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL REFERENCES library_generations(generation),
    PRIMARY KEY(source_id, document_id)
);
CREATE TABLE job_library_snapshots (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    payload TEXT NOT NULL
);
"""

LIBRARY_FTS_SCHEMA = """
CREATE VIRTUAL TABLE library_fts USING fts5(version_id UNINDEXED, title, body, tokenize='unicode61');
"""
