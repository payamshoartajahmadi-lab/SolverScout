"""The optional web route acquires evidence through the local API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from p_mc_optimizer.api import create_app
from p_mc_optimizer.research.contracts import SourceDocument
from p_mc_optimizer.research.fetch import DocumentFetcher
from p_mc_optimizer.research.library_store import LibraryEntry
from p_mc_optimizer.research.search import TAVILY_ENDPOINT, TavilySearch
from p_mc_optimizer.research.transport import Response
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, completion
from test_llm_workflow import configure
from test_v2_research import reply
from test_v2_workflow import SEARCH_KEY, SOURCE, profile_ref


HOST = SOURCE["hostname"]
URL = f"https://{HOST}/manual/solvers"
UNRELATED = f"https://{HOST}/manual/archived"
IMPLEMENTATION = "mealpy:evolutionary_based.DE:OriginalDE"
PLAN = {
    "schema_version": "2.1",
    "queries": [
        {"query_id": "variants", "purpose": "implementation_evidence", "source_ids": ["methods"],
         "text": "OriginalDE bounded optimization", "implementation_ids": [IMPLEMENTATION]},
        {"query_id": "discovery", "purpose": "method_discovery", "source_ids": ["methods"],
         "text": "bounded optimization methods", "implementation_ids": []},
    ],
}


def test_optional_web_uses_only_documents_observed_by_the_claimed_job(tmp_path):
    search_calls, fetch_calls = [], []
    raw = b"<html><head><title>Official method</title></head><body><p>OriginalDE supports bounded optimization.</p></body></html>"
    token = "TEST_FIXTURE_INTERNAL_TOKEN"

    def search(request):
        assert str(request.url) == TAVILY_ENDPOINT
        assert request.headers["Authorization"] == f"Bearer {SEARCH_KEY}"
        assert "X-PMC-Internal-Token" not in request.headers
        payload = json.loads(request.content)
        assert payload["include_domains"] == [HOST]
        assert payload["include_domains_mode"] == "restrict"
        search_calls.append(payload["query"])
        return reply(200, json.dumps({
            "results": [{"url": URL, "title": "Official method", "content": "Unverified snippet"}],
        }).encode(), **{"content-type": "application/json"})

    class Documents:
        def get(self, url, timeout, size, conditional):
            fetch_calls.append((url, conditional))
            if url == f"https://{HOST}/robots.txt":
                return Response(404, httpx.Headers(), b"")
            assert url == URL
            if conditional:
                assert conditional == {"If-None-Match": '"fixture"'}
                return Response(304, httpx.Headers(), b"")
            return Response(200, httpx.Headers({"content-type": "text/html", "etag": '"fixture"'}), raw)

    app = create_app(
        tmp_path, token, transport=httpx.MockTransport(lambda _: completion({"ok": True})),
        research_search=TavilySearch(transport=httpx.MockTransport(search)),
        research_fetch=DocumentFetcher(Documents()),
    )
    repo = app.state.repo
    with TestClient(app, headers={"X-PMC-Internal-Token": token}) as client:
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        sources = checked(client.get("/settings/research-sources"))
        source_revision = checked(client.put("/settings/research-sources", json={
            "expected_revision": sources["revision"], "sources": [SOURCE],
        }))["revision"]
        provider_route = "/settings/search-providers/tavily"
        checked(client.get("/settings/search-providers/uninstalled"), 422)
        provider = checked(client.get(provider_route))
        assert not provider["ready"]
        target = {"revision": provider["profile"]["revision"], "generation": provider["credential_generation"]}
        credential = checked(client.put(provider_route + "/credential", json={
            **target, "secret": SEARCH_KEY, "storage_mode": "session",
        }))
        target["generation"] = credential["credential_generation"]
        checked(client.post(provider_route + "/test", json=target, headers={"Idempotency-Key": str(uuid4())}))
        provider = checked(client.get(provider_route))
        assert provider["ready"] and len(search_calls) == 1 and not fetch_calls

        timestamp = datetime.now(UTC)
        local = "OriginalDE supports bounded optimization. Library-only fixture text."
        text_hash = hashlib.sha256(local.encode()).hexdigest()
        archived = SourceDocument(
            document_id=uuid4().hex, source_id="methods", requested_url=UNRELATED, final_url=UNRELATED,
            fetched_at=timestamp, validated_at=timestamp, content_type="text/plain",
            content_level="documentation", parser_version="fixture", bytes_sha256=text_hash,
            text_sha256=text_hash, fetch_status="read",
        )
        app.state.library.publish_documents([
            LibraryEntry(archived, local, local.encode(), title="OriginalDE", discovery={"kind": "direct_seed"}),
        ], app.state.search_settings.get_sources(), origin="fixture")

        problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
        payload = {
            "schema_version": "2.1",
            "input": {"kind": "structured_problem", "problem": problem},
            "policy": {
                "policy_version": "2.1", "portfolio_id": "continuous_catalog_v2",
                "selection_policy": "evidence_research_then_pilot", "retrieval_mode": "web_search",
                "max_objective_evaluations": 5502, "max_compute_seconds": 120,
                "master_seed": 73, "explain_via_llm": False,
            },
            "llm_profile": profile_ref(profile), "search_provider_ref": profile_ref(provider["profile"]),
            "source_revision": source_revision, "catalog_hash": app.state.catalog.snapshot().catalog_hash,
        }

        def start_job():
            job = checked(client.post("/jobs", json=payload, headers={"Idempotency-Key": str(uuid4())}), 202)
            job_id, worker_id = job["job_id"], str(uuid4())
            assert repo.claim_job(worker_id, job_id=job_id)
            repo.put_workflow(job_id, "query_plan", PLAN, worker_id=worker_id)
            repo.put_workflow(job_id, "solver_cards", [{
                "implementation_id": IMPLEMENTATION, "family_id": "de",
            }], worker_id=worker_id)
            repo.update_job(job_id, phase="searching", worker_id=worker_id)
            return {"job_id": job_id, "worker_id": worker_id}

        first = start_job()
        invoke = "/internal/research/invoke"
        gate = checked(client.post(invoke, json={**first, "operation": "check"}), 422)
        assert gate["error_code"] == "SEARCH_CONFIGURATION_REQUIRED"
        assert len(search_calls) == 1 and not fetch_calls
        settings = checked(client.get("/settings/retrieval"))
        checked(client.put("/settings/retrieval", json={
            "expected_revision": settings["revision"], "provider_id": "tavily",
        }))
        assert checked(client.post(invoke, json={**first, "operation": "check"})) == {"ready": True}

        read = checked(client.post(invoke, json={**first, "operation": "run"}))
        repo.update_job(first["job_id"], job_status="cancelled", worker_id=first["worker_id"])
        second = start_job()
        revalidated = checked(client.post(invoke, json={**second, "operation": "run"}))
        for result, state, usage, job in ((read, "read", 4, first), (revalidated, "revalidated", 3, second)):
            assert result["status"] == "completed"
            assert result["schema_version"] == "2.1" and result["retrieval_mode"] == "web_search"
            assert result["search_attempts"] == 2 and result["http_requests"] == usage
            document, = result["documents"]
            assert document["final_url"] == URL and document["fetch_status"] == state
            assert document["discovery"] == {"kind": "tavily", "job_id": job["job_id"]}
            assert all(passage["observation_id"] == document["observation_id"] for passage in result["evidence"])
            assert UNRELATED not in json.dumps(result)
            assert SEARCH_KEY not in json.dumps(result) and token not in json.dumps(result)
        assert read["documents"][0]["version_id"] == revalidated["documents"][0]["version_id"]
        assert read["documents"][0]["observation_id"] != revalidated["documents"][0]["observation_id"]
        assert checked(client.post(invoke, json={**second, "operation": "run"})) == revalidated
        assert len(search_calls) == 5
        assert fetch_calls == [(f"https://{HOST}/robots.txt", {}), (URL, {}), (URL, {"If-None-Match": '"fixture"'})]
