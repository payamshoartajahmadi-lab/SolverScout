"""Research checkpoints, network reservations and verified local evidence."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .transport import ResearchError


RESEARCH_SCHEMA = """
CREATE TABLE search_profiles (
    revision INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE search_credentials (
    credential_ref TEXT PRIMARY KEY,
    mode TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    tested_generation INTEGER
);
CREATE TABLE research_source_revisions (
    revision INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE research_runs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    request_hash TEXT NOT NULL,
    outcome TEXT,
    extra_active_seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE research_invocations (
    scope TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('search','fetch','robots','test')),
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','completed','failed','unknown_outcome')),
    payload TEXT,
    started_at TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    expanded_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(scope, invocation_id)
);
CREATE TABLE source_documents (
    document_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_runs(job_id),
    requested_url TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(job_id, requested_url)
);
CREATE INDEX source_documents_cache ON source_documents(cache_key);
CREATE TABLE evidence_passages (
    evidence_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_runs(job_id),
    document_id TEXT NOT NULL REFERENCES source_documents(document_id),
    payload TEXT NOT NULL
);
CREATE TABLE robots_cache (
    hostname TEXT PRIMARY KEY,
    validated_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    payload TEXT
);
"""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchStore:
    def __init__(self, db_path: str | Path, artifact_dir: str | Path):
        self.path = Path(db_path)
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def begin_run(self, job_id, payload):
        hashed = digest(payload)
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM research_runs WHERE job_id=?", (job_id,)).fetchone()
            if row:
                if row["request_hash"] != hashed:
                    raise ResearchError("IDEMPOTENCY_CONFLICT")
                return json.loads(row["outcome"]) if row["outcome"] else None
            conn.execute(
                "INSERT INTO research_runs(job_id,request_hash) VALUES(?,?)", (job_id, hashed),
            )
        return None

    def outcome(self, job_id):
        with self.connect() as conn:
            row = conn.execute("SELECT outcome FROM research_runs WHERE job_id=?", (job_id,)).fetchone()
            return json.loads(row["outcome"]) if row and row["outcome"] else None

    def save_outcome(self, job_id, payload, evidence, *, final=True):
        with self.connect(write=True) as conn:
            for item in evidence:
                conn.execute(
                    "INSERT OR IGNORE INTO evidence_passages VALUES(?,?,?,?)",
                    (item.evidence_id, job_id, item.document_id, item.model_dump_json()),
                )
            if final:
                conn.execute("UPDATE research_runs SET outcome=? WHERE job_id=?", (canonical(payload), job_id))

    def ledger(self, scope, conn=None):
        if conn is None:
            with self.connect() as own:
                return self.ledger(scope, own)
        row = conn.execute(
            """SELECT COUNT(*) AS http_requests,
                      COALESCE(SUM(kind='search'),0) AS search_attempts,
                      COALESCE(SUM(seconds),0) + COALESCE(
                          (SELECT extra_active_seconds FROM research_runs WHERE job_id=?), 0
                      ) AS active_seconds,
                      COALESCE(SUM(expanded_bytes),0) AS expanded_bytes
                 FROM research_invocations WHERE scope=?""",
            (scope, scope),
        ).fetchone()
        return dict(row)

    def add_active(self, job_id, elapsed):
        with self.connect(write=True) as conn:
            conn.execute(
                "UPDATE research_runs SET extra_active_seconds=extra_active_seconds+? WHERE job_id=?",
                (elapsed, job_id),
            )

    def reserve(self, scope, invocation_id, kind, request_data, limits):
        hashed = digest(request_data)
        with self.connect(write=True) as conn:
            previous = conn.execute(
                "SELECT * FROM research_invocations WHERE scope=? AND invocation_id=?",
                (scope, invocation_id),
            ).fetchone()
            if previous:
                if previous["request_hash"] != hashed or previous["kind"] != kind:
                    raise ResearchError("IDEMPOTENCY_CONFLICT")
                return dict(previous)
            if limits is not None:
                counts = self.ledger(scope, conn)
                if (
                    counts["http_requests"] >= limits.max_http_requests
                    or counts["active_seconds"] >= limits.active_timeout_seconds
                    or (kind == "search" and counts["search_attempts"] >= limits.max_search_attempts)
                ):
                    raise ResearchError("RESEARCH_BUDGET_EXHAUSTED")
            conn.execute(
                """INSERT INTO research_invocations
                   (scope,invocation_id,kind,request_hash,state,started_at) VALUES(?,?,?,?,'reserved',?)""",
                (scope, invocation_id, kind, hashed, now().isoformat()),
            )
        return None

    def settle(self, scope, invocation_id, state, payload, seconds, expanded_bytes=0):
        with self.connect(write=True) as conn:
            conn.execute(
                """UPDATE research_invocations SET state=?,payload=?,seconds=?,expanded_bytes=?
                   WHERE scope=? AND invocation_id=? AND state='reserved'""",
                (state, canonical(payload), seconds, expanded_bytes, scope, invocation_id),
            )

    def lookup_document(self, job_id, url):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM source_documents WHERE job_id=? AND requested_url=?", (job_id, url),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def cached_documents(self, keys):
        if not keys:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM source_documents WHERE cache_key IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def documents_for_policy(self, policy_key):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM source_documents WHERE cache_key >= ? AND cache_key < ?",
                (policy_key + ":", policy_key + ";"),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_document(self, job_id, document, cache_key, text=None):
        if text is not None:
            data = text.encode("utf-8")
            hashed = hashlib.sha256(data).hexdigest()
            if hashed != document.text_sha256:
                raise ValueError("Document text hash mismatch")
            target = self.artifact_dir / (hashed + ".txt")
            if not target.exists():
                pending = self.artifact_dir / (str(uuid4()) + ".tmp")
                try:
                    with pending.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    pending.replace(target)
                finally:
                    pending.unlink(missing_ok=True)
        with self.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO source_documents VALUES(?,?,?,?,?)",
                (document.document_id, job_id, document.requested_url, cache_key, document.model_dump_json()),
            )

    def load_text(self, text_sha256):
        if not isinstance(text_sha256, str) or len(text_sha256) != 64:
            return None
        if any(c not in "0123456789abcdef" for c in text_sha256):
            return None
        try:
            data = (self.artifact_dir / (text_sha256 + ".txt")).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != text_sha256:
            return None
        return data.decode("utf-8")
