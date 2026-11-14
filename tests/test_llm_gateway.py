"""TEST FIXTURE transport and exact Persian DTO examples. No external request."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from p_mc_optimizer.api import create_app
from p_mc_optimizer.domain.models import ProblemInput
from p_mc_optimizer.llm import credentials
from p_mc_optimizer.llm.explanation_context import build_explainer_context
from p_mc_optimizer.llm.formulation import RoleOutputError, convert_formulation
from p_mc_optimizer.llm.gateway import LLMGateway, _hash
from p_mc_optimizer.llm.models import (
    ROLE_MODELS, ConnectionTestResponse, ExplainerContext, ExplainerResponse, FormulatorResponse,
    ProviderSettings, SecretSubmission, provider_schema,
)
from p_mc_optimizer.llm.validation import validate_role_output
from p_mc_optimizer.selection.policies import TIE_BREAK_ORDER

_TEST_TOKEN = "TEST-FIXTURE-INSTANCE-TOKEN-do-not-send-to-provider"
_TEST_KEY = "TEST-FIXTURE-KEY-sentinel-never-real"
_HEADERS = {"X-PMC-Internal-Token": _TEST_TOKEN}
_FIXTURE = Path(__file__).parent / "fixtures" / "formulator_fa_golden.json"
_PROFILE_SETTINGS = {
    "preset": "custom_openai_compatible",
    "base_url": "https://provider.example.test/explicit-base",
    "model": "test-fixture-model",
    "protocol": "chat_completions_v1",
    "response_mode": "json_schema",
    "token_limit_field": "max_completion_tokens",
    "max_output_tokens": 4096,
    "connection_test_max_output_tokens": 512,
    "request_timeout_seconds": 5,
}
_POLICY = {
    "policy_version": "1.0", "portfolio_id": "continuous_v1",
    "selection_policy": "llm_only", "fixed_algorithm_id": None,
    "max_objective_evaluations": 1002, "max_compute_seconds": 45, "master_seed": 1405,
    "explain_via_llm": False,
}


def golden():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def completion(output=None, *, text=None, finish="stop", usage=None, model="fixture-returned"):
    return httpx.Response(
        200,
        headers={"x-request-id": "fixture-request"},
        json={
            "model": model,
            "usage": usage,
            "choices": [{
                "finish_reason": finish,
                "message": {"content": text if text is not None else json.dumps(output, ensure_ascii=False)},
            }],
        },
    )


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_keyring_backend", lambda: None)
    external = []
    answers = []

    def provider(request: httpx.Request):
        external.append({
            "body": json.loads(request.content),
            "url": str(request.url),
            "uses_test_key": request.headers.get("Authorization") == f"Bearer {_TEST_KEY}",
            "has_internal_header": request.headers.get("X-PMC-Internal-Token") is not None,
        })
        if not answers:
            return completion({"ok": True})
        answer = answers.pop(0)
        if answer == "timeout":
            raise httpx.ReadTimeout("TEST FIXTURE " + _TEST_KEY, request=request)
        return answer

    app = create_app(tmp_path, _TEST_TOKEN, transport=httpx.MockTransport(provider))
    with TestClient(app, headers=_HEADERS) as client:
        yield client, app.state.repo, external, answers


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def save_profile(client, expected_revision=None, **overrides):
    payload = {**_PROFILE_SETTINGS, "expected_revision": expected_revision, **overrides}
    return checked(client.put("/settings/llm/profile", json=payload))


def target(settings, job_id=None):
    profile, status = settings["profile"], settings["credential_status"]
    return {
        "profile_id": profile["profile_id"], "profile_revision": profile["revision"],
        "expected_credential_generation": status["credential_generation"],
        "target_job_id": job_id,
    }


def set_key(client, settings, *, job_id=None, storage_mode="session"):
    return client.put("/settings/llm/credential", json={
        **target(settings, job_id), "api_key": _TEST_KEY, "storage_mode": storage_mode,
    })


def run_connection_test(client, settings, *, key="test-fixture-connection-1", job_id=None):
    return client.post(
        "/settings/llm/test", json=target(settings, job_id), headers={"Idempotency-Key": key}
    )


def ready(client, **overrides):
    settings = save_profile(client, **overrides)
    checked(set_key(client, settings))
    settings = checked(client.get("/settings/llm/profile"))
    assert checked(run_connection_test(client, settings))["test_status"] == "passed"
    return checked(client.get("/settings/llm/profile"))


def start_formulator(client, repo, settings, *, start=True):
    profile = settings["profile"]
    payload = {
        "input": {"kind": "natural_language", "text": golden()["original_text"]},
        "policy": _POLICY,
        "llm_profile": {name: profile[name] for name in ("profile_id", "revision")},
    }
    job_id = checked(client.post("/jobs", json=payload, headers={"Idempotency-Key": str(uuid4())}), 202)["job_id"]
    if start:
        owner = str(uuid4())
        assert repo.claim_job(owner)["job_id"] == job_id
        repo.update_job(job_id, phase="formulating", worker_id=owner)
    return job_id


def invocation(job_id):
    fixture = golden()
    return {
        "job_id": job_id, "role": "formulator", "schema_id": "formulator.v1",
        "invocation_id": str(uuid4()),
        "context": {"sources": [{"source_ref": fixture["source_ref"], "text": fixture["original_text"]}]},
    }


def test_empty_profile_and_get_do_not_call_provider(local):
    """API-010 / LOC-01"""
    client, _, calls, _ = local
    assert checked(client.get("/settings/llm/profile")) == {
        "profile": None, "credential_status": None, "os_keyring_available": False,
    }
    settings = save_profile(client)
    assert settings["profile"]["test_status"] == "not_tested"
    assert not settings["credential_status"]["configured"]
    assert checked(client.get("/settings/llm/models"))["supported"] is False
    assert calls == []


def test_credential_write_only_and_safe_session_restart(local):
    """API-008 / LOC-02 / SYS-018"""
    client, repo, calls, _ = local
    settings = ready(client)
    assert settings["credential_status"]["available"]
    assert SecretSubmission.model_json_schema()["properties"]["api_key"]["writeOnly"] is True
    assert _TEST_KEY not in json.dumps(settings)
    restarted = LLMGateway(repo, internal_token=_TEST_TOKEN)
    status = restarted.get_profile()["credential_status"]
    assert status["configured"] and not status["available"]
    assert len(calls) == 1


def test_connection_forms_save_keys_and_keep_settings_after_restart(tmp_path, monkeypatch):
    # Streamlit replaces __main__ while executing the test script.
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    stored = {}
    keyring = SimpleNamespace(
        get_password=lambda service, ref: stored.get((service, ref)),
        set_password=lambda service, ref, value: stored.__setitem__((service, ref), value),
    )
    monkeypatch.setattr(credentials, "_keyring_backend", lambda: keyring)

    def client():
        return TestClient(create_app(tmp_path, _TEST_TOKEN), headers=_HEADERS)

    with client() as initial:
        checked(set_key(initial, save_profile(initial)))
        search = checked(initial.get("/settings/search"))
        checked(initial.put("/settings/search/credential", json={
            "revision": search["profile"]["revision"], "generation": search["credential_generation"],
            "secret": "TEST-FIXTURE-OLD-SEARCH-KEY", "storage_mode": "session",
        }))

    def settings_page():
        import streamlit as st
        from p_mc_optimizer.ui.connections import connections_page

        def attempt(method, path, payload=None, *, idempotency_key=None):
            headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
            response = st.session_state.client.request(method, path, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

        connections_page(attempt)

    with client() as reopened:
        assert not checked(reopened.get("/settings/llm/profile"))["credential_status"]["available"]
        assert not checked(reopened.get("/settings/search"))["credential_available"]
        page = AppTest.from_function(settings_page)
        page.session_state["client"] = reopened
        page.run()
        assert not page.exception
        assert all(field.label != "کلید API جست‌وجو" for field in page.text_input)
        next(box for box in page.selectbox if box.label == "موتور جست‌وجو").select(True)
        next(button for button in page.button if button.label == "ثبت تنظیمات پژوهش").click().run()
        assert not page.exception
        for label in ("نگهداری کلید جدید", "شیوهٔ نگهداری کلید جست‌وجو"):
            assert next(box.value for box in page.selectbox if box.label == label) == "os_keyring"
        next(field for field in page.text_input if field.label == "شناسهٔ مدل").set_value("updated-test-model")
        next(field for field in page.text_input if field.label == "کلید API").set_value(_TEST_KEY)
        next(button for button in page.button if button.label == "ثبت تنظیمات").click().run()
        assert not page.exception
        next(field for field in page.text_input if field.label == "کلید API جست‌وجو").set_value("TEST-FIXTURE-SEARCH-KEY")
        next(button for button in page.button if button.label == "ثبت کلید جست‌وجو").click().run()
        assert not page.exception

    with client() as restarted:
        model = checked(restarted.get("/settings/llm/profile"))
        search = checked(restarted.get("/settings/search"))
        assert checked(restarted.get("/settings/retrieval"))["web_search_enabled"]
        assert model["profile"]["model"] == "updated-test-model"
        assert model["profile"]["test_status"] == "not_tested"
        assert model["credential_status"]["available"] and model["credential_status"]["storage_mode"] == "os_keyring"
        assert search["credential_available"] and search["storage_mode"] == "os_keyring"


def test_stale_key_write_and_origin_change_preserve_job_snapshot(local):
    """LLM-018 / LOC-04 / SYS-005 / SYS-019"""
    client, repo, calls, answers = local
    original = ready(client)
    job_id = start_formulator(client, repo, original)
    changed_model = save_profile(client, 1, model="next-test-model")
    assert changed_model["credential_status"]["available"]
    assert changed_model["profile"]["test_status"] == "not_tested"
    changed_host = save_profile(client, 2, model="new-host-model", base_url="https://other.example.test/v2")
    assert not changed_host["credential_status"]["available"]
    assert changed_host["profile"]["credential_ref"] != original["profile"]["credential_ref"]
    assert checked(set_key(client, original), 409)["error_code"] == "STALE_VERSION"
    assert checked(run_connection_test(client, changed_host, key="test-fixture-new-origin"), 422)["error_code"] == "PROVIDER_CONFIGURATION_REQUIRED"
    answers.append(completion(golden()["response"]))
    result = checked(client.post("/internal/llm/invoke", json=invocation(job_id)))
    assert result["status"] == "succeeded"
    assert repo.get_job(job_id)["llm_profile"]["model"] == original["profile"]["model"]
    assert {call["url"] for call in calls} == {"https://provider.example.test/explicit-base/chat/completions"}
    assert {call["body"]["model"] for call in calls} == {"test-fixture-model"}


def test_credential_delete_and_keyring_fail_closed(local):
    """API-009 / LOC-08 / LOC-09"""
    client, _, calls, _ = local
    settings = save_profile(client)
    result = checked(set_key(client, settings, storage_mode="os_keyring"), 422)
    assert result["error_code"] == "SECURE_KEYRING_UNAVAILABLE"
    assert not checked(client.get("/settings/llm/profile"))["credential_status"]["configured"]
    checked(set_key(client, settings))
    settings = checked(client.get("/settings/llm/profile"))
    assert checked(run_connection_test(client, settings))["test_status"] == "passed"
    result = checked(client.request("DELETE", "/settings/llm/credential", json=target(settings)))
    assert not result["configured"] and not result["available"] and result["storage_mode"] is None
    current = checked(client.get("/settings/llm/profile"))
    assert checked(run_connection_test(client, current, key="test-fixture-after-delete"), 422)["error_code"] == "PROVIDER_CONFIGURATION_REQUIRED"
    assert len(calls) == 1


@pytest.mark.parametrize("mode,token_field", [
    ("json_schema", "max_completion_tokens"), ("json_object", "max_tokens"), ("text_json", "max_tokens"),
])
def test_connection_modes_explicit_and_idempotent(local, mode, token_field):
    """LOC-05 / LOC-06 / SYS-002 / SYS-003"""
    client, _, calls, _ = local
    settings = ready(client, response_mode=mode, token_limit_field=token_field)
    result = checked(run_connection_test(client, settings))
    assert result["test_status"] == "passed" and len(calls) == 1
    outbound = calls[0]
    request = outbound["body"]
    assert outbound["uses_test_key"] and not outbound["has_internal_header"]
    assert request[token_field] == 512 and request["n"] == 1 and request["stream"] is False
    assert set(request).isdisjoint({"temperature", "seed", "reasoning_effort"})
    assert "JSON" in request["messages"][0]["content"]
    if mode == "text_json":
        assert "response_format" not in request
    else:
        assert request["response_format"]["type"] == mode
        if mode == "json_schema":
            schema = request["response_format"]["json_schema"]
            assert schema["strict"] is True
            assert schema["schema"]["properties"] == {"ok": {"type": "boolean"}}


@pytest.mark.parametrize("provider_response, expected_code", [
    (httpx.Response(401, text=_TEST_KEY), "PROVIDER_AUTH_FAILED"),
    (httpx.Response(429, headers={"Retry-After": "60"}, text=_TEST_KEY), "PROVIDER_RATE_LIMITED"),
    (httpx.Response(400, text="Unsupported response_format " + _TEST_KEY), "PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED"),
    (httpx.Response(404), "UNSUPPORTED_PROVIDER_CONFIGURATION"),
    (httpx.Response(503), "PROVIDER_SERVER_ERROR"),
    (httpx.Response(307, headers={"Location": "https://redirect.example.test/path"}), "UNSUPPORTED_PROVIDER_CONFIGURATION"),
    ("timeout", "PROVIDER_TIMEOUT"),
])
def test_provider_errors_preserve_credentials_and_never_retry(local, provider_response, expected_code, tmp_path, caplog):
    """LOC-07 / LOC-10 / DAT-018 / SYS-002"""
    client, _, calls, answers = local
    settings = save_profile(client)
    checked(set_key(client, settings))
    settings = checked(client.get("/settings/llm/profile"))
    answers.append(provider_response)
    result = checked(run_connection_test(client, settings))
    assert result["test_status"] == "failed"
    assert result["error"]["error_code"] == expected_code
    assert result["metadata"]["usage"] is None
    assert result["metadata"]["cost"] is None
    assert len(calls) == 1
    assert _TEST_KEY not in json.dumps(result) and _TEST_KEY not in caplog.text
    assert _TEST_TOKEN not in json.dumps(result) and _TEST_TOKEN not in caplog.text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert _TEST_KEY.encode() not in raw and _TEST_TOKEN.encode() not in raw


def test_gateway_rejects_wrong_role_job_auth_and_context_before_call(local):
    """LLM-017 / LLM-013"""
    client, repo, calls, _ = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    assert checked(client.post("/internal/llm/invoke", json=body, headers={"X-PMC-Internal-Token": ""}), 401)
    assert checked(client.post("/internal/llm/invoke", json={**body, "job_id": str(uuid4())}), 404)
    assert checked(client.post("/internal/llm/invoke", json={**body, "schema_id": "advisor.v1"}), 422)
    assert checked(client.post("/internal/llm/invoke", json={**body, "context": {"sources": [{"source_ref":"input:text", "text":"Forged source"}]}}), 422)
    assert checked(client.post("/internal/llm/invoke", json={**body, "context": {**body["context"], "f_star": 0}}), 422)
    current = checked(client.get(f"/jobs/{job_id}"))
    checked(client.post(f"/jobs/{job_id}/cancel", json={"expected_job_version": current["version"]}), 202)
    assert checked(client.post("/internal/llm/invoke", json=body), 409)
    assert len(calls) == 1


def test_formulator_uses_one_repair_and_deduplicates_success(local):
    """LLM-007 / LLM-011 / LLM-012 / SYS-003"""
    client, repo, calls, answers = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    answers.extend([completion({**golden()["response"], "max_llm_calls": 999}), completion(golden()["response"])])
    invalid = checked(client.post("/internal/llm/invoke", json=body))
    assert invalid["error"]["error_code"] == "LLM_SCHEMA_INVALID"
    assert invalid["previous_output"]
    repaired_body = {
        **body, "invocation_id": str(uuid4()), "context": {
            **body["context"], "repair": {
                "previous_output": invalid["previous_output"],
                "field_errors": invalid["error"]["field_errors"],
            },
        },
    }
    succeeded = checked(client.post("/internal/llm/invoke", json=repaired_body))
    assert succeeded["status"] == "succeeded"
    assert succeeded["metadata"]["usage_status"] == "unavailable"
    assert succeeded["metadata"]["usage"] is None
    assert succeeded == checked(client.post("/internal/llm/invoke", json=repaired_body))
    assert checked(client.post("/internal/llm/invoke", json={**body, "invocation_id": str(uuid4())}), 422)["error_code"] == "LLM_BUDGET_EXHAUSTED"
    assert len(calls) == 3
    assert "previous output failed" in calls[-1]["body"]["messages"][0]["content"]


def test_timeout_is_charged_unknown_and_replayable(local):
    """LLM-012 / SYS-004"""
    client, repo, calls, answers = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    answers.append("timeout")
    result = checked(client.post("/internal/llm/invoke", json=body))
    assert result["status"] == "outcome_unknown"
    assert result["metadata"]["usage"] is None
    assert result == checked(client.post("/internal/llm/invoke", json=body))
    assert len(calls) == 2


def test_job_target_uses_old_profile_and_exact_generation(local):
    """API-012 / SYS-019"""
    client, repo, calls, _ = local
    old = ready(client)
    job_id = start_formulator(client, repo, old)
    updated = save_profile(client, 1, base_url="https://other.example.test")
    repo.update_job(job_id, job_status="needs_configuration", blocked_reason="PROVIDER_AUTH_FAILED")
    job_settings = checked(client.get("/settings/llm/profile", params={"job_id": job_id}))
    assert job_settings["profile"]["revision"] == 1
    assert job_settings["profile"]["base_url"] == old["profile"]["base_url"]
    assert checked(set_key(client, updated, job_id=job_id), 409)["error_code"] == "STALE_VERSION"
    assert checked(set_key(client, job_settings, job_id=job_id))["credential_generation"] == 2
    assert checked(set_key(client, job_settings, job_id=job_id), 409)["error_code"] == "STALE_VERSION"
    current = checked(client.get("/settings/llm/profile", params={"job_id": job_id}))
    assert current["profile"]["test_status"] == "not_tested"
    assert checked(run_connection_test(client, current, key="test-fixture-resume", job_id=job_id))["test_status"] == "passed"
    state = checked(client.get(f"/jobs/{job_id}"))
    assert checked(client.post(f"/jobs/{job_id}/resume", json={"expected_job_version": state["version"]}), 202)["job_status"] == "queued"
    assert [item["url"] for item in calls] == [
        "https://provider.example.test/explicit-base/chat/completions",
        "https://provider.example.test/explicit-base/chat/completions",
    ]


def test_formulator_quotes_numeric_traceability_and_offsets():
    """DAT-016 / LLM-008 / SYS-020"""
    fixture = golden()
    sources = {fixture["source_ref"]: fixture["original_text"]}
    converted = convert_formulation(fixture["response"], sources)
    problem = ProblemInput.model_validate(converted["problem"])
    assert problem.variables[0].lower == -5
    assert not converted["missing_fields"]
    assert any(evidence["match_count"] > 1 for evidence in converted["source_map"])
    for evidence in converted["source_map"]:
        for span in evidence["matches"]:
            assert fixture["original_text"][span["start"]:span["end"]] == evidence["quote"]
    fabricated = deepcopy(fixture["response"])
    fabricated["extracted_fields"]["variables"][0]["upper"] = 8
    with pytest.raises(RoleOutputError):
        convert_formulation(fabricated, sources)
    fabricated = deepcopy(fixture["response"])
    fabricated["source_map"][0]["source_ref"] = "fabricated-ref"
    with pytest.raises(RoleOutputError):
        convert_formulation(fabricated, sources)


def test_formulator_unknown_required_values_generate_structured_questions():
    """LLM-002 / DAT-016"""
    fixture = golden()
    response = fixture["response"]
    response["extracted_fields"]["variables"][1]["upper"] = None
    response["source_map"] = [entry for entry in response["source_map"] if entry["field_path"] != "/variables/1/upper"]
    converted = convert_formulation(response, {fixture["source_ref"]: fixture["original_text"]})
    assert converted["problem"] is None
    assert converted["missing_fields"] == ["/variables/1/upper"]
    assert converted["ambiguities"][0]["reason"] == "missing"


@pytest.mark.parametrize("caveat", [
    "بهینگی سراسری تأیید نشده است و نتیجه، برتری آماری یا تضمین موفقیت را اثبات نمی‌کند.",
    "نتیجه به جست‌وجوی انجام‌شده در این اجرا محدود است؛ بهینگی کلی تأیید نشده است.",
    "این توضیح، ادعایی دربارهٔ بهینگی کلی یا برتری فراتر از این اجرا نیست.",
    "راستی‌آزمایی سازگاری بازاجرا را پشتیبانی می‌کند، اما بهینگی سراسری یا برتری آماری را اثبات نمی‌کند و موفقیت را تضمین نمی‌کند.",
])
def test_explainer_accepts_explicit_caveats_from_live_responses(caveat):
    model = ExplainerResponse(
        summary_fa="این نقطه بهترین مشاهدهٔ معتبر در بودجهٔ اجراست.",
        selection_explanation_fa=None, limitations_fa=[caveat], evidence_refs=["report:solution"],
    )
    context = ExplainerContext(report={}, evidence_refs=["report:solution"], selection_made=False)
    validate_role_output("explainer", model, context)


@pytest.mark.parametrize("claim", [
    "این جواب بهینهٔ قطعی است",
    "جواب بهتری وجود ندارد",
    "We found the smallest possible value",
    "نتیجه برتری آماری یا تضمین موفقیت را اثبات می‌کند.",
    "نتیجه تضمین موفقیت را اثبات نمی‌کند، اما این پاسخ بهینگی سراسری تضمین‌شده دارد.",
    "این گزارش موفقیت را تضمین نمی‌کند، ولی جواب سراسری اثبات شده است.",
])
def test_explainer_rejects_implied_certification(claim):
    """LLM-009"""
    model = ExplainerResponse.model_validate({
        "summary_fa": claim, "selection_explanation_fa": None,
        "limitations_fa": [], "evidence_refs": ["report:solution"],
    })
    context = ExplainerContext(report={}, evidence_refs=["report:solution"], selection_made=False)
    with pytest.raises(RoleOutputError):
        validate_role_output("explainer", model, context)


_PASSIVE_CAVEAT = "موفقیت قطعی تضمین نمی‌شود."
_COMBINED_CLAIM = "موفقیت قطعی تضمین نمی‌شود، اما هیچ جواب بهتری وجود ندارد."
_OUTER_PREFIXES = (
    "این‌طور نیست که", "این طور نیست که", "اینطور نیست که",
    "چنین نیست که", "درست نیست که", "نادرست است که",
)


def explainer_context(*, selected):
    if not selected:
        return build_explainer_context({})
    return build_explainer_context({
        "selected_algorithm_id": "de.original", "selection_status": "selected", "objective_raw": 0.0,
        "policy": {"selection_policy": "llm_shortlist_then_pilot", "pilot_repeats": 3, "pilot_evaluations": 500},
        "problem_revision": {"spec": {"objective": {"sense": "minimize"}}},
        "decision": {
            "decision_source": "empirical_pilot_selection", "selection_status": "selected",
            "selected_algorithm_id": "de.original", "scale": 1.0, "tie_break_order": TIE_BREAK_ORDER,
            "metrics": [
                {
                    "algorithm_id": algorithm_id, "eligible": True, "trial_ids": [],
                    "median_score": score, "normalized_iqr": 0.1, "reason": None,
                }
                for algorithm_id, score in (("de.original", 0.2), ("pso.original", 0.5))
            ],
        },
    })


def explainer_response(*, selected=False, **overrides):
    return {
        "summary_fa": "این نقطه بهترین مشاهدهٔ معتبر در بودجهٔ اجراست.",
        "selection_explanation_fa": "میانهٔ امتیاز پایلوت‌ها انتخاب را تعیین کرد." if selected else None,
        "limitations_fa": [],
        "evidence_refs": ["report:solution", "report:decision"] if selected else ["report:solution"],
        **overrides,
    }


@pytest.mark.parametrize("text", [
    pytest.param(_PASSIVE_CAVEAT, id="NEG-A01"),
    pytest.param("موفقیت قطعی تضمین نمی شود.", id="NEG-A02"),
    pytest.param("موفقیت قطعی تضمین نمیشود.", id="NEG-A03"),
    pytest.param("موفقیت تضمین نمی‌شود.", id="NEG-A04"),
    pytest.param("موفقيت قطعي تضمين نمي‌شود.", id="NEG-A05"),
    pytest.param("بهینگی سراسری تضمین نمی‌شود.", id="NEG-A06"),
    pytest.param("بهینگی سراسری اثبات نشده است.", id="NEG-A07"),
    pytest.param("این آزمایش برتری آماری را اثبات نمی‌کند.", id="NEG-A08"),
    pytest.param("نتیجه، برتری آماری یا تضمین موفقیت را اثبات نمی‌کند.", id="NEG-A09"),
    pytest.param(_PASSIVE_CAVEAT.replace(" ", "\t"), id="NEG-A11-tab"),
    pytest.param(_PASSIVE_CAVEAT.replace(" ", "\u00a0"), id="NEG-A11-nbsp"),
])
def test_explainer_accepts_scoped_negation(text):
    context = ExplainerContext.model_validate(explainer_context(selected=False))
    model = ExplainerResponse.model_validate(explainer_response(summary_fa=text))
    validate_role_output("explainer", model, context)
    assert model.summary_fa == text


@pytest.mark.parametrize("text", [
    pytest.param("موفقیت قطعی تضمین می‌شود.", id="NEG-R01"),
    pytest.param("موفقیت تضمین می‌شود.", id="NEG-R02"),
    pytest.param("این جواب بهینهٔ سراسری است.", id="NEG-R03"),
    pytest.param("هیچ جواب بهتری وجود ندارد.", id="NEG-R04"),
    pytest.param(_COMBINED_CLAIM, id="NEG-R05"),
    pytest.param("بهینگی سراسری تضمین نمی‌شود، ولی این جواب بهینهٔ قطعی است.", id="NEG-R06"),
    pytest.param("این‌طور نیست که موفقیت قطعی تضمین نمی‌شود.", id="NEG-R07"),
    pytest.param("این‌طور نیست که بهینگی سراسری تضمین نمی‌شود.", id="NEG-R08"),
    pytest.param("درست نیست که موفقیت تضمین نمی‌شود.", id="NEG-R09"),
    pytest.param("نادرست است که بهینگی سراسری اثبات نشده است.", id="NEG-R10"),
    *(pytest.param(f"{prefix} {_PASSIVE_CAVEAT}", id=f"NEG-R11-{index}")
      for index, prefix in enumerate(_OUTER_PREFIXES)),
    *(pytest.param(f"موفقیت قطعی تضمین{separator}نمی‌شود.", id=f"NEG-R20-{index}")
      for index, separator in enumerate(("\n", "\r", "\r\n", "\u2028", "\u2029"))),
    pytest.param("ناموفقیت قطعی تضمین نمی‌شود.", id="prefix-word-boundary"),
    pytest.param("موفقیت قطعی تضمین نمی‌شودند.", id="suffix-word-boundary"),
])
def test_explainer_rejects_certainty_outside_caveats(text):
    context = ExplainerContext.model_validate(explainer_context(selected=False))
    model = ExplainerResponse.model_validate(explainer_response(summary_fa=text))
    with pytest.raises(RoleOutputError) as exc:
        validate_role_output("explainer", model, context)
    assert exc.value.field_errors == ["explanation: unsupported certainty or optimality claim"]


@pytest.mark.parametrize("separator", ("\n", "\r", "\r\n", "\u2028", "\u2029", ".", "!", "?", "؟", "؛", ";"))
def test_explainer_outer_negation_only_applies_within_sentence(separator):
    context = ExplainerContext.model_validate(explainer_context(selected=False))
    response = explainer_response(summary_fa=f"این‌طور نیست که بررسی پایان یافته باشد{separator}{_PASSIVE_CAVEAT}")
    validate_role_output("explainer", ExplainerResponse.model_validate(response), context)


@pytest.mark.parametrize("field", ("summary_fa", "limitations_fa", "selection_explanation_fa"))
def test_explainer_scoped_negation_applies_to_each_text_field(field):
    """NEG-A01 / NEG-R05"""
    context = ExplainerContext.model_validate(explainer_context(selected=True))
    for text in (_PASSIVE_CAVEAT, _COMBINED_CLAIM):
        response = explainer_response(selected=True, **{field: [text] if field == "limitations_fa" else text})
        model = ExplainerResponse.model_validate(response)
        if text == _PASSIVE_CAVEAT:
            validate_role_output("explainer", model, context)
            assert model.model_dump() == response
        else:
            with pytest.raises(RoleOutputError) as exc:
                validate_role_output("explainer", model, context)
            assert exc.value.field_errors == ["explanation: unsupported certainty or optimality claim"]


@pytest.mark.parametrize(("selected", "overrides", "error"), [
    *(pytest.param(False, {"summary_fa": _PASSIVE_CAVEAT + digit}, "reproducing numbers", id=f"NEG-R12-{index}")
      for index, digit in enumerate("3۳٣")),
    pytest.param(False, {"evidence_refs": []}, "missing or unknown evidence", id="NEG-R13"),
    pytest.param(False, {"evidence_refs": ["unknown"]}, "missing or unknown evidence", id="NEG-R14"),
    pytest.param(False, {"selection_explanation_fa": "روش انتخاب شد."}, "no selection", id="NEG-R15"),
    pytest.param(False, {"summary_fa": " \t"}, "explanation is empty", id="NEG-R16"),
    pytest.param(True, {"selection_explanation_fa": None}, "selection explanation is required", id="NEG-R18-null"),
    pytest.param(True, {"selection_explanation_fa": " \t"}, "selection explanation is required", id="NEG-R18-empty"),
    pytest.param(True, {"evidence_refs": ["report:solution"]}, "selection evidence is required", id="NEG-R19"),
])
def test_explainer_independent_response_checks(selected, overrides, error):
    context = ExplainerContext.model_validate(explainer_context(selected=selected))
    model = ExplainerResponse.model_validate(explainer_response(selected=selected, **overrides))
    with pytest.raises(RoleOutputError) as exc:
        validate_role_output("explainer", model, context)
    assert error in exc.value.field_errors[0]


@pytest.mark.parametrize("bad_shape", ("extra", "missing"))
def test_explainer_output_schema_rejects_malformed_response(bad_shape):
    """NEG-R17"""
    data = explainer_response()
    if bad_shape == "extra":
        data["extra"] = True
    else:
        data.pop("selection_explanation_fa")
    with pytest.raises(ValidationError):
        ExplainerResponse.model_validate(data)


def start_explainer(client, repo, *, selected=True):
    profile = ready(client)["profile"]
    job_input = {
        "input": {"kind": "natural_language", "text": golden()["original_text"]},
        "policy": {
            **_POLICY, "selection_policy": "llm_shortlist_then_pilot",
            "max_objective_evaluations": 7002, "explain_via_llm": True,
        },
        "llm_profile": {name: profile[name] for name in ("profile_id", "revision")},
    }
    job_id = checked(client.post("/jobs", json=job_input, headers={"Idempotency-Key": str(uuid4())}), 202)["job_id"]
    owner = str(uuid4())
    assert repo.claim_job(owner)["job_id"] == job_id
    repo.update_job(job_id, phase="reporting", worker_id=owner)
    # The v1 gate/replay fixtures model a persisted pre-upgrade explanation contract.
    repo.put_workflow(job_id, "execution_contracts", {}, worker_id=owner)
    context = explainer_context(selected=selected)
    repo.put_workflow(job_id, "role_context:explainer", context, worker_id=owner)
    return {
        "job_id": job_id, "role": "explainer", "schema_id": "explainer.v1",
        "invocation_id": str(uuid4()), "context": context,
    }, owner


def test_gateway_explainer_passes_snapshot_and_repair_unchanged(local):
    """GATE-01 / GATE-04 / GATE-07"""
    client, repo, calls, answers = local
    body, _ = start_explainer(client, repo)
    accepted = explainer_response(selected=True, limitations_fa=[_PASSIVE_CAVEAT])
    answers.extend([completion(explainer_response(selected=True, summary_fa=_COMBINED_CLAIM)), completion(accepted)])
    first = checked(client.post("/internal/llm/invoke", json=body))
    assert first["error"]["error_code"] == "LLM_SCHEMA_INVALID"
    original_context = body["context"]
    assert json.loads(calls[-1]["body"]["messages"][1]["content"]) == original_context
    repair_context = {
        **original_context, "repair": {
            "previous_output": first["previous_output"],
            "field_errors": first["error"]["field_errors"],
        },
    }
    repair = {**body, "invocation_id": str(uuid4()), "context": repair_context}
    second = checked(client.post("/internal/llm/invoke", json=repair))
    assert second["status"] == "succeeded"
    assert second["output"] == accepted
    assert json.loads(calls[-1]["body"]["messages"][1]["content"]) == repair_context
    assert second == checked(client.post("/internal/llm/invoke", json=repair))
    changed = {**repair, "context": original_context}
    conflict = checked(client.post("/internal/llm/invoke", json=changed), 409)
    assert conflict["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(calls) == 3
    assert LLMGateway(repo).count_calls(body["job_id"], "explainer") == 2


@pytest.mark.parametrize(("field", "expected_message"), [
    *(pytest.param(field, "زمینهٔ توضیح با snapshot", id=f"GATE-02-{field}")
      for field in ("median", "winner", "mode", "ranking", "refs", "selection_made", "objective_type")),
    pytest.param("snapshot", "زمینهٔ توضیح با snapshot", id="GATE-03"),
    pytest.param("repair", "زمینهٔ توضیح با snapshot", id="GATE-05"),
    pytest.param("private", "دادهٔ غیرمجاز", id="GATE-08"),
    pytest.param("unlisted", "فیلد نامجاز", id="report-whitelist"),
    pytest.param("summary_extra", "دادهٔ نقش با قرارداد سازگار نیست", id="closed-summary-dto"),
])
def test_gateway_explainer_rejects_untrusted_context_before_reserving(local, field, expected_message):
    client, repo, calls, _ = local
    body, owner = start_explainer(client, repo)
    context = body["context"]
    report = context["report"]
    decision = report["decision_summary"]
    if field in {"median", "repair"}:
        decision["metrics"][0]["median_score"] = 0.25
        if field == "repair":
            context["repair"] = {"previous_output": "invalid", "field_errors": ["fixture"]}
    elif field == "winner":
        report["selected_algorithm_id"] = "pso.original"
    elif field == "mode":
        decision.update(
            mode="policy_assignment", decision_source="deterministic_fallback",
            metrics=[], eligible_ranking=[], tie_break_order=[], decisive_criterion=None,
        )
    elif field == "ranking":
        decision["eligible_ranking"].reverse()
    elif field == "refs":
        context["evidence_refs"].remove("report:budget")
    elif field == "selection_made":
        context["selection_made"] = False
    elif field == "objective_type":
        report["objective_raw"] = False
    elif field == "snapshot":
        repo.put_workflow(body["job_id"], "role_context:explainer", None, worker_id=owner)
    elif field == "private":
        decision["evaluation_history"] = []
    elif field == "unlisted":
        report["unlisted"] = True
    elif field == "summary_extra":
        decision["unlisted"] = True
    error = checked(client.post("/internal/llm/invoke", json=body), 422)
    assert error["error_code"] == "INVALID_SCHEMA"
    assert expected_message in error["message_fa"]
    assert len(calls) == 1
    assert LLMGateway(repo).count_calls(body["job_id"], "explainer") == 0


def test_gateway_explainer_legacy_replay_keeps_old_context_hash(local):
    """GATE-06"""
    client, repo, calls, answers = local
    body, owner = start_explainer(client, repo)
    answers.append(completion(explainer_response(selected=True)))
    previous = checked(client.post("/internal/llm/invoke", json=body))
    assert previous["status"] == "succeeded"

    # Represent a row written before decision_summary and the snapshot gate existed.
    body["context"]["report"].pop("decision_summary")
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE llm_calls SET input=?, request_hash=? WHERE invocation_id=?",
            (json.dumps(body), _hash(body), body["invocation_id"]),
        )
    repo.put_workflow(body["job_id"], "role_context:explainer", None, worker_id=owner)
    repo.update_job(body["job_id"], phase="solving", worker_id=owner)

    replay = checked(client.post("/internal/llm/invoke", json=body))
    assert replay == previous
    assert len(calls) == 2
    assert LLMGateway(repo).count_calls(body["job_id"], "explainer") == 1


def test_gateway_explainer_requires_explicit_null_when_no_decision_exists(local):
    """GATE-10"""
    client, repo, calls, answers = local
    body, owner = start_explainer(client, repo, selected=False)
    legacy_context = deepcopy(body["context"])
    assert legacy_context["report"].pop("decision_summary") is None
    repo.put_workflow(body["job_id"], "role_context:explainer", legacy_context, worker_id=owner)
    missing = checked(client.post("/internal/llm/invoke", json={**body, "context": legacy_context}), 422)
    assert missing["error_code"] == "INVALID_SCHEMA"
    assert "فیلد خلاصهٔ تصمیم" in missing["message_fa"]
    assert len(calls) == 1
    assert LLMGateway(repo).count_calls(body["job_id"], "explainer") == 0

    repo.put_workflow(body["job_id"], "role_context:explainer", body["context"], worker_id=owner)
    answers.append(completion(explainer_response()))
    accepted = checked(client.post("/internal/llm/invoke", json=body))
    assert accepted["status"] == "succeeded"
    assert json.loads(calls[-1]["body"]["messages"][1]["content"]) == body["context"]


def test_output_dtos_are_closed_and_required():
    """LLM-007 / SYS-018 / SYS-020"""
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    for model in [*ROLE_MODELS.values(), ConnectionTestResponse]:
        check(provider_schema(model))
    with pytest.raises(ValidationError):
        ConnectionTestResponse.model_validate({"ok": 1})
    with pytest.raises(ValidationError):
        FormulatorResponse.model_validate({"extracted_fields": None})
    for bad in ("http://provider.example.test", "https://user:pass@example.test", "https://provider.example.test/v1/chat/completions"):
        with pytest.raises(ValidationError):
            ProviderSettings.model_validate({**_PROFILE_SETTINGS, "base_url": bad})


def test_advisor_rejects_registry_or_evidence_changes_before_selection(local):
    """LLM-004 / LLM-007 / LLM-008 / SEL-001 / SEL-002"""
    from p_mc_optimizer.problems.registry import freeze_problem
    from p_mc_optimizer.solvers.registry import solver_cards

    client, repo, calls, answers = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    fixture = golden()
    converted = convert_formulation(fixture["response"], {fixture["source_ref"]: fixture["original_text"]})
    problem = ProblemInput.model_validate(converted["problem"])
    repo.freeze_problem(job_id, freeze_problem(problem))
    repo.update_job(job_id, phase="planning")
    body = {
        "job_id": job_id, "role": "advisor", "schema_id": "advisor.v1", "invocation_id": str(uuid4()),
        "context": {
            "problem": problem.model_dump(mode="json"),
            "profile": {
                "n_variables": 2, "lower": [-5, -5], "upper": [5, 5], "widths": [10, 10],
                "objective_sense": "minimize", "sample_count": 492, "objective_scale": 10,
                "score_min": 0, "score_median": 5, "score_max": 10, "normalized_iqr": 0.4,
                "normalized_std": 0.2, "mean_evaluation_seconds": 0.001,
            },
            "solver_cards": solver_cards(),
            "candidate_count": 1,
            "evidence_refs": ["solver:de.original"],
        },
    }
    forged = deepcopy(body)
    forged["context"]["solver_cards"][0]["configuration_snapshot"]["epoch"] = 999
    assert checked(client.post("/internal/llm/invoke", json=forged), 422)
    assert len(calls) == 1
    proposed = {
        "candidates": [{
            "algorithm_id": "de.original", "config_id": "de.default.v1",
            "rationale_fa": "روش در سبد سازگار است.", "evidence_refs": ["fabricated-ref"],
        }],
        "uncertainty_notes": None,
    }
    answers.append(completion(proposed))
    first = checked(client.post("/internal/llm/invoke", json=body))
    assert first["error"]["error_code"] == "LLM_SCHEMA_INVALID"
    proposed["candidates"][0]["evidence_refs"] = ["solver:de.original"]
    answers.append(completion(proposed))
    body["invocation_id"] = str(uuid4())
    body["context"]["repair"] = {
        "previous_output": first["previous_output"], "field_errors": first["error"]["field_errors"],
    }
    result = checked(client.post("/internal/llm/invoke", json=body))
    assert result["status"] == "succeeded"
    assert result["output"] == proposed
    assert len(calls) == 3
    context_json = calls[-1]["body"]["messages"][1]["content"]
    assert not any(value in context_json for value in (_TEST_KEY, _TEST_TOKEN, "f_star", "reference_optimum"))


def test_restart_reconciles_pending_call_without_sending_again(local, tmp_path):
    """SYS-004 / SYS-008. Model a crash between send and durable terminal write."""
    client, repo, calls, answers = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    answers.append(completion(golden()["response"]))
    result = checked(client.post("/internal/llm/invoke", json=body))
    assert result["status"] == "succeeded"
    with repo.transaction() as conn:
        conn.execute(
            """UPDATE llm_calls SET status='reserved', output=NULL, metadata=NULL,
               finished_at=NULL WHERE invocation_id=?""",
            (body["invocation_id"],),
        )

    def cannot_call(_):
        pytest.fail("Reconciled invocation must not contact the provider")

    restarted = create_app(tmp_path, _TEST_TOKEN, transport=httpx.MockTransport(cannot_call))
    with TestClient(restarted, headers=_HEADERS) as resumed:
        replayed = checked(resumed.post("/internal/llm/invoke", json=body))
        assert replayed["status"] == "outcome_unknown"
        assert replayed["error"]["error_code"] == "LLM_OUTCOME_UNKNOWN"
        assert replayed["metadata"] is None and replayed["output"] is None
        assert restarted.state.gateway.count_calls(job_id, "formulator") == 1
    assert len(calls) == 2
