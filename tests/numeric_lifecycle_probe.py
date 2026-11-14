"""Trusted synthetic lifecycle fixtures; never modify the product deadline clock."""
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import psutil


def entered_hang(point, *, entered, entry_time, pid, creation_time, release):
    """The event proves entry into the actual objective after its start ACK."""
    pid.value = os.getpid()
    creation_time.value = psutil.Process(pid.value).create_time()
    entry_time.value = monotonic()
    entered.set()
    release.wait()  # Deliberately never released by a passing fixture.
    raise AssertionError("Synthetic hung objective unexpectedly released")


def blocked_startup(gate, entered, entered_at, target, args):
    entered_at.value = monotonic()
    entered.set()
    gate.wait()  # Deliberately blocks before the real worker sends identity.
    target(*args)


def process_state(pid, created):
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
        return {"pid": pid, "creation_time": actual,
                "same_process_running": actual == created and process.is_running()}
    except psutil.NoSuchProcess:
        return {"pid": pid, "same_process_running": False, "observation": "not_found"}


class ParentChannel:
    def __init__(self, channel, probe):
        self.channel, self.probe = channel, probe
        self.last_kind = self.last_sequence = None

    def __getattr__(self, name):
        return getattr(self.channel, name)

    def poll(self, *args, **kwargs):
        result = self.channel.poll(*args, **kwargs)
        self.probe.observe_deadline()
        return result

    def recv(self):
        message = self.channel.recv()
        self.last_kind, payload = message
        self.last_sequence = payload if self.last_kind == "started" else None
        if self.last_kind == "done":
            self.probe.done = {k: payload[k] for k in
                               ("execution_status", "termination_reason", "started_evaluations",
                                "successful_evaluations", "failed_evaluations")}
        self.probe.log("ipc_received", kind=self.last_kind, sequence=self.last_sequence,
                       done=self.probe.done if self.last_kind == "done" else None)
        return message

    def send(self, message):
        late_identity = (self.probe.mode == "late_identity_ack" and self.last_kind == "identity"
                         and message == "accepted")
        if late_identity:
            self.probe.log("identity_ack_held_until_original_deadline")
            while monotonic() <= self.probe.deadline:
                sleep(max(0.001, min(0.01, self.probe.deadline - monotonic())))
        self.probe.log("ack_send_begin", kind=self.last_kind, sequence=self.last_sequence, reply=message)
        output = self.channel.send(message)
        self.probe.log("ack_send_return", kind=self.last_kind, sequence=self.last_sequence, reply=message)
        if self.last_kind == "started" and message == "accepted":
            self.probe.accepted_sequences.append(self.last_sequence)
            self.probe.start_accepted.set()
        if late_identity:
            # The child has received an already expired, unchanged deadline. Let
            # its zero-start terminal message become readable before this test's
            # intentionally held parent callback returns to the normal loop.
            ready = self.channel.poll(0.25)
            self.probe.log("post_deadline_terminal_reply_ready", ready=ready)
        return output


class SpawnContext:
    def __init__(self, context, probe):
        self.context, self.probe = context, probe

    def __getattr__(self, name):
        return getattr(self.context, name)

    def Pipe(self, *args, **kwargs):
        parent, child = self.context.Pipe(*args, **kwargs)
        return ParentChannel(parent, self.probe), child

    def Process(self, **kwargs):
        self.probe.deadline = kwargs["args"][3]
        self.probe.product_started_at = self.probe.deadline - self.probe.item.deadline_seconds
        self.probe.log("process_constructed", deadline=self.probe.deadline,
                       product_started_at=self.probe.product_started_at)
        if self.probe.mode == "blocked_startup":
            kwargs["args"] = (self.probe.startup_gate, self.probe.startup_entered,
                              self.probe.startup_at, kwargs["target"], kwargs["args"])
            kwargs["target"] = blocked_startup
        return self.context.Process(**kwargs)


class NumericLifecycleProbe:
    """Observe IPC/reaping and coordinate entry without resetting any clock."""
    def __init__(self, monkeypatch, tmp_path, item, *, mode="entered_hang"):
        from p_mc_optimizer.execution import supervisor

        self.supervisor, self.monkeypatch = supervisor, monkeypatch
        self.tmp_path, self.item, self.mode = Path(tmp_path), item, mode
        self.original_context = supervisor.mp.get_context
        context = self.original_context("spawn")
        self.entered, self.release = context.Event(), context.Event()
        self.entry_time, self.pid, self.creation_time = context.RawValue("d", 0), context.RawValue("i", 0), context.RawValue("d", 0)
        self.startup_gate, self.startup_entered = context.Event(), context.Event()
        self.startup_at = context.RawValue("d", 0)
        self.cancel_requested, self.start_accepted = Event(), Event()
        self.events, self.accepted_sequences = [], []
        self.deadline = self.product_started_at = None
        self.identity = self.identity_created = None
        self.launched_pid = self.launched_created = None
        self.reaping = self.done = self.outcome = None
        self.thread = None
        self.errors = []
        self.cancel_seen = False
        self.deadline_seen = False

    def log(self, event, **details):
        self.events.append({"event": event, "monotonic": monotonic(), **details})

    def observe_deadline(self):
        if self.deadline is not None and monotonic() >= self.deadline and not self.deadline_seen:
            self.deadline_seen = True
            self.log("original_deadline_observed_reached", deadline=self.deadline)

    def __enter__(self):
        self.patch_context = self.monkeypatch.context()
        patch = self.patch_context.__enter__()
        original_start, original_join = self.supervisor._start_isolated, self.supervisor._join

        def start(process):
            self.log("launch_begin")
            result = original_start(process)
            self.launched_pid = process.pid
            self.launched_created = psutil.Process(process.pid).create_time()
            self.log("launch_return", pid=process.pid, creation_time=self.launched_created)
            return result

        def join(process, **kwargs):
            self.observe_deadline()
            self.log("join_begin", pid=process.pid, force=kwargs.get("force", False), alive=process.is_alive())
            result = original_join(process, **kwargs)
            self.reaping = {"multiprocessing_alive": process.is_alive(), "exitcode": process.exitcode,
                            **process_state(process.pid, self.launched_created)}
            self.log("join_return", **self.reaping)
            return result

        patch.setattr(self.supervisor.mp, "get_context", lambda method=None: SpawnContext(self.original_context(method), self))
        patch.setattr(self.supervisor, "_start_isolated", start)
        patch.setattr(self.supervisor, "_join", join)
        return self

    def identify(self, payload):
        raw = payload["identity"]
        self.identity = {key: raw[key] for key in ("pid", "role", "process_run_id")}
        self.identity_created = psutil.Process(raw["pid"]).create_time()
        self.log("identity_received", **self.identity, creation_time=self.identity_created)

    def cancelled(self):
        self.observe_deadline()
        value = self.cancel_requested.is_set()
        if value and not self.cancel_seen:
            self.cancel_seen = True
            self.log("cancellation_observed")
        return value

    def execute(self, runner, *, cancel=False, require_entry=True):
        from functools import partial

        objective = partial(entered_hang, entered=self.entered, entry_time=self.entry_time,
                            pid=self.pid, creation_time=self.creation_time, release=self.release)

        def invoke():
            try:
                self.log("supervisor_call_begin")
                self.outcome = runner(_objective_override=objective, cancelled=self.cancelled,
                                      on_identity=self.identify)
                self.log("supervisor_call_return")
            except BaseException as error:
                self.errors.append(error)
                self.log("supervisor_call_raised", exception_type=type(error).__name__)

        self.thread = Thread(target=invoke, daemon=True, name="numeric-lifecycle-fixture")
        self.thread.start()
        if require_entry:
            assert self.entered.wait(self.item.deadline_seconds + 5), "Objective-entry precondition was not reached"
            assert self.start_accepted.wait(1), "Objective entered without an observed start ACK"
            self.log("objective_entry_confirmed", pid=self.pid.value, entered_at=self.entry_time.value,
                     creation_time=self.creation_time.value)
            assert self.accepted_sequences == [1]
            assert self.identity["pid"] == self.launched_pid == self.pid.value
            assert self.identity_created == self.launched_created == self.creation_time.value
            assert self.entry_time.value < self.deadline
            if cancel:
                self.log("fixture_requests_cancel_after_entry")
                self.cancel_requested.set()
        self.thread.join(self.item.deadline_seconds + 5)
        assert not self.thread.is_alive(), "Supervisor did not return within the fixture bound"
        if self.errors:
            raise self.errors[0]
        return self.outcome

    def assert_reaped(self):
        assert self.reaping is not None
        assert self.reaping["multiprocessing_alive"] is False
        assert self.reaping["exitcode"] is not None
        assert self.reaping["same_process_running"] is False

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.thread is not None and self.thread.is_alive():
                self.log("fixture_cleanup_requests_cancel")
                self.cancel_requested.set()
                self.thread.join(self.item.deadline_seconds + 5)
            result = self.outcome[0] if self.outcome else None
            projection = ({key: getattr(result, key) for key in
                           ("execution_status", "termination_reason", "started_evaluations", "successful_evaluations",
                            "failed_evaluations", "history_complete", "history_sha256", "elapsed_compute_seconds", "overshoot_seconds")}
                          if result else None)
            history = []
            for suffix in (".jsonl", ".jsonl.part"):
                path = self.tmp_path / (self.item.execution_id + suffix)
                if path.is_file():
                    body = path.read_bytes()
                    history.append({"name": path.name, "bytes": len(body), "sha256": sha256(body).hexdigest()})
            record = {"evidence_kind": "SYNTHETIC_NUMERIC_LIFECYCLE_FIXTURE", "mode": self.mode,
                      "deadline_seconds": self.item.deadline_seconds, "product_started_at_monotonic": self.product_started_at,
                      "deadline_monotonic": self.deadline, "clock_reset": False, "events": self.events,
                      "accepted_sequences": self.accepted_sequences, "identity": self.identity,
                      "objective_entry": {"pid": self.pid.value, "monotonic": self.entry_time.value,
                                          "creation_time": self.creation_time.value},
                      "startup_gate_entered": self.startup_entered.is_set(), "startup_gate_monotonic": self.startup_at.value,
                      "done": self.done, "result": projection, "history_files": history, "reaping": self.reaping,
                      "failure_type": exc_type.__name__ if exc_type else None,
                      "observation_note": "Fixture synchronization and observer overhead are explicit; product deadline remains startup-inclusive."}
            (self.tmp_path / "numeric-lifecycle-trace.json").write_text(
                json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        finally:
            self.patch_context.__exit__(exc_type, exc, traceback)
