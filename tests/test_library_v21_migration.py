"""Exercise the guarded transition from an installed v2 state and its backup."""
import json
import sqlite3
from pathlib import Path
from shutil import copytree
from uuid import uuid4

import pytest

from p_mc_optimizer.llm.storage import LLM_SCHEMA
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.persistence import migrations
from p_mc_optimizer.persistence.migrations import MigrationError
from p_mc_optimizer.persistence.schema import BASE_SCHEMA


@pytest.fixture
def installed_v2(tmp_path):
    path = tmp_path / "state.sqlite3"
    job_id = str(uuid4())
    report = {"schema_version": "2.0", "job_id": job_id, "fixture": "TEST FIXTURE"}
    artifact = tmp_path / "artifacts" / job_id / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(report), encoding="utf-8")
    catalog = tmp_path / "catalog-artifacts"
    catalog.mkdir()
    (catalog / "existing.csv").write_text("TEST FIXTURE", encoding="utf-8")
    with sqlite3.connect(path) as db:
        db.executescript(BASE_SCHEMA + LLM_SCHEMA + "PRAGMA user_version=2;")
        migrations.install_v2_features(db)
        db.execute("CREATE TABLE job_v2_snapshots (job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), payload TEXT NOT NULL)")
        db.execute(
            """INSERT INTO jobs(job_id,input,policy,job_status,phase,created_at,updated_at)
               VALUES (?, '{}', '{"policy_version":"2.0"}', 'completed', 'reporting', 'a', 'a')""",
            (job_id,),
        )
        db.execute("INSERT INTO job_v2_snapshots VALUES (?, '{}')", (job_id,))
        db.execute("INSERT INTO reports VALUES (?, ?, '[]', 'a')", (job_id, json.dumps(report)))
        db.execute("INSERT INTO search_credentials(credential_ref, mode, generation) VALUES ('fixture', 'os_keyring', 4)")
    return path, report, artifact


def test_v2_migration_guards_data_and_preserves_backup(installed_v2):
    path, report, artifact = installed_v2
    root = path.parent
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET job_status='needs_input'")

    with pytest.raises(MigrationError, match="MIGRATION_REQUIRED"):
        Repository(root)
    with pytest.raises(MigrationError, match="MIGRATION_PENDING_V2"):
        Repository(root, allow_upgrade=True)
    assert not (root / "backups").exists()

    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET job_status='completed'")
    repo = Repository(root, allow_upgrade=True)
    assert repo.get_report(report["job_id"])["report"] == report
    assert repo.get_v2_snapshot(report["job_id"]) == {}
    with repo.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT generation FROM search_credentials").fetchone()[0] == 4
        db.execute("UPDATE jobs SET phase='refreshing_sources'")

    (backup,) = (root / "backups").glob("state-v2-*.sqlite3")
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(db.execute("SELECT payload FROM reports").fetchone()[0]) == report
    backup_assets = backup.with_suffix(".artifacts")
    assert (backup_assets / artifact.relative_to(root)).read_bytes() == artifact.read_bytes()
    assert (backup_assets / "catalog-artifacts" / "existing.csv").read_text() == "TEST FIXTURE"

    restored = root / "restored"
    copytree(backup_assets, restored)
    (restored / "state.sqlite3").write_bytes(backup.read_bytes())
    assert Repository(restored, allow_upgrade=True).get_report(report["job_id"])["report"] == report
    assert (restored / artifact.relative_to(root)).read_bytes() == artifact.read_bytes()
    assert Repository(root).get_report(report["job_id"])["report"] == report


def test_v2_migration_rolls_back_ddl_and_keeps_recovery_files(installed_v2, monkeypatch):
    path, report, artifact = installed_v2

    def fail_install(db):
        db.execute("CREATE TABLE incomplete_v3 (value TEXT)")
        raise MigrationError("TEST FIXTURE migration interrupted")

    monkeypatch.setattr(migrations, "install_v3_features", fail_install)
    with pytest.raises(MigrationError, match="migration interrupted"):
        Repository(path.parent, allow_upgrade=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='incomplete_v3'").fetchone() is None
        assert json.loads(db.execute("SELECT payload FROM reports").fetchone()[0]) == report
    assert artifact.is_file()
    assert len(list((path.parent / "backups").glob("state-v2-*.sqlite3"))) == 1


def test_schema_migration_without_fts5_keeps_other_routes_available(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(migrations, "fts5_available", lambda _: False)
    repo = Repository(tmp_path)
    with repo.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='library_fts'").fetchone() is None
        assert db.execute("SELECT COUNT(*) FROM library_versions").fetchone()[0] == 0
