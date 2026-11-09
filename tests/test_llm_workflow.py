"""Loopback workflow acceptance through TEST FIXTURE provider transport."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn

from p_mc_optimizer.api import create_app
from p_mc_optimizer.llm.formulation import convert_formulation
from p_mc_optimizer.llm.models import ProviderSettings
from p_mc_optimizer.llm.storage import job_call_metadata
from p_mc_optimizer.workflow.graph import Workflow
from test_llm_gateway import (
    _POLICY,
    _PROFILE_SETTINGS,
    _TEST_KEY,
    checked,
    completion,
    golden,
    run_connection_test,
    target,
)

_GOOD_PLAN = {
    "candidates": [{
        "algorithm_id": "de.original", "config_id": "de.default.v1",
        "rationale_fa": "این روش در سبد سازگار قرار دارد.",
        "evidence_refs": ["solver:de.original"],
    }],
    "uncertainty_notes": None,
}
_GOOD_EXPLANATION = {
    "summary_fa": "این نقطه بهترین مشاهدهٔ معتبر در بودجهٔ اجراست.",
    "selection_explanation_fa": "روش پیشنهادی برای اجرای اصلی به کار رفت.",
    "limitations_fa": ["بهینگی سراسری اثبات نشده است."],
    "evidence_refs": ["report:solution", "report:decision"],
}


@contextmanager
def local_api(data_dir, transport):
    token = secrets.token_urlsafe(32)
    app = create_app(data_dir, token, transport=transport)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", access_log=False, log_config=None, lifespan="off",
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Loopback FastAPI server did not start"
            with httpx.Client(
                base_url=address, headers={"X-PMC-Internal-Token": token}, timeout=315, trust_env=False,
            ) as client:
                yield app.state.repo, client, address, token
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Loopback FastAPI server did not stop"


def configure(client, settings, api_key):
    profile = checked(client.put("/settings/llm/profile", json={**settings, "expected_revision": None}))
    checked(client.put("/settings/llm/credential", json={
        **target(profile), "api_key": api_key, "storage_mode": "session",
    }))
    profile = checked(client.get("/settings/llm/profile"))
    connection = checked(run_connection_test(client, profile))
    assert connection["test_status"] == "passed", connection.get("error")
    return profile["profile"], connection


def submit(client, profile, problem_text, **policy_overrides):
    payload = {
        "input": {"kind": "natural_language", "text": problem_text},
        "policy": {**_POLICY, "explain_via_llm": True, **policy_overrides},
        "llm_profile": {"profile_id": profile["profile_id"], "revision": profile["revision"]},
    }
    return checked(client.post("/jobs", json=payload, headers={"Idempotency-Key": str(uuid4())}), 202)["job_id"]


def work(repo, address, token, job_id):
    owner = str(uuid4())
    job = repo.claim_job(owner, lease_seconds=1200)
    assert job is not None and job["job_id"] == job_id
    Workflow(repo, job_id, owner, api_url=address, token=token).run()


def fixture_transport(responses, called):
    """One labeled mock response per actual outbound attempt."""
    def post(request):
        body = json.loads(request.content)
        schema_name = body["response_format"]["json_schema"]["name"]
        called.append(schema_name)
        assert "X-PMC-Internal-Token" not in request.headers
        assert request.headers["Authorization"] == f"Bearer {_TEST_KEY}"
        try:
            value = responses.pop(0)
        except IndexError:
            pytest.fail(f"Unexpected provider invocation: {schema_name}")
        return value if isinstance(value, httpx.Response) else completion(value)
    return httpx.MockTransport(post)


def test_end_to_end_language_workflow(tmp_path):
    """LLM-001 and TEST FIXTURE counterpart of LOC-16."""
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN, _GOOD_EXPLANATION]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "completed", state.get("error")
        assert state["solution_status"] == "valid_best_found"
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["explanation_status"] == "generated"
        assert report["selected_algorithm_id"] == "de.original"
        assert report["optimality_status"] == "not_certified"
        assert report["objective_raw"] >= 0
        assert report["verification"]["validation_status"] == "passed"
        assert len(report["llm_calls"]) == 3
        assert report["llm_calls"] == job_call_metadata(repo, job_id)
        assert all(call["usage"] is None for call in report["llm_calls"])
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1", "explainer_v1"]
        assert not responses


def test_exhausted_formulator_allows_explicit_structured_completion(tmp_path):
    """LLM-002 / LLM-011 / SYS-017"""
    fixture = golden()
    input_text = fixture["original_text"].replace(
        "هر دو در بازهٔ [-۵,۵] هستند", "x در بازهٔ [-۵,۵] و y با کران پایین -۵ هستند"
    )
    complete = convert_formulation(fixture["response"], {fixture["source_ref"]: fixture["original_text"]})["problem"]
    ambiguous = fixture["response"]
    ambiguous["extracted_fields"]["variables"][1]["upper"] = None
    ambiguous["source_map"] = [
        item for item in ambiguous["source_map"] if item["field_path"] != "/variables/1/upper"
    ]
    called = []
    responses = [{"ok": True}, {}, ambiguous, _GOOD_PLAN, _GOOD_EXPLANATION]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, input_text)
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "needs_input", state.get("error")
        assert not repo.list_executions(job_id)
        draft = checked(client.get(f"/jobs/{job_id}/problem"))["draft"]
        assert draft["missing_fields"] == ["/variables/1/upper"]
        versions = {
            "expected_job_version": state["version"], "expected_draft_version": draft["draft_version"],
        }
        denied = checked(client.post(f"/jobs/{job_id}/answers", json={
            **versions, "mode": "textual_answers",
            "answers": [{"question_id": draft["ambiguities"][0]["question_id"], "answer": "۵"}],
        }), 422)
        assert denied["error_code"] == "LLM_BUDGET_EXHAUSTED"
        checked(client.post(f"/jobs/{job_id}/answers", json={
            **versions, "mode": "structured_completion", "problem": complete,
        }), 202)
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == "valid_best_found"
        assert called.count("formulator_v1") == 2
        assert not responses


@pytest.mark.parametrize(
    "fault_case, after_formulator, expected",
    [
        ("advisor_schema", [{"candidates": []}, {"unrecognized": True}, _GOOD_EXPLANATION], "generated"),
        ("explainer_auth", [_GOOD_PLAN, httpx.Response(401)], "provider_error"),
        ("explainer_schema", [_GOOD_PLAN,
                              {**_GOOD_EXPLANATION, "summary_fa": "این پاسخ بهینگی سراسری تضمین‌شده دارد."},
                              {**_GOOD_EXPLANATION, "summary_fa": "هیچ پاسخ بهتری وجود ندارد."}],
         "schema_fallback"),
    ],
)
def test_language_failure_keeps_valid_numerical_result(tmp_path, fault_case, after_formulator, expected):
    """LLM-005 / LLM-009 / LLM-010 / API-007 / LOC-12."""
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], *after_formulator]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed"
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == expected
        if fault_case == "advisor_schema":
            assert report["decision"]["decision_source"] == "deterministic_fallback"
            assert called.count("advisor_v1") == 2
        assert not responses


def test_advisor_auth_blocks_and_resumes_same_job(tmp_path):
    """API-011 / API-012 / LLM-006 / LOC-12."""
    fixture = golden()
    called = []
    responses = [
        {"ok": True}, fixture["response"], httpx.Response(401),
        {"ok": True}, _GOOD_PLAN, _GOOD_EXPLANATION,
    ]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "needs_configuration"
        assert state["phase"] == "planning"
        assert state["blocked_reason"] == "PROVIDER_AUTH_FAILED"
        initial_executions = [item["execution_id"] for item in repo.list_executions(job_id)]
        assert len(initial_executions) == 2
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1"]
        assert checked(run_connection_test(
            client, checked(client.get("/settings/llm/profile", params={"job_id": job_id})),
            key="fixture-restore", job_id=job_id,
        ))["test_status"] == "passed"
        checked(client.post(f"/jobs/{job_id}/resume", json={"expected_job_version": state["version"]}), 202)
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed" and report["solution_status"] == "valid_best_found"
        assert [item["execution_id"] for item in repo.list_executions(job_id)][:2] == initial_executions
        assert called.count("advisor_v1") == 2 and called.count("formulator_v1") == 1
        assert not responses


def test_default_policy_runs_three_real_candidates_and_selects_empirically(tmp_path):
    from p_mc_optimizer.solvers.registry import solver_cards

    plan = {"candidates": [
        {"algorithm_id": card["algorithm_id"], "config_id": card["config_id"],
         "rationale_fa": "این گزینه در سبد سازگار قرار دارد.",
         "evidence_refs": [f"solver:{card['algorithm_id']}"]}
        for card in reversed(solver_cards()) if card["algorithm_id"] != "cma_es.mealpy"
    ], "uncertainty_notes": None}
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], plan, _GOOD_EXPLANATION]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"],
                        selection_policy="llm_shortlist_then_pilot", max_objective_evaluations=5502)
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == "valid_best_found"
        assert report["proposal_source"] == "llm_proposal"
        assert report["decision"]["decision_source"] == "empirical_pilot_selection"
        assert report["budget"]["known_started"] == 5502
        pilots = [trial for trial in report["trials"] if trial["phase"] == "pilot"]
        assert len(pilots) == 9 and all(trial["started_evaluations"] == 500 for trial in pilots)
        order = [(trial["algorithm_id"], trial["replicate_index"]) for trial in pilots]
        assert order == sorted(order)
        assert report["explanation_status"] == "generated" and not responses


def test_two_invalid_formulations_fail_before_numerical_work(tmp_path):
    """LLM-003: bounded correction ends without a fabricated draft."""
    called = []
    responses = [{"ok": True}, {}, {"irrelevant": True}]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, golden()["original_text"])
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "failed"
        assert state["error"]["error_code"] == "LLM_SCHEMA_INVALID"
        assert state["budget"]["known_started"] == 0
        assert not repo.list_executions(job_id)
        assert called == ["connection_test_v1", "formulator_v1", "formulator_v1"]


@pytest.mark.live_provider
def test_configured_live_provider_roles_and_workflow(tmp_path):
    """LOC-03 and LOC-16. Explicit opt-in, production transport, session key only."""
    config_path = os.environ.get("PMC_TEST_LIVE_PROVIDER_CONFIG")
    if not config_path or not sys.stdin.isatty():
        pytest.skip("BLOCKED: live provider requires explicit profile JSON and interactive credential entry")
    settings = ProviderSettings.model_validate_json(Path(config_path).read_bytes()).model_dump(mode="json")
    api_key = getpass.getpass("Live provider API key (one connection test and up to six role calls): ")
    if not api_key:
        pytest.skip("BLOCKED: no provider credential was supplied")
    with local_api(tmp_path, None) as (repo, client, address, token):
        profile, connection = configure(client, settings, api_key)
        del api_key
        assert connection["metadata"]["model_requested"] == settings["model"]
        job_id = submit(client, profile, golden()["original_text"])
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed" and report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == "generated"
        assert {call["role"] for call in report["llm_calls"] if call["status"] == "succeeded"} == {
            "formulator", "advisor", "explainer",
        }
