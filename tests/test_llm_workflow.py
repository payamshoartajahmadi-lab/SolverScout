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
    "limitations_fa": ["موفقیت قطعی تضمین نمی‌شود."],
    "evidence_refs": ["report:solution", "report:decision"],
}


def evidence_plan(context):
    """A controlled v2 response copied from the actual outbound context."""
    return {
        "schema_version": "2.0", "facts": context["facts"], "facts_hash": context["facts_hash"],
        "note_ids": [note["note_id"] for note in context["allowed_notes"][:2]],
        "evidence_refs": context["evidence_refs"],
    }


def evidence_plan_response(body):
    return evidence_plan(json.loads(body["messages"][-1]["content"]))


@contextmanager
def local_api(data_dir, transport, *, enable_lifespan=False, **app_options):
    token = secrets.token_urlsafe(32)
    app = create_app(data_dir, token, transport=transport, **app_options)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", access_log=False, log_config=None,
            lifespan="on" if enable_lifespan else "off",
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


def work(repo, address, token, job_id, *, legacy_explanation=True):
    if legacy_explanation:
        # These fixtures preserve the stored-job explainer.v1 contract.
        repo.put_workflow(job_id, "execution_contracts", {})
    elif legacy_explanation is False:
        repo.put_workflow(job_id, "execution_contracts", {"explanation": "evidence_bound_v2"})
    owner = str(uuid4())
    job = repo.claim_job(owner, lease_seconds=1200)
    assert job is not None and job["job_id"] == job_id
    Workflow(repo, job_id, owner, api_url=address, token=token).run()


def fixture_transport(responses, called, *, explainer_requests=None):
    """One labeled mock response per actual outbound attempt."""
    def post(request):
        body = json.loads(request.content)
        schema_name = body["response_format"]["json_schema"]["name"]
        called.append(schema_name)
        if schema_name in {"explainer_v1", "explainer_v2"} and explainer_requests is not None:
            explainer_requests.append(body)
        assert "X-PMC-Internal-Token" not in request.headers
        assert request.headers["Authorization"] == f"Bearer {_TEST_KEY}"
        try:
            value = responses.pop(0)
        except IndexError:
            pytest.fail(f"Unexpected provider invocation: {schema_name}")
        if callable(value):
            value = value(body)
        return value if isinstance(value, httpx.Response) else completion(value)
    return httpx.MockTransport(post)


def assert_empirical_explainer_context(context, report):
    """Assert the actual transport input agrees with the recorded pilot decision."""
    summary = context["report"]["decision_summary"]
    decision = report["decision"]
    policy = report["policy"]
    assert summary["schema_version"] == "1.0"
    assert summary["mode"] == "empirical_pilots"
    assert summary["decision_source"] == decision["decision_source"] == "empirical_pilot_selection"
    assert summary["requested_selection_policy"] == (report.get("requested_selection_policy") or policy["selection_policy"])
    assert summary["effective_selection_policy"] == (report.get("effective_selection_policy") or policy["selection_policy"])
    assert summary["pilot_repeats"] == policy["pilot_repeats"]
    assert summary["pilot_evaluations"] == policy["pilot_evaluations"]
    assert summary["scale"] == decision["scale"]
    assert summary["tie_break_order"] == decision["tie_break_order"]
    assert summary["metrics"] == [
        {key: row[key] for key in ("algorithm_id", "eligible", "trial_ids", "median_score", "normalized_iqr", "reason")}
        for row in decision["metrics"]
    ]
    ranked = sorted((row for row in summary["metrics"] if row["eligible"]),
                    key=lambda row: (row["median_score"], row["normalized_iqr"], row["algorithm_id"]))
    assert summary["eligible_ranking"] == [row["algorithm_id"] for row in ranked]
    assert context["selection_made"] == (report["selected_algorithm_id"] is not None)
    assert "report:decision" in context["evidence_refs"]
    assert context["report"]["selected_algorithm_id"] == report["selected_algorithm_id"]
    assert context["report"]["solution_origin"] == report["solution_origin"]
    assert context["report"]["budget"]["known_started"] == report["budget"]["known_started"]
    assert context["report"]["verification"] == report["verification"]
    assert "decision_summary" not in report
    assert "trials" not in context["report"]
    return summary


def test_end_to_end_language_workflow(tmp_path):
    """LLM-001, LOC-16 fixture, and FLOW-01/06 for assignment without pilots."""
    fixture = golden()
    called = []
    explainer_requests = []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN, _GOOD_EXPLANATION]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=explainer_requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "completed", state.get("error")
        assert state["solution_status"] == "valid_best_found"
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["explanation_status"] == "generated"
        assert report["explanation"] == _GOOD_EXPLANATION
        assert report["selected_algorithm_id"] == "de.original"
        assert report["optimality_status"] == "not_certified"
        assert report["objective_raw"] >= 0
        assert report["verification"]["validation_status"] == "passed"
        assert len(report["llm_calls"]) == 3
        assert report["llm_calls"] == job_call_metadata(repo, job_id)
        assert all(call["usage"] is None for call in report["llm_calls"])
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1", "explainer_v1"]
        context = json.loads(explainer_requests[0]["messages"][-1]["content"])
        assert context == repo.get_job(job_id)["workflow"]["role_context:explainer"]
        summary = context["report"]["decision_summary"]
        assert summary["mode"] == "policy_assignment"
        assert summary["decision_source"] == report["decision"]["decision_source"] == "llm_proposal"
        assert summary["effective_selection_policy"] == "llm_only"
        assert summary["metrics"] == summary["eligible_ranking"] == summary["tie_break_order"] == []
        assert summary["decisive_criterion"] is None
        assert not any(trial["phase"] == "pilot" for trial in report["trials"])
        assert not responses


def test_new_v1_defaults_v3_while_explicit_v2_fixture_preserves_explainer_v2(tmp_path):
    """EXP-01/03/15. New jobs retain the v1 envelope and use the v2 explanation."""
    fixture = golden()
    called, requests = [], []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN, evidence_plan_response]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        marker = repo.get_job(job_id)["workflow"]["execution_contracts"]
        assert marker["explanation"] == "grounded_narrative_v3"
        work(repo, address, token, job_id, legacy_explanation=False)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == "generated"
        assert report["explanation_contract_version"] == "2.0"
        assert report["explanation_evidence"]["status"] == "consistent"
        assert report["explanation_evidence"]["note_selection_source"] == "llm"
        assert report["explanation_evidence"]["facts"]["selection_reason"] == "policy_assignment"
        context = json.loads(requests[0]["messages"][-1]["content"])
        assert context == repo.get_job(job_id)["workflow"]["role_context:explainer"]
        assert context["facts"]["origin_phase"] == report["solution_origin"]["phase"]
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1", "explainer_v2"]
        assert "بر اساس سیاست ثبت‌شده" in report["explanation"]["selection_explanation_fa"]
        assert not responses


def incorrect_evidence_plan(body):
    plan = evidence_plan_response(body)
    plan["facts"] = {**plan["facts"], "selection_reason": "median_score"}
    return plan


@pytest.mark.parametrize("outcomes,expected,calls_count", [
    pytest.param([incorrect_evidence_plan, evidence_plan_response], "generated", 2, id="plan-repair"),
    pytest.param([incorrect_evidence_plan, incorrect_evidence_plan], "schema_fallback", 2, id="plan-fallback"),
    pytest.param([httpx.Response(401)], "provider_error", 1, id="provider-error"),
])
def test_new_explanation_failures_preserve_deterministic_text(tmp_path, outcomes, expected, calls_count):
    """EXP-22/25. Invalid facts cannot change the server-rendered report."""
    fixture = golden()
    called, requests = [], []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN, *outcomes]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id, legacy_explanation=False)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == expected
        assert report["explanation_evidence"]["status"] == "consistent"
        assert report["explanation_evidence"]["note_selection_source"] == ("llm" if expected == "generated" else "default")
        assert "بر اساس سیاست ثبت‌شده" in report["explanation"]["selection_explanation_fa"]
        assert "کمترین میانه" not in report["explanation"]["selection_explanation_fa"]
        assert report["budget"]["known_started"] == 1002
        assert len(requests) == calls_count
        assert report["verification"]["validation_status"] == "passed"
        if calls_count == 2:
            contexts = [json.loads(body["messages"][-1]["content"]) for body in requests]
            assert "repair" in contexts[1]
            assert {key: value for key, value in contexts[1].items() if key != "repair"} == contexts[0]
        assert not responses


def test_new_explanation_disabled_still_renders_full_evidence(tmp_path):
    """EXP-21. Disabling this role retains the server explanation and default notes."""
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"], explain_via_llm=False)
        work(repo, address, token, job_id, legacy_explanation=False)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["explanation_status"] == "not_requested"
        assert report["explanation_evidence"]["note_selection_source"] == "default"
        assert report["explanation_evidence"]["llm_schema_id"] is None
        assert report["explanation"]["summary_fa"] and report["explanation_notes"]
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1"]


@pytest.mark.parametrize("change", ["partial_result", "cancellation"])
def test_new_explanation_discards_plan_when_facts_change_after_call(tmp_path, monkeypatch, change):
    """EXP-24. A completed plan cannot describe subsequently changed facts."""
    invoke = Workflow._invoke

    def change_report(workflow, role, *args, **kwargs):
        result = invoke(workflow, role, *args, **kwargs)
        if role == "explainer":
            if change == "cancellation":
                state = repo.get_job(workflow.job_id)
                checked(client.post(f"/jobs/{workflow.job_id}/cancel", json={"expected_job_version": state["version"]}), 202)
            else:
                workflow.report["partial_result"] = True
        return result

    monkeypatch.setattr(Workflow, "_invoke", change_report)
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN, evidence_plan_response]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id, legacy_explanation=False)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == ("validation_incomplete" if change == "cancellation" else "valid_best_found")
        assert report["explanation_status"] == "schema_fallback"
        assert report["explanation_error"]["field_errors"] == ["plan.facts: report changed after invocation"]
        assert report["diagnostics"]["explanation_plan_discard_reason"] == "report_changed"
        assert report["explanation_evidence"]["facts"]["partial_result"] is True
        assert report["explanation_evidence"]["note_selection_source"] == "default"
        assert repo.get_job(job_id)["workflow"]["role_context:explainer"]["facts"]["partial_result"] is False
        explainer = [call for call in report["llm_calls"] if call["role"] == "explainer"]
        assert len(explainer) == 1 and explainer[0]["status"] == "succeeded"


def test_new_explanation_invalid_context_skips_model_and_preserves_numerics(tmp_path, monkeypatch):
    original = Workflow.build_report_data

    def inconsistent_context(workflow):
        original(workflow)
        workflow.report["decision"]["selected_algorithm_id"] = "abc.original"

    monkeypatch.setattr(Workflow, "build_report_data", inconsistent_context)
    fixture = golden()
    called = []
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id, legacy_explanation=False)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == "schema_fallback"
        assert report["explanation_evidence"]["status"] == "invalid_context"
        assert report["explanation_evidence"]["facts"] is None
        assert report["explanation_evidence"]["facts_hash"] is None
        assert report["explanation_evidence"]["llm_schema_id"] is None
        assert report["explanation"]["selection_explanation_fa"] is None
        assert report["explanation_notes"] == []
        assert report["budget"]["known_started"] == 1002
        assert not [call for call in report["llm_calls"] if call["role"] == "explainer"]


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


@pytest.mark.parametrize("prefix", ["", "/extracted_fields"], ids=["relative-paths", "wrapped-paths"])
def test_conflicting_bounds_ask_then_resume_the_same_job(tmp_path, prefix):
    """A malformed evidence map must leave room for the user's clarification."""
    expression = "(x1-4)**2 + (x2+3)**2"
    original_text = (
        f"تابع زیر را کمینه کن:\n\nf(x1, x2) = {expression}\n\n"
        "متغیرها حقیقی هستند:\n\n0 <= x1 <= 2\n-1 <= x2 <= 1\n\n"
        "مسئله تک‌هدفه و تابع قطعی است.\nقیدی به‌جز همین کران‌ها وجود ندارد.\n\n"
        "-6 <= x1 <= 6\n-6 <= x2 <= 6\n\n"
        "مسئله تک‌هدفه و تابع قطعی است.\nقیدی به‌جز این کران‌ها وجود ندارد."
    )
    bounds = ((0, 2), (-1, 1))
    response = golden()["response"]
    response["extracted_fields"]["objective"]["expression"] = expression
    quotes = {"/objective/sense": "کمینه", "/objective/expression": expression}
    questions = []
    options = {}
    for index, (variable, (lower, upper)) in enumerate(zip(response["extracted_fields"]["variables"], bounds)):
        name = f"x{index + 1}"
        variable.update(name=name, lower=None, upper=None)
        path = f"/variables/{index}"
        quotes[f"{path}/name"] = name
        quotes[f"{path}/type"] = "حقیقی"
        narrow = f"{lower} <= {name} <= {upper}"
        wide = f"-6 <= {name} <= 6"
        fields = [f"{path}/lower", f"{path}/upper"]
        questions.append({
            "field_paths": [prefix + field for field in fields], "reason": "contradictory",
            "question_fa": f"برای {name} کدام کران‌ها درست هستند: {narrow} یا {wide}؟",
        })
        options.update({field: (narrow, wide) for field in fields})
    response["source_map"] = [
        {"field_path": prefix + path, "source_ref": "input:text", "quote": quote}
        for path, quote in quotes.items()
    ] + [
        {"field_path": prefix + path, "source_ref": "input:text", "quote": quote}
        for path, choices in options.items()
        for quote in choices
    ]
    response["normalizations"] = []
    response["clarification_questions"] = questions

    called = []
    retry = {
        **response,
        "source_map": [
            {**item, "field_path": item["field_path"].removeprefix("/extracted_fields")}
            for item in response["source_map"]
        ],
        "clarification_questions": [
            {**item, "field_paths": [path.removeprefix("/extracted_fields") for path in item["field_paths"]]}
            for item in questions
        ],
    }
    responses = [{"ok": True}, response, retry]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, original_text)
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "needs_input", state
        assert state["blocked_reason"] == "AMBIGUOUS_PROBLEM"
        assert not repo.list_executions(job_id)
        assert called == ["connection_test_v1", "formulator_v1"]
        draft = checked(client.get(f"/jobs/{job_id}/problem"))["draft"]
        assert set(draft["missing_fields"]) == set(options)
        assert len(draft["ambiguities"]) == len(questions)
        assert {item["question_fa"] for item in draft["ambiguities"]} == {q["question_fa"] for q in questions}
        assert all(item["reason"] == "contradictory" for item in draft["ambiguities"])
        assert {path for item in draft["ambiguities"] for path in item["field_paths"]} == set(options)

        answers = [
            {"question_id": item["question_id"], "answer": options[item["field_path"]][0]}
            for item in draft["ambiguities"]
        ]
        queued = checked(client.post(f"/jobs/{job_id}/answers", json={
            "expected_job_version": state["version"], "expected_draft_version": draft["draft_version"],
            "mode": "textual_answers", "answers": answers,
        }), 202)
        assert queued["job_id"] == job_id and queued["job_status"] == "queued"
        final_traces = [
            {"field_path": path, "source_ref": "input:text", "quote": quote} for path, quote in quotes.items()
        ]
        for item, answer in zip(draft["ambiguities"], answers):
            final_traces.extend({
                "field_path": path, "source_ref": f"answer:{answer['question_id']}", "quote": answer["answer"],
            } for path in item["field_paths"])
        for variable, (lower, upper) in zip(response["extracted_fields"]["variables"], bounds):
            variable.update(lower=lower, upper=upper)
        responses[:] = [
            {**response, "source_map": final_traces, "clarification_questions": []},
            _GOOD_PLAN, _GOOD_EXPLANATION,
        ]
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed", report.get("error")
        assert report["solution_status"] == "valid_best_found"
        assert report["best_x"] == pytest.approx([2, -1])
        assert report["objective_raw"] == pytest.approx(8)
        assert called == ["connection_test_v1", "formulator_v1", "formulator_v1", "advisor_v1", "explainer_v1"]
        assert not responses


@pytest.mark.parametrize(
    "fault_case, after_formulator, expected",
    [
        ("advisor_schema", [{"candidates": []}, {"unrecognized": True}, _GOOD_EXPLANATION], "generated"),
        ("explainer_auth", [_GOOD_PLAN, httpx.Response(401)], "provider_error"),
        ("explainer_repair", [_GOOD_PLAN,
                              {**_GOOD_EXPLANATION, "summary_fa": "موفقیت قطعی تضمین می‌شود."},
                              _GOOD_EXPLANATION], "generated"),
        ("explainer_schema", [_GOOD_PLAN,
                              {**_GOOD_EXPLANATION, "summary_fa": "این پاسخ بهینگی سراسری تضمین‌شده دارد."},
                              {**_GOOD_EXPLANATION, "summary_fa": "هیچ پاسخ بهتری وجود ندارد."}],
         "schema_fallback"),
    ],
)
def test_language_failure_keeps_valid_numerical_result(tmp_path, fault_case, after_formulator, expected):
    """LLM-005 / LLM-009 / LLM-010 / API-007 / LOC-12 and FLOW-02/03/04."""
    fixture = golden()
    called = []
    explainer_requests = []
    responses = [{"ok": True}, fixture["response"], *after_formulator]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=explainer_requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        bundle = checked(client.get(f"/jobs/{job_id}/report"))
        report = bundle["report"]
        assert report["job_status"] == "completed"
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == expected
        assert report["verification"]["validation_status"] == "passed"
        assert report["verification"]["repeat_objective_raw"] == [report["objective_raw"]] * 2
        assert report["budget"]["known_started"] == 1002
        assert report["selected_algorithm_id"] == "de.original"
        if fault_case == "advisor_schema":
            assert report["decision"]["decision_source"] == "deterministic_fallback"
            assert called.count("advisor_v1") == 2
        contexts = [json.loads(body["messages"][-1]["content"]) for body in explainer_requests]
        assert len(contexts) == (2 if fault_case in {"explainer_repair", "explainer_schema"} else 1)
        assert contexts[0]["report"]["decision_summary"]["decision_source"] == report["decision"]["decision_source"]
        if len(contexts) == 2:
            repair = contexts[1].pop("repair")
            assert repair["field_errors"] == ["explanation: unsupported certainty or optimality claim"]
            assert contexts[0] == contexts[1] == repo.get_job(job_id)["workflow"]["role_context:explainer"]
        if expected == "generated":
            assert report["explanation"] == _GOOD_EXPLANATION
        elif fault_case == "explainer_auth":
            assert report["explanation_error"]["error_code"] == "PROVIDER_AUTH_FAILED"
        else:
            assert report["explanation_error"]["error_code"] == "LLM_SCHEMA_INVALID"
            artifact = next(row for row in bundle["artifacts"] if row["kind"] == "report_html")
            exported = client.get(f"/artifacts/{artifact['artifact_id']}").raise_for_status().text
            assert "<h2>توضیح قالبی برنامه</h2>" in exported
            assert "<code>valid_best_found</code>" in exported
        assert not responses


def test_advisor_auth_blocks_and_resumes_same_job(tmp_path):
    """API-011 / API-012 / LLM-006 / LOC-12 and GATE-09."""
    fixture = golden()
    called = []
    explainer_requests = []
    responses = [
        {"ok": True}, fixture["response"], httpx.Response(401),
        {"ok": True}, _GOOD_PLAN, {**_GOOD_EXPLANATION, "summary_fa": "موفقیت قطعی تضمین می‌شود."},
        _GOOD_EXPLANATION,
    ]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=explainer_requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        state = checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "needs_configuration"
        assert state["phase"] == "planning"
        assert state["blocked_reason"] == "PROVIDER_AUTH_FAILED"
        initial_executions = [item["execution_id"] for item in repo.list_executions(job_id)]
        assert len(initial_executions) == 2
        initial_calls = job_call_metadata(repo, job_id)
        assert [call["role"] for call in initial_calls] == ["formulator", "advisor"]
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
        assert report["explanation_status"] == "generated"
        assert report["llm_calls"][:2] == initial_calls
        attempts = [call["attempt_no"] for call in report["llm_calls"] if call["role"] == "explainer"]
        assert attempts == [1, 2]
        assert len(attempts) == report["policy"]["max_llm_calls_per_role"]
        assert len(report["llm_calls"]) == 5 <= report["policy"]["max_llm_calls"]
        workflow = repo.get_job(job_id)["workflow"]
        assert workflow["resume_count"] == 1
        assert len(explainer_requests) == 2
        for body in explainer_requests:
            context = json.loads(body["messages"][-1]["content"])
            context.pop("repair", None)
            assert context == workflow["role_context:explainer"]
        assert not responses


def _pilot_plan():
    from p_mc_optimizer.solvers.registry import solver_cards

    return {"candidates": [
        {"algorithm_id": card["algorithm_id"], "config_id": card["config_id"],
         "rationale_fa": "این گزینه در سبد سازگار قرار دارد.",
         "evidence_refs": [f"solver:{card['algorithm_id']}"]}
        for card in reversed(solver_cards()) if card["algorithm_id"] != "cma_es.mealpy"
    ], "uncertainty_notes": None}


def test_default_policy_runs_three_real_candidates_and_selects_empirically(tmp_path):
    """FLOW-05/10. Compare a real provider request and pre-fix numeric replay."""
    fixture = golden()
    called = []
    explainer_requests = []
    explanation = {**_GOOD_EXPLANATION, "selection_explanation_fa": (
        "مقایسهٔ میانهٔ امتیازهای پایلوت روش منتخب را تعیین کرد."
    )}
    responses = [{"ok": True}, fixture["response"], _pilot_plan(), explanation]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=explainer_requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"],
                        selection_policy="llm_shortlist_then_pilot", max_objective_evaluations=5502,
                        # Nine spawned pilots need startup time on Windows; this tests selection, not deadlines.
                        max_compute_seconds=300)
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
        assert report["explanation"] == explanation
        assert called.count("explainer_v1") == 1
        context = json.loads(explainer_requests[0]["messages"][-1]["content"])
        summary = assert_empirical_explainer_context(context, report)
        assert summary["decisive_criterion"] == "median_score"
        assert summary["eligible_ranking"][0] == report["selected_algorithm_id"]
        assert summary["objective_sense"] == "minimize"
        assert summary["score_semantics"] == "raw_objective"
        assert summary["score_order"] == "lower_is_better"

        baseline_path = Path(__file__).parent / "fixtures" / "explainer_numerical_baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert report["policy"]["master_seed"] == baseline["master_seed"]
        assert report["budget"]["known_started"] == baseline["known_started"]
        assert report["verification"]["validation_status"] == baseline["validation_status"]
        assert {key: report["solution_origin"][key] for key in baseline["solution_origin"]} == baseline["solution_origin"]
        for key in ("selected_algorithm_id", "selected_config_id", "solution_status"):
            assert report[key] == baseline[key]
        for key in ("best_x", "objective_raw", "score_for_minimization"):
            assert report[key] == pytest.approx(baseline[key], rel=1e-9, abs=1e-12)
        assert report["decision"]["selected_algorithm_id"] == baseline["decision"]["selected_algorithm_id"]
        for actual, expected in zip(summary["metrics"], baseline["decision"]["metrics"], strict=True):
            for key in ("algorithm_id", "eligible", "reason"):
                assert actual[key] == expected[key]
            for key in ("median_score", "normalized_iqr"):
                assert actual[key] == pytest.approx(expected[key], rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("case", ["identifier_tie", "insufficient_evidence"])
def test_explainer_receives_full_tie_or_insufficient_evidence(tmp_path, monkeypatch, case):
    """FLOW-11/12. Use genuine selector outcomes and the provider transport."""
    is_tie = case == "identifier_tie"
    if not is_tie:
        from test_workflow_exports import _inject_trial_failure

        _inject_trial_failure(monkeypatch, "pilot")
    problem_path = Path(__file__).parents[1] / "examples" / "quadratic_2d.json"
    problem = json.loads(problem_path.read_text(encoding="utf-8"))
    if is_tie:
        # Every feasible point has the same objective, regardless of pilot or seed.
        problem["objective"]["source"]["expression"] = "x1-x1+x2-x2"
    explanation = (
        {**_GOOD_EXPLANATION, "selection_explanation_fa": "شناسهٔ روش تساوی را شکست."}
        if is_tie else
        {**_GOOD_EXPLANATION, "summary_fa": "شواهد پایلوت برای انتخاب روش کافی نبود.",
         "selection_explanation_fa": None}
    )
    called, explainer_requests = [], []
    responses = [{"ok": True}, _pilot_plan(), explanation]
    with local_api(tmp_path, fixture_transport(responses, called, explainer_requests=explainer_requests)) as (
        repo, client, address, token
    ):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        payload = {
            "input": {"kind": "structured_problem", "problem": problem},
            "policy": {**_POLICY, "explain_via_llm": True, "selection_policy": "llm_shortlist_then_pilot",
                       "max_objective_evaluations": 5502},
            "llm_profile": {"profile_id": profile["profile_id"], "revision": profile["revision"]},
        }
        job_id = checked(client.post("/jobs", json=payload, headers={"Idempotency-Key": str(uuid4())}), 202)["job_id"]
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed"
        assert report["solution_status"] == "valid_best_found"
        assert report["explanation_status"] == "generated"
        assert report["explanation"] == explanation
        assert called == ["connection_test_v1", "advisor_v1", "explainer_v1"]
        assert not responses
        messages = explainer_requests[0]["messages"]
        context = json.loads(messages[-1]["content"])
        summary = assert_empirical_explainer_context(context, report)
        if is_tie:
            assert summary["decisive_criterion"] == "algorithm_id"
            assert report["selected_algorithm_id"] == summary["eligible_ranking"][0]
            assert all(row["median_score"] == row["normalized_iqr"] == 0 for row in summary["metrics"])
            instructions = messages[0]["content"]
            assert "algorithm_id" in instructions and "تساوی" in instructions
        else:
            assert report["selection_status"] == "insufficient_evidence"
            assert report["selected_algorithm_id"] is None
            assert not context["selection_made"]
            assert summary["eligible_ranking"] == []
            assert summary["decisive_criterion"] is None


@pytest.mark.parametrize("case", ["missing_decision", "mismatched_winner"])
def test_inconsistent_decision_uses_template_without_calling_explainer(tmp_path, monkeypatch, case):
    original = Workflow.build_report_data

    def build_report_with_inconsistent_decision(workflow):
        original(workflow)
        if case == "missing_decision":
            workflow.report.pop("decision")
        else:
            workflow.report["decision"]["selected_algorithm_id"] = "abc.original"

    monkeypatch.setattr(Workflow, "build_report_data", build_report_with_inconsistent_decision)
    called = []
    fixture = golden()
    responses = [{"ok": True}, fixture["response"], _GOOD_PLAN]
    with local_api(tmp_path, fixture_transport(responses, called)) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, fixture["original_text"])
        work(repo, address, token, job_id)
        report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
        assert report["job_status"] == "completed"
        assert report["explanation_status"] == "schema_fallback"
        assert report["explanation_error"]["error_code"] == "LLM_SCHEMA_INVALID"
        assert report["explanation_error"]["field_errors"]
        assert all(item.startswith("context:") for item in report["explanation_error"]["field_errors"])
        assert report["explanation"]["selection_explanation_fa"] == (
            "جزئیات معتبرِ علت انتخاب برای تولید توضیح در دسترس نیست."
        )
        assert report["selected_algorithm_id"] == "de.original"
        assert report["solution_status"] == "valid_best_found"
        assert report["verification"]["validation_status"] == "passed"
        assert report["budget"]["known_started"] == 1002
        assert called == ["connection_test_v1", "formulator_v1", "advisor_v1"]
        assert [call["role"] for call in report["llm_calls"]] == ["formulator", "advisor"]
        assert "role_context:explainer" not in repo.get_job(job_id)["workflow"]
        assert not responses


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
