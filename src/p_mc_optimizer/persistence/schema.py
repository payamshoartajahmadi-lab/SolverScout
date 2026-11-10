"""SQLite schema and numbered migrations."""
from __future__ import annotations

SCHEMA_VERSION = 3

BASE_SCHEMA = """
CREATE TABLE problem_revisions (
    problem_revision_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    spec TEXT NOT NULL,
    problem_math_hash TEXT NOT NULL,
    evaluation_contract_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(problem_id, revision_no)
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    input TEXT NOT NULL,
    policy TEXT NOT NULL,
    llm_profile TEXT,
    problem_revision_id TEXT REFERENCES problem_revisions(problem_revision_id),
    job_status TEXT NOT NULL CHECK (job_status IN
        ('queued','running','needs_input','needs_configuration','completed','failed','cancelled')),
    phase TEXT NOT NULL CHECK (phase IN ('intake','formulating','validating_input','preflight',
        'profiling','planning','piloting','selecting','solving','verifying','reporting')),
    version INTEGER NOT NULL DEFAULT 1,
    event_sequence INTEGER NOT NULL DEFAULT 0,
    solution_status TEXT NOT NULL DEFAULT 'pending' CHECK (solution_status IN
        ('pending','valid_best_found','no_valid_solution','validation_failed','validation_incomplete')),
    optimality_status TEXT NOT NULL DEFAULT 'not_certified' CHECK (optimality_status='not_certified'),
    explanation_status TEXT NOT NULL DEFAULT 'pending' CHECK (explanation_status IN
        ('pending','generated','schema_fallback','provider_error','not_requested')),
    selection_status TEXT NOT NULL DEFAULT 'pending' CHECK (selection_status IN
        ('pending','selected','fixed_by_policy','insufficient_evidence','not_reached')),
    termination_reason TEXT CHECK (termination_reason IN ('evaluation_budget','time_budget',
        'solver_stop','execution_error','validation_error','insufficient_evidence','llm_budget','cancelled')),
    partial_result INTEGER NOT NULL DEFAULT 0 CHECK (partial_result IN (0,1)),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    blocked_reason TEXT,
    error TEXT,
    workflow TEXT NOT NULL DEFAULT '{}',
    completion_input TEXT,
    answers TEXT NOT NULL DEFAULT '[]',
    owner_id TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX jobs_claim_idx ON jobs(job_status, created_at, job_id);

CREATE TABLE problem_drafts (
    draft_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    draft_version INTEGER NOT NULL CHECK (draft_version >= 1),
    payload TEXT NOT NULL
);

CREATE TABLE trials (
    trial_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    problem_revision_id TEXT NOT NULL REFERENCES problem_revisions(problem_revision_id),
    logical_trial_key TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('pilot','main')),
    algorithm_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    replicate_index INTEGER NOT NULL,
    evaluation_quota INTEGER NOT NULL CHECK (evaluation_quota > 0),
    request TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    UNIQUE(job_id, logical_trial_key)
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    status TEXT NOT NULL CHECK (status='ready'),
    created_at TEXT NOT NULL
);
CREATE INDEX artifacts_job_idx ON artifacts(job_id, kind);

CREATE TABLE trial_attempts (
    attempt_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no=1),
    execution_key TEXT NOT NULL UNIQUE,
    evaluation_quota INTEGER NOT NULL CHECK (evaluation_quota > 0),
    deadline_seconds REAL NOT NULL CHECK (deadline_seconds > 0),
    execution_status TEXT NOT NULL CHECK (execution_status IN
        ('running','completed','failed','timed_out','cancelled','unknown_outcome')),
    known_started INTEGER NOT NULL DEFAULT 0 CHECK (known_started >= 0),
    reserved_active INTEGER NOT NULL CHECK (reserved_active >= 0),
    charged_unknown INTEGER NOT NULL DEFAULT 0 CHECK (charged_unknown >= 0),
    unused INTEGER NOT NULL DEFAULT 0 CHECK (unused >= 0),
    outcome TEXT,
    history_artifact_id TEXT REFERENCES artifacts(artifact_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(trial_id, attempt_no),
    CHECK (known_started + reserved_active + charged_unknown + unused = evaluation_quota)
);

CREATE TABLE stage_executions (
    stage_execution_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    stage TEXT NOT NULL CHECK (stage IN ('preflight','profile','verification')),
    evaluation_quota INTEGER NOT NULL CHECK (evaluation_quota > 0),
    deadline_seconds REAL NOT NULL CHECK (deadline_seconds > 0),
    execution_status TEXT NOT NULL CHECK (execution_status IN
        ('running','completed','failed','timed_out','cancelled','unknown_outcome')),
    known_started INTEGER NOT NULL DEFAULT 0 CHECK (known_started >= 0),
    reserved_active INTEGER NOT NULL CHECK (reserved_active >= 0),
    charged_unknown INTEGER NOT NULL DEFAULT 0 CHECK (charged_unknown >= 0),
    unused INTEGER NOT NULL DEFAULT 0 CHECK (unused >= 0),
    outcome TEXT,
    history_artifact_id TEXT REFERENCES artifacts(artifact_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(job_id, stage),
    CHECK (known_started + reserved_active + charged_unknown + unused = evaluation_quota)
);

CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX decisions_job_idx ON decisions(job_id);

CREATE TABLE events (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (job_id, sequence)
);

CREATE TABLE reports (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    payload TEXT NOT NULL,
    artifact_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE idempotency_records (
    operation_scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation_scope, idempotency_key)
);
"""
