"""Transaction boundaries for the on-disk state format."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from p_mc_optimizer.persistence.schema import BASE_SCHEMA, SCHEMA_VERSION


class MigrationError(RuntimeError):
    pass


def execute_sql(connection, script):
    """Execute complete statements inside the caller's transaction."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        raise MigrationError("Incomplete SQL migration")


def backup_database(path: Path, version: int) -> Path:
    directory = path.parent / "backups"
    directory.mkdir(exist_ok=True, mode=0o700)
    destination = directory / f"state-v{version}-{uuid4()}.sqlite3"
    temporary = destination.with_suffix(".part")
    temporary.touch(mode=0o600, exist_ok=False)
    try:
        with sqlite3.connect(path) as source, sqlite3.connect(temporary) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("SQLite backup failed its integrity check")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def migrate(path: Path, connect, *, allow_upgrade: bool):
    from p_mc_optimizer.llm.storage import LLM_SCHEMA

    with connect() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        # The jobs table must be rebuilt to extend its phase CHECK.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version not in (0, 1):
                raise MigrationError(f"Unsupported state.sqlite3 schema version {version}")
            if version == 1:
                if not allow_upgrade:
                    raise MigrationError("MIGRATION_REQUIRED: run p_mc_optimizer migrate with the app stopped")
                blocked_jobs = connection.execute(
                    """SELECT job_id, job_status FROM jobs WHERE job_status IN
                       ('queued','running','needs_input','needs_configuration') ORDER BY job_id"""
                ).fetchall()
                unresolved = sum(connection.execute(
                    f"""SELECT COUNT(*) FROM {table} WHERE reserved_active > 0 OR execution_status='running'"""
                ).fetchone()[0] for table in ("trial_attempts", "stage_executions"))
                if blocked_jobs or unresolved:
                    jobs = ", ".join(f"{row['job_id']} ({row['job_status']})" for row in blocked_jobs)
                    raise MigrationError(
                        f"MIGRATION_PENDING_V1: resolve jobs in v1 first: {jobs or 'none'}; "
                        f"unreconciled executions: {unresolved}"
                    )
                backup_database(path, 1)
            else:
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if existing:
                    backup_database(path, 0)
                execute_sql(connection, BASE_SCHEMA + "\n" + LLM_SCHEMA)

            _, jobs = BASE_SCHEMA.split("CREATE TABLE jobs (", 1)
            definition = jobs.split("CREATE INDEX jobs_claim_idx", 1)[0]
            definition = definition.replace(
                "'profiling','planning'", "'profiling','research_planning','searching','fetching_evidence','planning'"
            )
            execute_sql(connection, "CREATE TABLE jobs_v2 (" + definition)
            connection.execute("INSERT INTO jobs_v2 SELECT * FROM jobs")
            connection.execute("DROP TABLE jobs")
            connection.execute("ALTER TABLE jobs_v2 RENAME TO jobs")
            connection.execute("CREATE INDEX jobs_claim_idx ON jobs(job_status, created_at, job_id)")
            connection.execute("""CREATE TABLE job_v2_snapshots (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                payload TEXT NOT NULL
            )""")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationError("Migration would leave invalid foreign keys")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
