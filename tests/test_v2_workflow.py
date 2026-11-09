"""Network fixtures with the real loopback worker and isolated numeric processes."""
from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx

from p_mc_optimizer.research.fetch import DocumentFetcher
from p_mc_optimizer.research.transport import ResearchError, Response
from p_mc_optimizer.workflow.worker import run_claimed
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, completion
from test_llm_workflow import configure, local_api

SOURCE = {
    "source_id": "methods", "label": "TEST FIXTURE", "hostname": "methods.example.test",
    "path_prefixes": ["/manual"], "source_kind": "official_docs", "enabled": True,
}
SEARCH_KEY = "TEST_FIXTURE_SEARCH_CREDENTIAL"
QUERY = "continuous bounded derivative free optimization swarm methods"


def keyed_post(client, route, value=None):
    return client.post(route, json=value, headers={"Idempotency-Key": str(uuid4())})


class SearchFixture:
    def __init__(self):
        self.calls = []
        self.failure = None

    def search(self, text, domains, count, timeout, credential, sanitize):
        assert credential == SEARCH_KEY
        assert domains == ("methods.example.test",)
        self.calls.append(text)
        if self.failure:
            raise ResearchError(self.failure)
        return [{"url": "https://methods.example.test/manual/solvers", "provider_rank": 1,
                 "title": "Test methodology", "snippet": "Unverified snippet"}]


class DocumentFixture:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout, size, conditional):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            return Response(404, httpx.Headers(), b"")
        assert url == "https://methods.example.test/manual/solvers"
        return Response(200, httpx.Headers({"content-type": "text/html"}), b"""
            <html><head><title>Test methods</title></head><body><main>
            <p>Grey Wolf Optimization, Whale Optimization and Differential Evolution are
            derivative free methods for bounded, continuous optimization.</p>
            <p>Fixture evidence with escaped markup: &lt;script&gt;demo&lt;/script&gt;.</p>
            </main></body></html>
        """)


def model_fixture(request):
    body = json.loads(request.content)
    name = body["response_format"]["json_schema"]["name"]
    assert request.headers["Authorization"] == f"Bearer {_TEST_KEY}"
    if name == "connection_test_v1":
        return completion({"ok": True})
    context = json.loads(body["messages"][-1]["content"])
    if name == "research_planner_v2":
        assert set(context) == {"profile", "sources", "max_queries"}
        assert "expression" not in json.dumps(context)
        return completion({"schema_version": "2.0", "queries": [{
            "query_id": "q1", "text": QUERY, "source_ids": ["methods"], "purpose": "method_discovery",
        }]})
    assert name == "advisor_v2"
    evidence = [row["evidence_id"] for row in context["evidence"]][:1]
    assert bool(evidence) == context["require_web"]
    return completion({
        "schema_version": "2.0", "status": "ready", "reason_codes": [], "research_findings": [],
        "uncertainty_notes": None,
        "candidates": [
            {"implementation_id": row["implementation_id"], "config_id": row["config_id"],
             "card_ref": row["implementation_id"], "rationale_fa": "TEST FIXTURE",
             "evidence_refs": evidence, "evidence_scope": "family_level" if evidence else None}
            for row in context["solver_cards"][:3]
        ],
    })


def qualify_three(client):
    catalog = checked(client.post("/catalog/discover"))
    assert catalog["discovered_count"] >= 6 and catalog["qualified_count"] == 0
    algorithms = {"OriginalGWO", "OriginalWOA", "OriginalDE"}
    configs = [row["config_id"] for row in catalog["suggested_configurations"] if row["class_name"] in algorithms]
    assert len(configs) == 3
    batch = checked(keyed_post(client, "/catalog/qualifications", {"config_ids": configs, "max_seconds": 120}), 202)
    batch_id = batch["qualification_id"]
    deadline = monotonic() + 120
    while batch["status"] == "running" and monotonic() < deadline:
        sleep(0.2)
        batch = checked(client.get(f"/catalog/qualifications/{batch_id}"))
    catalog = checked(client.get("/catalog/implementations?status=qualified"))
    assert catalog["qualified_count"] == 3, batch
    return catalog


def profile_ref(profile):
    return {key: profile[key] for key in ("profile_id", "revision")}


def run_job(repo, client, address, token, payload):
    created = checked(keyed_post(client, "/jobs", payload), 202)
    job_id = created["job_id"]
    worker_id = str(uuid4())
    job = repo.claim_job(worker_id, job_id=job_id, lease_seconds=1200)
    run_claimed(repo, job, worker_id, api_url=address, token=token)
    return job_id, checked(client.get(f"/jobs/{job_id}"))


def test_v2_web_and_catalog_policies_use_real_pilot_evidence(tmp_path):
    search = SearchFixture()
    document = DocumentFixture()
    with local_api(tmp_path, httpx.MockTransport(model_fixture), research_search=search,
                   research_fetch=DocumentFetcher(document)) as (repo, client, address, token):
        catalog = qualify_three(client)
        llm, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        profile = checked(client.get("/settings/search"))
        sources = checked(client.get("/settings/research-sources"))
        source_snapshot = checked(client.put("/settings/research-sources", json={
            "expected_revision": sources["revision"], "sources": [SOURCE],
        }))
        revision = profile["profile"]["revision"]
        target = {"revision": revision, "generation": profile["credential_generation"]}
        response = client.put("/settings/search/credential", json={
            **target, "secret": SEARCH_KEY, "storage_mode": "session",
        })
        updated = checked(response)
        assert SEARCH_KEY not in response.text
        updated = checked(client.get("/settings/search"))
        tested = checked(keyed_post(client, "/settings/search/test", {
            "revision": revision, "generation": updated["credential_generation"],
        }))
        assert checked(client.get("/settings/search"))["ready"], tested
        policy = {
            "policy_version": "2.0", "portfolio_id": "continuous_catalog_v2",
            "selection_policy": "web_research_then_pilot", "max_objective_evaluations": 5502,
            "max_compute_seconds": 120, "master_seed": 73, "explain_via_llm": False,
        }
        problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
        payload = {
            "schema_version": "2.0", "input": {"kind": "structured_problem", "problem": problem},
            "policy": policy, "llm_profile": profile_ref(llm), "search_profile": profile_ref(profile["profile"]),
            "source_revision": source_snapshot["revision"], "catalog_hash": catalog["catalog_hash"],
        }
        before = len(search.calls)
        job_id, state = run_job(repo, client, address, token, payload)
        assert state["job_status"] == "completed", (state, repo.list_events(job_id)[-8:])
        assert search.calls[before:] == [QUERY]
        outcome = checked(client.get(f"/jobs/{job_id}/research"))
        assert outcome["status"] == "completed" and outcome["search_attempts"] == 1
        assert outcome["http_requests"] == 3 and len(outcome["evidence"]) >= 1
        report_body = checked(client.get(f"/jobs/{job_id}/report"))
        report = report_body["report"]
        assert report["schema_version"] == "2.0" and report["selection_status"] == "selected"
        assert report["solution_status"] == "valid_best_found"
        assert len(report["trials"]) == 10
        assert all(row["started_evaluations"] == 500 for row in report["trials"])
        assert report["budget"]["known_started"] == 5502
        assert {a["kind"] for a in report_body["artifacts"]} >= {"report_html", "research_json", "sources_csv"}
        html_artifact = next(a for a in report_body["artifacts"] if a["kind"] == "report_html")
        html = client.get(f"/artifacts/{html_artifact['artifact_id']}").text
        assert "<script>" not in html and "Grey Wolf" in html
        assert SEARCH_KEY not in html and _TEST_KEY not in html

        search.failure = "SEARCH_AUTH_FAILED"
        failed_job, state = run_job(repo, client, address, token, payload)
        assert state["job_status"] == "needs_configuration", state
        assert state["blocked_reason"] == "SEARCH_AUTH_FAILED"
        assert state["budget"]["known_started"] == 500
        assert checked(client.get(f"/jobs/{failed_job}/research"))["search_attempts"] == 1

        fallback_payload = {**payload, "policy": {**policy, "on_research_failure": "continue_with_catalog"}}
        fallback_job, state = run_job(repo, client, address, token, fallback_payload)
        assert state["job_status"] == "completed", (state, repo.list_events(fallback_job)[-8:])
        fallback = checked(client.get(f"/jobs/{fallback_job}/report"))["report"]
        assert fallback["effective_selection_policy"] == "catalog_llm_then_pilot"
        assert fallback["proposal_source"] == "catalog_after_research_failure"
        assert fallback["research_error"]["error_code"] == "SEARCH_AUTH_FAILED"
        assert fallback["solution_status"] == "valid_best_found"

        checked(client.request("DELETE", "/settings/search/credential", json={
            "revision": revision, "generation": updated["credential_generation"],
        }))
        before = len(search.calls)
        offline_payload = {**payload, "policy": {**policy, "research_mode": "cache_only"}, "search_profile": None}
        offline_job, state = run_job(repo, client, address, token, offline_payload)
        assert state["job_status"] == "completed", (state, repo.list_events(offline_job)[-8:])
        cached = checked(client.get(f"/jobs/{offline_job}/research"))
        assert cached["cache_only"] and cached["http_requests"] == 0 and len(search.calls) == before
        assert cached["documents"][0]["fetch_status"] == "cached"

        for fail_policy in ("stop", "continue_with_catalog"):
            missing_payload = {**payload, "policy": {**policy, "on_research_failure": fail_policy}}
            _, state = run_job(repo, client, address, token, missing_payload)
            assert state["job_status"] == "needs_configuration"
            assert state["blocked_reason"] == "SEARCH_CONFIGURATION_REQUIRED"
            assert state["budget"]["known_started"] == 0 and len(search.calls) == before
