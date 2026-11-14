"""Historical clarification payloads through the real gateway and workflow.

Only the provider transport is mocked. No formulator, converter, numeric stage,
answer endpoint, or budget decision is replaced.
"""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from p_mc_optimizer.llm import credentials
from p_mc_optimizer.llm.formulation import convert_formulation
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked, completion
from test_llm_workflow import (
    _GOOD_PLAN, configure, evidence_plan, local_api, submit, work,
)


FIXTURES = Path(__file__).parent / "fixtures" / "targeted_formulation"
ANSWER = "کران پایین x1 برابر -5 و کران بالای آن 5 است.\nکران پایین x2 برابر -5 و کران بالای آن 5 است."


def payload(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def mock_historical_provider(second_response, calls):
    def respond(request):
        body = json.loads(request.content)
        schema = body["response_format"]["json_schema"]["name"]
        context = json.loads(body["messages"][-1]["content"]) if schema != "connection_test_v1" else None
        calls.append({"schema": schema, "context": context})
        if schema == "connection_test_v1":
            return completion({"ok": True})
        if schema == "formulator_v1":
            attempts = sum(call["schema"] == schema for call in calls)
            assert attempts <= 2, "A third Formulator request reached the provider"
            if attempts == 1:
                return completion(payload("q08_initial"))
            actual_answers = [source for source in context["sources"] if source["source_ref"].startswith("answer:")]
            assert len(actual_answers) == 1 and actual_answers[0]["text"] == ANSWER
            response = payload(second_response)
            for item in response["source_map"]:
                if item["source_ref"].startswith("answer:"):
                    item["source_ref"] = actual_answers[0]["source_ref"]
            return completion(response)
        if schema == "advisor_v1":
            return completion(deepcopy(_GOOD_PLAN))
        assert schema == "explainer_v2"
        return completion(evidence_plan(context))

    return httpx.MockTransport(respond)


def get_state_and_draft(client, job_id):
    state = checked(client.get(f"/jobs/{job_id}"))
    draft = checked(client.get(f"/jobs/{job_id}/problem"))["draft"]
    return state, draft


def reply(client, job_id, state, draft):
    assert len(draft["ambiguities"]) == 1
    question = draft["ambiguities"][0]
    assert question["field_paths"] == [
        "/variables/0/lower", "/variables/0/upper", "/variables/1/lower", "/variables/1/upper",
    ]
    request = {
        "mode": "textual_answers", "expected_job_version": state["version"],
        "expected_draft_version": draft["draft_version"],
        "answers": [{"question_id": question["question_id"], "answer": ANSWER}],
    }
    queued = checked(client.post(f"/jobs/{job_id}/answers", json=request), 202)
    assert queued["job_id"] == job_id
    return request


def call_rows(repo, job_id):
    with repo.connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT invocation_id,role,status,error,previous_output FROM llm_calls WHERE job_id=? ORDER BY started_at,rowid",
            (job_id,),
        )]


def assert_complete(client, job_id, calls):
    state = checked(client.get(f"/jobs/{job_id}"))
    report = checked(client.get(f"/jobs/{job_id}/report"))["report"]
    assert state["job_id"] == job_id and state["job_status"] == "completed", state.get("error")
    assert report["solution_status"] == "valid_best_found"
    assert report["verification"]["validation_status"] == "passed"
    assert report["explanation_status"] == "generated"
    assert state["budget"]["known_started"] == 20000
    assert state["budget"]["reserved_active"] == state["budget"]["charged_unknown"] == 0
    assert sum(call["schema"] == "formulator_v1" for call in calls) == 2
    assert report["problem_revision"]["spec"]["variables"][0]["lower"] == -5
    assert report["problem_revision"]["spec"]["variables"][1]["upper"] == 5
    return report


def test_historical_grouped_answer_completes_same_job_in_two_formulator_calls(tmp_path, monkeypatch):
    """T03/T08: exact derived quote, real answer versions, and no duplicate work."""
    monkeypatch.setattr(credentials, "_keyring_backend", lambda: None)
    calls = []
    transport = mock_historical_provider("q08_valid_exact_quote", calls)
    with local_api(tmp_path, transport) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, (FIXTURES / "q08_input.txt").read_text(),
                        max_objective_evaluations=20000, max_compute_seconds=300)
        work(repo, address, token, job_id, legacy_explanation=False)
        state, draft = get_state_and_draft(client, job_id)
        assert state["job_status"] == "needs_input"
        assert state["problem_revision_id"] is None and state["budget"]["known_started"] == 0
        assert repo.list_executions(job_id) == []
        request = reply(client, job_id, state, draft)
        snapshot = checked(client.get(f"/jobs/{job_id}"))
        count = len(calls)
        stale = checked(client.post(f"/jobs/{job_id}/answers", json=request), 409)
        assert stale["error_code"] == "STALE_VERSION"
        assert checked(client.get(f"/jobs/{job_id}")) == snapshot
        assert len(calls) == count
        work(repo, address, token, job_id, legacy_explanation=False)
        report = assert_complete(client, job_id, calls)
        final_state = checked(client.get(f"/jobs/{job_id}"))
        assert checked(client.post(f"/jobs/{job_id}/answers", json=request), 409)["error_code"] == "STALE_VERSION"
        assert checked(client.get(f"/jobs/{job_id}")) == final_state
        assert report["problem_revision"]["spec"]["source_map"]
        assert [call["schema"] for call in calls] == [
            "connection_test_v1", "formulator_v1", "formulator_v1", "advisor_v1", "explainer_v2",
        ]


def test_rejected_historical_quote_exhausts_then_only_explicit_full_model_continues(tmp_path, monkeypatch):
    """T01/T04/T05/T08: preserve both causes; never promote the rejected model."""
    monkeypatch.setattr(credentials, "_keyring_backend", lambda: None)
    calls = []
    transport = mock_historical_provider("q08_rejected", calls)
    with local_api(tmp_path, transport) as (repo, client, address, token):
        profile, _ = configure(client, _PROFILE_SETTINGS, _TEST_KEY)
        job_id = submit(client, profile, (FIXTURES / "q08_input.txt").read_text(),
                        max_objective_evaluations=20000, max_compute_seconds=300)
        work(repo, address, token, job_id, legacy_explanation=False)
        state, draft = get_state_and_draft(client, job_id)
        reply(client, job_id, state, draft)
        work(repo, address, token, job_id, legacy_explanation=False)
        state, draft = get_state_and_draft(client, job_id)
        assert state["job_status"] == "needs_input" and state["blocked_reason"] == "LLM_BUDGET_EXHAUSTED"
        assert state["problem_revision_id"] is None and state["budget"]["known_started"] == 0
        assert repo.list_executions(job_id) == []
        rows = call_rows(repo, job_id)
        assert len(rows) == 2 and [row["status"] for row in rows] == ["succeeded", "failed"]
        error = json.loads(rows[-1]["error"])
        assert error["error_code"] == "LLM_SCHEMA_INVALID"
        assert any("/variables/1/upper" in value and "answer:" in value for value in error["field_errors"])
        assert json.loads(rows[-1]["previous_output"]) == payload("q08_rejected") | {
            "source_map": [
                {**item, "source_ref": f"answer:{draft['ambiguities'][0]['question_id']}"}
                if item["source_ref"].startswith("answer:") else item
                for item in payload("q08_rejected")["source_map"]
            ],
        }
        assert sum(call["schema"] == "formulator_v1" for call in calls) == 2
        versions = {"expected_job_version": state["version"], "expected_draft_version": draft["draft_version"]}
        duplicate = checked(client.post(f"/jobs/{job_id}/answers", json={
            **versions, "mode": "textual_answers",
            "answers": [{"question_id": draft["ambiguities"][0]["question_id"], "answer": ANSWER}],
        }), 422)
        assert duplicate["error_code"] == "INVALID_SCHEMA"
        assert len(call_rows(repo, job_id)) == 2
        assert checked(client.get(f"/jobs/{job_id}")) == state
        # A malformed or stale structured completion also cannot bypass the gate.
        assert client.post(f"/jobs/{job_id}/answers", json={
            **versions, "mode": "structured_completion", "problem": {"lower": -5, "upper": 5},
        }).status_code == 422
        valid = payload("q08_valid_exact_quote")
        answer_ref = next(item["source_ref"] for item in valid["source_map"] if item["source_ref"].startswith("answer:"))
        complete = convert_formulation(valid, {"input:text": (FIXTURES / "q08_input.txt").read_text(), answer_ref: ANSWER})["problem"]
        stale = checked(client.post(f"/jobs/{job_id}/answers", json={
            **versions, "expected_draft_version": draft["draft_version"] - 1,
            "mode": "structured_completion", "problem": complete,
        }), 409)
        assert stale["error_code"] == "STALE_VERSION" and len(call_rows(repo, job_id)) == 2
        assert checked(client.get(f"/jobs/{job_id}")) == state
        queued = checked(client.post(f"/jobs/{job_id}/answers", json={
            **versions, "mode": "structured_completion", "problem": complete,
        }), 202)
        assert queued["job_id"] == job_id
        work(repo, address, token, job_id, legacy_explanation=False)
        assert_complete(client, job_id, calls)
        assert [row["role"] for row in call_rows(repo, job_id)].count("formulator") == 2
