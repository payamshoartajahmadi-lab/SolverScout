import sqlite3
from uuid import uuid4

import pytest

from p_mc_optimizer.llm.storage import LLM_SCHEMA
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence.migrations import MigrationError
from p_mc_optimizer.persistence.schema import BASE_SCHEMA


def test_upgrade_keeps_v1_report_and_blocks_pending_jobs(tmp_path):
    path = tmp_path / "state.sqlite3"
    job_id = str(uuid4())
    with sqlite3.connect(path) as db:
        db.executescript(BASE_SCHEMA + LLM_SCHEMA + "PRAGMA user_version=1;")
        db.execute(
            """INSERT INTO jobs(job_id,input,policy,job_status,phase,created_at,updated_at)
               VALUES (?, '{}', '{}', 'needs_input', 'intake', 'a', 'a')""",
            (job_id,),
        )
        db.execute("INSERT INTO reports VALUES (?, ?, '[]', 'a')", (job_id, '{"schema_version":"1.0"}'))

    with pytest.raises(MigrationError, match="MIGRATION_REQUIRED"):
        Repository(tmp_path)
    with pytest.raises(MigrationError, match=job_id):
        Repository(tmp_path, allow_upgrade=True)
    assert not (tmp_path / "backups").exists()

    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET job_status='cancelled'")
        db.execute(
            """INSERT INTO stage_executions(
              stage_execution_id,job_id,stage,evaluation_quota,deadline_seconds,execution_status,
              reserved_active,charged_unknown,started_at)
              VALUES ('fixture',?,'preflight',8,1,'unknown_outcome',0,8,'a')""",
            (job_id,),
        )

    repo = Repository(tmp_path, allow_upgrade=True)
    backups = list((tmp_path / "backups").glob("state-v1-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with repo.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT payload FROM reports").fetchone()[0] == '{"schema_version":"1.0"}'
        assert db.execute("SELECT charged_unknown FROM stage_executions").fetchone()[0] == 8
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("UPDATE jobs SET phase='research_planning'")
