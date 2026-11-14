"""External pytest observation plugin for the unchanged hung-objective fixture.

Only the two existing test_exe_009_010 cases are instrumented. Wrappers forward
the exact original request, deadline, IPC messages, cancellation return values
and join behavior. The picklable objective wrapper forwards to original hang.
Instrumentation adds imports, shared-memory writes and observation overhead;
these traces supplement the separate uninstrumented reproduction.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from time import monotonic

import pytest


class ObjectiveEntryProbe:
    """Spawn-safe, one-way observation immediately before the original objective."""

    def __init__(self, objective, entry_time, entry_pid):
        self.objective = objective
        self.entry_time = entry_time
        self.entry_pid = entry_pid

    def __call__(self, *args, **kwargs):
        self.entry_time.value = monotonic()
        self.entry_pid.value = os.getpid()
        return self.objective(*args, **kwargs)


class ParentPipeProbe:
    """Only the parent endpoint is wrapped; the child endpoint stays unchanged."""

    def __init__(self, channel, state):
        self.channel = channel
        self.state = state
        self.last_event = None
        self.last_sequence = None

    def __getattr__(self, name):
        return getattr(self.channel, name)

    def poll(self, *args, **kwargs):
        value = self.channel.poll(*args, **kwargs)
        self.state.observe_shared()
        self.state.observe_deadline("parent_poll_return")
        return value

    def recv(self):
        message = self.channel.recv()
        if isinstance(message, tuple) and len(message) == 2:
            kind, payload = message
            self.last_event = kind
            self.last_sequence = payload if kind == "started" else None
            details = {"kind": kind}
            if kind == "identity" and isinstance(payload, dict):
                identity = (payload.get("runtime") or {}).get("identity") or {}
                details.update({key: identity.get(key) for key in
                                ("pid", "role", "process_run_id", "source_manifest_sha256")})
            elif kind == "started":
                details["sequence"] = payload
            elif kind == "result" and isinstance(payload, dict):
                details.update({key: payload.get(key) for key in ("sequence", "status")})
            elif kind == "done" and isinstance(payload, dict):
                details.update({key: payload.get(key) for key in
                                ("execution_status", "termination_reason", "started_evaluations",
                                 "successful_evaluations", "failed_evaluations", "rejected_proposals")})
                details["error_code"] = (payload.get("error") or {}).get("error_code")
                self.state.done_projection = dict(details)
            self.state.event("parent_received_ipc", **details)
        else:
            self.state.event("parent_received_unexpected_message_shape")
        return message

    def send(self, message):
        # Never record arbitrary payloads. These are the existing protocol's ACKs.
        safe = message if message in ("accepted", "cancelled", "timed_out") else "OTHER"
        details = {"reply": safe, "for_kind": self.last_event, "sequence": self.last_sequence}
        self.state.event("parent_ack_send_begin", **details)
        result = self.channel.send(message)
        self.state.event("parent_ack_send_return", **details)
        if message == "accepted" and self.last_event == "started":
            self.state.accepted_sequences.append(self.last_sequence)
        return result


class ContextProbe:
    def __init__(self, context, state):
        self.context = context
        self.state = state

    def __getattr__(self, name):
        return getattr(self.context, name)

    def Pipe(self, *args, **kwargs):
        parent, child = self.context.Pipe(*args, **kwargs)
        return ParentPipeProbe(parent, self.state), child

    def Process(self, *args, **kwargs):
        arguments = kwargs.get("args", ())
        if len(arguments) >= 4 and isinstance(arguments[3], (int, float)):
            self.state.deadline = arguments[3]
            seconds = self.state.request_projection.get("deadline_seconds")
            self.state.product_started_at = self.state.deadline - seconds if seconds is not None else None
            self.state.event("child_process_constructed", deadline_monotonic=self.state.deadline,
                             product_started_at_monotonic=self.state.product_started_at)
        return self.context.Process(*args, **kwargs)


class TraceState:
    def __init__(self, nodeid, context):
        self.nodeid = nodeid
        self.events = []
        self.request_projection = {}
        self.result_projection = None
        self.done_projection = None
        self.deadline = None
        self.product_started_at = None
        self.deadline_seen = False
        self.entry_time = context.RawValue("d", 0.0)
        self.entry_pid = context.RawValue("i", 0)
        self.original_objective_pid = None
        self.entry_seen = False
        self.original_seen = False
        self.accepted_sequences = []
        self.launched_pid = None
        self.launched_creation_time = None
        self.final_reaping = None
        self.cancel_true_seen = False
        self.cancel_poll_count = 0
        self.exception_type = None
        self.history_path = None

    def event(self, name, **details):
        self.events.append({"event": name, "observed_at_monotonic": monotonic(), **details})

    def observe_shared(self):
        if self.entry_pid.value and not self.entry_seen:
            self.entry_seen = True
            self.event("objective_wrapper_entry_observed", pid=self.entry_pid.value,
                       entered_at_monotonic=self.entry_time.value,
                       semantics="Wrapper entered immediately before forwarding to unchanged original objective")
        if self.original_objective_pid is not None and self.original_objective_pid.value and not self.original_seen:
            self.original_seen = True
            self.event("original_hang_pid_observed", pid=self.original_objective_pid.value,
                       semantics="Original hang wrote this PID itself; observation time is an upper bound on that write")

    def observe_deadline(self, observer):
        now = monotonic()
        if self.deadline is not None and now >= self.deadline and not self.deadline_seen:
            self.deadline_seen = True
            self.event("deadline_observed_reached", deadline_monotonic=self.deadline, observer=observer,
                       semantics="Observer saw the unchanged product deadline expire; not an injected timeout")


def process_observation(pid, expected_creation_time=None):
    """Use PID plus creation time, so PID reuse is never a live-child assertion."""
    import psutil

    try:
        process = psutil.Process(pid)
        created = process.create_time()
        same = expected_creation_time is None or created == expected_creation_time
        return {"pid": pid, "creation_time": created, "same_process": same,
                "same_process_running": same and process.is_running(),
                "status": process.status()}
    except psutil.NoSuchProcess:
        return {"pid": pid, "same_process_running": False, "observation": "not_found"}
    except (psutil.AccessDenied, OSError) as error:
        return {"pid": pid, "same_process_running": None, "observation_error_type": type(error).__name__}


@pytest.fixture(autouse=True)
def trace_existing_numeric_case(request, monkeypatch):
    if not request.node.name.startswith("test_exe_009_010_supervisor_reaps_hung_objective["):
        yield
        return

    from p_mc_optimizer.execution import supervisor

    original_context = supervisor.mp.get_context
    raw_context = original_context("spawn")
    state = TraceState(request.node.nodeid, raw_context)
    original_start = supervisor._start_isolated
    original_join = supervisor._join
    original_run = request.module.run_execution

    def context_probe(method=None):
        return ContextProbe(original_context(method), state)

    def start_probe(process):
        state.event("child_launch_begin")
        result = original_start(process)
        state.launched_pid = process.pid
        observed = process_observation(process.pid)
        state.launched_creation_time = observed.get("creation_time")
        state.event("child_launch_return", **observed)
        return result

    def join_probe(process, **kwargs):
        state.observe_shared()
        state.observe_deadline("before_original_join")
        state.event("child_join_begin", pid=process.pid, force=kwargs.get("force", False),
                    alive=process.is_alive(), exitcode=process.exitcode)
        result = original_join(process, **kwargs)
        observed = process_observation(process.pid, state.launched_creation_time)
        state.final_reaping = {"multiprocessing_alive": process.is_alive(),
                               "exitcode": process.exitcode, **observed}
        state.event("child_join_return", **state.final_reaping)
        state.observe_shared()
        return result

    def run_probe(item, **kwargs):
        state.request_projection = {key: getattr(item, key) for key in
                                    ("job_id", "execution_id", "phase", "deadline_seconds", "evaluation_quota")}
        state.history_path = Path(kwargs["history_path"])
        state.event("run_execution_entered", **state.request_projection)
        original_cancel = kwargs.get("cancelled", lambda: False)
        original_event = kwargs.get("on_event")
        original_identity = kwargs.get("on_identity")
        objective = kwargs.get("_objective_override")
        if objective is not None:
            state.original_objective_pid = getattr(objective, "keywords", {}).get("process_id")
            kwargs["_objective_override"] = ObjectiveEntryProbe(objective, state.entry_time, state.entry_pid)

        def cancelled():
            state.cancel_poll_count += 1
            result = original_cancel()
            state.observe_shared()
            state.observe_deadline("original_cancellation_predicate_return")
            if result and not state.cancel_true_seen:
                state.cancel_true_seen = True
                state.event("original_cancellation_predicate_true")
            return result

        def event(payload):
            state.event("on_event_callback", kind=payload.get("kind"), sequence=payload.get("sequence"),
                        semantics="A started callback precedes, and alone does not prove, the acceptance ACK")
            return original_event(payload) if original_event else None

        def identity(payload):
            raw = payload.get("identity") or {}
            state.event("on_identity_callback", pid=raw.get("pid"), role=raw.get("role"),
                        process_run_id=raw.get("process_run_id"))
            return original_identity(payload) if original_identity else None

        kwargs.update(cancelled=cancelled, on_event=event, on_identity=identity)
        try:
            result = original_run(item, **kwargs)
            state.result_projection = {key: getattr(result, key) for key in
                                       ("execution_status", "termination_reason", "started_evaluations",
                                        "successful_evaluations", "failed_evaluations", "history_complete",
                                        "history_sha256", "elapsed_compute_seconds", "overshoot_seconds")}
            state.result_projection["error_code"] = result.error.error_code if result.error else None
            provenance = result.runtime_provenance or {}
            raw = provenance.get("identity") or {}
            state.result_projection["runtime_identity_projection"] = {key: raw.get(key) for key in
                                                                       ("pid", "role", "process_run_id")}
            state.event("run_execution_returned", **state.result_projection)
            return result
        except BaseException as error:
            state.exception_type = type(error).__name__
            state.event("run_execution_raised", exception_type=state.exception_type)
            raise
        finally:
            state.observe_shared()

    monkeypatch.setattr(supervisor.mp, "get_context", context_probe)
    monkeypatch.setattr(supervisor, "_start_isolated", start_probe)
    monkeypatch.setattr(supervisor, "_join", join_probe)
    monkeypatch.setattr(request.module, "run_execution", run_probe)
    try:
        yield
    finally:
        state.observe_shared()
        source_root = Path(request.module.__file__).resolve().parents[1]
        source_paths = ["tests/test_numeric.py", "src/p_mc_optimizer/execution/supervisor.py",
                        "src/p_mc_optimizer/execution/evaluator.py", "src/p_mc_optimizer/runtime_provenance.py"]
        history = []
        if state.history_path is not None:
            for path in (state.history_path, state.history_path.with_name(state.history_path.name + ".part")):
                if path.is_file():
                    body = path.read_bytes()
                    history.append({"name": path.name, "bytes": len(body), "lines": len(body.splitlines()),
                                    "sha256": sha256(body).hexdigest()})
        guard_path = os.environ.get("SOLVERSCOUT_OFFLINE_GUARD_REPORT")
        if guard_path:
            guard = Path(guard_path)
            output_dir = guard.parent
            label = guard.name.removesuffix("-network-guard.json")
        else:
            output_dir = Path(__file__).resolve().parent / "offline"
            label = "numeric-probe"
        output_dir.mkdir(parents=True, exist_ok=True)
        case = re.sub(r"[^A-Za-z0-9_-]+", "-", request.node.name).strip("-")
        target = output_dir / (label + "-" + case + "-trace.json")
        record = {
            "evidence_kind": "INSTRUMENTED_EXISTING_OFFLINE_TEST_TRACE",
            "test_nodeid": state.nodeid,
            "probe_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_files": [{"path": path, "sha256": sha256((source_root / path).read_bytes()).hexdigest()}
                             for path in source_paths],
            "request_unchanged": state.request_projection,
            "clock_origin": "Original supervisor clock, started before child launch; deadline observed from unchanged Process args",
            "product_started_at_monotonic": state.product_started_at,
            "deadline_monotonic": state.deadline,
            "events": state.events,
            "accepted_evaluation_sequences": state.accepted_sequences,
            "objective_wrapper_entry": {"pid": state.entry_pid.value or None,
                                        "monotonic": state.entry_time.value or None},
            "original_hang_pid": state.original_objective_pid.value if state.original_objective_pid is not None else None,
            "cancellation_predicate_poll_count": state.cancel_poll_count,
            "cancellation_predicate_returned_true": state.cancel_true_seen,
            "done_projection": state.done_projection,
            "result_projection": state.result_projection,
            "history_files": history,
            "reaping": state.final_reaping,
            "exception_type": state.exception_type,
            "limitations": [
                "Instrumented diagnostic, separate from unchanged baseline; observer imports and shared-memory/IPC logging may alter timing.",
                "Objective wrapper timestamp precedes forwarding to original hang; original hang PID is independently observed from its existing shared value.",
                "Deadline observation is observer timing, not a modified product clock or a synthetic deadline event.",
                "No credential, environment dump, connection header, objective vector or provider payload is exported.",
            ],
        }
        with target.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
