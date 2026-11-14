"""Direct refresh coverage, lease recovery and library publication contracts."""

from concurrent.futures import ThreadPoolExecutor
import gzip
from uuid import uuid4

import httpx
import pytest

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.persistence import Repository
from p_mc_optimizer.research.contracts import SourceRule, SourceSnapshot
from p_mc_optimizer.research.contracts_v21 import LibrarySourceUpdate, SourceSyncRequest, SyncLimits
from p_mc_optimizer.research.library_retrieval import LibraryRetriever
from p_mc_optimizer.research.library_store import LibraryStore
from p_mc_optimizer.research.library_sync import SourceSynchronizer
from p_mc_optimizer.research.storage import ResearchStore, now
from p_mc_optimizer.research.transport import ResearchError, Response


BASE = "https://docs.example.org"
SEED = BASE + "/guide/start"
MAP = BASE + "/sitemap.xml.gz"
LINK = BASE + "/guide/link"
DISCOVERED = BASE + "/guide/sitemap-method"
SECRET = "fixture_key_hidden_12345"


@pytest.fixture
def library(tmp_path):
    repo = Repository(tmp_path)
    storage = ResearchStore(repo.path, tmp_path / "research")

    def sanitize(value):
        if isinstance(value, str):
            return value.replace(SECRET, "[REDACTED]")
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(sanitize(item) for item in value)
        return value

    store = LibraryStore(storage, sanitize=sanitize)
    rule = SourceRule(
        source_id="docs", hostname="docs.example.org", label="Test documentation",
        path_prefixes=("/guide",), source_kind="official_docs", enabled=True,
    )
    with store.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO research_source_revisions VALUES(?,?)",
            (1, SourceSnapshot(revision=1, sources=(rule,)).model_dump_json()),
        )
    return store


def response(status=200, body=b"", **headers):
    return Response(status, httpx.Headers(headers), body)


class Transport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout, size, conditional):
        assert timeout > 0 and size > 0
        self.calls.append((url, conditional))
        if url == BASE + "/robots.txt":
            return response(404)
        value = self.pages[url]
        return value(conditional) if callable(value) else value


def html(text, **headers):
    return response(200, text.encode(), **{"content-type": "text/html", **headers})


def refresh(library, transport, *, limits=None):
    worker = SourceSynchronizer(library, transport)
    request = SourceSyncRequest(source_ids=["docs"], limits=limits or SyncLimits())
    queued = worker.start_sync(request, str(uuid4()))
    result = worker.run_next("worker")
    assert result["run_id"] == queued["run_id"]
    return result


def test_seed_sitemap_and_links_are_bounded_to_approved_paths(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED], sitemap_urls=[MAP]))
    transport = Transport({
        SEED: html(f'<title>Starting page</title><p>Optimization</p><a href="{LINK}">Method</a>'
                   '<a href="/private">Outside source</a><a href="/guide/start#section">Duplicate</a>'),
        MAP: response(200, gzip.compress(
            f"<urlset><url><loc>{DISCOVERED}</loc></url><url><loc>{BASE}/private</loc></url></urlset>".encode(),
        )),
        DISCOVERED: html("<p>Sitemap method</p>"),
        LINK: html("<p>Linked method</p><a href='/guide/deeper'>Past configured depth</a>"),
    })
    run = refresh(library, transport)
    assert run["status"] == "completed", run
    assert [url for url, _ in transport.calls] == [BASE + "/robots.txt", SEED, MAP, DISCOVERED, LINK]
    assert run["usage"]["http_requests"] == 5
    assert run["sources"][0]["new"] == 3
    assert run["sources"][0]["skipped_urls"] == 2
    docs = library.list_documents()
    assert {doc["canonical_url"] for doc in docs} == {SEED, LINK, DISCOVERED}
    doc = next(doc for doc in docs if doc["canonical_url"] == SEED)
    assert doc["version"]["title"] == "Starting page"
    assert "Optimization" in library.verify_artifacts(doc["version"])
    with library.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM library_fts").fetchone()[0] == 3


def test_revalidation_missing_artifact_and_remote_removal(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    transport = Transport({SEED: html("<p>First version</p>", etag='"test"')})
    first = refresh(library, transport)
    before = library.list_documents()[0]
    transport.pages[SEED] = lambda headers: response(304) if headers == {"If-None-Match": '"test"'} else response(500)
    again = refresh(library, transport)
    after = library.list_documents()[0]
    assert first["status"] == again["status"] == "completed"
    assert again["sources"][0]["unchanged"] == 1
    assert before["version"] == after["version"]
    assert before["last_successful_validation_at"] < after["last_successful_validation_at"]
    raw = library.artifact_dir / (after["version"]["bytes_sha256"] + ".bin")
    raw.unlink()
    failed = refresh(library, transport)
    assert failed["status"] == "failed"
    assert "LIBRARY_ARTIFACT_MISSING" in failed["reason_codes"]
    assert library.list_documents()[0]["last_successful_validation_at"] == after["last_successful_validation_at"]
    transport.pages[SEED] = response(410)
    removed = refresh(library, transport)
    assert removed["status"] == "failed"
    old = library.list_documents()[0]
    assert old["remote_unavailable"] == 1 and old["version"] == after["version"]
    assert library.document_versions(old["document_id"])[0]["observation"]["http_status"] == 410


def test_rejects_unsafe_sitemap_and_restricted_redirect(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED], sitemap_urls=[MAP]))
    transport = Transport({
        SEED: response(302, location="https://127.0.0.1/secret"),
        MAP: response(200, b'<!DOCTYPE urlset [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset>&e;</urlset>'),
    })
    run = refresh(library, transport)
    assert run["status"] == "failed"
    assert set(run["reason_codes"]) == {"SOURCE_NOT_ALLOWED", "SOURCE_SITEMAP_INVALID"}
    assert [url for url, _ in transport.calls] == [BASE + "/robots.txt", SEED, MAP]


@pytest.mark.parametrize("unusable", [
    response(403), response(429), response(200, b"invalid pdf", **{"content-type": "application/pdf"}),
])
def test_failed_refresh_retains_the_last_usable_version(library, unusable):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    transport = Transport({SEED: html("<p>Verified content</p>")})
    assert refresh(library, transport)["status"] == "completed"
    before = library.list_documents()[0]
    transport.pages[SEED] = unusable
    assert refresh(library, transport)["status"] == "failed"
    after = library.list_documents()[0]
    assert after["version"] == before["version"]
    assert after["last_successful_validation_at"] == before["last_successful_validation_at"]


def test_truncated_tail_is_not_searchable_as_evidence(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    transport = Transport({SEED: html("<p>" + "general overview " * 100 + "</p><p>OriginalWOA</p>")})
    assert refresh(library, transport, limits=SyncLimits(max_text_chars=128))["status"] == "completed"
    version = library.list_documents()[0]["version"]
    assert version["truncated"] is True
    assert "OriginalWOA" not in library.verify_artifacts(version)
    with library.connect() as connection:
        snapshot = library.source_snapshot(connection)
    assert LibraryRetriever(library).search("OriginalWOA", snapshot) == []


@pytest.mark.parametrize("unexpected_304", [False, True])
def test_parser_upgrade_never_relabels_old_cached_text(library, monkeypatch, unexpected_304):
    from p_mc_optimizer.research import fetch
    from p_mc_optimizer.research.extraction import extraction_profile

    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    content = '<dl class="py class"><dt id="mealpy.swarm_based.GWO.OriginalGWO">class OriginalGWO()</dt>' \
              '<dd><p>The original Grey Wolf Optimizer population description.</p></dd></dl>'
    transport = Transport({SEED: html(content, etag='"old"')})
    with monkeypatch.context() as old_parser:
        old_parser.setattr(fetch, "extraction_profile", lambda limit: "legacy-prefix-parser")
        old_parser.setattr(fetch, "extract", lambda *args, **kwargs: {"text":"Old contents list OriginalGWO", "truncated":True})
        assert refresh(library, transport)["status"] == "completed"
    before = library.list_documents()[0]
    original_version = before["version"]
    transport.pages[SEED] = lambda headers: response(304) if unexpected_304 or headers else html(content, etag='"old"')
    result = refresh(library, transport)
    after = library.list_documents()[0]
    assert transport.calls[-1] == (SEED, {})
    assert library.load_raw(original_version["bytes_sha256"])
    if unexpected_304:
        assert result["status"] == "failed"
        assert "SOURCE_PARSER_REEXTRACTION_REQUIRED" in result["reason_codes"]
        assert after["version"] == original_version
        assert after["last_successful_validation_at"] == before["last_successful_validation_at"]
    else:
        assert result["status"] == "completed"
        assert after["version"]["parser_version"] == extraction_profile(SyncLimits().max_text_chars)
        assert after["version"]["version_id"] != original_version["version_id"]
        assert "population description" in library.verify_artifacts(after["version"])
        assert "Old contents" not in library.verify_artifacts(after["version"])
        assert any(row["version"] == original_version for row in library.document_versions(after["document_id"]))


def test_extraction_limit_change_requires_full_get_before_publishing_a_new_profile(library):
    from p_mc_optimizer.research.extraction import extraction_profile

    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    content = "<p>" + "early introduction " * 30 + "</p><p>Late target OriginalWOA description.</p>"
    transport = Transport({SEED: html(content, etag='"same-bytes"')})
    assert refresh(library, transport, limits=SyncLimits(max_text_chars=128))["status"] == "completed"
    before = library.list_documents()[0]["version"]
    assert "OriginalWOA" not in library.verify_artifacts(before)
    transport.pages[SEED] = lambda headers: response(304) if headers else html(content, etag='"same-bytes"')
    assert refresh(library, transport, limits=SyncLimits(max_text_chars=2048))["status"] == "completed"
    after = library.list_documents()[0]["version"]
    assert transport.calls[-1] == (SEED, {})
    assert after["parser_version"] == extraction_profile(2048)
    assert before["parser_version"] == extraction_profile(128)
    assert before["bytes_sha256"] == after["bytes_sha256"]
    assert before["text_sha256"] != after["text_sha256"]
    assert "OriginalWOA" in library.verify_artifacts(after)


def test_ledger_caps_and_unknown_bytes(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    transport = Transport({SEED: html("<p>Optimization</p>")})
    run = refresh(library, transport, limits=SyncLimits(max_http_requests_per_source=1))
    assert run["status"] == "failed"
    assert "SYNC_BUDGET_EXHAUSTED" in run["reason_codes"]
    assert run["usage"]["http_requests"] == 1
    assert [url for url, _ in transport.calls] == [BASE + "/robots.txt"]

    def timeout(_headers):
        raise ResearchError("SOURCE_UNREACHABLE")

    transport.pages[SEED] = timeout
    run = refresh(library, transport, limits=SyncLimits(max_document_bytes=10, max_total_bytes=10))
    assert run["status"] == "failed"
    assert run["usage"]["http_requests"] == 1
    assert run["usage"]["unknown_bytes"] == 10
    assert run["usage"]["reserved_bytes"] == 0


def test_sync_overlap_idempotency_and_lease_recovery(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    sync = SourceSynchronizer(library, Transport({}))
    req = SourceSyncRequest(source_ids=["docs"])
    first = sync.start_sync(req, "first")
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.map(lambda _: sync.start_sync(req, "first"), range(2))
    assert one == two == first
    with pytest.raises(ApiError) as err:
        sync.start_sync(req, "new")
    assert err.value.error_code == "SOURCE_SYNC_IN_PROGRESS"
    sync.cancel_owned_sync(first["run_id"], "unrelated-job")
    assert sync.sync_status(first["run_id"])["status"] == "queued"

    claimed = sync.claim_sync("crashed")
    with library.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO source_sync_invocations
               (run_id,source_id,url,kind,state,started_at,reserved_bytes,reserved_seconds)
               VALUES(?, 'docs', ?, 'fetch', 'reserved', ?, 50, 20)""",
            (claimed["run_id"], SEED, now().isoformat()),
        )
    assert sync.recover_sync(force=True) == 1
    ended = sync.sync_status(first["run_id"])
    assert ended["status"] == "interrupted" and ended["usage"]["unknown_bytes"] == 50
    assert ended["usage"]["http_requests"] == 1
    assert sync.run_next("replacement") is None
    assert sync.start_sync(req, "new")["status"] == "queued"


def test_cancellation_revocation_and_raw_secret_do_not_publish(library):
    library.configure("docs", LibrarySourceUpdate(expected_revision=0, seed_urls=[SEED]))
    transport = Transport({})
    sync = SourceSynchronizer(library, transport)
    req = SourceSyncRequest(source_ids=["docs"])
    pending = sync.start_sync(req, "cancel")

    def cancel(_headers):
        active = sync.sync_status(pending["run_id"])
        sync.cancel_sync(active["run_id"], active["version"])
        return html("<p>Would have become a document</p>")

    transport.pages[SEED] = cancel
    cancelled = sync.run_next()
    assert cancelled["status"] == "cancelled"
    assert library.list_documents() == []

    def revoke(_headers):
        with library.connect(write=True) as conn:
            snapshot = library.source_snapshot(conn)
            rule = snapshot.sources[0].model_copy(update={"enabled": False})
            conn.execute(
                "INSERT INTO research_source_revisions VALUES(?,?)",
                (2, SourceSnapshot(revision=2, sources=(rule,)).model_dump_json()),
            )
        return html("<p>Permission changed before publication</p>")

    transport.pages[SEED] = revoke
    stale = refresh(library, transport)
    assert stale["status"] == "failed"
    assert "SOURCE_CONFIG_CHANGED" in stale["reason_codes"]
    assert library.list_documents() == []

    with library.connect(write=True) as conn:
        snapshot = library.source_snapshot(conn, 1)
        conn.execute(
            "INSERT INTO research_source_revisions VALUES(?,?)",
            (3, snapshot.model_copy(update={"revision": 3}).model_dump_json()),
        )
    transport.pages[SEED] = html(f"<p>Evidence</p><script>{SECRET}</script>")
    denied = refresh(library, transport)
    assert denied["status"] == "failed"
    assert "LIBRARY_SECRET_CONTENT" in denied["reason_codes"]
    assert not any(SECRET.encode() in path.read_bytes() for path in library.artifact_dir.iterdir())
