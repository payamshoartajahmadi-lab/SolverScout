"""Real local gateway reservations and the exact bytes seen by MockTransport."""
from hashlib import sha256
import json

import httpx
import pytest

from p_mc_optimizer.llm import gateway as gateway_module
from p_mc_optimizer.llm.gateway import LLMGateway
from test_llm_gateway import checked, completion, golden, invocation, local, ready, start_formulator


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
