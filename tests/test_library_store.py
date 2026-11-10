"""Document identity, publication and legacy import in the source library."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from p_mc_optimizer.__main__ import main
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.research.contracts import SourceDocument, SourceRule, SourceSnapshot
from p_mc_optimizer.research.library_store import LibraryEntry, LibraryStore
from p_mc_optimizer.research.storage import ResearchStore, canonical
from p_mc_optimizer.research.transport import ResearchError

URL = "https://methods.example.org/manual/fixture"
TIME = datetime(2025, 1, 2, tzinfo=UTC)


@pytest.fixture
def library(tmp_path):
    repo = Repository(tmp_path)
    research = ResearchStore(repo.path, repo.data_dir / "artifacts" / "research")
    library = LibraryStore(research)
    sources = SourceSnapshot(revision=1, sources=(SourceRule(
        source_id="methods", label="TEST FIXTURE", hostname="methods.example.org",
        path_prefixes=("/manual",), source_kind="official_docs", enabled=True,
    ),))
    with repo.transaction() as db:
        db.execute("INSERT INTO research_source_revisions VALUES(?,?)", (1, sources.model_dump_json()))
    return repo, library, sources


def entry(text="TEST FIXTURE", *, status=200, when=TIME, url=URL):
    if status >= 400:
        return LibraryEntry(
            SourceDocument(
                document_id=uuid4().hex, source_id="methods", requested_url=url,
                fetch_status="failed", parser_version="test-fixture",
                reason_code="SOURCE_UNREACHABLE",
            ),
            None, http_status=status, observed_at=when.isoformat(),
        )
    raw = text.encode()
    hashed = sha256(raw).hexdigest()
    return LibraryEntry(
        SourceDocument(
            document_id=uuid4().hex, source_id="methods", requested_url=url, final_url=url,
            fetch_status="revalidated" if status == 304 else "read", parser_version="test-fixture",
            fetched_at=TIME, validated_at=when, content_type="text/plain", content_level="full_text",
            bytes_sha256=hashed, text_sha256=hashed,
        ),
        text, raw, title="TEST FIXTURE", http_status=status,
    )


def test_revalidation_and_failures_keep_last_success_and_version(library):
    _, store, snapshot = library
    original = entry()
    same = entry(when=TIME + timedelta(hours=1))
    revalidated = entry(status=304, when=TIME + timedelta(hours=2))
    timeout = entry(status=503, when=TIME + timedelta(hours=3))
    gone = entry(status=404, when=TIME + timedelta(hours=4))
    created = [store.publish_documents([item], snapshot)[0] for item in (original, same, revalidated, timeout, gone)]
    assert len({row["version_id"] for row in created[:3]}) == 1
    assert len({row["observation_id"] for row in created}) == 5
    document, = store.list_documents()
    assert document["last_attempt_observation_id"] == created[-1]["observation_id"]
    assert document["observation_id"] == created[2]["observation_id"]
    assert document["last_successful_validation_at"] == created[2]["validated_at"]
    assert document["remote_unavailable"] and document["version"]["version_id"] == created[0]["version_id"]
    assert store.verify_artifacts(document["version"]) == original.text
    updated = store.publish_documents([entry("Updated TEST FIXTURE", when=TIME + timedelta(hours=5))], snapshot)[0]
    assert updated["version_id"] != created[0]["version_id"]
    assert not store.list_documents()[0]["remote_unavailable"]


def test_artifact_failure_keeps_generation_atomic_and_prior_content(library):
    repo, store, snapshot = library
    original = entry()
    store.publish_documents([original], snapshot)
    with repo.connect() as db:
        generation = store.generation(db)
    bad = replace(entry("Different TEST FIXTURE", url=URL + "/bad"), raw=b"WRONG HASH")
    with pytest.raises(ResearchError, match="LIBRARY_ARTIFACT_MISSING"):
        store.publish_documents([entry("unpublished", url=URL + "/staging"), bad], snapshot)
    with repo.connect() as db:
        assert store.generation(db) == generation
        assert db.execute("SELECT COUNT(*) FROM library_versions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0] == 1
    store.purge_unreferenced()
    assert store.verify_artifacts(store.list_documents()[0]["version"]) == original.text
    store.set_storage_limit(store.storage_usage()["used_bytes"])
    with pytest.raises(ResearchError, match="LIBRARY_STORAGE_FULL"):
        store.publish_documents([entry("Additional TEST FIXTURE")], snapshot)
    assert store.verify_artifacts(store.list_documents()[0]["version"]) == original.text


def test_repeated_legacy_import_keeps_provenance_and_old_validated_at(library):
    repo, store, snapshot = library
    identifier = str(uuid4())
    with repo.transaction() as db:
        db.execute(
            """INSERT INTO jobs(job_id,input,policy,job_status,phase,created_at,updated_at)
               VALUES (?, '{}', '{"policy_version":"2.0"}', 'completed', 'reporting', 'a', 'a')""",
            (identifier,),
        )
        db.execute("INSERT INTO job_v2_snapshots VALUES (?,?)", (
            identifier, canonical({"research": {"source_snapshot": snapshot.model_dump(mode="json")}}),
        ))
    original = entry()
    store.research_store.begin_run(identifier, {})
    store.research_store.save_document(identifier, original.document, "fixture-cache-key", original.text)
    store.import_legacy()
    store.import_legacy()
    document, = store.list_documents()
    version, = store.document_versions(document["document_id"])
    assert version["observation"]["discovery"]["kind"] == "legacy_import"
    assert version["observation"]["validated_at"] == TIME.isoformat(timespec="microseconds")
    assert not version["version"]["raw_available"]
    assert store.verify_artifacts(version["version"]) == original.text
    with repo.connect() as db:
        assert store.generation(db) == 1
        assert db.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1


def test_reindex_builds_fts_from_verified_artifacts(library, capsys):
    repo, store, snapshot = library
    good = entry("Verified TEST FIXTURE")
    bad = entry("Corrupted TEST FIXTURE", url=URL + "/invalid")
    observations = store.publish_documents([good, bad], snapshot)
    (store.artifact_dir / (bad.document.text_sha256 + ".txt")).write_bytes(b"INVALID FIXTURE")
    with repo.transaction() as db:
        db.execute("DROP TABLE library_fts")
    for _ in range(2):
        assert main(["library-reindex", "--data-dir", str(repo.data_dir)]) == 0
        assert json.loads(capsys.readouterr().out) == {"indexed_versions": 1, "skipped_invalid_versions": 1}
    with repo.connect() as db:
        assert db.execute("SELECT version_id FROM library_fts").fetchone()[0] == observations[0]["version_id"]
        assert db.execute("SELECT COUNT(*) FROM library_versions").fetchone()[0] == 2
