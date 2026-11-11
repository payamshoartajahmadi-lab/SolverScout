"""Real local gateway reservations and the exact bytes seen by MockTransport."""
from hashlib import sha256
import json

import httpx
import pytest

from p_mc_optimizer.llm import gateway as gateway_module
from p_mc_optimizer.llm import credentials
from p_mc_optimizer.llm.gateway import LLMGateway
from p_mc_optimizer.llm.models import ExplainerResponse
from p_mc_optimizer.llm.provider import freeze_prompt_bundle
from p_mc_optimizer.workflow.graph import Workflow
from test_llm_gateway import (
    checked, completion, explainer_response, golden, invocation, local, ready, start_explainer,
    start_formulator,
)
from test_llm_workflow import local_api


def reserved_provenance(repo, invocation_id):
    with repo.connect() as connection:
        row = connection.execute("SELECT status,metadata FROM llm_calls WHERE invocation_id=?", (invocation_id,)).fetchone()
    assert row["status"] == "reserved"
    return json.loads(row["metadata"])["provenance"]


def test_prepared_bytes_are_durably_hashed_before_send_and_replay(local, monkeypatch):
    client, repo, _, _ = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    observed = []

    def transport(request):
        provenance = reserved_provenance(repo, body["invocation_id"])
        observed.append(provenance)
        assert sha256(request.content).hexdigest() == provenance["request_body_sha256"]
        payload = json.loads(request.content)
        assert sha256(payload["messages"][0]["content"].encode()).hexdigest() == provenance["system_message_sha256"]
        assert request.headers["content-type"] == "application/json"
        assert provenance["schema_id"] == "formulator.v1"
        assert provenance["process_run_id"] == provenance["gateway"]["process_run_id"]
        return completion(golden()["response"])

    client.app.state.gateway._transport = httpx.MockTransport(transport)
    result = checked(client.post("/internal/llm/invoke", json=body))
    assert result["status"] == "succeeded"
    assert result["metadata"]["provenance"] == observed[0]
    monkeypatch.setattr(gateway_module, "prepare_provider_request", lambda *a, **kw: pytest.fail("Replay rebuilt a request"))
    assert checked(client.post("/internal/llm/invoke", json=body)) == result
    changed = {**body, "context": {"sources": [{"source_ref": "input:text", "text": "changed"}]}}
    assert checked(client.post("/internal/llm/invoke", json=changed), 409)["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(observed) == 1


def test_repair_bytes_and_system_are_durably_hashed_against_frozen_snapshot(
    tmp_path, monkeypatch,
):
    """RT-05 repair: real bounded Workflow retry, local API, MockTransport provider."""
    monkeypatch.setattr(credentials, "_keyring_backend", lambda: None)
    network_requests = []
    original_send = httpx.HTTPTransport.handle_request

    def loopback_only(transport, request):
        # Fail before any socket can reach a real provider, including accidental fallback.
        assert request.url.host == "127.0.0.1", "Offline repair test attempted external HTTP"
        network_requests.append(str(request.url))
        return original_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", loopback_only)
    rejected = explainer_response(selected=True, summary_fa="موفقیت قطعی تضمین می‌شود.")
    accepted = explainer_response(selected=True, limitations_fa=["موفقیت قطعی تضمین نمی‌شود."])
    observed = []
    provider_requests = []
    prompts = freeze_prompt_bundle()

    def provider(request):
        provider_requests.append(request)
        payload = json.loads(request.content)
        if payload["response_format"]["json_schema"]["name"] == "connection_test_v1":
            return completion({"ok": True})

        assert request.url.host == "provider.example.test"
        with repo.connect() as connection:
            row = connection.execute(
                "SELECT * FROM llm_calls WHERE job_id=? AND role='explainer' ORDER BY attempt_no DESC LIMIT 1",
                (body["job_id"],),
            ).fetchone()
        assert row is not None and row["status"] == "reserved"
        provenance = reserved_provenance(repo, row["invocation_id"])
        assert sha256(request.content).hexdigest() == provenance["request_body_sha256"]
        system = payload["messages"][0]["content"]
        assert payload["messages"][0]["role"] == "system"
        assert sha256(system.encode("utf-8")).hexdigest() == provenance["system_message_sha256"]
        assert provenance["schema_id"] == "explainer.v1"
        assert provenance["process_run_id"] == provenance["gateway"]["process_run_id"]
        context = json.loads(payload["messages"][1]["content"])
        assert json.loads(row["input"])["context"] == context
        assert repo.get_job(body["job_id"])["workflow"]["role_context:explainer"] == snapshot
        if not observed:
            assert context == snapshot
            assert system == prompts["explainer.v1"]
        else:
            assert row["attempt_no"] == 2
            assert {key: value for key, value in context.items() if key != "repair"} == snapshot
            repair = context["repair"]
            assert json.loads(repair["previous_output"]) == rejected
            assert repair["field_errors"]
            assert system == prompts["explainer.v1"] + "\n\n" + prompts["repair.v1"]
            assert provenance["prompt_bundle_sha256"] == observed[0]["provenance"]["prompt_bundle_sha256"]
            assert provenance["request_body_sha256"] != observed[0]["provenance"]["request_body_sha256"]
            assert provenance["system_message_sha256"] != observed[0]["provenance"]["system_message_sha256"]
        observed.append({"invocation_id": row["invocation_id"], "provenance": provenance})
        return completion(rejected if len(observed) == 1 else accepted)

    with local_api(tmp_path, httpx.MockTransport(provider)) as (repo, client, address, token):
        body, owner = start_explainer(client, repo)
        snapshot = json.loads(json.dumps(body["context"]))
        workflow = Workflow(repo, body["job_id"], owner, api_url=address, token=token)
        result, error = workflow._invoke(
            "explainer", body["context"], validate=ExplainerResponse.model_validate, schema_id="explainer.v1",
        )
        assert error is None
        assert result.model_dump(mode="json") == accepted
        assert len(observed) == 2
        assert len({item["invocation_id"] for item in observed}) == 2
        assert LLMGateway(repo).count_calls(body["job_id"], "explainer") == 2
        for index, item in enumerate(observed):
            with repo.connect() as connection:
                row = connection.execute(
                    "SELECT status,metadata,error FROM llm_calls WHERE invocation_id=?", (item["invocation_id"],),
                ).fetchone()
            assert row["status"] == ("failed" if index == 0 else "succeeded")
            assert json.loads(row["metadata"])["provenance"] == item["provenance"]
            if index == 0:
                assert json.loads(row["error"])["error_code"] == "LLM_SCHEMA_INVALID"
        assert workflow.workflow["role_context:explainer"] == snapshot

    assert len(provider_requests) == 3  # One mock connection check and two mock role attempts.
    assert network_requests and all(url.startswith(address + "/") for url in network_requests)


@pytest.mark.parametrize("failure", ["timeout", "exception", "crash"])
def test_reservation_provenance_survives_unknown_provider_outcomes(local, monkeypatch, failure):
    client, repo, _, _ = local
    settings = ready(client)
    job_id = start_formulator(client, repo, settings)
    body = invocation(job_id)
    prepared = []
    original = gateway_module.prepare_provider_request

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        prepared.append(result.provenance)
        return result

    monkeypatch.setattr(gateway_module, "prepare_provider_request", prepare)

    def transport(request):
        assert reserved_provenance(repo, body["invocation_id"]) == prepared[0]
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        raise RuntimeError("fixture provider exception")

    instance = client.app.state.gateway
    instance._transport = httpx.MockTransport(transport)
    if failure == "crash":
        monkeypatch.setattr(instance, "_call", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash before send")))
        checked(client.post("/internal/llm/invoke", json=body), 500)
        assert reserved_provenance(repo, body["invocation_id"]) == prepared[0]
        instance.reconcile()
        with repo.connect() as connection:
            row = connection.execute("SELECT status,metadata FROM llm_calls WHERE invocation_id=?", (body["invocation_id"],)).fetchone()
        assert row["status"] == "outcome_unknown"
        assert json.loads(row["metadata"])["provenance"] == prepared[0]
    else:
        result = checked(client.post("/internal/llm/invoke", json=body))
        assert result["status"] == "outcome_unknown"
        assert result["metadata"]["provenance"] == prepared[0]
        assert result["metadata"].get("request_id") is None
    assert LLMGateway(repo).count_calls(job_id, "formulator") == 1
