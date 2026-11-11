"""Reject invalid numeric child handshakes without authorizing objective evaluations."""
from copy import deepcopy
import hashlib
import os

import pytest

from p_mc_optimizer.catalog.budget_policy import numeric_runtime_fingerprint
from p_mc_optimizer.catalog.configuration import digest
from p_mc_optimizer.execution import supervisor
from p_mc_optimizer.runtime_provenance import runtime_payload
from test_numeric import request

CHILD_PID = 92741


class ScriptedChannel:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def poll(self, timeout):
        return bool(self.messages)

    def recv(self):
        return self.messages.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


class ScriptedEvent:
    def set(self):
        pass


class ScriptedProcess:
    def __init__(self, channel):
        self.channel = channel
        self.pid = None
        self.stopped = False

    def start(self):
        self.pid = CHILD_PID

    def is_alive(self):
        return not self.stopped and bool(self.channel.messages)

    def join(self, timeout=None):
        pass

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def close(self):
        pass


class ScriptedContext:
    def __init__(self, messages):
        self.parent = ScriptedChannel(messages)
        self.child = ScriptedChannel([])
        self.process = ScriptedProcess(self.parent)

    def Pipe(self, duplex):
        return self.parent, self.child

    def Event(self):
        return ScriptedEvent()

    def Process(self, **kwargs):
        return self.process


def identity_message():
    payload = runtime_payload()
    payload["identity"].update(pid=CHILD_PID, role="numeric")
    fingerprint = numeric_runtime_fingerprint()
    return {
        "runtime": payload,
        "numeric_runtime_fingerprint": fingerprint,
        "numeric_runtime_fingerprint_sha256": digest(fingerprint),
    }


def scripted_run(monkeypatch, tmp_path, messages):
    context = ScriptedContext(messages)
    monkeypatch.setattr(supervisor.mp, "get_context", lambda method: context)
    monkeypatch.setattr(supervisor, "_start_isolated", lambda process: process.start())
    seen, identities = [], []
    result = supervisor.run_execution(
        request("preflight"), history_path=tmp_path/"history.jsonl",
        on_event=seen.append, on_identity=identities.append,
    )
    return result, context.parent.sent, seen, identities


@pytest.mark.parametrize("fault", [
    "invalid_payload", "wrong_pid", "wrong_role", "started_first", "result_first", "done_first",
    "duplicate_identity", "malformed_message",
])
def test_invalid_identity_or_pre_identity_message_has_explicit_zero_fe_error(monkeypatch, tmp_path, fault):
    identity = identity_message()
    messages = [("identity", identity)]
    if fault == "invalid_payload":
        messages = [("identity", {})]
    elif fault == "wrong_pid":
        identity["runtime"]["identity"]["pid"] = CHILD_PID + 1
    elif fault == "wrong_role":
        identity["runtime"]["identity"]["role"] = "worker"
    elif fault == "started_first":
        messages = [("started", 1)]
    elif fault == "result_first":
        messages = [("result", {})]
    elif fault == "done_first":
        messages = [("done", {})]
    elif fault == "duplicate_identity":
        messages.append(("identity", deepcopy(identity)))
    elif fault == "malformed_message":
        messages = [("too", "many", "parts")]

    result, sent, seen, identities = scripted_run(monkeypatch, tmp_path, messages)
    assert result.execution_status == "failed"
    assert result.error.error_code == "NUMERIC_PROTOCOL_ERROR"
    assert result.started_evaluations == result.successful_evaluations == result.failed_evaluations == 0
    assert seen == []
    assert result.history_complete
    assert (tmp_path/"history.jsonl").read_bytes() == b""
    assert result.history_sha256 == hashlib.sha256(b"").hexdigest()
    if fault == "duplicate_identity":
        assert sent == ["accepted"]
        assert result.runtime_provenance == identities[0]
    else:
        assert sent == []
        assert identities == [] and result.runtime_provenance is None


@pytest.mark.parametrize("fault", ["fingerprint_body", "fingerprint_hash"])
def test_known_fingerprint_mismatch_survives_missing_done_message(monkeypatch, tmp_path, fault):
    identity = identity_message()
    if fault == "fingerprint_body":
        identity["numeric_runtime_fingerprint"]["numpy_version"] = "different"
        identity["numeric_runtime_fingerprint_sha256"] = digest(identity["numeric_runtime_fingerprint"])
    else:
        identity["numeric_runtime_fingerprint_sha256"] = "0"*64
    result, sent, seen, identities = scripted_run(monkeypatch, tmp_path, [("identity", identity)])
    assert result.execution_status == "failed"
    assert result.error.error_code == "NUMERIC_POLICY_ENVIRONMENT_MISMATCH"
    assert result.started_evaluations == 0 and result.history_complete
    assert sent == seen == []
    assert identities == [identity["runtime"]]
    assert result.runtime_provenance == identity["runtime"]
    assert result.runtime_provenance["identity"]["pid"] == CHILD_PID


def test_death_before_identity_keeps_child_identity_unknown(monkeypatch, tmp_path):
    result, sent, seen, identities = scripted_run(monkeypatch, tmp_path, [])
    assert result.execution_status == "unknown_outcome"
    assert result.error.error_code == "NUMERIC_OUTCOME_UNKNOWN"
    assert result.runtime_provenance is None and not result.history_complete
    assert result.started_evaluations == 0 and sent == seen == identities == []
    assert not (tmp_path/"history.jsonl").exists()


def test_real_child_identity_is_saved_before_first_start(tmp_path):
    events = []

    def identity(payload):
        assert payload["identity"]["role"] == "numeric"
        assert payload["identity"]["pid"] != os.getpid()
        events.append("identity")

    def event(payload):
        if payload["kind"] == "started":
            assert events and events[0] == "identity"
            events.append(payload["sequence"])

    result = supervisor.run_execution(
        request("preflight"), history_path=tmp_path/"history.jsonl",
        on_event=event, on_identity=identity,
    )
    assert result.execution_status == "completed"
    assert events == ["identity", *range(1, 9)]
    assert result.started_evaluations == 8 and result.history_complete
