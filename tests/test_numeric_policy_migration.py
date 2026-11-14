"""Incremental v3->v4 migration and atomic rollback without changing legacy jobs."""
import json
import sqlite3
from uuid import uuid4

import pytest

from p_mc_optimizer.persistence import Repository, migrations
from p_mc_optimizer.persistence.migrations import MigrationError


@pytest.fixture
def installed_v3(tmp_path):
    repo = Repository(tmp_path)
    job_id = str(uuid4())
    with repo.connect() as db:
        db.execute("DROP TABLE numeric_policy_qualification_reports")
        db.execute("ALTER TABLE qualification_runs DROP COLUMN scope")
        db.execute("ALTER TABLE qualification_runs DROP COLUMN numeric_policy_snapshot")
        db.execute("PRAGMA user_version=3")
        db.execute("""INSERT INTO jobs(job_id,input,policy,job_status,phase,workflow,created_at,updated_at)
                   VALUES (?, '{}','{}','completed','reporting','{"legacy":true}','a','a')""", (job_id,))
        db.execute("INSERT INTO reports VALUES (?, '{" + '"legacy":true' + "}', '[]', 'a')", (job_id,))
    return repo.path, job_id


def test_incremental_v3_upgrade_preserves_rows_and_uses_only_new_step(installed_v3, monkeypatch):
    path, job_id = installed_v3

    def forbidden(_):
        pytest.fail("A v3 database re-ran a historical migration step")

    monkeypatch.setattr(migrations, "extend_job_phases", forbidden)
    monkeypatch.setattr(migrations, "install_v3_features", forbidden)
    repo = Repository(path.parent, allow_upgrade=True)
    with repo.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert json.loads(db.execute("SELECT workflow FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]) == {"legacy": True}
        assert db.execute("SELECT COUNT(*) FROM numeric_policy_qualification_reports").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list((path.parent / "backups").glob("state-v3-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "scope" not in {r[1] for r in db.execute("PRAGMA table_info(qualification_runs)")}
    assert repo.get_report(job_id)["report"] == {"legacy": True}
    Repository(path.parent)
    assert len(list((path.parent / "backups").glob("state-v3-*.sqlite3"))) == 1


def test_v3_guard_does_not_modify_active_work(installed_v3):
    path, job_id = installed_v3
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET job_status='needs_input'")
    with pytest.raises(MigrationError, match="MIGRATION_PENDING_V3"):
        Repository(path.parent, allow_upgrade=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT job_status FROM jobs").fetchone()[0] == "needs_input"
    assert not (path.parent / "backups").exists()


def test_v4_ddl_failure_rolls_back_and_preserves_recovery_backup(installed_v3, monkeypatch):
    path, _ = installed_v3
    install = migrations.install_v4_features

    def interrupted(db):
        install(db)
        raise MigrationError("TEST FIXTURE interrupted v4")

    monkeypatch.setattr(migrations, "install_v4_features", interrupted)
    with pytest.raises(MigrationError, match="interrupted v4"):
        Repository(path.parent, allow_upgrade=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "scope" not in {r[1] for r in db.execute("PRAGMA table_info(qualification_runs)")}
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='numeric_policy_qualification_reports'").fetchone() is None
    assert len(list((path.parent / "backups").glob("state-v3-*.sqlite3"))) == 1


def test_v4_install_is_idempotent_and_rejects_incompatible_shape(tmp_path):
    repo = Repository(tmp_path)
    with repo.connect() as db:
        migrations.install_v4_features(db)
        migrations.install_v4_features(db)
        db.execute("DROP TABLE numeric_policy_qualification_reports")
        db.execute("CREATE TABLE numeric_policy_qualification_reports (qualification_id TEXT)")
        with pytest.raises(MigrationError, match="Incompatible"):
            migrations.install_v4_features(db)
