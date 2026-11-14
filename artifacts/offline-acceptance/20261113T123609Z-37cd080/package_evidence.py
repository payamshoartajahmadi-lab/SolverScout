"""Package completed, explicitly selected offline evidence; stdlib only.

Run only after build_report.py and capture_final_provenance.py have completed.
No product/test imports, test execution, database reads, or network operations.
This tool never redacts originals: unexpected secret-bearing data stops export.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile


B = Path(__file__).resolve().parent
ARCHIVE = "SolverScout_Offline_Acceptance_Evidence.zip"
INDEX = "evidence_index.json"
TARGETED_RUNS = ("targeted-01", "targeted-02", "targeted-03")
BASE_RUNS = ("reproduce-01", "reproduce-02", "reproduce-03", "reproduce-04",
             "numeric-probe-01", *TARGETED_RUNS)
RUN_SUFFIXES = (".log", ".xml", "-result.json", "-source_manifest.json",
                "-harness_manifest.json", "-network-guard.json", "-progress.jsonl")
TRACE_NAMES = ("hung-timeout.json", "hung-cancel.json", "startup-blocked.json",
               "startup-late-identity-ack.json")
TOP_FILES = {
    ".gitattributes", ".gitignore", "Offline_Acceptance_Status_FA.md", "summary.json",
    "source_manifest.json", "harness_manifest.json", "baseline.json", "environment.json",
    "diagnosis-before-fix.json", "numeric-review.json", "preservation-check.json",
    "offline-fixtures.patch", "Next_Live_Prerequisites_FA.md", "git-provenance.json",
    "remote-before-publication.json", "build_report.py", "capture_tie_evidence.py",
    "capture_numeric_evidence.py", "capture_final_provenance.py", "numeric_trace.py",
    "offline_network_guard.py", "run_offline.py", "source_hashes.py", "package_evidence.py",
}
# Add a diagnostic subtree file only after its exact relative path is reviewed.
# An unknown subtree is rejected without descending into it.
OPTIONAL_FILES = {"full-run-environment-diagnosis.json", "full-run-environment-verification.json"}
ENVIRONMENT_EVIDENCE_FILES = ("preliminary.json", "catalog-controlled_failure.jsonl")
PRIVATE_COMPONENTS = {"_private_qa", "_private_test_state", "__pycache__", ".git",
                      ".pytest_cache", ".pytest-tmp", "temp", "tmp", "credentials",
                      "credential_store", "cookies", "headers"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite3-wal",
                    ".sqlite3-shm", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx"}
SECRET_KEYS = {"api_key", "apikey", "access_token", "refresh_token", "client_secret",
               "password", "cookie", "cookies", "set_cookie", "headers",
               "request_headers", "response_headers", "proxy_authorization"}
SECRET_VALUES = re.compile(
    r"\b(?:sk-|tvly-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{16,}"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|https?://[^\s/@:]+:[^\s/@]+@"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return sha256(raw).hexdigest()


def normalized(relative):
    value = str(relative).replace("\\", "/")
    parts = PurePosixPath(value).parts
    require(value and not value.startswith("/") and ":" not in value
            and all(part not in {"", ".", ".."} for part in parts), "Unsafe evidence-relative path")
    return "/".join(parts)


def private_reason(relative):
    parts = [part.lower() for part in PurePosixPath(relative).parts]
    if any(part in PRIVATE_COMPONENTS or part.startswith("pytest-of-") for part in parts):
        return "Private, temporary, or cache directory; contents not read"
    name = parts[-1]
    if name == ".env" or name.startswith(".env.") or name.startswith(("credentials.", "cookies.")):
        return "Environment or credential filename; contents not read"
    if any(name.endswith(suffix) for suffix in PRIVATE_SUFFIXES):
        return "Database, secret-key, or compiled-cache file type; contents not read"
    return None


def safe_path(relative):
    relative = normalized(relative)
    require(private_reason(relative) is None, "Forbidden selected path: " + relative)
    current = B
    for part in PurePosixPath(relative).parts:
        current = current / part
        require(not current.is_symlink() and not current.is_junction(), "Link/reparse path rejected: " + relative)
    require(current.resolve().is_relative_to(B), "Evidence path escaped package root")
    return current


def read_json(relative):
    return json.loads(safe_path(relative).read_bytes())


def scan_data(relative, raw):
    text = raw.decode("utf-8-sig")
    require(not SECRET_VALUES.search(text), "Credential-shaped content requires review: " + relative)

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                key = str(key).lower().replace("-", "_")
                require(not (key in SECRET_KEYS and item not in (None, "", [], {})),
                        "Secret/header field requires review; value omitted: " + relative)
                # Public numeric_execution.authorization objects remain intact.
                if key == "authorization" and isinstance(item, str):
                    require(not item.strip(), "String authorization field requires review: " + relative)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    if relative.endswith(".json"):
        visit(json.loads(text))
    elif relative.endswith(".jsonl"):
        for line in text.splitlines():
            if line.strip():
                visit(json.loads(line))


def run_relation(run):
    return {"run_label": run["run_label"], "source_sha": run["head_sha"],
            "source_sha256": run.get("source_sha256"), "harness_sha256": run.get("harness_sha256"),
            "source_changes_at_start": run.get("source_changes_at_start"),
            "version_relationship": ("Recorded run HEAD plus its actual source/harness byte manifests; "
                                     "dirty or untracked test changes are not attributed to the HEAD tree.")}


def main():
    require(not (B / ARCHIVE).exists(), "Refusing to overwrite the existing evidence ZIP")
    require(not (B / INDEX).exists(), "Refusing to overwrite the existing top-level evidence index")
    selected = {}

    def select(relative, scenario, relation):
        relative = normalized(relative)
        require(relative not in selected, "Duplicate selected file: " + relative)
        require(safe_path(relative).is_file(), "Required evidence missing: " + relative)
        selected[relative] = {"scenario": scenario, **relation}

    summary = read_json("summary.json")
    tested = summary["final_tested_commit"]
    final_label = summary.get("final_run_label", "full-final-01")
    require(re.fullmatch(r"full-final-[0-9]{2}", final_label) is not None, "Invalid final full-run label")
    full_runs = tuple(sorted(path.name.removesuffix("-result.json")
                             for path in safe_path("offline").iterdir()
                             if re.fullmatch(r"full-final-[0-9]{2}-result\.json", path.name)))
    require(final_label in full_runs, "Selected final full run has no completed result")
    run_labels = (*BASE_RUNS, *full_runs)
    final_runs = (*TARGETED_RUNS, final_label)
    baseline_sha = read_json("baseline.json")["head"]
    extra_files = {name for name in OPTIONAL_FILES if safe_path(name).exists()}
    extra_files.update("full-run-environment-evidence/" + name for name in ENVIRONMENT_EVIDENCE_FILES)
    for relative in sorted(TOP_FILES | extra_files):
        relation = {"source_sha": tested, "version_relationship": "Final offline evidence for the recorded tested commit"}
        if relative.endswith(".py"):
            relation = {"source_sha": None, "version_relationship": "Evidence tooling bytes only; not an application/test execution or a claim this tool was part of the tested source commit"}
        elif relative in {"baseline.json", "diagnosis-before-fix.json", "numeric-review.json"}:
            relation = {"source_sha": baseline_sha, "version_relationship": "Baseline/diagnosis; individual runs and dirty fixtures retain their own recorded provenance"}
        elif relative == "preservation-check.json":
            relation = {"source_sha": baseline_sha, "version_relationship": "Preservation of earlier evidence, original worktree, historical documents, and stash; see per-file expected commits and hashes"}
        elif relative == "full-run-environment-diagnosis.json" or relative.startswith("full-run-environment-evidence/"):
            relation = run_relation(read_json("offline/full-final-01-result.json"))
            relation["version_relationship"] = "Read-only environment diagnosis of full-final-01 and its frozen source/harness bytes; not attributed to a later full run"
        select(relative, "offline_acceptance_package", relation)

    runs = {}
    for label in run_labels:
        run = read_json("offline/" + label + "-result.json")
        require(run.get("run_label") == label and bool(run.get("finished_at_utc"))
                and type(run.get("exit_code")) is int, "Run is incomplete or mislabeled: " + label)
        runs[label] = run
        for suffix in RUN_SUFFIXES:
            select("offline/" + label + suffix, "completed_offline_run", run_relation(run))
        for key, suffix in (("log", ".log"), ("junit", ".xml"), ("guard_report", "-network-guard.json")):
            require(normalized(run[key]) == "offline/" + label + suffix, "Unexpected run artifact path: " + label)
        cases = list(ET.parse(safe_path("offline/" + label + ".xml")).getroot().iter("testcase"))
        counts = {"tests": len(cases), "failures": sum(c.find("failure") is not None for c in cases),
                  "errors": sum(c.find("error") is not None for c in cases),
                  "skipped": sum(c.find("skipped") is not None for c in cases),
                  "passed": sum(all(c.find(tag) is None for tag in ("failure", "error", "skipped")) for c in cases)}
        require(counts == run["junit_counts"], "JUnit/result counts disagree: " + label)
    require({r["run_label"]: r for r in summary["runs"]} == runs, "Summary is stale or omits a completed run")
    require(runs[final_label]["head_sha"] == tested, "Final full run SHA differs from summary")
    for label in final_runs:
        run = runs[label]
        require(run.get("source_bytes_unchanged_during_run") is True
                and run.get("harness_bytes_unchanged_during_run") is True,
                "Final-run byte stability is unverified: " + label)
        require(run["source_sha256"] == summary["source_sha256"]
                and run["harness_sha256"] == summary["harness_sha256"], "Final run byte versions disagree: " + label)
    for kind, canonical in (("source", "canonical_source_sha256"), ("harness", "canonical_harness_sha256")):
        final_manifest = read_json(kind + "_manifest.json")
        run_manifest = read_json("offline/" + final_label + "-" + kind + "_manifest.json")
        require(final_manifest["tested_commit"] == tested and final_manifest["files"] == run_manifest["files"]
                and final_manifest[canonical] == summary[kind + "_sha256"], "Final manifest is inconsistent: " + kind)
    for flag in ("False", "True"):
        select("offline/numeric-probe-01-test_exe_009_010_supervisor_reaps_hung_objective-" + flag + "-trace.json",
               "pre_fix_instrumented_numeric_probe", run_relation(runs["numeric-probe-01"]))

    versions = safe_path("capture-tool-versions")
    require(versions.is_dir(), "Archived capture-tool version directory is required")
    tool_hashes = {digest(safe_path(name).read_bytes()) for name in ("capture_tie_evidence.py", "capture_numeric_evidence.py")}
    for path in sorted(versions.iterdir()):
        match = re.fullmatch(r"capture_(?:tie|numeric)_evidence-([0-9a-f]{64})\.py", path.name)
        require(match is not None and path.is_file(), "Unknown archived capture-tool entry: " + path.name)
        relative = "capture-tool-versions/" + path.name
        require(digest(safe_path(relative).read_bytes()) == match[1], "Archived capture-tool filename hash differs")
        tool_hashes.add(match[1])
        select(relative, "capture_tool_provenance", {"source_sha": None,
               "version_relationship": "Archived capture-tool bytes identified by SHA-256 in capture records"})

    def check_capture(directory, label, names, record):
        relation = run_relation(runs[label])
        legacy_capture = label in {"reproduce-03", "reproduce-04"}
        relation["capture_junit_hash_status"] = ("RECORDED_AND_VERIFIED" if "source_junit" in record
                                                 else "NOT_RECORDED_IN_ORIGINAL_CAPTURE")
        relation["capture_dirty_paths_status"] = ("RECORDED_AND_VERIFIED" if "source_changes_at_start" in record
                                                  else "NOT_RECORDED_IN_ORIGINAL_CAPTURE; run result supplies its own provenance")
        require(record.get("run_label") == label and record.get("source_sha") == runs[label]["head_sha"],
                "Capture run/source mismatch: " + directory)
        require(record.get("capture_script_sha256") in tool_hashes, "Capture tool bytes are absent: " + directory)
        require(record.get("source_manifest_sha256") == runs[label]["source_sha256"]
                and record.get("harness_manifest_sha256") == runs[label]["harness_sha256"],
                "Capture byte-version attribution differs: " + directory)
        if "source_changes_at_start" in record:
            require(record["source_changes_at_start"] == runs[label]["source_changes_at_start"],
                    "Capture dirty-path attribution differs: " + directory)
        else:
            require(legacy_capture, "Final capture lacks dirty-path provenance: " + directory)
        index = read_json(directory + "/evidence_index.json")
        require(index.get("run_label") == label and index.get("source_sha") == runs[label]["head_sha"], "Capture index attribution mismatch")
        indexed = {normalized(row["path"]): row for row in index["files"]}
        require(len(indexed) == len(index["files"]) and set(indexed) == set(names), "Capture index member set differs: " + directory)
        for name in (*names, "evidence_index.json"):
            relative = directory + "/" + name
            select(relative, "stored_offline_synthetic_capture", relation)
            if name in indexed:
                raw = safe_path(relative).read_bytes()
                require(len(raw) == indexed[name]["size_bytes"] and digest(raw) == indexed[name]["sha256"],
                        "Capture index hash/size differs: " + relative)
        if "source_junit" in record:
            junit = record["source_junit"]
            require(normalized(junit["path"]) == "offline/" + label + ".xml"
                    and digest(safe_path(junit["path"]).read_bytes()) == junit["sha256"], "Capture JUnit changed: " + directory)
        else:
            require(legacy_capture, "Final capture lacks original JUnit binding: " + directory)
        result = record.get("source_result") or {"path": record.get("source_result_path"), "sha256": record.get("source_result_sha256")}
        require(normalized(result["path"]) == "offline/" + label + "-result.json"
                and digest(safe_path(result["path"]).read_bytes()) == result["sha256"], "Capture result changed: " + directory)

    tie_runs = ("reproduce-03", "reproduce-04", *TARGETED_RUNS,
                *(label for label in full_runs if label == final_label or safe_path("tie-" + label).exists()))
    for label in tie_runs:
        directory = "tie-" + label
        check_capture(directory, label, ("report.json", "explainer-invocations.json", "diagnosis.json"),
                      read_json(directory + "/diagnosis.json"))
    numeric_runs = (*TARGETED_RUNS,
                    *(label for label in full_runs if label == final_label or safe_path("numeric-" + label).exists()))
    for label in numeric_runs:
        directory = "numeric-" + label
        capture = read_json(directory + "/capture.json")
        require(capture.get("trace_count") == 4 and len(capture.get("traces", [])) == 4,
                "Four numeric traces are required: " + label)
        check_capture(directory, label, ("capture.json", *("traces/" + name for name in TRACE_NAMES)), capture)
        destinations = {item["destination_path"] for item in capture["traces"]}
        require(destinations == {"traces/" + name for name in TRACE_NAMES}, "Numeric trace set differs: " + label)
        for item in capture["traces"]:
            raw = safe_path(directory + "/" + item["destination_path"]).read_bytes()
            require(len(raw) == item["source_size_bytes"] and digest(raw) == item["source_sha256"],
                    "Copied numeric trace differs from capture record: " + label)

    # Visit only the evidence root and allowlisted directories. Never enter a
    # private/temp directory, and never read an unknown file's contents.
    allowed_dirs = {str(parent) for name in selected for parent in PurePosixPath(name).parents if str(parent) != "."}
    excluded = []
    def inventory(directory):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            relative = Path(entry.path).relative_to(B).as_posix()
            reason = private_reason(relative)
            if reason:
                excluded.append({"path": relative, "reason": reason, "contents_read": False})
                continue
            path = safe_path(relative)
            if entry.is_dir(follow_symlinks=False):
                require(relative in allowed_dirs, "Unknown directory requires explicit review: " + relative)
                inventory(path)
            else:
                require(relative in selected, "Unknown file requires explicit review: " + relative)
    inventory(B)

    contents = {}
    records = []
    for relative, relation in sorted(selected.items()):
        raw = safe_path(relative).read_bytes()
        scan_data(relative, raw)
        contents[relative] = raw
        records.append({"path": relative, "size_bytes": len(raw), "sha256": digest(raw), **relation})
    index = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
             "evidence_kind": "SELECTED_EXISTING_OFFLINE_ACCEPTANCE_EVIDENCE",
             "package_root": B.name, "tested_commit": tested, "file_count": len(records),
             "files": records, "excluded_paths": excluded,
             "index_scope": "All selected files; this top-level index and ZIP are excluded to avoid circular hashes. ZIP includes the index.",
             "secret_policy": "Explicit paths/types, no databases/env/private/temp trees or connection credentials; suspicious content fails closed. Public numeric_execution.authorization objects are retained.",
             "capture_limit": "Stored synthetic offline fixtures; no live model acceptance or independently captured HTTP wire transcript is claimed.",
             "new_test_execution": "NOT_RUN_BY_PACKAGER", "new_model_calls": 0,
             "new_objective_evaluations": 0, "new_solver_calls": 0}
    index_raw = (json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    # Detect concurrent edits before writing; no results/logs may still be live.
    for relative, raw in contents.items():
        require(safe_path(relative).read_bytes() == raw, "Evidence changed during packaging: " + relative)
    with (B / INDEX).open("xb") as stream:
        stream.write(index_raw)
    with zipfile.ZipFile(B / ARCHIVE, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, raw in contents.items():
            archive.writestr(relative, raw)
        archive.writestr(INDEX, index_raw)
    with zipfile.ZipFile(B / ARCHIVE, "r") as archive:
        require(archive.testzip() is None, "ZIP CRC verification failed")
        require(len(archive.namelist()) == len(contents) + 1
                and set(archive.namelist()) == set(contents) | {INDEX}, "ZIP member set mismatch")
        require(archive.read(INDEX) == index_raw, "ZIP index bytes differ")
        for row in records:
            member = archive.read(row["path"])
            current = safe_path(row["path"]).read_bytes()
            require(len(member) == row["size_bytes"] and digest(member) == row["sha256"]
                    and current == member, "ZIP/index/current bytes disagree: " + row["path"])
    require((B / INDEX).read_bytes() == index_raw, "Top-level index changed during packaging")
    print(json.dumps({"archive": str(B / ARCHIVE), "archive_bytes": (B / ARCHIVE).stat().st_size,
                      "archive_sha256": digest((B / ARCHIVE).read_bytes()), "selected_file_count": len(records),
                      "zip_member_count": len(records) + 1, "crc_verified": True,
                      "index_and_current_bytes_verified": True, "tested_commit": tested}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, FileNotFoundError, json.JSONDecodeError, UnicodeError) as error:
        print("Evidence packaging stopped: " + str(error), file=sys.stderr)
        raise SystemExit(1)
