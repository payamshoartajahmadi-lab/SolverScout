import gzip
import io
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from p_mc_optimizer.application.errors import ApiError
from p_mc_optimizer.llm.credentials import CredentialBroker
from p_mc_optimizer.research.contracts import ResearchLimits, ResearchQueryPlan, SourceRule, SourceSnapshot
from p_mc_optimizer.research.extraction import extract
from p_mc_optimizer.research.fetch import DocumentFetcher
from p_mc_optimizer.research.gateway import ResearchGateway
from p_mc_optimizer.research.search import TavilySearch
from p_mc_optimizer.research.settings import CredentialTarget, CredentialUpdate, SearchSettings, SourcesUpdate
from p_mc_optimizer.research.source_policy import SourcePolicy
from p_mc_optimizer.research.storage import RESEARCH_SCHEMA, ResearchStore
from p_mc_optimizer.research.transport import PinnedTransport, ResearchError, Response


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "state.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE jobs (job_id TEXT PRIMARY KEY);" + RESEARCH_SCHEMA)
    return ResearchStore(db, tmp_path / "evidence")


def job(store):
    key = str(uuid4())
    with store.connect(write=True) as conn:
        conn.execute("INSERT INTO jobs VALUES(?)", (key,))
    return key


RULE = SourceRule(
    source_id="docs", hostname="example.org", label="Public examples",
    path_prefixes=("/docs",), source_kind="official_docs", enabled=True,
)
POLICY = SourcePolicy(SourceSnapshot(revision=1, sources=(RULE,)))
PLAN = ResearchQueryPlan.model_validate({
    "schema_version": "2.0",
    "queries": [
        {"query_id": "q1", "text": "continuous optimization", "source_ids": ["docs"], "purpose": "method_discovery"},
    ],
})
LIMITS = ResearchLimits()
KEY = "fixture_key_43ab"


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def reply(status, body=b"", **headers):
    return httpx.Response(status, headers=headers, stream=Bytes(body))


def tavily_result(*urls):
    return json.dumps({"results": [
        {"url": url, "title": KEY, "content": "Optimization algorithm", "published_date": "2024-09-08"}
        for url in urls
    ]}).encode()


class DocumentStub:
    def __init__(self, robots=404):
        self.calls = []
        self.robots = robots

    def get(self, url, timeout, size, conditional):
        self.calls.append((url, conditional))
        if url.endswith("/robots.txt"):
            return Response(self.robots, httpx.Headers(), b"User-agent: *\nDisallow: /docs/blocked\n")
        if conditional:
            return Response(304, httpx.Headers(), b"")
        headers = httpx.Headers({"Content-Type": "text/html; charset=utf-8", "ETag": '"foo"'})
        return Response(200, headers, f"<main><p>Continuous optimization {KEY}</p><p>Derivative free.</p></main>".encode())


def test_live_cache_validation_and_checkpoint(store):
    requests = []

    def handle(request):
        requests.append(request)
        return reply(200, tavily_result("https://example.org/docs/a", "https://example.org/docs-fake/x"))

    transport = DocumentStub()
    engine = ResearchGateway(store, search=TavilySearch(transport=httpx.MockTransport(handle)), fetch=DocumentFetcher(transport))
    first_job = job(store)
    first = engine.run(first_job, PLAN, POLICY, LIMITS, credential=KEY)
    assert first.status == "completed", first
    assert first.search_attempts == 1 and first.http_requests == 3
    assert len(first.documents) == 1 and first.documents[0].fetch_status == "read"
    assert len(first.evidence) == 2 and first.evidence[0].keyword_matches == 2
    assert first.hits[1].rejection_code == "SOURCE_NOT_ALLOWED"
    assert KEY not in first.model_dump_json()
    saved = store.load_text(first.documents[0].text_sha256)
    assert KEY not in saved
    assert all(item.text == saved[item.start:item.end] for item in first.evidence)
    assert requests[0].url.host == "api.tavily.com"
    sent = json.loads(requests[0].content)
    assert sent["include_domains"] == ["example.org"] and sent["include_domains_mode"] == "restrict"
    assert requests[0].headers["Authorization"] == f"Bearer {KEY}"

    assert engine.run(first_job, PLAN, POLICY, LIMITS, credential=KEY) == first
    assert len(requests) == 1
    assert engine.cache_available(POLICY, 24)
    cached = engine.run(job(store), None, POLICY, LIMITS, mode="cache_only")
    assert cached.http_requests == 0 and len(transport.calls) == 2
    assert cached.documents[0].fetch_status == "cached"
    assert cached.documents[0].validated_at == first.documents[0].validated_at
    assert cached.documents[0].cache_age_hours >= 0

    second = engine.run(job(store), PLAN, POLICY, LIMITS, credential=KEY)
    assert second.documents[0].fetch_status == "revalidated"
    assert second.documents[0].fetched_at == first.documents[0].fetched_at
    assert second.documents[0].text_sha256 == first.documents[0].text_sha256
    assert second.documents[0].validated_at > first.documents[0].validated_at
    assert transport.calls[-1][1] == {"If-None-Match": '"foo"'}
    assert len(requests) == 2


@pytest.mark.parametrize(("robots", "code"), [(200, "SOURCE_ROBOTS_DISALLOWED"), (403, "SOURCE_ROBOTS_DISALLOWED"), (500, "SOURCE_POLICY_UNAVAILABLE")])
def test_robots_blocks_before_document_request(store, robots, code):
    def handle(request):
        return reply(200, tavily_result("https://example.org/docs/blocked"))

    transport = DocumentStub(robots)
    engine = ResearchGateway(store, search=TavilySearch(transport=httpx.MockTransport(handle)), fetch=DocumentFetcher(transport))
    result = engine.run(job(store), PLAN, POLICY, LIMITS, credential=KEY)
    assert result.status == "insufficient_evidence"
    assert result.documents[0].reason_code == code
    assert transport.calls == [("https://example.org/robots.txt", {})]
    assert result.http_requests == 2


def test_redirect_rejected_before_network(store):
    class RedirectStub(DocumentStub):
        def get(self, url, timeout, size, conditional):
            if url.endswith("/robots.txt"):
                return super().get(url, timeout, size, conditional)
            self.calls.append((url, conditional))
            return Response(302, httpx.Headers({"Location": "https://bad.example/docs/leak"}), b"")

    transport = RedirectStub()
    search = TavilySearch(transport=httpx.MockTransport(
        lambda request: reply(200, tavily_result("https://example.org/docs/x")),
    ))
    outcome = ResearchGateway(store, search=search, fetch=DocumentFetcher(transport)).run(
        job(store), PLAN, POLICY, LIMITS, credential=KEY,
    )
    assert outcome.documents[0].reason_code == "SOURCE_NOT_ALLOWED"
    assert [item[0] for item in transport.calls] == [
        "https://example.org/robots.txt", "https://example.org/docs/x",
    ]


def test_transport_pins_dns_strips_keys_and_rejects_expansion():
    requests = []

    def handle(request):
        requests.append(request)
        return reply(200, gzip.compress(b"z" * 65536), **{"Content-Encoding": "gzip"})

    transport = PinnedTransport(
        resolver=lambda host, timeout: ["8.8.8.8"],
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(ResearchError, match="RESEARCH_DATA_LIMIT"):
        transport.get("https://example.org/docs/x", 1, 1024, {"Authorization": KEY})
    assert requests[0].url.host == "8.8.8.8"
    assert requests[0].extensions["sni_hostname"] == "example.org"
    assert requests[0].headers["Host"] == "example.org"
    assert "authorization" not in requests[0].headers
    for address in ("127.0.0.1", "::1", "169.254.169.254", "fc00::1"):
        transport.resolver = lambda host, timeout: ["8.8.8.8", address]
        with pytest.raises(ResearchError, match="SOURCE_NOT_ALLOWED"):
            transport.get("https://example.org/docs/x", 1, 1024, {})
    assert len(requests) == 1


def test_ledger_never_replays_unknown_and_manual_retry_uses_cap(store):
    requests = []

    def handle(request):
        requests.append(request)
        return reply(401)

    engine = ResearchGateway(store, search=TavilySearch(transport=httpx.MockTransport(handle)))
    identifier = job(store)
    for _ in range(3):
        result = engine.run(identifier, PLAN, POLICY, LIMITS, credential=KEY, resume=True)
        assert result.status == "failed" and result.reason_codes == ("SEARCH_AUTH_FAILED",)
    assert engine.run(identifier, PLAN, POLICY, LIMITS, credential=KEY, resume=True).search_attempts == 3
    assert len(requests) == 3

    interrupted = job(store)
    assert store.reserve(interrupted, "search:q1:0", "search", {
        "query": PLAN.queries[0].model_dump(mode="json"),
        "domains": ("example.org",),
        "limits": LIMITS.model_dump(),
    }, LIMITS) is None
    result = engine.run(interrupted, PLAN, POLICY, LIMITS, credential=KEY)
    assert result.reason_codes == ("SEARCH_OUTCOME_UNKNOWN",)
    assert len(requests) == 3


@pytest.mark.parametrize(("status", "content_type", "expected_code"), [
    (403, "text/html; charset=utf-8", "SEARCH_ACCESS_DENIED"),
    (403, "Text/HTML; charset=UTF-8", "SEARCH_ACCESS_DENIED"),
    (403, "application/xhtml+xml", "SEARCH_ACCESS_DENIED"),
    (403, "application/json", "SEARCH_AUTH_FAILED"),
    (403, None, "SEARCH_AUTH_FAILED"),
    (401, "text/html", "SEARCH_AUTH_FAILED"),
    (401, "application/json", "SEARCH_AUTH_FAILED"),
])
def test_search_access_errors_preserve_auth_semantics_and_do_not_replay(
    store, status, content_type, expected_code,
):
    requests = []

    def handle(request):
        requests.append(request)
        headers = {"content-type": content_type} if content_type else {}
        return reply(status, **headers)

    engine = ResearchGateway(store, search=TavilySearch(transport=httpx.MockTransport(handle)))
    identifier = job(store)
    result = engine.run(identifier, PLAN, POLICY, LIMITS, credential=KEY)
    assert result.status == "failed"
    assert result.reason_codes == (expected_code,)
    assert result.search_attempts == result.http_requests == 1
    assert not result.hits and not result.evidence
    assert engine.run(identifier, PLAN, POLICY, LIMITS, credential=KEY) == result
    assert len(requests) == 1


def test_cancel_during_research_stops_new_requests(store):
    requests = []

    def handle(request):
        requests.append(request)
        return reply(200, tavily_result("https://example.org/docs/x"))

    transport = DocumentStub()
    result = ResearchGateway(
        store, search=TavilySearch(transport=httpx.MockTransport(handle)), fetch=DocumentFetcher(transport),
    ).run(job(store), PLAN, POLICY, LIMITS, credential=KEY, cancelled=lambda: bool(requests))
    assert result.status == "cancelled"
    assert len(requests) == 1 and not transport.calls


def test_pdf_text_and_scan_are_distinct():
    def pdf(text):
        writer = PdfWriter()
        page = writer.add_blank_page(width=128, height=128)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 0 50 Td (Optimization test) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }),
                }),
            })
        target = io.BytesIO()
        writer.write(target)
        return target.getvalue()

    assert "Optimization test" in extract(pdf(True), "application/pdf", "full_text", 1000, 2)["text"]
    with pytest.raises(ResearchError, match="SOURCE_PDF_NO_TEXT"):
        extract(pdf(False), "application/pdf", "full_text", 1000, 2)


def test_search_settings_write_only_stale_and_test_replay(store):
    requests = []

    def handle(request):
        requests.append(request)
        return reply(200, tavily_result())

    engine = ResearchGateway(store, search=TavilySearch(transport=httpx.MockTransport(handle)))
    llm = CredentialBroker(internal_token="internal-fixture-token")
    settings = SearchSettings(store, llm, engine)
    settings.update_sources(SourcesUpdate(expected_revision=1, sources=(RULE,)))
    update = CredentialUpdate(revision=1, generation=0, secret=KEY)
    public = settings.mutate_credential(update)
    assert public["credential_available"] and not public["ready"]
    assert "credential_ref" not in public["profile"]
    assert KEY not in json.dumps(public)
    assert settings.sanitize(KEY) != KEY
    assert llm.sanitize(KEY) == KEY
    assert KEY.encode() not in store.path.read_bytes()
    with pytest.raises(ApiError, match="STALE_RESEARCH_SETTINGS"):
        settings.mutate_credential(update)
    target = CredentialTarget(revision=1, generation=1)
    assert settings.test(target, "same-id") == settings.test(target, "same-id")
    assert len(requests) == 1 and settings.is_ready(settings.get_profile())
