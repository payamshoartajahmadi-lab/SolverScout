"""Local-library jobs through loopback FastAPI and the real numeric worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

from p_mc_optimizer.research.fetch import DocumentFetcher
from p_mc_optimizer.research.transport import Response
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, completion
from test_llm_workflow import _GOOD_EXPLANATION, assert_empirical_explainer_context, configure, evidence_plan, local_api
from test_v2_workflow import SOURCE, keyed_post, profile_ref, qualify_three, run_job

CORPUS_PATH = Path(__file__).parent / "fixtures" / "library_v21_corpus.json"


class NoResearchNetwork:
    def __init__(self):
        self.calls = []

    def search(self, *_args, **_kwargs):
        self.calls.append("search")
        pytest.fail("A local-only job reached the web search adapter")

    def get(self, *_args, **_kwargs):
        self.calls.append("fetch")
        pytest.fail("A local-only job reached the document transport")


class DirectSourceFixture:
    def __init__(self, corpus):
        self.documents = {}
        self.calls = []
        for row in (*corpus["positive"], *corpus["unrelated"]):
            text = row["text"]
            raw = text.encode()
            ref = row["reference"]
            assert hashlib.sha256(raw).hexdigest() == row["text_sha256"]
            assert text[ref["start"]:ref["end"]] == ref["text"]
            self.documents[row["url"]] = raw

    def get(self, url, _timeout, size, conditional):
        self.calls.append(url)
        if url == "https://methods.example.test/robots.txt":
            return Response(404, httpx.Headers(), b"")
        raw = self.documents[url]
        assert len(raw) <= size
        return Response(200, httpx.Headers({"content-type": "text/plain; charset=utf-8"}), raw)


def model_fixture(calls):
    def respond(request):
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        assert request.headers["Authorization"] == f"Bearer {_TEST_KEY}"
        assert "X-PMC-Internal-Token" not in request.headers
        if name == "connection_test_v1":
            return completion({"ok": True})
        context = json.loads(body["messages"][-1]["content"])
        calls.append((name, context))
        if name == "research_planner_v2_1":
            assert context["retrieval_mode"] in {"local_only", "direct_refresh"}
            assert "expression" not in json.dumps(context)
            source_ids = [source["source_id"] for source in context["sources"]]
            cards = context["solver_cards"]
            assert {card["family_id"] for card in cards} == {"de", "gwo", "woa"}
            return completion({
                "schema_version": "2.1",
                "queries": [{
                    "query_id": "q1", "purpose": "implementation_evidence", "source_ids": source_ids,
                    "text": "bounded continuous optimization " + " ".join(card["name"] for card in cards),
                    "implementation_ids": [card["implementation_id"] for card in cards],
                }, {
                    "query_id": "q2", "purpose": "method_discovery", "source_ids": source_ids,
                    "text": "new methods for bounded continuous optimization", "implementation_ids": [],
                }],
            })
        if name == "explainer_v2":
            assert "repair" not in context
            return completion(evidence_plan(context))
        assert name == "advisor_v2_1"
        candidates = []
        for card in context["solver_cards"]:
            variant = card["implementation_id"].rsplit(":", 1)[-1]
            refs = [row["evidence_id"] for row in context["evidence"] if variant in row["text"]]
            if refs or not context["require_evidence"]:
                candidates.append({
                    "implementation_id": card["implementation_id"], "config_id": card["config_id"],
                    "card_ref": card["implementation_id"], "rationale_fa": "TEST FIXTURE",
                    "evidence_refs": refs, "evidence_scope": "method_level" if refs else None,
                })
        novel = [row["evidence_id"] for row in context["evidence"] if "Orbit Lattice Search" in row["text"]]
        ready = len(candidates) == 3
        if ready and context["require_evidence"]:
            if repair := context.get("repair"):
                assert any("Set card_ref to implementation_id" in message for message in repair["field_errors"])
            else:
                candidates[0]["card_ref"] = candidates[0]["config_id"]
        return completion({
            "schema_version": "2.1", "status": "ready" if ready else "insufficient_evidence",
            "reason_codes": [] if ready else ["RESEARCH_EVIDENCE_INSUFFICIENT"],
            "research_findings": [{
                "name": "Orbit Lattice Search", "variant": None, "claims": [{
                    "text": "The fixture names Orbit Lattice Search.",
                    "status": "supported", "evidence_refs": novel,
                }],
                "uncertainty": "Fictional fixture method.", "implementation_match": None,
                "mapping_reason": "No qualified implementation in the catalog.",
            }] if novel else [],
            "uncertainty_notes": None, "candidates": candidates if ready else [],
        })
    return httpx.MockTransport(respond)


def register_corpus(client, rows, source_id):
    current = checked(client.get("/settings/research-sources"))
    configured = checked(client.put("/settings/research-sources", json={
        "expected_revision": current["revision"], "sources": [{**SOURCE, "source_id": source_id}],
    }))
    config = checked(client.put(f"/library/sources/{source_id}", json={
        "expected_revision": 0, "seed_urls": [row["url"] for row in rows], "follow_links": False,
    }))
    return configured["revision"], config


def sync_corpus(client, rows, source_id):
    source_revision, _ = register_corpus(client, rows, source_id)
    run = checked(keyed_post(client, "/library/sync-runs", {"source_ids": [source_id]}), 202)
    deadline = monotonic() + 15
    while run["status"] in {"queued", "running"} and monotonic() < deadline:
        sleep(0.1)
        run = checked(client.get(f"/library/sync-runs/{run['run_id']}"))
    assert run["status"] == "completed", run
    documents = checked(client.get(f"/library/documents?source_id={source_id}"))["documents"]
    assert {row["text_sha256"] for row in documents} == {row["text_sha256"] for row in rows}
    return source_revision


def test_library_jobs_preserve_exact_evidence_and_pilot_budget_without_tavily(tmp_path):
    """FLOW-08. Run the v2.1 Explainer with local-only research and real pilots."""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    calls = []
    network = NoResearchNetwork()
    direct = DirectSourceFixture(corpus)
    with local_api(tmp_path, model_fixture(calls), enable_lifespan=True,
                   library_transport=direct, research_search=network,
                   research_fetch=DocumentFetcher(network)) as (repo, client, address, token):
        catalog = qualify_three(client)
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        search = checked(client.get("/settings/search"))
        assert not search["ready"]
        stored = checked(client.put("/settings/search/credential", json={
            "revision": search["profile"]["revision"], "generation": search["credential_generation"],
            "secret": "TEST_FIXTURE_INVALID_TAVILY_KEY", "storage_mode": "session",
        }))
        assert stored["credential_available"] and not stored["ready"]
        assert not checked(client.get("/settings/retrieval"))["web_search_enabled"]
        revision = sync_corpus(client, corpus["positive"], "methods")
        assert set(direct.calls) == {"https://methods.example.test/robots.txt", *(row["url"] for row in corpus["positive"])}
        network_before_job = list(direct.calls)
        problem = json.loads((Path(__file__).parents[1] / "examples/quadratic_2d.json").read_text())
        policy = {
            "policy_version": "2.1", "portfolio_id": "continuous_catalog_v2",
            "selection_policy": "evidence_research_then_pilot", "retrieval_mode": "local_only",
            "max_objective_evaluations": 5502, "max_compute_seconds": 120,
            "master_seed": 73, "explain_via_llm": False,
        }
        payload = {
            "schema_version": "2.1", "input": {"kind": "structured_problem", "problem": problem},
            "policy": policy, "llm_profile": profile_ref(profile),
            "source_revision": revision, "catalog_hash": catalog["catalog_hash"],
        }
        explained_payload = {**payload, "policy": {**policy, "explain_via_llm": True}}
        job_id, state = run_job(repo, client, address, token, explained_payload)
        assert state["job_status"] == "completed", (state, repo.list_events(job_id)[-8:])
        outcome = checked(client.get(f"/jobs/{job_id}/research"))
        assert outcome["schema_version"] == "2.1"
        assert outcome["retrieval_mode"] == "local_only"
        assert outcome["http_requests"] == outcome["search_attempts"] == 0
        passages = {row["evidence_id"]: row for row in outcome["evidence"]}
        by_url = {row["url"]: row for row in corpus["positive"]}
        for passage in passages.values():
            expected = by_url[passage["url"]]
            assert expected["text"][passage["start"]:passage["end"]] == passage["text"]
            assert passage["version_id"] and passage["observation_id"]
        bundle = checked(client.get(f"/jobs/{job_id}/report"))
        report = bundle["report"]
        assert report["schema_version"] == "2.1" and report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == "generated"
        assert report["explanation_evidence"]["status"] == "consistent"
        assert report["explanation"]["limitations_fa"][0] == "بهینگی سراسری در این اجرا اثبات نشده است."
        assert report["budget"]["known_started"] == 5502
        assert len(report["trials"]) == 10
        assert all(trial["started_evaluations"] == 500 for trial in report["trials"])
        candidates = report["candidate_plan"]["candidates"]
        assert len(candidates) == 3
        for candidate in candidates:
            variant = candidate["algorithm_id"].rsplit(":", 1)[-1]
            assert any(variant in passages[ref]["text"] for ref in candidate["evidence_refs"])
        with repo.connect() as db:
            finding = json.loads(db.execute("SELECT payload FROM method_records WHERE job_id=?", (job_id,)).fetchone()[0])
        assert finding["name"] == "Orbit Lattice Search"
        assert finding["status"] == "research_only" and finding["implementation_match"] is None
        assert {artifact["kind"] for artifact in bundle["artifacts"]} >= {"research_json", "report_html", "sources_csv"}
        for artifact in bundle["artifacts"]:
            if artifact["kind"] in {"research_json", "report_html", "sources_csv"}:
                exported = client.get(f"/artifacts/{artifact['artifact_id']}").text
                assert token not in exported and _TEST_KEY not in exported
        assert [name for name, _ in calls] == ["research_planner_v2_1", "advisor_v2_1", "advisor_v2_1", "explainer_v2"]
        context = calls[-1][1]
        assert context == repo.get_job(job_id)["workflow"]["role_context:explainer"]
        summary = assert_empirical_explainer_context(context, report)
        assert summary["effective_selection_policy"] == "evidence_research_then_pilot"
        assert summary["eligible_ranking"][0] == report["selected_algorithm_id"]
        assert direct.calls == network_before_job and not network.calls

        revision, unrelated = register_corpus(client, corpus["unrelated"], "unrelated")
        assert checked(client.get(f"/jobs/{job_id}/research")) == outcome
        bad_payload = {**payload, "source_revision": revision}
        stopped_id, stopped = run_job(repo, client, address, token, {
            **bad_payload,
            "source_config_refs": [{"source_id": unrelated["source_id"], "revision": unrelated["revision"]}],
            "policy": {**policy, "retrieval_mode": "direct_refresh", "sync_limits": {}},
        })
        assert stopped["job_status"] == "failed", (stopped, repo.list_events(stopped_id)[-8:])
        assert direct.calls[len(network_before_job):] == [corpus["unrelated"][0]["url"]]
        phases = [event["payload"]["phase"] for event in repo.list_events(stopped_id)
                  if event["event_type"] == "phase_changed"]
        assert phases.index("refreshing_sources") < phases.index("preflight")
        failed = checked(client.get(f"/jobs/{stopped_id}/report"))["report"]
        assert failed["research_error"]["error_code"] == "RESEARCH_EVIDENCE_INSUFFICIENT"
        assert not failed["trials"] and failed["budget"]["known_started"] <= 502
        sync = failed["research"]["sync_run"]
        assert sync["owner_job_id"] == stopped_id and sync["status"] == "completed"
        assert sync["usage"]["http_requests"] == 1 and failed["research"]["http_requests"] == 0

        network_before_job = list(direct.calls)
        fallback_payload = {**bad_payload, "policy": {**policy, "on_research_failure": "continue_with_catalog"}}
        fallback_id, state = run_job(repo, client, address, token, fallback_payload)
        assert state["job_status"] == "completed", (state, repo.list_events(fallback_id)[-8:])
        fallback = checked(client.get(f"/jobs/{fallback_id}/report"))["report"]
        assert fallback["effective_selection_policy"] == "catalog_llm_then_pilot"
        assert fallback["research_error"]["error_code"] == "RESEARCH_EVIDENCE_INSUFFICIENT"
        assert fallback["budget"]["known_started"] == 5502
        assert direct.calls == network_before_job and not network.calls
