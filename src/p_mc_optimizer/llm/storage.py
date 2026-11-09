"""SQLite schema installed by the application migration."""
import json


LLM_SCHEMA = """
CREATE TABLE IF NOT EXISTS credential_refs (
    credential_ref TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    storage_mode TEXT CHECK(storage_mode IN ('session', 'os_keyring'))
);
CREATE TABLE IF NOT EXISTS provider_profiles (
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    credential_ref TEXT NOT NULL REFERENCES credential_refs,
    settings TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, revision)
);
CREATE TABLE IF NOT EXISTS active_provider_profile (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    FOREIGN KEY(profile_id, revision) REFERENCES provider_profiles(profile_id, revision)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(job_id),
    role TEXT,
    purpose TEXT NOT NULL CHECK(purpose IN ('job_role', 'connection_test')),
    invocation_id TEXT UNIQUE,
    test_idempotency_key TEXT UNIQUE,
    request_hash TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
    provider_snapshot TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL,
    credential_generation INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT,
    previous_output TEXT,
    metadata TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','failed','outcome_unknown')),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(profile_id, profile_revision) REFERENCES provider_profiles(profile_id, revision),
    CHECK (
        (purpose='job_role' AND job_id IS NOT NULL AND role IS NOT NULL
         AND invocation_id IS NOT NULL AND test_idempotency_key IS NULL)
        OR (purpose='connection_test' AND job_id IS NULL AND role IS NULL
            AND invocation_id IS NULL AND test_idempotency_key IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS llm_calls_role ON llm_calls(job_id, role, attempt_no);
CREATE INDEX IF NOT EXISTS llm_tests_profile ON llm_calls(
    profile_id, profile_revision, credential_generation, purpose, started_at
);
"""


def job_call_metadata(repo, job_id: str) -> list[dict]:
    """Read durable audit metadata without request bodies, credentials, or prompts."""
    with repo.connect() as conn:
        rows = conn.execute(
            """SELECT call_id, invocation_id, role, attempt_no, status, prompt_version, provider_snapshot,
                      metadata, error, started_at, finished_at
               FROM llm_calls WHERE job_id=? ORDER BY started_at, rowid""",
            (str(job_id),),
        ).fetchall()
    results = []
    for row in rows:
        values = dict(row)
        snapshot = json.loads(values.pop("provider_snapshot"))
        metadata = json.loads(values.pop("metadata") or "null") or {}
        error = json.loads(values.pop("error") or "null")
        results.append({
            "model_requested": snapshot["model"],
            "model_returned": None,
            "request_id": None,
            "finish_reason": None,
            "usage": None,
            "usage_status": "unavailable",
            "cost": None,
            "elapsed_seconds": None,
            **metadata,
            **values,
            "provider_snapshot_hash": snapshot["provider_snapshot_hash"],
            "error_code": error["error_code"] if error else None,
        })
    return results
