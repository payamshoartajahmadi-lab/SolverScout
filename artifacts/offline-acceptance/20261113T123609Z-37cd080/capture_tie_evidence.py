"""Read only one completed synthetic identifier-tie test database.

Stdlib only. No app import, test invocation, schema mutation, database export,
credential/profile-table access, directory search, or network operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET


EVIDENCE = Path(__file__).resolve().parent
WORKSPACE = EVIDENCE.parents[2]
TEST_NODEID = "tests/test_llm_workflow.py::test_explainer_receives_full_tie_or_insufficient_evidence[identifier_tie]"
TEST_NAME = TEST_NODEID.split("::", 1)[1]
TEST_CLASS = "tests.test_llm_workflow"
CALL_SQL = """SELECT call_id,job_id,role,invocation_id,request_hash,attempt_no,
                     prompt_version,input,output,previous_output,status,error,started_at,finished_at
              FROM llm_calls WHERE job_id=? AND role='explainer' AND purpose='job_role'
              ORDER BY started_at,rowid"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw):
    return sha256(raw).hexdigest()


def parse_json(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def safe_copy(value, redactions, path=()):
    """Redact actual secret fields/strings, preserving public execution contracts."""
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            location = (*path, str(key))
            # These fields carry secrets or connection headers by type/path.
            # An authorization object is not categorically a secret: in
            # particular numeric_execution.authorization remains untouched.
            if normalized in {"api_key", "access_token", "refresh_token", "client_secret",
                              "password", "cookie", "cookies", "set_cookie", "http_headers",
                              "request_headers", "response_headers", "connection_headers"}:
                if item is not None:
                    redactions.append({"path": ".".join(location), "reason": "secret_or_connection_header_field"})
                    output[key] = "[REDACTED]"
                else:
                    output[key] = None
            elif normalized == "headers" and isinstance(item, dict):
                redactions.append({"path": ".".join(location), "reason": "connection_header_mapping"})
                output[key] = "[REDACTED]"
            else:
                output[key] = safe_copy(item, redactions, location)
        return output
    if isinstance(value, list):
        return [safe_copy(item, redactions, (*path, str(i))) for i, item in enumerate(value)]
    if not isinstance(value, str):
        return value
    text = "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c
                   for c in value if ord(c) >= 32 or c in "\r\n\t")
    patterns = [
        r"\b(?:sk-|tvly-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{6,}",
        r"\bAKIA[A-Z0-9]{16}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>;,]+",
        r"(?i)(?<![\w.])(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*[^\r\n]+",
        r"(?i)(?<![\w.])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s,;]+)",
        r"(?i)https?://[^\s/@]+:[^\s/@]+@[^\s\"'<>]+",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    if text != value:
        redactions.append({"path": ".".join(path), "reason": "credential_string_or_control_character"})
    return text


def save(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def recorded_basetemp(argv):
    for index, token in enumerate(argv):
        if token.startswith("--basetemp="):
            return Path(token.split("=", 1)[1]).resolve()
        if token == "--basetemp" and index + 1 < len(argv):
            return Path(argv[index + 1]).resolve()
    raise ValueError("The completed run must record its explicit --basetemp")


def case_status(case):
    if case.find("failure") is not None:
        return "FAIL"
    if case.find("error") is not None:
        return "ERROR"
    if case.find("skipped") is not None:
        return "SKIPPED"
    return "PASS"


def recorded_test_root(run, basetemp):
    """Bind the exact completed case to its pytest tmp_path without a DB search."""
    junit_path = (EVIDENCE / run["junit"]).resolve()
    if not junit_path.is_relative_to(EVIDENCE / "offline"):
        raise ValueError("Unexpected JUnit path")
    cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    matching = [case for case in cases if case.get("classname") == TEST_CLASS
                and case.get("name") == TEST_NAME]
    if len(matching) != 1 or case_status(matching[0]) == "SKIPPED":
        raise ValueError("Completed JUnit must record this exact executed synthetic case once")
    target = matching[0]
    prefix = re.sub(r"[\W]", "_", TEST_NAME)[:30]
    family = [case for case in cases
              if re.sub(r"[\W]", "_", case.get("name", ""))[:30] == prefix]
    if any(case.get("classname") != TEST_CLASS or case_status(case) == "SKIPPED"
           for case in family):
        raise ValueError("Ambiguous JUnit tmp_path prefix family")
    numbered = []
    for path in basetemp.iterdir():
        match = re.fullmatch(re.escape(prefix) + r"([0-9]+)", path.name)
        if not match:
            continue
        if not path.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(basetemp):
            raise ValueError("Unsafe case directory in explicit basetemp")
        numbered.append((int(match.group(1)), path.resolve()))
    numbered.sort()
    if len(numbered) != len(family):
        raise ValueError("JUnit case family and numbered pytest temporary directories differ")
    index = family.index(target)
    counter, root = numbered[index]
    return root, {"path": junit_path.relative_to(EVIDENCE).as_posix(),
                  "sha256": digest(junit_path.read_bytes()), "test_nodeid": TEST_NODEID,
                  "test_status": case_status(target),
                  "time_seconds": float(target.get("time", "0")),
                  "pytest_tmp_directory_counter": counter,
                  "case_directory_relative_to_basetemp": root.relative_to(basetemp).as_posix(),
                  "mapping_method": "Exact completed JUnit testcase matched in family order to pytest sanitized 30-character tmp_path prefix and numeric directory counter. Only the mapped case database was opened."}


def capture(label, basetemp):
    if not re.fullmatch(r"[a-z0-9_-]+", label):
        raise ValueError("Unsafe run label")
    result_path = EVIDENCE / "offline" / (label + "-result.json")
    run = json.loads(result_path.read_text(encoding="utf-8"))
    if not run.get("finished_at_utc") or run.get("run_label") != label:
        raise ValueError("The named run has not completed")
    if Path(run["cwd"]).resolve() != WORKSPACE:
        raise ValueError("Run does not identify this workspace")
    basetemp = Path(basetemp).resolve()
    if basetemp != recorded_basetemp(run["command_argv"]):
        raise ValueError("Explicit basetemp does not match the completed run")
    allowed = [WORKSPACE / ".pytest-tmp", Path(tempfile.gettempdir()).resolve()]
    if not any(basetemp != scope and basetemp.is_relative_to(scope) for scope in allowed):
        raise ValueError("Basetemp is outside the dedicated project or OS temporary test scope")
    test_root, junit_provenance = recorded_test_root(run, basetemp)
    db = test_root / "state.sqlite3"
    if not db.is_file() or db.is_symlink():
        raise ValueError("Expected synthetic test database is missing or unsafe")
    output = EVIDENCE / ("tie-" + label)
    if output.exists():
        raise ValueError("Refusing to replace an earlier capture")

    # URI read-only plus query_only; no connection to profile/credential tables.
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        reports = connection.execute("SELECT job_id,payload,created_at FROM reports").fetchall()
        if len(reports) != 1:
            raise ValueError("Expected exactly one report in the explicitly scoped test database")
        stored = reports[0]
        rows = connection.execute(CALL_SQL, (stored["job_id"],)).fetchall()
    finally:
        connection.close()

    raw_report = stored["payload"].encode("utf-8")
    original_report = json.loads(raw_report)
    redactions = []
    report = safe_copy(original_report, redactions, ("report",))
    calls = []
    for row in rows:
        original_input = json.loads(row["input"])
        original_context = original_input.get("context")
        previous = parse_json(row["previous_output"])
        call = {key: row[key] for key in ("call_id", "job_id", "role", "invocation_id", "request_hash",
                                         "attempt_no", "prompt_version", "status", "started_at", "finished_at")}
        call.update(schema_id=original_input.get("schema_id"),
                    context=original_context, output=parse_json(row["output"]),
                    previous_output=previous, error=parse_json(row["error"]),
                    source_input_payload_sha256=digest(row["input"].encode("utf-8")),
                    source_context_canonical_sha256=digest(canonical(original_context)),
                    source_previous_output_sha256=(digest(row["previous_output"].encode("utf-8"))
                                                   if row["previous_output"] is not None else None))
        calls.append(safe_copy(call, redactions, ("explainer_calls", str(row["attempt_no"]))))

    projections = []
    for call in calls:
        context = call["context"] or {}
        previous = call["previous_output"]
        returned = call["output"] if call["output"] is not None else previous
        projections.append({"attempt_no": call["attempt_no"], "status": call["status"],
                            "selection_made": context.get("selection_made"),
                            "decision_summary": (context.get("report") or {}).get("decision_summary"),
                            "first_or_current_error": call["error"],
                            "repair_field_errors": (context.get("repair") or {}).get("field_errors"),
                            "stored_mock_output_keys": sorted(returned) if isinstance(returned, dict) else None,
                            "stored_fixed_mock_selection_claim": (returned.get("selection_explanation_fa")
                                                                  if isinstance(returned, dict) else None)})
    provenance = {"evidence_kind": "FRESH_STORED_OFFLINE_SYNTHETIC_TEST",
                  "captured_at_utc": datetime.now(timezone.utc).isoformat(), "run_label": label,
                  "test_nodeid": TEST_NODEID, "source_sha": run["head_sha"],
                  "source_manifest_sha256": run.get("source_sha256"),
                  "harness_manifest_sha256": run.get("harness_sha256"),
                  "source_changes_at_start": run.get("source_changes_at_start"),
                  "source_bytes_unchanged_during_run": run.get("source_bytes_unchanged_during_run"),
                  "harness_bytes_unchanged_during_run": run.get("harness_bytes_unchanged_during_run"),
                  "source_junit": junit_provenance, "recorded_basetemp": str(basetemp),
                  "source_database_path": str(db), "database_access": "SQLite mode=ro and PRAGMA query_only=ON",
                  "database_bytes_exported": False,
                  "selected_tables": {"reports": ["job_id", "payload", "created_at"],
                                      "llm_calls": ["call_id", "job_id", "role", "invocation_id", "request_hash",
                                                    "attempt_no", "prompt_version", "input", "output",
                                                    "previous_output", "status", "error", "started_at", "finished_at"]},
                  "source_report_storage": "reports.payload UTF-8 bytes",
                  "source_report_payload_sha256": digest(raw_report),
                  "source_context_hash_definition": "SHA-256 of sorted compact UTF-8 JSON of the context object inside stored llm_calls.input",
                  "context_evidence_limit": "Actual persisted gateway invocation contexts. HTTP body bytes were not independently captured; no reconstructed wire transcript is claimed.",
                  "source_result_path": result_path.relative_to(EVIDENCE).as_posix(),
                  "source_result_sha256": digest(result_path.read_bytes()),
                  "capture_script_sha256": digest(Path(__file__).read_bytes()),
                  "sanitized_copy": bool(redactions), "redactions": redactions,
                  "numeric_execution_authorization_policy": "Public contract objects preserved; no blanket authorization-key removal",
                  "new_test_execution": "NOT_RUN_BY_CAPTURE", "new_model_calls": 0,
                  "new_objective_evaluations": 0, "new_solver_calls": 0}
    diagnostic = {**provenance, "job_id": stored["job_id"], "report_created_at": stored["created_at"],
                  "report_projection": {key: report.get(key) for key in (
                      "job_status", "phase", "termination_reason", "selection_status", "selected_algorithm_id",
                      "decision", "explanation_status", "explanation_error", "proposal_source", "proposal_error",
                      "policy", "budget", "timing", "profile", "candidate_plan", "progress", "diagnostics")},
                  "all_recorded_trials": report.get("trials"),
                  "recorded_pilot_count": sum(t.get("phase") == "pilot" for t in report.get("trials", [])),
                  "explainer_call_count": len(calls), "explainer_call_projection": projections,
                  "interpretation_limit": "Fresh records only. No inferred OS/drive latency cause or historical diagnosis substituted."}
    output.mkdir()
    report_path = output / "report.json"
    if any(item["path"].startswith("report.") for item in redactions):
        save(report_path, report)
    else:
        with report_path.open("xb") as stream:
            stream.write(raw_report)
    save(output / "explainer-invocations.json", {"provenance": provenance, "calls": calls})
    save(output / "diagnosis.json", diagnostic)
    files = [{"path": path.name, "size_bytes": path.stat().st_size, "sha256": digest(path.read_bytes())}
             for path in (report_path, output / "explainer-invocations.json", output / "diagnosis.json")]
    save(output / "evidence_index.json", {"run_label": label, "source_sha": run["head_sha"], "files": files})
    print(json.dumps({"output_directory": str(output), "source_sha": run["head_sha"],
                      "job_id": stored["job_id"], "explainer_calls": len(calls),
                      "recorded_pilots": diagnostic["recorded_pilot_count"],
                      "sanitized_copy": bool(redactions), "files": files}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--basetemp", required=True)
    arguments = parser.parse_args()
    capture(arguments.run_label, arguments.basetemp)
