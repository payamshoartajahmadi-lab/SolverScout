"""Transaction boundaries for the on-disk state format."""
from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from shutil import copytree, rmtree
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
    artifacts = destination.with_suffix(".artifacts")
    staging = destination.with_suffix(".artifacts.part")
    temporary.touch(mode=0o600, exist_ok=False)
    try:
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("SQLite backup failed its integrity check")
            backup.execute("PRAGMA journal_mode=DELETE")
        staging.mkdir(mode=0o700)
        for name in ("artifacts", "catalog-artifacts"):
            source_dir = path.parent / name
            if source_dir.is_dir():
                copytree(source_dir, staging / name, symlinks=True)
        staging.replace(artifacts)
        temporary.replace(destination)
    except BaseException:
        if artifacts.exists():
            rmtree(artifacts)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        if staging.exists():
            rmtree(staging)
    return destination


def install_v2_features(connection):
    from p_mc_optimizer.catalog import CATALOG_SCHEMA
    from p_mc_optimizer.research.storage import RESEARCH_SCHEMA

    execute_sql(connection, CATALOG_SCHEMA + "\n" + RESEARCH_SCHEMA)
    connection.execute("CREATE TABLE v2_features_installed (marker INTEGER PRIMARY KEY CHECK(marker=1))")
    connection.execute("INSERT INTO v2_features_installed VALUES (1)")


def v2_features_ready(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_features_installed'"
    ).fetchone() is not None


def fts5_available(connection):
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.library_fts_probe USING fts5(content)")
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such module: fts5":
            raise
        return False
    connection.execute("DROP TABLE temp.library_fts_probe")
    return True


def install_v3_features(connection):
    from p_mc_optimizer.research.library_schema import LIBRARY_FTS_SCHEMA, LIBRARY_SCHEMA

    execute_sql(connection, LIBRARY_SCHEMA)
    if fts5_available(connection):
        execute_sql(connection, LIBRARY_FTS_SCHEMA)


def install_v4_features(connection):
    from p_mc_optimizer.catalog.store import NUMERIC_POLICY_QUALIFICATION_SCHEMA

    columns = {row[1]: row for row in connection.execute("PRAGMA table_info(qualification_runs)")}
    if "scope" not in columns:
        connection.execute("""ALTER TABLE qualification_runs ADD COLUMN scope TEXT NOT NULL DEFAULT 'base'
                           CHECK(scope IN ('base','budget_horizon.v1'))""")
    elif columns["scope"][2:5] != ("TEXT", 1, "'base'"):
        raise MigrationError("Incompatible qualification_runs.scope column")
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='qualification_runs'").fetchone()[0]
    if not re.search(r"CHECK\s*\(\s*scope\s+IN\s*\(\s*'base'\s*,\s*'budget_horizon.v1'\s*\)\s*\)", definition, re.IGNORECASE):
        raise MigrationError("Incompatible qualification_runs.scope constraint")
    if "numeric_policy_snapshot" not in columns:
        connection.execute("ALTER TABLE qualification_runs ADD COLUMN numeric_policy_snapshot TEXT")
    elif columns["numeric_policy_snapshot"][2:5] != ("TEXT", 0, None):
        raise MigrationError("Incompatible qualification_runs.numeric_policy_snapshot column")
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='numeric_policy_qualification_reports'"
    ).fetchone()
    if present:
        expected = ["qualification_id", "config_id", "policy_hash", "environment", "report_hash", "payload", "created_at"]
        fields = list(connection.execute("PRAGMA table_info(numeric_policy_qualification_reports)"))
        if ([row[1] for row in fields] != expected
                or any(row[2] != "TEXT" or row[3] != 1 for row in fields)
                or [row[5] for row in fields] != [1, 2, 0, 0, 0, 0, 0]):
            raise MigrationError("Incompatible numeric policy qualification table")
        keys = {(row[2], row[3], row[4]) for row in connection.execute("PRAGMA foreign_key_list(numeric_policy_qualification_reports)")}
        if keys != {("qualification_runs", "qualification_id", "qualification_id"),
                    ("solver_configurations", "config_id", "config_id")}:
            raise MigrationError("Incompatible numeric policy qualification foreign keys")
        connection.execute("""CREATE INDEX IF NOT EXISTS numeric_policy_reports_lookup_idx
                           ON numeric_policy_qualification_reports(config_id,policy_hash,environment,created_at)""")
    else:
        execute_sql(connection, NUMERIC_POLICY_QUALIFICATION_SCHEMA)
    index = list(connection.execute("PRAGMA index_info(numeric_policy_reports_lookup_idx)"))
    if [row[2] for row in index] != ["config_id", "policy_hash", "environment", "created_at"]:
        raise MigrationError("Incompatible numeric policy qualification lookup index")


def require_idle_state(connection, version: int):
    blocked_jobs = connection.execute(
        """SELECT job_id, job_status FROM jobs WHERE job_status IN
           ('queued','running','needs_input','needs_configuration')
           OR lease_expires_at > ? ORDER BY job_id""",
        (datetime.now(UTC).isoformat(),),
    ).fetchall()
    unresolved = sum(connection.execute(
        f"""SELECT COUNT(*) FROM {table} WHERE reserved_active > 0 OR execution_status='running'"""
    ).fetchone()[0] for table in ("trial_attempts", "stage_executions"))
    has_qualifications = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qualification_runs'"
    ).fetchone()
    active = connection.execute(
        "SELECT COUNT(*) FROM qualification_runs WHERE status='running'"
    ).fetchone()[0] if has_qualifications else 0
    if blocked_jobs or unresolved or active:
        jobs = ", ".join(f"{row['job_id']} ({row['job_status']})" for row in blocked_jobs)
        raise MigrationError(
            f"MIGRATION_PENDING_V{version}: resolve jobs in v{version} first: {jobs or 'none'}; "
            f"unreconciled executions: {unresolved}; pending qualifications: {active}"
        )


def extend_job_phases(connection):
    _, jobs = BASE_SCHEMA.split("CREATE TABLE jobs (", 1)
    definition = jobs.split("CREATE INDEX jobs_claim_idx", 1)[0]
    definition = definition.replace(
        "'profiling','planning'",
        "'refreshing_sources','profiling','research_planning','searching','fetching_evidence','planning'",
    )
    execute_sql(connection, "CREATE TABLE jobs_next (" + definition)
    connection.execute("INSERT INTO jobs_next SELECT * FROM jobs")
    connection.execute("DROP TABLE jobs")
    connection.execute("ALTER TABLE jobs_next RENAME TO jobs")
    connection.execute("CREATE INDEX jobs_claim_idx ON jobs(job_status, created_at, job_id)")


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
                connection.rollback()
                return
            if version not in (0, 1, 2, 3):
                raise MigrationError(f"Unsupported state.sqlite3 schema version {version}")
            if version:
                if not allow_upgrade:
                    raise MigrationError("MIGRATION_REQUIRED: run p_mc_optimizer migrate with the app stopped")
                require_idle_state(connection, version)
                backup_database(path, version)
            else:
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if existing:
                    backup_database(path, 0)
                execute_sql(connection, BASE_SCHEMA + "\n" + LLM_SCHEMA)

            if version < 3:
                extend_job_phases(connection)
                connection.execute("""CREATE TABLE IF NOT EXISTS job_v2_snapshots (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    payload TEXT NOT NULL
                )""")
                if not v2_features_ready(connection):
                    install_v2_features(connection)
                install_v3_features(connection)
            install_v4_features(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationError("Migration would leave invalid foreign keys")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
