import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.persistence.repository import Repository
from p_mc_optimizer.research.contracts import SourceDocument, SourceRule, SourceSnapshot
from p_mc_optimizer.research.library_retrieval import LibraryRetriever, alias_present
from p_mc_optimizer.research.library_store import LibraryEntry, LibraryStore
from p_mc_optimizer.research.storage import ResearchStore

CATALOG = {
    f"mealpy:{family}:{variant}": (variant,)
    for family, variant in [("de", "OriginalDE"), ("gwo", "OriginalGWO"), ("woa", "OriginalWOA")]
}
PLAN = {
    "schema_version": "2.1",
    "queries": [
        {"query_id": "variants", "text": "bounded continuous optimization", "source_ids": ["methods"],
         "purpose": "implementation_evidence", "implementation_ids": list(CATALOG)},
        {"query_id": "discovery", "text": "bounded continuous optimization", "source_ids": ["methods"],
         "purpose": "method_discovery", "implementation_ids": []},
    ],
}


@pytest.fixture
def library(tmp_path):
    repo = Repository(tmp_path)
    store = LibraryStore(ResearchStore(repo.path, tmp_path / "research"))
    snapshot = SourceSnapshot(revision=1, sources=(SourceRule(
        source_id="methods", label="Methods", hostname="methods.example.test",
        path_prefixes=("/manual",), source_kind="official_docs", enabled=True,
    ),))
    with store.connect(write=True) as conn:
        conn.execute("INSERT INTO research_source_revisions VALUES (?, ?)", (1, snapshot.model_dump_json()))
    return store, LibraryRetriever(store), snapshot


def _job(store):
    identifier = str(uuid4())
    with store.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,input,policy,job_status,phase,created_at,updated_at) "
            "VALUES (?, '{}', '{}', 'queued', 'intake', 'a', 'a')", (identifier,),
        )
    return identifier


def _entry(url, title, text, *, timestamp=None, status="read"):
    timestamp = timestamp or datetime.now(UTC)
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return LibraryEntry(SourceDocument(
        document_id=str(uuid4()), source_id="methods", requested_url=url, final_url=url,
        fetched_at=timestamp, validated_at=timestamp, content_type="text/plain", content_level="documentation",
        parser_version="fixture-v1", bytes_sha256=digest, text_sha256=digest, fetch_status=status,
    ), text, data, title=title)


def _fixture():
    return json.loads((Path(__file__).parent / "fixtures/library_v21_corpus.json").read_text())


def test_catalog_targeted_passages_preserve_original_offsets_and_job_snapshot(library):
    store, retriever, snapshot = library
    fixture = _fixture()["positive"]
    store.publish_documents([_entry(row["url"], row["title"], row["text"]) for row in fixture], snapshot)
    job = _job(store)
    result = retriever.retrieve(job, PLAN, snapshot, aliases=CATALOG)
    assert result["status"] == "completed"
    expected = {row["url"]: row for row in fixture}
    assert {row["canonical_url"] for row in result["documents"]} == set(expected)
    assert {row["text"] for row in result["evidence"]}.issuperset(
        row["reference"]["text"] for row in fixture
    )
    for passage in result["evidence"]:
        original = expected[passage["url"]]
        assert original["text"][passage["start"]:passage["end"]] == passage["text"]
        document = next(row for row in result["documents"] if row["observation_id"] == passage["observation_id"])
        assert document["text_sha256"] == original["text_sha256"]
    original = fixture[0]
    store.publish_documents([_entry(original["url"], "New edition", "Only the new edition.")], snapshot)
    revoked = SourceSnapshot(revision=2, sources=(snapshot.sources[0].model_copy(update={"enabled": False}),))
    with store.connect(write=True) as conn:
        conn.execute("INSERT INTO research_source_revisions VALUES (?, ?)", (2, revoked.model_dump_json()))
    assert retriever.retrieve(job, PLAN, snapshot, aliases=CATALOG) == result
    assert retriever.search("OriginalDE", snapshot) == []


def test_stale_evidence_requires_explicit_policy_and_retains_exact_validation_time(library):
    store, retriever, snapshot = library
    timestamp = datetime.now(UTC) - timedelta(days=10)
    row = _fixture()["positive"][0]
    store.publish_documents([_entry(row["url"], row["title"], row["text"], timestamp=timestamp)], snapshot)
    with pytest.raises(ApiError, match="LIBRARY_EVIDENCE_STALE"):
        retriever.readiness(snapshot)
    assert retriever.readiness(snapshot, stale_policy="allow_with_warning")
    result = retriever.retrieve(_job(store), PLAN, snapshot, stale_policy="allow_with_warning", aliases=CATALOG)
    assert result["documents"][0]["validated_at"] == timestamp.isoformat(timespec="microseconds")
    assert all(item["stale"] for item in result["evidence"])


def test_web_evidence_is_limited_to_its_observations_even_when_version_is_shared(library):
    store, retriever, snapshot = library
    row, unrelated = _fixture()["positive"][:2]
    stale, _ = store.publish_documents([
        _entry(row["url"], row["title"], row["text"], timestamp=datetime.now(UTC) - timedelta(days=10)),
        _entry(unrelated["url"], unrelated["title"], unrelated["text"]),
    ], snapshot)
    job = _job(store)
    fresh, = store.publish_documents([
        _entry(row["url"], row["title"], row["text"], status="revalidated"),
    ], snapshot, owner_job_id=job)
    assert fresh["version_id"] == stale["version_id"]
    result = retriever.retrieve(job, PLAN, snapshot, eligible_observation_ids=[fresh["observation_id"]], aliases=CATALOG)
    assert [doc["observation_id"] for doc in result["documents"]] == [fresh["observation_id"]]
    assert result["documents"][0]["fetch_status"] == "revalidated"


def test_search_treats_fts_operators_as_text_and_normalizes_without_altering_passages(library):
    store, retriever, snapshot = library
    original = "آزمايش كاربردي الگوریتم تکامل تفاضلی"
    observation, = store.publish_documents([
        _entry("https://methods.example.test/manual/fa", "آزمایش", original),
    ], snapshot)
    row, = retriever.search('title:"کاربردی" NEAR(آزمایش)', snapshot)
    assert row["preview"] == original
    assert retriever.search("'; DROP TABLE library_versions; --", snapshot) == []
    assert retriever.versions(row["document_id"], snapshot)[0]["observations"][0]["observation_id"] == observation["observation_id"]
    assert alias_present("Using (DE) produces a candidate.", "DE")
    assert not alias_present("A field of de facto expertise.", "DE")
    assert not alias_present("OriginalDEA is a different variant.", "OriginalDE")


def test_retrieval_filters_corrupted_artifact_and_freezes_empty_result(library):
    store, retriever, snapshot = library
    row = _fixture()["positive"][0]
    observation, = store.publish_documents([_entry(row["url"], row["title"], row["text"])], snapshot)
    (store.artifact_dir / (observation["document"]["text_sha256"] + ".txt")).write_text("corrupt")
    job = _job(store)
    result = retriever.retrieve(job, PLAN, snapshot, aliases=CATALOG)
    assert result["status"] == "insufficient_evidence"
    assert "LIBRARY_ARTIFACT_MISSING" in result["reason_codes"]
    assert not result["documents"]
    store.publish_documents([_entry(row["url"], row["title"], row["text"])], snapshot)
    assert retriever.retrieve(job, PLAN, snapshot, aliases=CATALOG) == result
