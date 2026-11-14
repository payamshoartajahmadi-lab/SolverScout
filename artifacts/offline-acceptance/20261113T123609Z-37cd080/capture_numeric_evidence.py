"""Copy four saved synthetic numeric traces from one completed pytest run.

Stdlib only. Reads the named result/JUnit and numeric-lifecycle-trace.json files
one directory below that result's explicit --basetemp. Never runs tests or app
code, reads a database, or searches outside the recorded test temporary root.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET


EVIDENCE = Path(__file__).resolve().parent
WORKSPACE = EVIDENCE.parents[2]
HUNG = "test_exe_009_010_supervisor_reaps_hung_objective"
STARTUP = "test_exe_009_startup_timeout_keeps_zero_evaluations_and_correct_history"
EXPECTED = {
    HUNG + "[False]": ("hung-timeout", "entered_hang"),
    HUNG + "[True]": ("hung-cancel", "entered_hang"),
    STARTUP + "[blocked_startup]": ("startup-blocked", "blocked_startup"),
    STARTUP + "[late_identity_ack]": ("startup-late-identity-ack", "late_identity_ack"),
}
TRACE_FIELDS = {
    "evidence_kind", "mode", "deadline_seconds", "product_started_at_monotonic",
    "deadline_monotonic", "clock_reset", "events", "accepted_sequences", "identity",
    "objective_entry", "startup_gate_entered", "startup_gate_monotonic", "done", "result",
    "history_files", "reaping", "failure_type", "observation_note",
}


def digest(raw):
    return sha256(raw).hexdigest()


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def basetemp_from_command(argv):
    for index, token in enumerate(argv):
        if token.startswith("--basetemp="):
            return Path(token.split("=", 1)[1]).resolve()
        if token == "--basetemp" and index + 1 < len(argv):
            return Path(argv[index + 1]).resolve()
    raise ValueError("Completed run must record an explicit --basetemp")


def safe_trace(data):
    if not isinstance(data, dict) or set(data) - TRACE_FIELDS:
        raise ValueError("Unexpected trace fields; review before exporting")
    if data.get("evidence_kind") != "SYNTHETIC_NUMERIC_LIFECYCLE_FIXTURE":
        raise ValueError("File is not the expected synthetic numeric trace")
    secret_names = {"api_key", "access_token", "refresh_token", "client_secret", "password",
                    "cookie", "cookies", "set_cookie", "headers", "request_headers", "response_headers"}
    def inspect(value, path=()):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower().replace("-", "_") in secret_names and item is not None:
                    raise ValueError("Unexpected secret/header field in trace; values were not printed")
                inspect(item, (*path, str(key)))
        elif isinstance(value, list):
            for item in value:
                inspect(item, path)
        elif isinstance(value, str) and re.search(
            r"\b(?:sk-|tvly-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{10,}|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~-]{8,}",
            value, re.I,
        ):
            raise ValueError("Unexpected credential-shaped value in trace; value was not printed")
    inspect(data)


def case_status(case):
    if case.find("failure") is not None:
        return "FAIL"
    if case.find("error") is not None:
        return "ERROR"
    if case.find("skipped") is not None:
        return "SKIPPED"
    return "PASS"


def capture(label, explicit_basetemp):
    if not re.fullmatch(r"[a-z0-9_-]+", label):
        raise ValueError("Unsafe run label")
    result_path = EVIDENCE / "offline" / (label + "-result.json")
    run = json.loads(result_path.read_text(encoding="utf-8"))
    if not run.get("finished_at_utc") or run.get("run_label") != label:
        raise ValueError("The named run is not complete")
    if Path(run["cwd"]).resolve() != WORKSPACE:
        raise ValueError("Completed run belongs to another workspace")
    basetemp = Path(explicit_basetemp).resolve()
    if basetemp != basetemp_from_command(run["command_argv"]):
        raise ValueError("Basetemp is not the exact path recorded for this run")
    if basetemp == Path(basetemp.anchor) or basetemp == WORKSPACE or not basetemp.is_dir():
        raise ValueError("Expected a dedicated existing pytest basetemp")
    allowed = [WORKSPACE / ".pytest-tmp", Path(tempfile.gettempdir()).resolve()]
    if not any(basetemp != scope and basetemp.is_relative_to(scope) for scope in allowed):
        raise ValueError("Basetemp is outside the dedicated project or OS temporary test scope")
    junit_path = (EVIDENCE / run["junit"]).resolve()
    if not junit_path.is_relative_to(EVIDENCE / "offline"):
        raise ValueError("Unexpected JUnit path")
    cases = [case for case in ET.parse(junit_path).getroot().iter("testcase")
             if case.get("classname") == "tests.test_numeric" and case.get("name") in EXPECTED]
    if len(cases) != 4 or {case.get("name") for case in cases} != set(EXPECTED):
        raise ValueError("Completed JUnit must contain each of the four numeric fixture cases once")
    output = EVIDENCE / ("numeric-" + label)
    if output.exists():
        raise ValueError("Refusing to overwrite a previous capture")

    # pytest tmp_path truncates the sanitized node name to 30 characters, then
    # gives successive cases sharing that prefix monotonically numbered dirs.
    # The JUnit order and recorded counters bind every path to its real nodeid;
    # trace modes and cancellation events provide independent cross-checks.
    groups = {}
    for case in cases:
        prefix = re.sub(r"[\W]", "_", case.get("name"))[:30]
        groups.setdefault(prefix, []).append(case)
    paths = []
    for path in basetemp.glob("*/numeric-lifecycle-trace.json"):
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(basetemp):
            raise ValueError("Unsafe trace path in explicit basetemp")
        paths.append(path)
    if len(paths) != 4:
        raise ValueError("Expected exactly four saved lifecycle traces in the explicit run basetemp")
    captures = []
    for prefix, family_cases in groups.items():
        numbered = []
        for path in paths:
            match = re.fullmatch(re.escape(prefix) + r"([0-9]+)", path.parent.name)
            if match:
                numbered.append((int(match.group(1)), path))
        numbered.sort()
        if len(numbered) != len(family_cases):
            raise ValueError("Trace directory counters do not match this JUnit fixture family")
        for case, (counter, path) in zip(family_cases, numbered, strict=True):
            raw = path.read_bytes()
            data = json.loads(raw)
            safe_trace(data)
            key, expected_mode = EXPECTED[case.get("name")]
            if data.get("mode") != expected_mode:
                raise ValueError("Trace mode does not match its JUnit case mapping")
            events = data.get("events") or []
            requested_cancel = any(event.get("event") == "fixture_requests_cancel_after_entry" for event in events)
            if key == "hung-timeout" and requested_cancel:
                raise ValueError("Cancellation event conflicts with the timeout case mapping")
            if key == "hung-cancel" and case_status(case) == "PASS" and not requested_cancel:
                raise ValueError("Passing cancellation case is missing its actual cancellation event")
            captures.append({"key": key, "raw": raw, "data": data,
                             "record": {"test_nodeid": "tests/test_numeric.py::" + case.get("name"),
                                        "test_status": case_status(case), "junit_time_seconds": float(case.get("time", "0")),
                                        "source_path": str(path), "source_relative_to_basetemp": path.relative_to(basetemp).as_posix(),
                                        "pytest_tmp_directory_counter": counter,
                                        "source_size_bytes": len(raw), "source_sha256": digest(raw),
                                        "destination_path": "traces/" + key + ".json", "mode": data.get("mode"),
                                        "cancellation_requested_after_entry": requested_cancel,
                                        "clock_reset": data.get("clock_reset"), "failure_type": data.get("failure_type"),
                                        "result": data.get("result"), "accepted_sequences": data.get("accepted_sequences"),
                                        "reaping": data.get("reaping")}})
    if len({capture["record"]["source_path"] for capture in captures}) != 4:
        raise ValueError("Trace mapping is not one-to-one")
    (output / "traces").mkdir(parents=True)
    for capture in captures:
        destination = output / capture["record"]["destination_path"]
        with destination.open("xb") as stream:
            stream.write(capture["raw"])
    provenance = {"evidence_kind": "FRESH_SAVED_SYNTHETIC_NUMERIC_FIXTURE_TRACES",
                  "captured_at_utc": datetime.now(timezone.utc).isoformat(), "run_label": label,
                  "source_sha": run["head_sha"], "source_manifest_sha256": run.get("source_sha256"),
                  "harness_manifest_sha256": run.get("harness_sha256"),
                  "source_changes_at_start": run.get("source_changes_at_start"),
                  "source_bytes_unchanged_during_run": run.get("source_bytes_unchanged_during_run"),
                  "harness_bytes_unchanged_during_run": run.get("harness_bytes_unchanged_during_run"),
                  "recorded_basetemp": str(basetemp),
                  "source_result": {"path": result_path.relative_to(EVIDENCE).as_posix(), "sha256": digest(result_path.read_bytes())},
                  "source_junit": {"path": junit_path.relative_to(EVIDENCE).as_posix(), "sha256": digest(junit_path.read_bytes())},
                  "capture_script_sha256": digest(Path(__file__).read_bytes()),
                  "mapping_method": "Exact completed JUnit nodeids in fixture-family order matched to pytest sanitized 30-character tmp_path prefix and numeric directory counter; trace modes and actual cancellation events cross-checked.",
                  "copies_are_byte_exact": True, "redactions_performed": False,
                  "trace_count": 4, "traces": [capture["record"] for capture in captures],
                  "new_test_execution": "NOT_RUN_BY_CAPTURE", "new_model_calls": 0,
                  "new_objective_evaluations": 0, "new_solver_calls": 0,
                  "limits": "Saved synthetic fixture observations, not a live model acceptance result. No database was read or exported."}
    write_json(output / "capture.json", provenance)
    files = []
    for path in [output / "capture.json", *sorted((output / "traces").glob("*.json"))]:
        files.append({"path": path.relative_to(output).as_posix(), "size_bytes": path.stat().st_size,
                      "sha256": digest(path.read_bytes())})
    write_json(output / "evidence_index.json", {"run_label": label, "source_sha": run["head_sha"], "files": files})
    print(json.dumps({"output_directory": str(output), "source_sha": run["head_sha"], "trace_count": 4,
                      "files": files}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--basetemp", required=True)
    args = parser.parse_args()
    capture(args.run_label, args.basetemp)
